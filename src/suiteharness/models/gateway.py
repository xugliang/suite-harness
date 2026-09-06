"""Root-owned model routing, feature validation and bounded failover."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping

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
)


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
    ) -> None:
        self._registry = registry
        self._profiles = dict(profiles)
        self._routes = dict(routes)
        self._transport = transport
        self._secrets = secrets
        self._providers: dict[str, ModelProvider] = {}
        self._validate_configuration()

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
        route = self.route(route_id)
        last_error: ModelProviderError | None = None
        for profile_id in (route.primary_profile, *route.fallback_profiles):
            profile = self.profile(profile_id)
            self._validate_request(profile, request)
            provider = self._provider(profile)
            for attempt in range(profile.max_retries + 1):
                try:
                    response = await provider.complete(request)
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
            if last_error is not None and not last_error.retryable:
                raise last_error
        if last_error is not None:
            raise last_error
        raise ModelProviderError(ModelErrorCode.UNAVAILABLE, "model route has no usable profile")

    async def _stream(self, route_id: str, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        route = self.route(route_id)
        last_error: ModelProviderError | None = None
        for profile_id in (route.primary_profile, *route.fallback_profiles):
            profile = self.profile(profile_id)
            self._validate_request(profile, request, streaming=True)
            provider = self._provider(profile)
            descriptor = self._registry.descriptor(profile.provider_id)
            assert descriptor is not None
            capabilities = profile.effective_capabilities(descriptor.capabilities)
            for attempt in range(profile.max_retries + 1):
                emitted = False
                call_ids: set[str] = set()
                try:
                    async for event in provider.stream(request):
                        if event.tool_call is not None:
                            call_ids.add(event.tool_call.call_id)
                            if len(call_ids) > 1 and not capabilities.parallel_tool_calls:
                                raise ModelProviderError(
                                    ModelErrorCode.UNSUPPORTED_FEATURE,
                                    "model returned parallel tool calls disabled by the profile",
                                    provider_id=profile.provider_id,
                                )
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
            if last_error is not None and not last_error.retryable:
                raise last_error
        if last_error is not None:
            raise last_error
        raise ModelProviderError(ModelErrorCode.UNAVAILABLE, "model route has no usable profile")

    def stream(self, route_id: str, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        return self._stream(route_id, request)
