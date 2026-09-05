"""OpenAI-compatible Chat Completions adapter."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from pydantic import SecretStr

from suiteharness.models.adapters._common import (
    decode_tool_arguments,
    finish_reason,
    openai_content,
    response_id,
    response_object,
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


class OpenAIChatAdapter:
    """One adapter serves OpenAI-compatible commercial and private endpoints."""

    def __init__(
        self,
        descriptor: ProviderDescriptor,
        profile: ModelProfile,
        transport: HttpTransport,
        credential: SecretStr | None,
    ) -> None:
        self.provider_id = descriptor.provider_id
        self._descriptor = descriptor
        self._profile = profile
        self._transport = transport
        self._credential = credential
        base_url = profile.base_url or descriptor.default_base_url
        if not base_url:
            raise ModelProviderError(
                ModelErrorCode.CONFIGURATION,
                f"provider {self.provider_id!r} requires a base_url",
                provider_id=self.provider_id,
            )
        self._url = f"{base_url.rstrip('/')}/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self._profile.default_headers}
        if self._credential is not None:
            headers["Authorization"] = f"Bearer {self._credential.get_secret_value()}"
        return headers

    def _body(self, request: ModelRequest, *, stream: bool) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        for message in request.messages:
            converted: dict[str, Any] = {"role": message.role.value}
            if message.role is ModelRole.TOOL:
                converted["tool_call_id"] = message.tool_call_id
            converted["content"] = openai_content(message)
            if message.name:
                converted["name"] = message.name
            if message.tool_calls:
                converted["tool_calls"] = [
                    {
                        "id": call.call_id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(
                                call.arguments,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        },
                    }
                    for call in message.tool_calls
                ]
            messages.append(converted)
        body: dict[str, Any] = {
            "model": request.model or self._profile.model,
            "messages": messages,
            "max_tokens": request.max_output_tokens,
            "stream": stream,
        }
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.top_p is not None:
            body["top_p"] = request.top_p
        if request.stop:
            body["stop"] = list(request.stop)
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
                for tool in request.tools
            ]
            body["tool_choice"] = request.tool_choice
        if request.response_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": request.response_schema},
            }
        if stream:
            body["stream_options"] = {"include_usage": True}
        return body

    def _http_request(self, request: ModelRequest, *, stream: bool) -> HttpRequest:
        return HttpRequest(
            method="POST",
            url=self._url,
            headers=self._headers(),
            json_body=self._body(request, stream=stream),
            timeout_seconds=self._profile.timeout_seconds,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        response = await self._transport.send(self._http_request(request, stream=False))
        payload = response_object(response, self.provider_id)
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ModelProviderError(
                ModelErrorCode.RESPONSE_INVALID,
                "OpenAI-compatible response has no choice",
                provider_id=self.provider_id,
            )
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ModelProviderError(
                ModelErrorCode.RESPONSE_INVALID,
                "OpenAI-compatible response has no message",
                provider_id=self.provider_id,
            )
        text = message.get("content")
        content = () if text is None else (TextContent(text=str(text)),)
        calls: list[ModelToolCall] = []
        for raw in message.get("tool_calls") or []:
            if not isinstance(raw, dict) or not isinstance(raw.get("function"), dict):
                raise ModelProviderError(
                    ModelErrorCode.RESPONSE_INVALID,
                    "OpenAI-compatible tool call is malformed",
                    provider_id=self.provider_id,
                )
            function = raw["function"]
            calls.append(
                ModelToolCall(
                    call_id=response_id(raw.get("id")),
                    name=str(function.get("name") or ""),
                    arguments=decode_tool_arguments(function.get("arguments"), self.provider_id),
                )
            )
        usage = _usage(payload.get("usage"))
        return ModelResponse(
            response_id=response_id(payload.get("id")),
            provider_id=self.provider_id,
            model=str(payload.get("model") or request.model or self._profile.model),
            content=content,
            tool_calls=tuple(calls),
            finish_reason=finish_reason(choice.get("finish_reason")),
            usage=usage,
        )

    async def _stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        response_identifier: str | None = None
        final_reason = FinishReason.UNKNOWN
        calls: dict[int, dict[str, str]] = {}
        usage: ModelUsage | None = None
        async for payload in iter_sse_json(
            self._transport.stream(self._http_request(request, stream=True))
        ):
            if response_identifier is None:
                response_identifier = response_id(payload.get("id"))
                yield ModelStreamEvent(
                    type=ModelStreamEventType.MESSAGE_START,
                    response_id=response_identifier,
                )
            raw_usage = payload.get("usage")
            if isinstance(raw_usage, dict):
                usage = _usage(raw_usage)
                yield ModelStreamEvent(type=ModelStreamEventType.USAGE, usage=usage)
            choices = payload.get("choices")
            if not isinstance(choices, list):
                continue
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                if choice.get("finish_reason") is not None:
                    final_reason = finish_reason(choice.get("finish_reason"))
                delta = choice.get("delta")
                if not isinstance(delta, dict):
                    continue
                text = delta.get("content")
                if isinstance(text, str) and text:
                    yield ModelStreamEvent(
                        type=ModelStreamEventType.TEXT_DELTA,
                        text_delta=text,
                    )
                reasoning = delta.get("reasoning_content")
                if isinstance(reasoning, str) and reasoning:
                    yield ModelStreamEvent(
                        type=ModelStreamEventType.REASONING_DELTA,
                        reasoning_delta=reasoning,
                    )
                for raw_call in delta.get("tool_calls") or []:
                    if not isinstance(raw_call, dict):
                        continue
                    index = int(raw_call.get("index") or 0)
                    item = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    if raw_call.get("id"):
                        item["id"] = str(raw_call["id"])
                    function = raw_call.get("function")
                    if isinstance(function, dict):
                        if function.get("name"):
                            item["name"] += str(function["name"])
                        if function.get("arguments"):
                            item["arguments"] += str(function["arguments"])
        if response_identifier is None:
            response_identifier = response_id(None)
            yield ModelStreamEvent(
                type=ModelStreamEventType.MESSAGE_START,
                response_id=response_identifier,
            )
        for index in sorted(calls):
            item = calls[index]
            yield ModelStreamEvent(
                type=ModelStreamEventType.TOOL_CALL_DELTA,
                tool_call=ModelToolCall(
                    call_id=response_id(item["id"]),
                    name=item["name"],
                    arguments=decode_tool_arguments(item["arguments"], self.provider_id),
                ),
            )
        yield ModelStreamEvent(
            type=ModelStreamEventType.MESSAGE_END,
            response_id=response_identifier,
            finish_reason=final_reason,
            usage=usage,
        )

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        return self._stream(request)


def _usage(value: Any) -> ModelUsage:
    if not isinstance(value, dict):
        return ModelUsage()
    prompt_details = value.get("prompt_tokens_details")
    completion_details = value.get("completion_tokens_details")
    return ModelUsage(
        input_tokens=int(value.get("prompt_tokens") or 0),
        output_tokens=int(value.get("completion_tokens") or 0),
        cached_input_tokens=(
            int(prompt_details.get("cached_tokens") or 0)
            if isinstance(prompt_details, dict)
            else 0
        ),
        reasoning_tokens=(
            int(completion_details.get("reasoning_tokens") or 0)
            if isinstance(completion_details, dict)
            else 0
        ),
    )
