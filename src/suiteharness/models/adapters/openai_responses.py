"""OpenAI Responses API adapter using an organization/server API credential."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from pydantic import SecretStr

from suiteharness.models.adapters._common import (
    decode_tool_arguments,
    response_id,
    response_object,
)
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


class OpenAIResponsesAdapter:
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
        base_url = profile.base_url or descriptor.default_base_url or "https://api.openai.com/v1"
        self._url = f"{base_url.rstrip('/')}/responses"

    def _content(self, message: ModelMessage, *, output: bool) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for part in message.content:
            if isinstance(part, TextContent):
                result.append(
                    {
                        "type": "output_text" if output else "input_text",
                        "text": part.text,
                        **({"annotations": []} if output else {}),
                    }
                )
            elif isinstance(part, ImageContent):
                url = part.url or f"data:{part.mime_type};base64,{part.data_base64}"
                result.append({"type": "input_image", "image_url": url})
            elif isinstance(part, DocumentContent):
                result.append(
                    {
                        "type": "input_file",
                        "filename": part.name,
                        "file_data": f"data:{part.mime_type};base64,{part.data_base64}",
                    }
                )
        return result

    def _input(self, request: ModelRequest) -> tuple[str | None, list[dict[str, Any]]]:
        instructions: list[str] = []
        result: list[dict[str, Any]] = []
        for message in request.messages:
            if message.role is ModelRole.SYSTEM:
                instructions.extend(
                    part.text for part in message.content if isinstance(part, TextContent)
                )
                continue
            if message.role is ModelRole.TOOL:
                result.append(
                    {
                        "type": "function_call_output",
                        "call_id": message.tool_call_id,
                        "output": "\n".join(
                            part.text
                            for part in message.content
                            if isinstance(part, TextContent)
                        ),
                    }
                )
                continue
            output = message.role is ModelRole.ASSISTANT
            content = self._content(message, output=output)
            if content:
                item: dict[str, Any] = {"role": message.role.value, "content": content}
                if output:
                    item["type"] = "message"
                result.append(item)
            for call in message.tool_calls:
                result.append(
                    {
                        "type": "function_call",
                        "call_id": call.call_id,
                        "name": call.name,
                        "arguments": json.dumps(
                            call.arguments,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    }
                )
        return ("\n\n".join(instructions) or None), result

    def _body(self, request: ModelRequest, *, stream: bool) -> dict[str, Any]:
        instructions, input_items = self._input(request)
        body: dict[str, Any] = {
            "model": request.model or self._profile.model,
            "input": input_items,
            "max_output_tokens": request.max_output_tokens,
            "stream": stream,
            "store": False,
        }
        if instructions:
            body["instructions"] = instructions
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.top_p is not None:
            body["top_p"] = request.top_p
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                    "strict": True,
                }
                for tool in request.tools
            ]
            body["tool_choice"] = request.tool_choice
            body["parallel_tool_calls"] = True
        if request.response_schema is not None:
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "response",
                    "schema": request.response_schema,
                    "strict": True,
                }
            }
        return body

    def _request(self, request: ModelRequest, *, stream: bool) -> HttpRequest:
        return HttpRequest(
            method="POST",
            url=self._url,
            headers={
                "Authorization": f"Bearer {self._credential.get_secret_value()}",
                "Content-Type": "application/json",
                **self._profile.default_headers,
            },
            json_body=self._body(request, stream=stream),
            timeout_seconds=self._profile.timeout_seconds,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        response = await self._transport.send(self._request(request, stream=False))
        payload = response_object(response, self.provider_id)
        content, calls = self._output(payload)
        return ModelResponse(
            response_id=response_id(payload.get("id")),
            provider_id=self.provider_id,
            model=str(payload.get("model") or request.model or self._profile.model),
            content=tuple(TextContent(text=text) for text in content),
            tool_calls=tuple(calls),
            finish_reason=_responses_finish(payload, bool(calls)),
            usage=_usage(payload.get("usage")),
        )

    def _output(self, payload: dict[str, Any]) -> tuple[list[str], list[ModelToolCall]]:
        raw_output = payload.get("output")
        if not isinstance(raw_output, list):
            raise ModelProviderError(
                ModelErrorCode.RESPONSE_INVALID,
                "OpenAI Responses result has no output list",
                provider_id=self.provider_id,
            )
        text: list[str] = []
        calls: list[ModelToolCall] = []
        for item in raw_output:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "message":
                blocks = item.get("content")
                for block in blocks if isinstance(blocks, list) else []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "output_text":
                        text.append(str(block.get("text") or ""))
                    elif block.get("type") == "refusal":
                        text.append(str(block.get("refusal") or ""))
            elif item.get("type") == "function_call":
                calls.append(
                    ModelToolCall(
                        call_id=response_id(item.get("call_id")),
                        name=str(item.get("name") or ""),
                        arguments=decode_tool_arguments(item.get("arguments"), self.provider_id),
                    )
                )
        return text, calls

    async def _stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        identifier: str | None = None
        final_reason = FinishReason.UNKNOWN
        usage = ModelUsage()
        tool_items: dict[str, dict[str, str]] = {}
        async for event in iter_sse_json(self._transport.stream(self._request(request, stream=True))):
            event_type = str(event.get("type") or "")
            if event_type == "response.created":
                response = event.get("response")
                response = response if isinstance(response, dict) else {}
                identifier = response_id(response.get("id"))
                yield ModelStreamEvent(
                    type=ModelStreamEventType.MESSAGE_START,
                    response_id=identifier,
                )
            elif event_type == "response.output_text.delta":
                delta = str(event.get("delta") or "")
                if delta:
                    yield ModelStreamEvent(
                        type=ModelStreamEventType.TEXT_DELTA,
                        text_delta=delta,
                    )
            elif event_type in {
                "response.reasoning_summary_text.delta",
                "response.reasoning_text.delta",
            }:
                delta = str(event.get("delta") or "")
                if delta:
                    yield ModelStreamEvent(
                        type=ModelStreamEventType.REASONING_DELTA,
                        reasoning_delta=delta,
                    )
            elif event_type == "response.output_item.added":
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "function_call":
                    item_id = str(item.get("id") or item.get("call_id") or len(tool_items))
                    tool_items[item_id] = {
                        "call_id": str(item.get("call_id") or ""),
                        "name": str(item.get("name") or ""),
                        "arguments": str(item.get("arguments") or ""),
                    }
            elif event_type == "response.function_call_arguments.delta":
                item_id = str(event.get("item_id") or event.get("output_index") or "")
                item = tool_items.setdefault(
                    item_id,
                    {"call_id": "", "name": "", "arguments": ""},
                )
                item["arguments"] += str(event.get("delta") or "")
            elif event_type == "response.output_item.done":
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "function_call":
                    item_id = str(item.get("id") or item.get("call_id") or len(tool_items))
                    tool_items[item_id] = {
                        "call_id": str(item.get("call_id") or ""),
                        "name": str(item.get("name") or ""),
                        "arguments": str(item.get("arguments") or "{}"),
                    }
            elif event_type in {"response.completed", "response.incomplete"}:
                response = event.get("response")
                response = response if isinstance(response, dict) else {}
                usage = _usage(response.get("usage"))
                final_reason = _responses_finish(response, bool(tool_items))
                if usage.total_tokens:
                    yield ModelStreamEvent(type=ModelStreamEventType.USAGE, usage=usage)
            elif event_type in {"response.failed", "error"}:
                raise ModelProviderError(
                    ModelErrorCode.UNAVAILABLE,
                    "OpenAI Responses stream failed (provider detail redacted)",
                    provider_id=self.provider_id,
                    retryable=False,
                )
        if identifier is None:
            identifier = response_id(None)
            yield ModelStreamEvent(
                type=ModelStreamEventType.MESSAGE_START,
                response_id=identifier,
            )
        for item in tool_items.values():
            yield ModelStreamEvent(
                type=ModelStreamEventType.TOOL_CALL_DELTA,
                tool_call=ModelToolCall(
                    call_id=response_id(item["call_id"]),
                    name=item["name"],
                    arguments=decode_tool_arguments(item["arguments"], self.provider_id),
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
    input_details = value.get("input_tokens_details")
    output_details = value.get("output_tokens_details")
    return ModelUsage(
        input_tokens=int(value.get("input_tokens") or 0),
        output_tokens=int(value.get("output_tokens") or 0),
        cached_input_tokens=(
            int(input_details.get("cached_tokens") or 0)
            if isinstance(input_details, dict)
            else 0
        ),
        reasoning_tokens=(
            int(output_details.get("reasoning_tokens") or 0)
            if isinstance(output_details, dict)
            else 0
        ),
    )


def _responses_finish(payload: dict[str, Any], has_calls: bool) -> FinishReason:
    status = str(payload.get("status") or "")
    if status == "completed":
        return FinishReason.TOOL_CALLS if has_calls else FinishReason.STOP
    if status == "incomplete":
        return FinishReason.LENGTH
    if status == "cancelled":
        return FinishReason.CANCELLED
    if status == "failed":
        return FinishReason.ERROR
    return FinishReason.UNKNOWN
