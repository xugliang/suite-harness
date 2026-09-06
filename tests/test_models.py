from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest
from pydantic import SecretStr, ValidationError

from suiteharness.models import (
    BUILTIN_PROVIDERS,
    AdapterKind,
    BedrockConverseAdapter,
    FinishReason,
    HttpRequest,
    HttpResponse,
    HttpxTransport,
    MappingSecretResolver,
    ModelErrorCode,
    ModelGateway,
    ModelMessage,
    ModelProfile,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ModelRole,
    ModelRoute,
    ModelTool,
    ProviderAuthKind,
    ProviderCapabilities,
    ProviderDescriptor,
    ProviderRegistry,
    TextContent,
    VertexGeminiAdapter,
    bedrock_factory,
    create_builtin_provider_registry,
    iter_sse_json,
    vertex_factory,
)
from suiteharness.models.adapters import (
    AnthropicMessagesAdapter,
    GeminiAdapter,
    OpenAIChatAdapter,
    OpenAIResponsesAdapter,
)


class FakeTransport:
    def __init__(
        self,
        responses: list[HttpResponse] | None = None,
        chunks: tuple[bytes, ...] = (),
    ) -> None:
        self.responses = list(responses or [])
        self.chunks = chunks
        self.requests: list[HttpRequest] = []

    async def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        return self.responses.pop(0)

    async def _stream(self, request: HttpRequest) -> AsyncIterator[bytes]:
        self.requests.append(request)
        for chunk in self.chunks:
            yield chunk

    def stream(self, request: HttpRequest) -> AsyncIterator[bytes]:
        return self._stream(request)


def _profile(provider: str, *, model: str = "model-1") -> ModelProfile:
    return ModelProfile(
        profile_id=f"{provider}-production",
        provider_id=provider,
        model=model,
        credential_ref=f"models.{provider}.api_key",
        max_retries=0,
    )


def _request(*, model: str | None = None) -> ModelRequest:
    return ModelRequest(
        model=model,
        messages=(ModelMessage.text(ModelRole.USER, "你好"),),
        tools=(
            ModelTool(
                name="suiteharness.fs.read",
                description="read a file",
                input_schema={"type": "object"},
            ),
        ),
    )


def _json_response(value: object, status: int = 200) -> HttpResponse:
    return HttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        body=json.dumps(value).encode(),
    )


def test_builtin_registry_covers_server_providers_without_personal_oauth() -> None:
    names = {item.provider_id for item in BUILTIN_PROVIDERS}
    assert {
        "openai",
        "anthropic",
        "gemini",
        "bedrock",
        "vertex",
        "deepseek",
        "dashscope",
        "volcengine",
        "moonshot",
        "minimax",
        "zhipu",
        "baidu",
        "modelscope",
        "siliconflow",
        "openrouter",
        "aihubmix",
        "groq",
        "mistral",
        "stepfun",
        "ollama",
        "vllm",
    } <= names
    assert "github_copilot" not in names
    assert "openai_codex" not in names
    assert "anthropic_claude" not in names
    assert all(item.server_only is True for item in BUILTIN_PROVIDERS)


def test_secret_and_http_request_repr_are_redacted() -> None:
    resolver = MappingSecretResolver({"models.openai.api_key": "top-secret"})
    assert resolver.resolve("models.openai.api_key").get_secret_value() == "top-secret"
    assert "top-secret" not in repr(resolver)
    request = HttpRequest(
        method="POST",
        url="https://example.invalid",
        headers={"Authorization": "Bearer top-secret"},
        json_body={"prompt": "private-customer-source"},
    )
    assert "top-secret" not in repr(request)
    assert "private-customer-source" not in repr(request)
    response = HttpResponse(
        status_code=200,
        headers={},
        body=b"private-model-response",
    )
    assert "private-model-response" not in repr(response)


