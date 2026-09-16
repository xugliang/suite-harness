"""Root-owned model routing, feature validation and bounded failover."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator, Mapping

from suiteharness.models.context import ContextCapacityError, ContextManager, PreparedContext
from suiteharness.models.errors import ModelErrorCode, ModelProviderError
from suiteharness.models.protocols import ModelProvider, SecretResolver
from suiteharness.models.registry import ProviderRegistry
from suiteharness.models.transport import HttpTransport
from suiteharness.models.types import (
    DocumentContent,
    ImageContent,
    ModelProfile,
    ModelRequest,
    ModelResponse,
    ModelRoute,
    ModelStreamEvent,
    ModelStreamEventType,
)
from suiteharness.models.usage import UsageLedger

_LOGGER = logging.getLogger(__name__)


class ModelGateway:
    """Resolve only administrator-declared routes and profiles.

    A workflow selects a route, not a credential or arbitrary endpoint.  Model
    overrides are accepted only when explicitly allowlisted by that profile.
    """

    def __init__(
        self,
        registry: ProviderRegistry,
        profiles: Mapping[str, ModelProfile],
        routes: Mapping[str, ModelRoute],
        transport: HttpTransport,
        secrets: SecretResolver,
        *,
        context_manager: ContextManager | None = None,
        default_context_tokens: int = 32768,
    ) -> None:
        if isinstance(default_context_tokens, bool) or not isinstance(default_context_tokens, int) or default_context_tokens < 1:
            raise ValueError('default_context_tokens must be a positive integer')
        self._context = context_manager or ContextManager()
        self._default_context_tokens = default_context_tokens
        self._registry = registry
        self._profiles = dict(profiles)
        self._routes = dict(routes)
        self._transport = transport
        self._secrets = secrets
        self._providers: dict[str, ModelProvider] = {}
        self._validate_configuration()
        self._usage = UsageLedger(self._routes)

    def diagnostics(self) -> dict:
        """Administrator/server-side snapshot, not a public client endpoint."""
        return self._usage.snapshot()

    def _record_call(self, route_id, status, started, usage):
        elapsed = time.monotonic() - started
        self._usage.record(route_id, status, elapsed, usage)
        if route_id in self._routes:
            _LOGGER.info(
                "model_call route=%s status=%s seconds=%.3f input_tokens=%s output_tokens=%s",
                route_id, status, elapsed,
                None if usage is None else usage.input_tokens,
                None if usage is None else usage.output_tokens,
            )

    def _validate_configuration(self) -> None:
        for key, profile in self._profiles.items():
            if key != profile.profile_id:
                raise ValueError("model profile map key must equal profile_id")
            descriptor = self._registry.descriptor(profile.provider_id)
            if descriptor is None:
                raise ValueError(f"unknown provider in model profile {key!r}")
            profile.effective_capabilities(descriptor.capabilities)
        for key, route in self._routes.items():
            if key != route.route_id:
                raise ValueError("model route map key must equal route_id")
            for profile_id in (route.primary_profile, *route.fallback_profiles):
                if profile_id not in self._profiles:
                    raise ValueError(
                        f"model route {route.route_id!r} references unknown profile "
                        f"{profile_id!r}"
                    )

    def profile(self, profile_id: str) -> ModelProfile:
        try:
            return self._profiles[profile_id]
        except KeyError as exc:
            raise ModelProviderError(
                ModelErrorCode.PROFILE_NOT_FOUND,
                f"model profile {profile_id!r} was not found",
            ) from exc

    def route(self, route_id: str) -> ModelRoute:
        try:
            return self._routes[route_id]
        except KeyError as exc:
            raise ModelProviderError(
                ModelErrorCode.PROFILE_NOT_FOUND,
                f"model route {route_id!r} was not found",
            ) from exc

    def resume_fingerprint(self, route_id: str) -> str:
        """Bind effective route configuration without resolving or exposing secrets."""
        route = self.route(route_id)
        profiles = []
        for profile_id in (route.primary_profile, *route.fallback_profiles):
            profile = self.profile(profile_id)
            descriptor = self._registry.descriptor(profile.provider_id)
            if descriptor is None:
                raise ModelProviderError(
                    ModelErrorCode.PROVIDER_NOT_FOUND,
                    f"provider {profile.provider_id!r} was not found",
                )
            profile_data = profile.model_dump(mode='json')
            profile_data['allowed_models'] = sorted(profile.allowed_models)
            provider_data = descriptor.model_dump(mode='json')
            provider_data['aliases'] = sorted(descriptor.aliases)
            profiles.append({
                'profile': profile_data,
                'provider': provider_data,
                'effective_endpoint': profile.base_url or descriptor.default_base_url,
            })
        payload = json.dumps(
            {'route': route.model_dump(mode='json'), 'profiles': profiles},
            sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False,
        )
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()

    def _provider(self, profile: ModelProfile) -> ModelProvider:
        provider = self._providers.get(profile.profile_id)
        if provider is None:
            provider = self._registry.create(profile, self._transport, self._secrets)
            self._providers[profile.profile_id] = provider
        return provider

    def _validate_request(
        self, profile: ModelProfile, request: ModelRequest, *, streaming: bool = False
    ) -> None:
        model = request.model or profile.model
        if not profile.permits_model(model):
            raise ModelProviderError(
                ModelErrorCode.PERMISSION_DENIED,
                f"model {model!r} is not allowed by profile {profile.profile_id!r}",
                provider_id=profile.provider_id,
            )
        descriptor = self._registry.descriptor(profile.provider_id)
        if descriptor is None:
            raise ModelProviderError(
                ModelErrorCode.PROVIDER_NOT_FOUND,
                f"provider {profile.provider_id!r} was not found",
            )
        capabilities = profile.effective_capabilities(descriptor.capabilities)
        if streaming and not capabilities.streaming:
            raise ModelProviderError(
                ModelErrorCode.UNSUPPORTED_FEATURE,
                f"profile {profile.profile_id!r} does not support streaming",
                provider_id=descriptor.provider_id,
            )
        if request.tools and not capabilities.tools:
            raise ModelProviderError(
                ModelErrorCode.UNSUPPORTED_FEATURE,
                f"provider {descriptor.provider_id!r} does not support tools",
                provider_id=descriptor.provider_id,
            )
        if request.response_schema is not None and not capabilities.json_schema:
            raise ModelProviderError(
                ModelErrorCode.UNSUPPORTED_FEATURE,
                f"provider {descriptor.provider_id!r} does not support JSON Schema output",
                provider_id=descriptor.provider_id,
            )
        parts = [part for message in request.messages for part in message.content]
        if any(isinstance(part, ImageContent) for part in parts) and not capabilities.vision:
            raise ModelProviderError(
                ModelErrorCode.UNSUPPORTED_FEATURE,
                f"provider {descriptor.provider_id!r} does not support image input",
                provider_id=descriptor.provider_id,
            )
        if any(isinstance(part, DocumentContent) for part in parts) and not capabilities.documents:
            raise ModelProviderError(
                ModelErrorCode.UNSUPPORTED_FEATURE,
                f"provider {descriptor.provider_id!r} does not support document input",
                provider_id=descriptor.provider_id,
            )

    async def complete(self, route_id: str, request: ModelRequest) -> ModelResponse:
        started = time.monotonic()
        status = 'failed'
        usage = None
        try:
            response = await self._complete(route_id, request)
            usage = response.usage if 'usage' in response.model_fields_set else None
            status = 'succeeded'
            return response
        except asyncio.CancelledError:
            status = 'cancelled'
            raise
        finally:
            self._record_call(route_id, status, started, usage)

    def _prepare_context(self, profile: ModelProfile, request: ModelRequest) -> PreparedContext:
        descriptor = self._registry.descriptor(profile.provider_id)
        assert descriptor is not None
        window = profile.effective_capabilities(descriptor.capabilities).max_context_tokens
        prepared = self._context.prepare(request, window_tokens=window or self._default_context_tokens)
        if prepared.removed_rounds or prepared.truncated_tools:
            _LOGGER.info('context_compacted profile=%s estimated_tokens=%d removed_rounds=%d truncated_tools=%d',
                profile.profile_id, prepared.estimated_input_tokens, prepared.removed_rounds, prepared.truncated_tools)
        return prepared

    async def _complete(self, route_id: str, request: ModelRequest) -> ModelResponse:
        route = self.route(route_id)
        last_error: ModelProviderError | ContextCapacityError | None = None
        for profile_id in (route.primary_profile, *route.fallback_profiles):
            profile = self.profile(profile_id)
            self._validate_request(profile, request)
            try:
                prepared = self._prepare_context(profile, request)
            except ContextCapacityError as exc:
                last_error = exc
                continue
            provider = self._provider(profile)
            for attempt in range(profile.max_retries + 1):
                try:
                    response = await provider.complete(prepared.request)
                    descriptor = self._registry.descriptor(profile.provider_id)
                    assert descriptor is not None
                    capabilities = profile.effective_capabilities(descriptor.capabilities)
                    if len(response.tool_calls) > 1 and not capabilities.parallel_tool_calls:
                        raise ModelProviderError(
                            ModelErrorCode.UNSUPPORTED_FEATURE,
                            "model returned parallel tool calls disabled by the profile",
                            provider_id=profile.provider_id,
                        )
                    return response
                except ModelProviderError as exc:
                    last_error = exc
                    if not exc.retryable or attempt >= profile.max_retries:
                        break
                    await asyncio.sleep(min(0.25 * (2**attempt), 2.0))
            if isinstance(last_error, ModelProviderError) and not last_error.retryable:
                raise last_error
        if last_error is not None:
            raise last_error
        raise ModelProviderError(ModelErrorCode.UNAVAILABLE, "model route has no usable profile")

    async def _stream(self, route_id: str, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        started = time.monotonic()
        status = 'failed'
        usage = None
        source = self._stream_unobserved(route_id, request)
        try:
            async for event in source:
                if event.usage is not None:
                    # Adapter usage events are cumulative snapshots, not deltas.
                    usage = event.usage
                yield event
            status = 'succeeded'
        except (asyncio.CancelledError, GeneratorExit):
            status = 'cancelled'
            raise
        finally:
            try:
                await source.aclose()
            finally:
                self._record_call(route_id, status, started, usage)

    async def _stream_unobserved(self, route_id: str, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        route = self.route(route_id)
        last_error: ModelProviderError | ContextCapacityError | None = None
        for profile_id in (route.primary_profile, *route.fallback_profiles):
            profile = self.profile(profile_id)
            self._validate_request(profile, request, streaming=True)
            try:
                prepared = self._prepare_context(profile, request)
            except ContextCapacityError as exc:
                last_error = exc
                continue
            provider = self._provider(profile)
            descriptor = self._registry.descriptor(profile.provider_id)
            assert descriptor is not None
            capabilities = profile.effective_capabilities(descriptor.capabilities)
            for attempt in range(profile.max_retries + 1):
                emitted = False
                call_ids: set[str] = set()
                provider_stream = provider.stream(prepared.request)
                try:
                    async for event in provider_stream:
                        if event.type is ModelStreamEventType.CONTEXT_COMPACTED or event.prepared_messages is not None:
                            raise ModelProviderError(ModelErrorCode.RESPONSE_INVALID,
                                'provider attempted to emit gateway-owned context state', provider_id=profile.provider_id)
                        if event.tool_call is not None:
                            call_ids.add(event.tool_call.call_id)
                            if len(call_ids) > 1 and not capabilities.parallel_tool_calls:
                                raise ModelProviderError(
                                    ModelErrorCode.UNSUPPORTED_FEATURE,
                                    "model returned parallel tool calls disabled by the profile",
                                    provider_id=profile.provider_id,
                                )
                        if not emitted and (prepared.removed_rounds or prepared.truncated_tools):
                            # Only adopt context from a provider that actually
                            # starts yielding. A failed attempt must not replace
                            # the successful fallback's workflow state.
                            yield ModelStreamEvent(type=ModelStreamEventType.CONTEXT_COMPACTED,
                                prepared_messages=tuple(message.model_copy(deep=True) for message in prepared.request.messages),
                                context_info={'estimated_input_tokens': prepared.estimated_input_tokens,
                                    'removed_rounds': prepared.removed_rounds, 'truncated_tools': prepared.truncated_tools})
                        emitted = True
                        yield event
                    return
                except ModelProviderError as exc:
                    last_error = exc
                    # Once bytes reached the caller, switching providers could splice two answers.
                    if emitted or not exc.retryable:
                        raise
                    if attempt < profile.max_retries:
                        await asyncio.sleep(min(0.25 * (2**attempt), 2.0))
                        continue
                    break
                finally:
                    close = getattr(provider_stream, 'aclose', None)
                    if close is not None:
                        await close()
            if isinstance(last_error, ModelProviderError) and not last_error.retryable:
                raise last_error
        if last_error is not None:
            raise last_error
        raise ModelProviderError(ModelErrorCode.UNAVAILABLE, "model route has no usable profile")

    def stream(self, route_id: str, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        return self._stream(route_id, request)
