"""Google Gemini native API adapter for server API keys."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote

from pydantic import SecretStr

from suiteharness.models.adapters._common import finish_reason, response_id, response_object
from suiteharness.models.errors import ModelErrorCode, ModelProviderError
from suiteharness.models.transport import HttpRequest, HttpTransport, iter_sse_json
from suiteharness.models.types import (
    DocumentContent,
    FinishReason,
    ImageContent,
    ModelMessage,
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


class GeminiAdapter:
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
        self._base_url = (
            profile.base_url
            or descriptor.default_base_url
            or "https://generativelanguage.googleapis.com/v1beta"
        ).rstrip("/")

    def _url(self, model: str, *, stream: bool) -> str:
        operation = "streamGenerateContent?alt=sse" if stream else "generateContent"
        return f"{self._base_url}/models/{quote(model, safe='')}:" + operation

    def _parts(self, message: ModelMessage) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = []
        for part in message.content:
            if isinstance(part, TextContent):
                parts.append({"text": part.text})
            elif isinstance(part, ImageContent):
                if part.data_base64 is not None:
                    parts.append(
                        {
                            "inlineData": {
                                "mimeType": part.mime_type,
                                "data": part.data_base64,
                            }
                        }
                    )
                elif part.url is not None:
                    parts.append(
                        {"fileData": {"mimeType": part.mime_type, "fileUri": part.url}}
                    )
            elif isinstance(part, DocumentContent):
                parts.append(
                    {
                        "inlineData": {
                            "mimeType": part.mime_type,
                            "data": part.data_base64,
                        }
                    }
                )
        if message.role is ModelRole.TOOL:
            if not message.name:
                raise ModelProviderError(
                    ModelErrorCode.INVALID_REQUEST,
                    "Gemini tool result messages require the function name",
                    provider_id=self.provider_id,
                )
            text = "\n".join(
                part.text for part in message.content if isinstance(part, TextContent)
            )
            parts = [{"functionResponse": {"name": message.name, "response": {"result": text}}}]
        for call in message.tool_calls:
            parts.append({"functionCall": {"name": call.name, "args": call.arguments}})
        return parts

    def _body(self, request: ModelRequest) -> dict[str, Any]:
        system: list[dict[str, Any]] = []
        contents: list[dict[str, Any]] = []
        for message in request.messages:
            if message.role is ModelRole.SYSTEM:
                system.extend(self._parts(message))
                continue
            role = "model" if message.role is ModelRole.ASSISTANT else "user"
            contents.append({"role": role, "parts": self._parts(message)})
        generation_config: dict[str, Any] = {
            "maxOutputTokens": request.max_output_tokens
        }
        if request.temperature is not None:
            generation_config["temperature"] = request.temperature
        if request.top_p is not None:
            generation_config["topP"] = request.top_p
        if request.stop:
            generation_config["stopSequences"] = list(request.stop)
        if request.response_schema is not None:
            generation_config["responseMimeType"] = "application/json"
            generation_config["responseSchema"] = request.response_schema
        body: dict[str, Any] = {
            "contents": contents,
            "generationConfig": generation_config,
        }
        if system:
            body["systemInstruction"] = {"parts": system}
        if request.tools:
            body["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.input_schema,
                        }
                        for tool in request.tools
                    ]
                }
            ]
            mode = {"auto": "AUTO", "none": "NONE", "required": "ANY"}[request.tool_choice]
            body["toolConfig"] = {"functionCallingConfig": {"mode": mode}}
        return body

    def _request(self, request: ModelRequest, *, stream: bool) -> HttpRequest:
        model = request.model or self._profile.model
        return HttpRequest(
            method="POST",
            url=self._url(model, stream=stream),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self._credential.get_secret_value(),
                **self._profile.default_headers,
            },
            json_body=self._body(request),
            timeout_seconds=self._profile.timeout_seconds,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        response = await self._transport.send(self._request(request, stream=False))
        payload = response_object(response, self.provider_id)
        text, calls, reason = self._candidate(payload)
        return ModelResponse(
            response_id=response_id(payload.get("responseId")),
            provider_id=self.provider_id,
            model=request.model or self._profile.model,
            content=tuple(TextContent(text=value) for value in text),
            tool_calls=tuple(calls),
            finish_reason=reason,
            usage=_usage(payload.get("usageMetadata")),
        )

    def _candidate(
        self, payload: dict[str, Any]
    ) -> tuple[list[str], list[ModelToolCall], FinishReason]:
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
            raise ModelProviderError(
                ModelErrorCode.RESPONSE_INVALID,
                "Gemini response has no candidate",
                provider_id=self.provider_id,
            )
        candidate = candidates[0]
        content = candidate.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            raise ModelProviderError(
                ModelErrorCode.RESPONSE_INVALID,
                "Gemini candidate has no content parts",
                provider_id=self.provider_id,
            )
        text: list[str] = []
        calls: list[ModelToolCall] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            if isinstance(part.get("text"), str):
                text.append(part["text"])
            function = part.get("functionCall")
            if isinstance(function, dict):
                arguments = function.get("args") or {}
                if not isinstance(arguments, dict):
                    raise ModelProviderError(
                        ModelErrorCode.RESPONSE_INVALID,
                        "Gemini function arguments must be an object",
                        provider_id=self.provider_id,
                    )
                calls.append(
                    ModelToolCall(
                        call_id=response_id(function.get("id")),
                        name=str(function.get("name") or ""),
                        arguments=arguments,
                    )
                )
        return text, calls, finish_reason(candidate.get("finishReason"))

    async def _stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        identifier: str | None = None
        reason = FinishReason.UNKNOWN
        usage = ModelUsage()
        async for payload in iter_sse_json(self._transport.stream(self._request(request, stream=True))):
            if identifier is None:
                identifier = response_id(payload.get("responseId"))
                yield ModelStreamEvent(
                    type=ModelStreamEventType.MESSAGE_START,
                    response_id=identifier,
                )
            values, calls, current_reason = self._candidate(payload)
            if current_reason is not FinishReason.UNKNOWN:
                reason = current_reason
            for value in values:
                if value:
                    yield ModelStreamEvent(
                        type=ModelStreamEventType.TEXT_DELTA,
                        text_delta=value,
                    )
            for call in calls:
                yield ModelStreamEvent(
                    type=ModelStreamEventType.TOOL_CALL_DELTA,
                    tool_call=call,
                )
            raw_usage = payload.get("usageMetadata")
            if isinstance(raw_usage, dict):
                usage = _usage(raw_usage)
                yield ModelStreamEvent(type=ModelStreamEventType.USAGE, usage=usage)
        if identifier is None:
            identifier = response_id(None)
            yield ModelStreamEvent(
                type=ModelStreamEventType.MESSAGE_START,
                response_id=identifier,
            )
        yield ModelStreamEvent(
            type=ModelStreamEventType.MESSAGE_END,
            response_id=identifier,
            finish_reason=reason,
            usage=usage,
        )

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        return self._stream(request)


def _usage(value: Any) -> ModelUsage:
    if not isinstance(value, dict):
        return ModelUsage()
    return ModelUsage(
        input_tokens=int(value.get("promptTokenCount") or 0),
        output_tokens=int(value.get("candidatesTokenCount") or 0),
        cached_input_tokens=int(value.get("cachedContentTokenCount") or 0),
        reasoning_tokens=int(value.get("thoughtsTokenCount") or 0),
    )