def test_plain_http_profiles_require_an_explicit_internal_opt_in() -> None:
    with pytest.raises(ValidationError):
        ModelProfile(
            profile_id="unsafe",
            provider_id="vllm",
            model="private-model",
            base_url="http://model.internal/v1",
        )
    profile = ModelProfile(
        profile_id="internal",
        provider_id="vllm",
        model="private-model",
        base_url="http://model.internal/v1",
        allow_plain_http=True,
    )
    assert profile.allow_plain_http is True


def test_openai_adapter_converts_tools_and_normalizes_response() -> None:
    transport = FakeTransport(
        [
            _json_response(
                {
                    "id": "chatcmpl-1",
                    "model": "gpt-enterprise",
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "function": {
                                            "name": "suiteharness.fs.read",
                                            "arguments": '{"path":"report.txt"}',
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 5},
                }
            )
        ]
    )
    descriptor = create_builtin_provider_registry().descriptor("openai")
    assert descriptor is not None
    adapter = OpenAIChatAdapter(
        descriptor,
        _profile("openai", model="gpt-enterprise"),
        transport,
        SecretStr("server-key"),
    )
    response = asyncio.run(adapter.complete(_request()))
    assert response.finish_reason is FinishReason.TOOL_CALLS
    assert response.tool_calls[0].arguments == {"path": "report.txt"}
    sent = transport.requests[0]
    assert sent.json_body["tools"][0]["function"]["name"] == "suiteharness.fs.read"  # type: ignore[index]
    assert "server-key" not in repr(sent)


def test_dashscope_structured_output_disables_thinking_explicitly() -> None:
    transport = FakeTransport(
        [
            _json_response(
                {
                    "id": "response-1",
                    "model": "qwen-vision",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": '{"claims":[]}'},
                        }
                    ],
                }
            )
        ]
    )
    descriptor = create_builtin_provider_registry().descriptor("dashscope")
    assert descriptor is not None
    profile = ModelProfile(
        profile_id="dashscope-vision",
        provider_id="dashscope",
        model="qwen-vision",
        credential_ref="models.dashscope.api_key",
        max_retries=0,
        provider_options={"enable_thinking": False},
    )
    request = ModelRequest(
        messages=(ModelMessage.text(ModelRole.USER, "Return JSON"),),
        response_schema={"type": "object", "properties": {"claims": {"type": "array"}}},
    )

    response = asyncio.run(
        OpenAIChatAdapter(descriptor, profile, transport, SecretStr("server-key")).complete(
            request
        )
    )

    assert response.finish_reason is FinishReason.STOP
    body = transport.requests[0].json_body
    assert body["enable_thinking"] is False  # type: ignore[index]
    assert body["response_format"]["json_schema"]["strict"] is True  # type: ignore[index]


def test_dashscope_structured_output_fails_without_explicit_non_thinking_mode() -> None:
    descriptor = create_builtin_provider_registry().descriptor("dashscope")
    assert descriptor is not None
    adapter = OpenAIChatAdapter(
        descriptor,
        _profile("dashscope", model="qwen-vision"),
        FakeTransport(),
        SecretStr("server-key"),
    )
    request = ModelRequest(
        messages=(ModelMessage.text(ModelRole.USER, "Return JSON"),),
        response_schema={"type": "object"},
    )

    with pytest.raises(ModelProviderError, match="enable_thinking=false") as caught:
        asyncio.run(adapter.complete(request))

    assert caught.value.code is ModelErrorCode.CONFIGURATION


@pytest.mark.parametrize(
    "options, match",
    [
        ({"enable_thinking": "false"}, "must be boolean"),
        ({"arbitrary_body_field": True}, "unsupported.*provider option"),
    ],
)
def test_openai_compatible_provider_options_are_fail_closed(
    options: dict[str, object], match: str
) -> None:
    descriptor = create_builtin_provider_registry().descriptor("dashscope")
    assert descriptor is not None
    profile = ModelProfile(
        profile_id="dashscope-vision",
        provider_id="dashscope",
        model="qwen-vision",
        provider_options=options,  # type: ignore[arg-type]
    )

    with pytest.raises(ModelProviderError, match=match) as caught:
        OpenAIChatAdapter(descriptor, profile, FakeTransport(), None)

    assert caught.value.code is ModelErrorCode.CONFIGURATION


