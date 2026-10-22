"""Root-owned model routing, feature validation and bounded failover."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Mapping

from suiteharness import observability
from suiteharness.diagnostics import emit as diagnostic
from suiteharness.models.characters import comparison_fields, measure_characters
from suiteharness.models.context import ContextCapacityError, ContextManager, PreparedContext
from suiteharness.models.errors import ModelErrorCode, ModelProviderError
from suiteharness.models.protocols import ModelProvider, SecretResolver
from suiteharness.models.registry import ProviderRegistry
from suiteharness.models.transport import HttpTransport
from suiteharness.models.types import (
    DocumentContent,
    FinishReason,
    ImageContent,
    ModelProfile,
    ModelRequest,
    ModelResponse,
    ModelRoute,
    ModelStreamEvent,
    ModelStreamEventType,
    ModelToolCall,
)
from suiteharness.models.usage import UsageLedger

_LOGGER = logging.getLogger(__name__)


def _can_record_skill_response(
    request: ModelRequest, calls: tuple[ModelToolCall, ...], finish_reason: FinishReason | None,
) -> bool:
    """Accept only successful output for this final, host-prepared request.

    Some OpenAI-compatible services finish one explicitly named function call
    with STOP rather than TOOL_CALLS. Keep that compatibility deliberately
    narrower than ordinary tool responses: one complete call, exactly the
    named function, and that function present in the actual request's tools.
    This records provenance only; it neither grants tool authority nor changes
    provider events or relaxes normal ReAct finish-reason validation.
    """
    if not calls:
        return finish_reason is FinishReason.STOP
    if finish_reason is FinishReason.TOOL_CALLS:
        return True
    if finish_reason is not FinishReason.STOP or len(calls) != 1:
        return False
    choice = request.tool_choice
    if (not isinstance(choice, dict) or set(choice) != {"type", "function"}
            or choice.get("type") != "function"):
        return False
    function = choice.get("function")
    if not isinstance(function, dict) or set(function) != {"name"}:
        return False
    name = function.get("name")
    if not isinstance(name, str) or name not in {tool.name for tool in request.tools}:
        return False
    call = calls[0]
    return isinstance(call, ModelToolCall) and call.name == name


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
        request = request.model_copy(update={"metadata": {
            **request.metadata, "model_call_id": "model-" + uuid.uuid4().hex}})
        fields = {key: request.metadata[key] for key in ("run_id", "iteration", "model_call_id") if key in request.metadata}
        fields["route_id"] = route_id
        observability.capture("model.input.before", lambda: {
            **fields, "request": observability.model_request_payload(request),
        })
        diagnostic("model.started", **fields, message_count=len(request.messages), tool_count=len(request.tools),
                   streaming=False, provider_invoked=False, max_output_tokens=request.max_output_tokens,
                   **comparison_fields(measure_characters(request), None))
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
        except Exception as exc:
            diagnostic("model.failed", **fields, error=exc, stage="model_complete")
            raise
        finally:
            self._record_call(route_id, status, started, usage)
            diagnostic("model.finished", **fields, status=status, seconds=time.monotonic() - started,
                       input_tokens=None if usage is None else usage.input_tokens,
                       output_tokens=None if usage is None else usage.output_tokens)

    def _prepare_context(self, profile: ModelProfile, request: ModelRequest) -> PreparedContext:
        descriptor = self._registry.descriptor(profile.provider_id)
        assert descriptor is not None
        window = profile.effective_capabilities(descriptor.capabilities).max_context_tokens
        prepared = self._context.prepare(request, window_tokens=window or self._default_context_tokens)
        if prepared.removed_rounds or prepared.truncated_tools:
            _LOGGER.info('context_compacted profile=%s estimated_tokens=%d removed_rounds=%d truncated_tools=%d',
                profile.profile_id, prepared.estimated_input_tokens, prepared.removed_rounds, prepared.truncated_tools)
        return prepared

    def _prepare_observed(self, profile, request, fields, *, streaming=False):
        """One profile's measurements, including locally rejected requests.

        provider_invoked means the provider adapter method was entered, not an
        assertion that the remote service received or billed an HTTP request.
        """
        from suiteharness.skills.runtime import (
            prepare_skill_model_request,
            verify_skill_model_request,
        )

        before = measure_characters(request)
        diagnostic("model.context_prepare", **fields, streaming=streaming,
                   provider_invoked=False)
        try:
            request = prepare_skill_model_request(request)
            before = measure_characters(request)
            self._validate_request(profile, request, streaming=streaming)
            prepared = self._prepare_context(profile, request)
            # Verify *after* profile-specific trimming and immediately before
            # provider use. A run having read a Skill in the past is not proof
            # that its full instructions reached this particular model call.
            verify_skill_model_request(prepared.request)
        except BaseException as exc:
            # Capacity failures expose the final attempted size even when
            # truncation/removal could not make the protected input fit.
            after = exc.character_counts if isinstance(exc, ContextCapacityError) else None
            diagnostic("model.context_failed", **fields, error=exc, stage="context_prepare",
                       streaming=streaming, provider_invoked=False,
                       removed_rounds=getattr(exc, "removed_rounds", None),
                       truncated_tools=getattr(exc, "truncated_tools", None),
                       **comparison_fields(before, after))
            raise
        sizes = comparison_fields(before, measure_characters(prepared.request))
        diagnostic("model.context_ready", **fields, streaming=streaming, provider_invoked=False,
                   estimated_input_tokens=prepared.estimated_input_tokens,
                   removed_rounds=prepared.removed_rounds, truncated_tools=prepared.truncated_tools,
                   **sizes)
        return prepared, sizes

    async def _complete(self, route_id: str, request: ModelRequest) -> ModelResponse:
        route = self.route(route_id)
        last_error: ModelProviderError | ContextCapacityError | None = None
        for profile_id in (route.primary_profile, *route.fallback_profiles):
            profile = self.profile(profile_id)
            fields = {key: request.metadata[key] for key in ("run_id", "iteration", "model_call_id") if key in request.metadata}
            fields.update(route_id=route_id, provider_id=profile.provider_id, profile_id=profile_id, model=profile.model)
            try:
                prepared, sizes = self._prepare_observed(profile, request, fields)
            except ContextCapacityError as exc:
                last_error = exc
                continue
            provider = self._provider(profile)
            for attempt in range(profile.max_retries + 1):
                diagnostic("model.attempt_started", **fields, attempt=attempt + 1,
                           retry_limit=profile.max_retries + 1, streaming=False,
                           provider_invoked=True, **sizes)
                observability.capture("model.request", lambda: {
                    **fields, "attempt": attempt + 1, "streaming": False,
                    "request": observability.model_request_payload(prepared.request),
                })
                attempt_started = time.monotonic()
                attempt_status = "failed"
                attempt_usage = None
                try:
                    response = await provider.complete(prepared.request)
                    attempt_usage = response.usage if 'usage' in response.model_fields_set else None
                    descriptor = self._registry.descriptor(profile.provider_id)
                    assert descriptor is not None
                    capabilities = profile.effective_capabilities(descriptor.capabilities)
                    if len(response.tool_calls) > 1 and not capabilities.parallel_tool_calls:
                        raise ModelProviderError(
                            ModelErrorCode.UNSUPPORTED_FEATURE,
                            "model returned parallel tool calls disabled by the profile",
                            provider_id=profile.provider_id,
                        )
                    if _can_record_skill_response(prepared.request, response.tool_calls, response.finish_reason):
                        from suiteharness.skills.runtime import record_skill_model_response
                        record_skill_model_response(prepared.request, response.tool_calls)
                    attempt_status = "succeeded"
                    observability.capture("model.response", lambda: {
                        **fields, "attempt": attempt + 1,
                        "response": observability.model_response_payload(response),
                    })
                    return response
                except asyncio.CancelledError:
                    attempt_status = "cancelled"
                    raise
                except ModelProviderError as exc:
                    diagnostic("model.attempt_failed", **fields, error=exc, attempt=attempt + 1,
                               code=exc.code.value, status_code=exc.status_code, retryable=exc.retryable)
                    last_error = exc
                    if not exc.retryable or attempt >= profile.max_retries:
                        break
                    await asyncio.sleep(min(0.25 * (2**attempt), 2.0))
                finally:
                    observability.capture("model.attempt.end", lambda: {
                        **fields, "attempt": attempt + 1, "status": attempt_status,
                        "seconds": time.monotonic() - attempt_started,
                    })
                    diagnostic("model.attempt_finished", **fields, attempt=attempt + 1,
                               streaming=False, provider_invoked=True, status=attempt_status,
                               seconds=time.monotonic() - attempt_started,
                               input_tokens=None if attempt_usage is None else attempt_usage.input_tokens,
                               output_tokens=None if attempt_usage is None else attempt_usage.output_tokens)
            if isinstance(last_error, ModelProviderError) and not last_error.retryable:
                raise last_error
        if last_error is not None:
            raise last_error
        raise ModelProviderError(ModelErrorCode.UNAVAILABLE, "model route has no usable profile")

    async def _stream(self, route_id: str, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        started = time.monotonic()
        request = request.model_copy(update={"metadata": {
            **request.metadata, "model_call_id": "model-" + uuid.uuid4().hex}})
        fields = {key: request.metadata[key] for key in ("run_id", "iteration", "model_call_id")
                  if key in request.metadata}
        fields["route_id"] = route_id
        observability.capture("model.input.before", lambda: {
            **fields, "request": observability.model_request_payload(request),
        })
        first_event = None
        diagnostic("model.started", **fields, message_count=len(request.messages),
                   tool_count=len(request.tools), max_output_tokens=request.max_output_tokens,
                   streaming=True, provider_invoked=False,
                   **comparison_fields(measure_characters(request), None))
        status = 'failed'
        usage = None
        source = self._stream_unobserved(route_id, request)
        try:
            async for event in source:
                if first_event is None:
                    first_event = time.monotonic() - started
                    diagnostic("model.first_event", **fields, first_event_seconds=first_event)
                if event.finish_reason is not None:
                    diagnostic("model.finish_reason", **fields, finish_reason=event.finish_reason.value)
                if event.usage is not None:
                    # Adapter usage events are cumulative snapshots, not deltas.
                    usage = event.usage
                yield event
            status = 'succeeded'
        except (asyncio.CancelledError, GeneratorExit):
            status = 'cancelled'
            raise
        except Exception as exc:
            diagnostic("model.failed", **fields, error=exc, stage="model_stream",
                       emitted=first_event is not None)
            raise
        finally:
            try:
                await source.aclose()
            finally:
                self._record_call(route_id, status, started, usage)
                diagnostic("model.finished", **fields, status=status,
                           seconds=time.monotonic() - started,
                           input_tokens=None if usage is None else usage.input_tokens,
                           output_tokens=None if usage is None else usage.output_tokens)

    async def _stream_unobserved(self, route_id: str, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        route = self.route(route_id)
        last_error: ModelProviderError | ContextCapacityError | None = None
        for profile_id in (route.primary_profile, *route.fallback_profiles):
            profile = self.profile(profile_id)
            fields = {key: request.metadata[key] for key in ("run_id", "iteration", "model_call_id")
                      if key in request.metadata}
            fields.update(route_id=route_id, profile_id=profile_id, provider_id=profile.provider_id,
                          model=profile.model)
            try:
                prepared, sizes = self._prepare_observed(profile, request, fields, streaming=True)
            except ContextCapacityError as exc:
                last_error = exc
                continue
            provider = self._provider(profile)
            descriptor = self._registry.descriptor(profile.provider_id)
            assert descriptor is not None
            capabilities = profile.effective_capabilities(descriptor.capabilities)
            for attempt in range(profile.max_retries + 1):
                attempt_started = time.monotonic()
                diagnostic("model.attempt_started", **fields, attempt=attempt + 1,
                           retry_limit=profile.max_retries + 1, streaming=True,
                           provider_invoked=True, **sizes)
                observability.capture("model.request", lambda: {
                    **fields, "attempt": attempt + 1, "streaming": True,
                    "request": observability.model_request_payload(prepared.request),
                })
                emitted = False
                attempt_status = "failed"
                attempt_usage = None
                call_ids: set[str] = set()
                completed_calls = {}
                finish_reason = None
                valid_stream_shape = True
                provider_stream = None
                try:
                    provider_stream = provider.stream(prepared.request)
                    async for event in provider_stream:
                        if finish_reason is not None:
                            valid_stream_shape = False
                        if event.usage is not None:
                            attempt_usage = event.usage
                        if event.type is ModelStreamEventType.CONTEXT_COMPACTED or event.prepared_messages is not None:
                            raise ModelProviderError(ModelErrorCode.RESPONSE_INVALID,
                                'provider attempted to emit gateway-owned context state', provider_id=profile.provider_id)
                        if event.tool_call is not None:
                            if event.tool_call.call_id in call_ids:
                                valid_stream_shape = False
                            call_ids.add(event.tool_call.call_id)
                            completed_calls[event.tool_call.call_id] = event.tool_call
                            if len(call_ids) > 1 and not capabilities.parallel_tool_calls:
                                raise ModelProviderError(
                                    ModelErrorCode.UNSUPPORTED_FEATURE,
                                    "model returned parallel tool calls disabled by the profile",
                                    provider_id=profile.provider_id,
                                )
                        if event.type is ModelStreamEventType.MESSAGE_END:
                            finish_reason = event.finish_reason
                        if not emitted and (prepared.removed_rounds or prepared.truncated_tools):
                            # Only adopt context from a provider that actually
                            # starts yielding. A failed attempt must not replace
                            # the successful fallback's workflow state.
                            yield ModelStreamEvent(type=ModelStreamEventType.CONTEXT_COMPACTED,
                                prepared_messages=tuple(message.model_copy(deep=True) for message in prepared.request.messages),
                                context_info={'estimated_input_tokens': prepared.estimated_input_tokens,
                                    'removed_rounds': prepared.removed_rounds, 'truncated_tools': prepared.truncated_tools})
                        emitted = True
                        observability.capture("model.stream.delta", lambda: {
                            **fields, "attempt": attempt + 1,
                            **observability.safe_value(event),
                        })
                        yield event
                    # Register only a successfully exhausted stream. A partial
                    # or cancelled attempt must not authorize tool execution.
                    if valid_stream_shape and _can_record_skill_response(
                        prepared.request, tuple(completed_calls.values()), finish_reason,
                    ):
                        from suiteharness.skills.runtime import record_skill_model_response
                        record_skill_model_response(prepared.request, tuple(completed_calls.values()))
                    attempt_status = "succeeded"
                    return
                except (asyncio.CancelledError, GeneratorExit):
                    attempt_status = "cancelled"
                    raise
                except ModelProviderError as exc:
                    diagnostic("model.attempt_failed", **fields, error=exc, attempt=attempt + 1,
                               emitted=emitted, retryable=exc.retryable, status_code=exc.status_code,
                               code=exc.code.value, seconds=time.monotonic() - attempt_started)
                    last_error = exc
                    # Once bytes reached the caller, switching providers could splice two answers.
                    if emitted or not exc.retryable:
                        raise
                    if attempt < profile.max_retries:
                        await asyncio.sleep(min(0.25 * (2**attempt), 2.0))
                        continue
                    break
                finally:
                    observability.capture("model.stream.end", lambda: {
                        **fields, "attempt": attempt + 1, "status": attempt_status,
                        "seconds": time.monotonic() - attempt_started,
                        "usage": observability.safe_value(attempt_usage),
                    })
                    diagnostic("model.attempt_finished", **fields, attempt=attempt + 1,
                               emitted=emitted, seconds=time.monotonic() - attempt_started,
                               streaming=True, provider_invoked=True, status=attempt_status,
                               input_tokens=None if attempt_usage is None else attempt_usage.input_tokens,
                               output_tokens=None if attempt_usage is None else attempt_usage.output_tokens)
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
