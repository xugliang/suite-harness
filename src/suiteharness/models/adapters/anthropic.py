"""Anthropic Messages API adapter using server-managed credentials."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from pydantic import SecretStr

from suiteharness.models.adapters._common import (
    anthropic_content,
    finish_reason,
    response_id,
    response_object,
    text_from_message,
)
from suiteharness.models.errors import ModelErrorCode, ModelProviderError
from suiteharness.models.transport import HttpRequest, HttpTransport, iter_sse_json
from suiteharness.models.types import (
    FinishReason,
    ModelProfile,
    ModelRequest,
    ModelResponse,
    ModelRole,
    ModelStreamEvent,
    ModelStreamEventType,
    ModelToolCall,
    ModelUsage,
    ProviderDescriptor,
    TextContent,
)


class AnthropicMessagesAdapter:
    def __init__(
        self,
        descriptor: ProviderDescriptor,
        profile: ModelProfile,
        transport: HttpTransport,
        credential: SecretStr,
    ) -> None:
        self.provider_id = descriptor.provider_id
        self._profile = profile
        self._transport = transport
        self._credential = credential
        base_url = profile.base_url or descriptor.default_base_url or "https://api.anthropic.com"
        self._url = f"{base_url.rstrip('/')}/v1/messages"

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": self._credential.get_secret_value(),
            **self._profile.default_headers,
        }

    def _body(self, request: ModelRequest, *, stream: bool) -> dict[str, Any]:
        system: list[str] = []
        messages: list[dict[str, Any]] = []
        for message in request.messages:
            if message.role is ModelRole.SYSTEM:
                system.append(text_from_message(message))
                continue
            if message.role is ModelRole.TOOL:
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": message.tool_call_id,
                                "content": text_from_message(message),
                            }
                        ],
                    }
                )
                continue
            content = anthropic_content(message)
            if message.tool_calls:
                content.extend(
                    {
                        "type": "tool_use",
                        "id": call.call_id,
                        "name": call.name,
                        "input": call.arguments,
                    }
                    for call in message.tool_calls
                )
            messages.append({"role": message.role.value, "content": content})
        body: dict[str, Any] = {
            "model": request.model or self._profile.model,
            "messages": messages,
            "max_tokens": request.max_output_tokens,
            "stream": stream,
        }
        if system:
            body["system"] = "\n\n".join(system)
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.top_p is not None:
            body["top_p"] = request.top_p
        if request.stop:
            body["stop_sequences"] = list(request.stop)
        if request.tools:
            body["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
                for tool in request.tools
            ]
            if request.tool_choice != "auto":
                body["tool_choice"] = {"type": request.tool_choice}
        if request.response_schema is not None:
            raise ModelProviderError(
                ModelErrorCode.UNSUPPORTED_FEATURE,
                "portable JSON Schema output is not supported by this Anthropic adapter",
                provider_id=self.provider_id,
            )
        return body

    def _request(self, request: ModelRequest, *, stream: bool) -> HttpRequest:
        return HttpRequest(
            method="POST",
            url=self._url,
            headers=self._headers(),
            json_body=self._body(request, stream=stream),
            timeout_seconds=self._profile.timeout_seconds,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        response = await self._transport.send(self._request(request, stream=False))
        payload = response_object(response, self.provider_id)
        text: list[TextContent] = []
        calls: list[ModelToolCall] = []
        content = payload.get("content")
        if not isinstance(content, list):
            raise ModelProviderError(
                ModelErrorCode.RESPONSE_INVALID,
                "Anthropic response content must be a list",
                provider_id=self.provider_id,
            )
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text.append(TextContent(text=str(block.get("text") or "")))
            elif block.get("type") == "tool_use":
                raw_input = block.get("input")
                if not isinstance(raw_input, dict):
                    raise ModelProviderError(
                        ModelErrorCode.RESPONSE_INVALID,
                        "Anthropic tool input must be an object",
                        provider_id=self.provider_id,
                    )
                calls.append(
                    ModelToolCall(
                        call_id=response_id(block.get("id")),
                        name=str(block.get("name") or ""),
                        arguments=raw_input,
                    )
                )
        return ModelResponse(
            response_id=response_id(payload.get("id")),
            provider_id=self.provider_id,
            model=str(payload.get("model") or request.model or self._profile.model),
            content=tuple(text),
            tool_calls=tuple(calls),
            finish_reason=finish_reason(payload.get("stop_reason")),
            usage=_usage(payload.get("usage")),
        )

    async def _stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        identifier: str | None = None
        final_reason = FinishReason.UNKNOWN
        usage = ModelUsage()
        tools: dict[int, dict[str, str]] = {}
        async for event in iter_sse_json(self._transport.stream(self._request(request, stream=True))):
            event_type = event.get("type")
            if event_type == "message_start":
                message = event.get("message")
                message = message if isinstance(message, dict) else {}
                identifier = response_id(message.get("id"))
                usage = _usage(message.get("usage"))
                yield ModelStreamEvent(
                    type=ModelStreamEventType.MESSAGE_START,
                    response_id=identifier,
                )
                if usage.input_tokens:
                    yield ModelStreamEvent(type=ModelStreamEventType.USAGE, usage=usage)
            elif event_type == "content_block_start":
                index = int(event.get("index") or 0)
                block = event.get("content_block")
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tools[index] = {
                        "id": str(block.get("id") or ""),
                        "name": str(block.get("name") or ""),
                        "arguments": "",
                    }
            elif event_type == "content_block_delta":
                index = int(event.get("index") or 0)
                delta = event.get("delta")
                if not isinstance(delta, dict):
                    continue
                if delta.get("type") == "text_delta":
                    value = str(delta.get("text") or "")
                    if value:
                        yield ModelStreamEvent(
                            type=ModelStreamEventType.TEXT_DELTA,
                            text_delta=value,
                        )
                elif delta.get("type") == "thinking_delta":
                    value = str(delta.get("thinking") or "")
                    if value:
                        yield ModelStreamEvent(
                            type=ModelStreamEventType.REASONING_DELTA,
                            reasoning_delta=value,
                        )
                elif delta.get("type") == "input_json_delta" and index in tools:
                    tools[index]["arguments"] += str(delta.get("partial_json") or "")
            elif event_type == "message_delta":
                delta = event.get("delta")
                if isinstance(delta, dict):
                    final_reason = finish_reason(delta.get("stop_reason"))
                raw_usage = event.get("usage")
                if isinstance(raw_usage, dict):
                    update = _usage(raw_usage)
                    usage = ModelUsage(
                        input_tokens=max(usage.input_tokens, update.input_tokens),
                        output_tokens=max(usage.output_tokens, update.output_tokens),
                        cached_input_tokens=max(
                            usage.cached_input_tokens,
                            update.cached_input_tokens,
                        ),
                    )
                    yield ModelStreamEvent(type=ModelStreamEventType.USAGE, usage=usage)
        if identifier is None:
            identifier = response_id(None)
            yield ModelStreamEvent(
                type=ModelStreamEventType.MESSAGE_START,
                response_id=identifier,
            )
        for index in sorted(tools):
            item = tools[index]
            try:
                arguments = json.loads(item["arguments"] or "{}")
            except json.JSONDecodeError as exc:
                raise ModelProviderError(
                    ModelErrorCode.RESPONSE_INVALID,
                    "Anthropic returned invalid streamed tool input",
                    provider_id=self.provider_id,
                ) from exc
            if not isinstance(arguments, dict):
                raise ModelProviderError(
                    ModelErrorCode.RESPONSE_INVALID,
                    "Anthropic streamed tool input must be an object",
                    provider_id=self.provider_id,
                )
            yield ModelStreamEvent(
                type=ModelStreamEventType.TOOL_CALL_DELTA,
                tool_call=ModelToolCall(
                    call_id=response_id(item["id"]),
                    name=item["name"],
                    arguments=arguments,
                ),
            )
        yield ModelStreamEvent(
            type=ModelStreamEventType.MESSAGE_END,
            response_id=identifier,
            finish_reason=final_reason,
            usage=usage,
        )

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        return self._stream(request)


def _usage(value: Any) -> ModelUsage:
    if not isinstance(value, dict):
        return ModelUsage()
    return ModelUsage(
        input_tokens=int(value.get("input_tokens") or 0),
        output_tokens=int(value.get("output_tokens") or 0),
        cached_input_tokens=int(value.get("cache_read_input_tokens") or 0),
    )