def test_openai_responses_adapter_uses_server_api_and_normalizes_function_calls() -> None:
    transport = FakeTransport(
        [
            _json_response(
                {
                    "id": "resp-1",
                    "model": "gpt-enterprise",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "先读取"}],
                        },
                        {
                            "type": "function_call",
                            "call_id": "call-1",
                            "name": "suiteharness.fs.read",
                            "arguments": '{"path":"report.txt"}',
                        },
                    ],
                    "usage": {"input_tokens": 7, "output_tokens": 3},
                }
            )
        ]
    )
    descriptor = create_builtin_provider_registry().descriptor("openai")
    assert descriptor is not None
    assert descriptor.adapter is AdapterKind.OPENAI_RESPONSES
    adapter = OpenAIResponsesAdapter(
        descriptor,
        _profile("openai", model="gpt-enterprise"),
        transport,
        SecretStr("server-key"),
    )
    response = asyncio.run(adapter.complete(_request()))
    assert response.finish_reason is FinishReason.TOOL_CALLS
    assert response.content == (TextContent(text="先读取"),)
    assert response.tool_calls[0].name == "suiteharness.fs.read"
    assert transport.requests[0].url == "https://api.openai.com/v1/responses"
    assert transport.requests[0].json_body["store"] is False  # type: ignore[index]


