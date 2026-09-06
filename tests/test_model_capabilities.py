from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from suiteharness.models import (
    AdapterKind,
    ImageContent,
    MappingSecretResolver,
    ModelCapabilities,
    ModelErrorCode,
    ModelGateway,
    ModelMessage,
    ModelProfile,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ModelRole,
    ModelRoute,
    ModelStreamEvent,
    ModelStreamEventType,
    ModelTool,
    ModelToolCall,
    ProviderAuthKind,
    ProviderCapabilities,
    ProviderDescriptor,
    ProviderRegistry,
    TextContent,
)


class FakeProvider:
    def __init__(self, *, tool_calls: tuple[ModelToolCall, ...] = ()) -> None:
        self.requests: list[ModelRequest] = []
        self.tool_calls = tool_calls

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            response_id="response",
            provider_id="fake",
            model="test-model",
            content=(TextContent(text="synthetic result"),),
            tool_calls=self.tool_calls,
        )

    async def stream(self, request: ModelRequest):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        for call in self.tool_calls:
            yield ModelStreamEvent(type=ModelStreamEventType.TOOL_CALL_DELTA, tool_call=call)
        yield ModelStreamEvent(type=ModelStreamEventType.TEXT_DELTA, text_delta="synthetic")


class NoNetworkTransport:
    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        raise AssertionError(f"network transport must not be used: {name}")


def gateway(profile: ModelProfile, provider: FakeProvider) -> ModelGateway:
    registry = ProviderRegistry()
    registry.register(
        ProviderDescriptor(
            provider_id="fake",
            display_name="Offline test double",
            adapter=AdapterKind.OPENAI_CHAT,
            auth=ProviderAuthKind.INTERNAL_NONE,
            capabilities=ProviderCapabilities(vision=False, json_schema=False),
        ),
        lambda descriptor, profile, transport, credential: provider,
    )
    return ModelGateway(
        registry,
        {"test": profile},
        {"main": ModelRoute(route_id="main", primary_profile="test")},
        NoNetworkTransport(),
        MappingSecretResolver({}),
    )


def profile(**updates: object) -> ModelProfile:
    return ModelProfile.model_validate(
        {
            "profile_id": "test",
            "provider_id": "fake",
            "model": "test-model",
            **updates,
        }
    )


def text_request(**updates: object) -> ModelRequest:
    return ModelRequest.model_validate(
        {
            "messages": (ModelMessage.text(ModelRole.USER, "synthetic input"),),
            **updates,
        }
    )


def test_undeclared_vision_remains_disabled_and_no_provider_is_called() -> None:
    provider = FakeProvider()
    model_gateway = gateway(profile(), provider)
    request = ModelRequest(
        messages=(
            ModelMessage(
                role=ModelRole.USER,
                content=(ImageContent(mime_type="image/png", data_base64="c3ludGhldGlj"),),
            ),
        )
    )
    with pytest.raises(ModelProviderError) as error:
        asyncio.run(model_gateway.complete("main", request))
    assert error.value.code is ModelErrorCode.UNSUPPORTED_FEATURE
    assert provider.requests == []


def test_capability_elevation_requires_explicit_opt_in_at_configuration() -> None:
    with pytest.raises(ValueError, match="allow_capability_overrides"):
        gateway(profile(capabilities=ModelCapabilities(vision=True)), FakeProvider())


def test_explicit_capabilities_enable_only_declared_fields() -> None:
    configured = profile(
        capabilities=ModelCapabilities(vision=True, json_schema=True),
        allow_capability_overrides=True,
    )
    effective = configured.effective_capabilities(
        ProviderCapabilities(
            tools=False,
            parallel_tool_calls=False,
            streaming=False,
        )
    )
    assert effective.vision and effective.json_schema
    assert not effective.tools and not effective.parallel_tool_calls and not effective.streaming
    provider = FakeProvider()
    request = ModelRequest(
        messages=(
            ModelMessage(role=ModelRole.USER, content=(ImageContent(data_base64="c3ludGhldGlj"),)),
        ),
        response_schema={"type": "object"},
    )
    asyncio.run(gateway(configured, provider).complete("main", request))
    assert provider.requests == [request]


def test_model_override_cannot_inherit_elevated_vision_from_another_model() -> None:
    with pytest.raises(ValidationError, match="one exact model"):
        profile(
            capabilities=ModelCapabilities(vision=True),
            allow_capability_overrides=True,
            allowed_models=frozenset({"unverified-model"}),
        )


def test_profile_can_disable_tools_and_streaming() -> None:
    provider = FakeProvider()
    model_gateway = gateway(
        profile(
            capabilities=ModelCapabilities(
                tools=False,
                streaming=False,
            )
        ),
        provider,
    )
    with pytest.raises(ModelProviderError, match="tools"):
        asyncio.run(model_gateway.complete("main", text_request(tools=(ModelTool(name="read"),))))

    async def collect() -> None:
        async for _event in model_gateway.stream("main", text_request()):
            pytest.fail("disabled stream emitted data")

    with pytest.raises(ModelProviderError, match="streaming"):
        asyncio.run(collect())
    assert provider.requests == []


def test_disabled_parallel_calls_are_rejected_in_complete_and_stream() -> None:
    provider = FakeProvider(
        tool_calls=(
            ModelToolCall(call_id="one", name="read"),
            ModelToolCall(call_id="two", name="read"),
        )
    )
    model_gateway = gateway(
        profile(capabilities=ModelCapabilities(parallel_tool_calls=False)), provider
    )
    request = text_request(tools=(ModelTool(name="read"),))
    with pytest.raises(ModelProviderError, match="parallel"):
        asyncio.run(model_gateway.complete("main", request))

    async def collect() -> None:
        async for _event in model_gateway.stream("main", request):
            pass

    with pytest.raises(ModelProviderError, match="parallel"):
        asyncio.run(collect())


def test_context_limit_cannot_be_silently_increased() -> None:
    configured = profile(capabilities=ModelCapabilities(max_context_tokens=100))
    assert (
        configured.effective_capabilities(
            ProviderCapabilities(
                max_context_tokens=200,
            )
        ).max_context_tokens
        == 100
    )
    with pytest.raises(ValueError, match="max_context_tokens"):
        configured.effective_capabilities(ProviderCapabilities(max_context_tokens=50))
