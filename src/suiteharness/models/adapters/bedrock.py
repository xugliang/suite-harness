"""AWS Bedrock Converse adapter with an injectable server-IAM client."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from typing import Any, Protocol

from pydantic import SecretStr

from suiteharness.models.adapters._common import finish_reason, response_id
from suiteharness.models.errors import ModelErrorCode, ModelProviderError
from suiteharness.models.transport import HttpTransport
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


class BedrockRuntimeClient(Protocol):
    async def converse(self, *, model_id: str, request: dict[str, Any]) -> dict[str, Any]: ...

    def converse_stream(
        self,
        *,
        model_id: str,
        request: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]: ...


class Boto3BedrockRuntimeClient:
    """Optional boto3 bridge using instance/task roles or server credentials."""

    def __init__(
        self,
        *,
        region_name: str,
        endpoint_url: str | None = None,
        aws_access_key_id: SecretStr | None = None,
        aws_secret_access_key: SecretStr | None = None,
        aws_session_token: SecretStr | None = None,
        client: object | None = None,
    ) -> None:
        if (aws_access_key_id is None) != (aws_secret_access_key is None):
            raise ModelProviderError(
                ModelErrorCode.CONFIGURATION,
                "Bedrock static access key id and secret must be configured together",
                provider_id="bedrock",
            )
        if client is None:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover - deployment diagnostic
                raise ModelProviderError(
                    ModelErrorCode.CONFIGURATION,
                    "AWS Bedrock support requires boto3 or an injected BedrockRuntimeClient",
                    provider_id="bedrock",
                ) from exc
            options: dict[str, Any] = {
                "region_name": region_name,
                "endpoint_url": endpoint_url,
            }
            if aws_access_key_id is not None and aws_secret_access_key is not None:
                options["aws_access_key_id"] = aws_access_key_id.get_secret_value()
                options["aws_secret_access_key"] = aws_secret_access_key.get_secret_value()
            if aws_session_token is not None:
                options["aws_session_token"] = aws_session_token.get_secret_value()
            client = boto3.client("bedrock-runtime", **options)
        self._client = client

    async def converse(self, *, model_id: str, request: dict[str, Any]) -> dict[str, Any]:
        try:
            value = await asyncio.to_thread(
                self._client.converse,  # type: ignore[attr-defined]
                modelId=model_id,
                **request,
            )
        except Exception as exc:
            raise _bedrock_error(exc) from exc
        if not isinstance(value, dict):
            raise ModelProviderError(
                ModelErrorCode.RESPONSE_INVALID,
                "Bedrock Converse response must be an object",
                provider_id="bedrock",
            )
        return value

    async def _stream(
        self,
        *,
        model_id: str,
        request: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        try:
            response = await asyncio.to_thread(
                self._client.converse_stream,  # type: ignore[attr-defined]
                modelId=model_id,
                **request,
            )
            raw_stream = response.get("stream") if isinstance(response, dict) else None
            if raw_stream is None:
                raise ModelProviderError(
                    ModelErrorCode.RESPONSE_INVALID,
                    "Bedrock streaming response has no event stream",
                    provider_id="bedrock",
                )
            iterator = iter(raw_stream)
            sentinel = object()
            while True:
                event = await asyncio.to_thread(next, iterator, sentinel)
                if event is sentinel:
                    break
                if isinstance(event, dict):
                    yield event
        except ModelProviderError:
            raise
        except Exception as exc:
            raise _bedrock_error(exc) from exc

    def converse_stream(
        self,
        *,
        model_id: str,
        request: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        return self._stream(model_id=model_id, request=request)


class BedrockConverseAdapter:
    def __init__(
        self,
        descriptor: ProviderDescriptor,
        profile: ModelProfile,
        client: BedrockRuntimeClient,
    ) -> None:
        self.provider_id = descriptor.provider_id
        self._profile = profile
        self._client = client

    @staticmethod
    def _document_name(value: str) -> str:
        clean = "".join(char if char.isalnum() or char in " -_()[]" else "_" for char in value)
        return (clean.strip() or "document")[:200]

    def _parts(self, message: ModelMessage) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = []
        for part in message.content:
            if isinstance(part, TextContent):
                parts.append({"text": part.text})
            elif isinstance(part, ImageContent):
                if part.data_base64 is None:
                    raise ModelProviderError(
                        ModelErrorCode.UNSUPPORTED_FEATURE,
                        "Bedrock image input must use inline base64 data",
                        provider_id=self.provider_id,
                    )
                image_format = part.mime_type.rsplit("/", 1)[-1].replace("jpg", "jpeg")
                parts.append(
                    {
                        "image": {
                            "format": image_format,
                            "source": {"bytes": base64.b64decode(part.data_base64)},
                        }
                    }
                )
            elif isinstance(part, DocumentContent):
                document_format = part.mime_type.rsplit("/", 1)[-1]
                parts.append(
                    {
                        "document": {
                            "format": document_format,
                            "name": self._document_name(part.name),
                            "source": {"bytes": base64.b64decode(part.data_base64)},
                        }
                    }
                )
        if message.role is ModelRole.TOOL:
            text = "\n".join(
                part.text for part in message.content if isinstance(part, TextContent)
            )
            return [
                {
                    "toolResult": {
                        "toolUseId": message.tool_call_id,
                        "content": [{"text": text}],
                        "status": "success",
                    }
                }
            ]
        parts.extend(
            {
                "toolUse": {
                    "toolUseId": call.call_id,
                    "name": call.name,
                    "input": call.arguments,
                }
            }
            for call in message.tool_calls
        )
        return parts

    def _body(self, request: ModelRequest) -> dict[str, Any]:
        system: list[dict[str, str]] = []
        messages: list[dict[str, Any]] = []
        for message in request.messages:
            if message.role is ModelRole.SYSTEM:
                system.extend(
                    {"text": part.text}
                    for part in message.content
                    if isinstance(part, TextContent)
                )
                continue
            role = "assistant" if message.role is ModelRole.ASSISTANT else "user"
            messages.append({"role": role, "content": self._parts(message)})
        inference: dict[str, Any] = {"maxTokens": request.max_output_tokens}
        if request.temperature is not None:
            inference["temperature"] = request.temperature
        if request.top_p is not None:
            inference["topP"] = request.top_p
        if request.stop:
            inference["stopSequences"] = list(request.stop)
        body: dict[str, Any] = {"messages": messages, "inferenceConfig": inference}
        if system:
            body["system"] = system
        if request.tools:
            body["toolConfig"] = {
                "tools": [
                    {
                        "toolSpec": {
                            "name": tool.name,
                            "description": tool.description,
                            "inputSchema": {"json": tool.input_schema},
                        }
                    }
                    for tool in request.tools
                ],
                "toolChoice": {"auto": {}}
                if request.tool_choice == "auto"
                else ({"any": {}} if request.tool_choice == "required" else None),
            }
            if request.tool_choice == "none":
                body.pop("toolConfig", None)
        if request.response_schema is not None:
            raise ModelProviderError(
                ModelErrorCode.UNSUPPORTED_FEATURE,
                "portable JSON Schema output is not supported by Bedrock Converse",
                provider_id=self.provider_id,
            )
        return body

    async def complete(self, request: ModelRequest) -> ModelResponse:
        model = request.model or self._profile.model
        payload = await self._client.converse(model_id=model, request=self._body(request))
        output = payload.get("output")
        message = output.get("message") if isinstance(output, dict) else None
        blocks = message.get("content") if isinstance(message, dict) else None
        if not isinstance(blocks, list):
            raise ModelProviderError(
                ModelErrorCode.RESPONSE_INVALID,
                "Bedrock Converse response has no message content",
                provider_id=self.provider_id,
            )
        text: list[TextContent] = []
        calls: list[ModelToolCall] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if isinstance(block.get("text"), str):
                text.append(TextContent(text=block["text"]))
            tool = block.get("toolUse")
            if isinstance(tool, dict):
                arguments = tool.get("input") or {}
                if not isinstance(arguments, dict):
                    raise ModelProviderError(
                        ModelErrorCode.RESPONSE_INVALID,
                        "Bedrock tool input must be an object",
                        provider_id=self.provider_id,
                    )
                calls.append(
                    ModelToolCall(
                        call_id=response_id(tool.get("toolUseId")),
                        name=str(tool.get("name") or ""),
                        arguments=arguments,
                    )
                )
        return ModelResponse(
            response_id=response_id(payload.get("ResponseMetadata", {}).get("RequestId")),
            provider_id=self.provider_id,
            model=model,
            content=tuple(text),
            tool_calls=tuple(calls),
            finish_reason=finish_reason(payload.get("stopReason")),
            usage=_usage(payload.get("usage")),
        )

    async def _stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        model = request.model or self._profile.model
        identifier = response_id(None)
        yield ModelStreamEvent(
            type=ModelStreamEventType.MESSAGE_START,
            response_id=identifier,
        )
        reason = FinishReason.UNKNOWN
        usage = ModelUsage()
        tools: dict[int, dict[str, str]] = {}
        async for event in self._client.converse_stream(
            model_id=model,
            request=self._body(request),
        ):
            start = event.get("contentBlockStart")
            if isinstance(start, dict):
                index = int(start.get("contentBlockIndex") or 0)
                raw = start.get("start")
                tool = raw.get("toolUse") if isinstance(raw, dict) else None
                if isinstance(tool, dict):
                    tools[index] = {
                        "id": str(tool.get("toolUseId") or ""),
                        "name": str(tool.get("name") or ""),
                        "arguments": "",
                    }
            delta_event = event.get("contentBlockDelta")
            if isinstance(delta_event, dict):
                index = int(delta_event.get("contentBlockIndex") or 0)
                delta = delta_event.get("delta")
                if isinstance(delta, dict) and isinstance(delta.get("text"), str):
                    yield ModelStreamEvent(
                        type=ModelStreamEventType.TEXT_DELTA,
                        text_delta=delta["text"],
                    )
                reasoning = delta.get("reasoningContent") if isinstance(delta, dict) else None
                if isinstance(reasoning, dict) and isinstance(reasoning.get("text"), str):
                    yield ModelStreamEvent(
                        type=ModelStreamEventType.REASONING_DELTA,
                        reasoning_delta=reasoning["text"],
                    )
                tool_delta = delta.get("toolUse") if isinstance(delta, dict) else None
                if isinstance(tool_delta, dict) and index in tools:
                    tools[index]["arguments"] += str(tool_delta.get("input") or "")
            stop = event.get("messageStop")
            if isinstance(stop, dict):
                reason = finish_reason(stop.get("stopReason"))
            metadata = event.get("metadata")
            if isinstance(metadata, dict) and isinstance(metadata.get("usage"), dict):
                usage = _usage(metadata["usage"])
                yield ModelStreamEvent(type=ModelStreamEventType.USAGE, usage=usage)
        for index in sorted(tools):
            item = tools[index]
            try:
                arguments = json.loads(item["arguments"] or "{}")
            except json.JSONDecodeError as exc:
                raise ModelProviderError(
                    ModelErrorCode.RESPONSE_INVALID,
                    "Bedrock streamed tool input is invalid JSON",
                    provider_id=self.provider_id,
                ) from exc
            if not isinstance(arguments, dict):
                raise ModelProviderError(
                    ModelErrorCode.RESPONSE_INVALID,
                    "Bedrock streamed tool input must be an object",
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
            finish_reason=reason,
            usage=usage,
        )

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        return self._stream(request)


def _usage(value: Any) -> ModelUsage:
    if not isinstance(value, dict):
        return ModelUsage()
    return ModelUsage(
        input_tokens=int(value.get("inputTokens") or 0),
        output_tokens=int(value.get("outputTokens") or 0),
        cached_input_tokens=int(value.get("cacheReadInputTokens") or 0),
    )


def _bedrock_error(error: Exception) -> ModelProviderError:
    name = type(error).__name__.lower()
    if "throttl" in name:
        code, retryable = ModelErrorCode.RATE_LIMIT, True
    elif "timeout" in name:
        code, retryable = ModelErrorCode.TIMEOUT, True
    elif "access" in name or "auth" in name or "credential" in name:
        code, retryable = ModelErrorCode.AUTHENTICATION, False
    elif "validation" in name:
        code, retryable = ModelErrorCode.INVALID_REQUEST, False
    else:
        code, retryable = ModelErrorCode.UNAVAILABLE, True
    return ModelProviderError(
        code,
        f"Bedrock request failed: {type(error).__name__}",
        provider_id="bedrock",
        retryable=retryable,
    )


def bedrock_factory(client: BedrockRuntimeClient):
    """Return a registry factory bound to a deployment-owned Bedrock client."""

    def create(
        descriptor: ProviderDescriptor,
        profile: ModelProfile,
        transport: HttpTransport,
        credential: SecretStr | None,
    ) -> BedrockConverseAdapter:
        del transport, credential
        return BedrockConverseAdapter(descriptor, profile, client)

    return create