def test_anthropic_and_gemini_have_native_wire_shapes() -> None:
    registry = create_builtin_provider_registry()
    anthropic_descriptor = registry.descriptor("anthropic")
    gemini_descriptor = registry.descriptor("gemini")
    assert anthropic_descriptor is not None
    assert gemini_descriptor is not None

    anthropic_transport = FakeTransport(
        [
            _json_response(
                {
                    "id": "msg-1",
                    "model": "claude-enterprise",
                    "content": [{"type": "text", "text": "完成"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 4, "output_tokens": 2},
                }
            )
        ]
    )
    anthropic = AnthropicMessagesAdapter(
        anthropic_descriptor,
        _profile("anthropic", model="claude-enterprise"),
        anthropic_transport,
        SecretStr("server-key"),
    )
    result = asyncio.run(anthropic.complete(_request()))
    assert result.content == (TextContent(text="完成"),)
    assert anthropic_transport.requests[0].url.endswith("/v1/messages")

    gemini_transport = FakeTransport(
        [
            _json_response(
                {
                    "responseId": "gemini-1",
                    "candidates": [
                        {
                            "finishReason": "STOP",
                            "content": {"parts": [{"text": "完成"}]},
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 3,
                        "candidatesTokenCount": 2,
                    },
                }
            )
        ]
    )
    gemini = GeminiAdapter(
        gemini_descriptor,
        _profile("gemini", model="gemini-enterprise"),
        gemini_transport,
        SecretStr("server-key"),
    )
    result = asyncio.run(gemini.complete(_request()))
    assert result.content == (TextContent(text="完成"),)
    assert ":generateContent" in gemini_transport.requests[0].url


def test_bedrock_and_vertex_use_injected_server_identities() -> None:
    class FakeBedrock:
        def __init__(self) -> None:
            self.request: dict[str, object] | None = None

        async def converse(self, *, model_id, request):  # type: ignore[no-untyped-def]
            self.request = {"model_id": model_id, **request}
            return {
                "ResponseMetadata": {"RequestId": "aws-request-1"},
                "output": {"message": {"content": [{"text": "完成"}]}},
                "stopReason": "end_turn",
                "usage": {"inputTokens": 5, "outputTokens": 2},
            }

        async def _stream(self):  # type: ignore[no-untyped-def]
            if False:
                yield {}

        def converse_stream(self, *, model_id, request):  # type: ignore[no-untyped-def]
            return self._stream()

    registry = create_builtin_provider_registry()
    bedrock_descriptor = registry.descriptor("bedrock")
    assert bedrock_descriptor is not None
    bedrock_client = FakeBedrock()
    bedrock = BedrockConverseAdapter(
        bedrock_descriptor,
        ModelProfile(
            profile_id="bedrock-production",
            provider_id="bedrock",
            model="anthropic.claude-enterprise",
        ),
        bedrock_client,
    )
    response = asyncio.run(bedrock.complete(_request()))
    assert response.content == (TextContent(text="完成"),)
    assert bedrock_client.request is not None
    assert "toolConfig" in bedrock_client.request

    class FakeTokenProvider:
        async def access_token(self, audience: str) -> str:
            assert audience == "https://www.googleapis.com/auth/cloud-platform"
            return "short-lived-service-token"

    vertex_transport = FakeTransport(
        [
            _json_response(
                {
                    "responseId": "vertex-1",
                    "candidates": [
                        {"finishReason": "STOP", "content": {"parts": [{"text": "完成"}]}}
                    ],
                }
            )
        ]
    )
    vertex_descriptor = registry.descriptor("vertex")
    assert vertex_descriptor is not None
    vertex = VertexGeminiAdapter(
        vertex_descriptor,
        ModelProfile(
            profile_id="vertex-production",
            provider_id="vertex",
            model="gemini-enterprise",
            provider_options={"project": "company-project", "location": "asia-east1"},
        ),
        vertex_transport,
        FakeTokenProvider(),
    )
    response = asyncio.run(vertex.complete(_request()))
    assert response.content == (TextContent(text="完成"),)
    sent = vertex_transport.requests[0]
    assert "projects/company-project/locations/asia-east1" in sent.url
    assert "short-lived-service-token" not in repr(sent)


def test_cloud_factories_are_bound_explicitly_to_server_clients() -> None:
    class FakeBedrock:
        async def converse(self, *, model_id, request):  # type: ignore[no-untyped-def]
            return {
                "output": {"message": {"content": [{"text": "ok"}]}},
                "stopReason": "end_turn",
            }

        async def _stream(self):  # type: ignore[no-untyped-def]
            if False:
                yield {}

        def converse_stream(self, *, model_id, request):  # type: ignore[no-untyped-def]
            return self._stream()

    class FakeTokenProvider:
        async def access_token(self, audience: str) -> str:
            return "token"

    registry = create_builtin_provider_registry()
    registry.bind_factory("bedrock", bedrock_factory(FakeBedrock()))
    registry.bind_factory("vertex", vertex_factory(FakeTokenProvider()))
    assert (
        registry.create(
            ModelProfile(profile_id="aws", provider_id="bedrock", model="model"),
            FakeTransport(),
            MappingSecretResolver({}),
        ).provider_id
        == "bedrock"
    )
    assert (
        registry.create(
            ModelProfile(
                profile_id="gcp",
                provider_id="vertex",
                model="model",
                provider_options={"project": "project"},
            ),
            FakeTransport(),
            MappingSecretResolver({}),
        ).provider_id
        == "vertex"
    )


def test_gateway_rejects_model_override_outside_server_allowlist() -> None:
    registry = create_builtin_provider_registry()
    profile = _profile("openai", model="approved-model")
    gateway = ModelGateway(
        registry,
        {profile.profile_id: profile},
        {
            "default": ModelRoute(
                route_id="default",
                primary_profile=profile.profile_id,
            )
        },
        FakeTransport(),
        MappingSecretResolver({"models.openai.api_key": "server-key"}),
    )
    with pytest.raises(ModelProviderError) as caught:
        asyncio.run(gateway.complete("default", _request(model="attacker-model")))
    assert caught.value.code is ModelErrorCode.PERMISSION_DENIED


def test_registry_refuses_metadata_without_a_real_adapter_at_creation() -> None:
    registry = ProviderRegistry()
    registry.register(
        ProviderDescriptor(
            provider_id="cloud",
            display_name="Cloud",
            adapter=AdapterKind.BEDROCK_CONVERSE,
            auth=ProviderAuthKind.AWS_IAM,
            capabilities=ProviderCapabilities(),
        ),
        None,
    )
    with pytest.raises(ModelProviderError) as caught:
        registry.create(
            ModelProfile(profile_id="cloud-prod", provider_id="cloud", model="model"),
            FakeTransport(),
            MappingSecretResolver({}),
        )
    assert caught.value.code is ModelErrorCode.CONFIGURATION


def test_sse_decoder_handles_arbitrary_chunk_boundaries() -> None:
    async def chunks() -> AsyncIterator[bytes]:
        yield b'data: {"a":'
        yield b" 1}\r\n\r\ndata: [DONE]\n\n"

    async def collect() -> list[dict[str, object]]:
        return [item async for item in iter_sse_json(chunks())]

    assert asyncio.run(collect()) == [{"a": 1}]


def test_sse_decoder_handles_utf8_code_points_split_across_chunks() -> None:
    encoded = 'data: {"answer":"你好🙂"}\n\n'.encode()
    chinese = "你".encode()
    split_at = encoded.index(chinese) + 1

    async def chunks() -> AsyncIterator[bytes]:
        yield encoded[:split_at]
        yield encoded[split_at : split_at + 1]
        yield encoded[split_at + 1 :]

    async def collect() -> list[dict[str, object]]:
        return [item async for item in iter_sse_json(chunks())]

    assert asyncio.run(collect()) == [{"answer": "你好🙂"}]


def test_sse_decoder_enforces_total_and_unterminated_event_limits() -> None:
    async def oversized() -> AsyncIterator[bytes]:
        yield b"data: 1234567890\n\n"

    async def unterminated() -> AsyncIterator[bytes]:
        yield b"data: " + b"x" * 20

    async def exercise() -> None:
        with pytest.raises(ModelProviderError, match="byte limit"):
            _ = [item async for item in iter_sse_json(oversized(), max_bytes=5)]
        with pytest.raises(ModelProviderError, match="buffer limit"):
            _ = [
                item
                async for item in iter_sse_json(
                    unterminated(),
                    max_bytes=100,
                    max_buffer_characters=10,
                )
            ]

    asyncio.run(exercise())


def test_httpx_transport_rejects_oversized_buffered_model_response() -> None:
    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}

        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, *_exc):  # type: ignore[no-untyped-def]
            return None

        async def aiter_bytes(self):  # type: ignore[no-untyped-def]
            yield b"1234"
            yield b"5678"

    class Client:
        def stream(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return Response()

    async def exercise() -> None:
        transport = HttpxTransport(max_response_bytes=6)
        await transport._client.aclose()  # noqa: SLF001
        transport._client = Client()  # type: ignore[assignment]  # noqa: SLF001
        with pytest.raises(ModelProviderError, match="byte limit") as caught:
            await transport.send(HttpRequest("POST", "https://model.example/v1"))
        assert caught.value.code is ModelErrorCode.RESPONSE_INVALID

    asyncio.run(exercise())


def test_response_object_never_exposes_provider_error_body() -> None:
    transport = FakeTransport([_json_response({"secret": "leak"}, status=401)])
    descriptor = create_builtin_provider_registry().descriptor("openai")
    assert descriptor is not None
    adapter = OpenAIChatAdapter(
        descriptor,
        _profile("openai"),
        transport,
        SecretStr("server-key"),
    )
    with pytest.raises(ModelProviderError) as caught:
        asyncio.run(adapter.complete(_request()))
    assert caught.value.code is ModelErrorCode.AUTHENTICATION
    assert "leak" not in str(caught.value)


def test_model_response_is_provider_neutral() -> None:
    response = ModelResponse(
        response_id="response-1",
        provider_id="any-provider",
        model="any-model",
        content=(TextContent(text="ok"),),
        finish_reason=FinishReason.STOP,
    )
    assert response.usage.total_tokens == 0
