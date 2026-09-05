"""Shared, non-public adapter helpers."""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any

from pydantic import JsonValue

from suiteharness.models.errors import ModelErrorCode, ModelProviderError
from suiteharness.models.transport import HttpResponse, http_status_error
from suiteharness.models.types import (
    DocumentContent,
    FinishReason,
    ImageContent,
    ModelMessage,
    ModelRole,
    TextContent,
)


def response_object(response: HttpResponse, provider_id: str) -> dict[str, Any]:
    if response.status_code >= 400:
        raise http_status_error(
            response.status_code,
            body=response.body,
            provider_id=provider_id,
        )
    value = response.json()
    if not isinstance(value, dict):
        raise ModelProviderError(
            ModelErrorCode.RESPONSE_INVALID,
            "model provider response must be a JSON object",
            provider_id=provider_id,
        )
    return value


def text_from_message(message: ModelMessage) -> str:
    values: list[str] = []
    for part in message.content:
        if isinstance(part, TextContent):
            values.append(part.text)
        else:
            raise ModelProviderError(
                ModelErrorCode.UNSUPPORTED_FEATURE,
                "this provider message position only supports text",
            )
    return "\n".join(values)


def openai_content(message: ModelMessage) -> str | list[dict[str, Any]]:
    if all(isinstance(item, TextContent) for item in message.content):
        return "\n".join(item.text for item in message.content if isinstance(item, TextContent))
    content: list[dict[str, Any]] = []
    for part in message.content:
        if isinstance(part, TextContent):
            content.append({"type": "text", "text": part.text})
        elif isinstance(part, ImageContent):
            url = part.url
            if url is None:
                url = f"data:{part.mime_type};base64,{part.data_base64}"
            content.append({"type": "image_url", "image_url": {"url": url}})
        elif isinstance(part, DocumentContent):
            raise ModelProviderError(
                ModelErrorCode.UNSUPPORTED_FEATURE,
                "OpenAI-compatible chat messages do not have a portable document block",
            )
    return content


def anthropic_content(message: ModelMessage) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    for part in message.content:
        if isinstance(part, TextContent):
            content.append({"type": "text", "text": part.text})
        elif isinstance(part, ImageContent):
            if part.data_base64 is None:
                content.append({"type": "image", "source": {"type": "url", "url": part.url}})
            else:
                content.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": part.mime_type,
                            "data": part.data_base64,
                        },
                    }
                )
        elif isinstance(part, DocumentContent):
            content.append(
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": part.mime_type,
                        "data": part.data_base64,
                    },
                    "title": part.name,
                }
            )
    return content


def decode_tool_arguments(value: Any, provider_id: str) -> dict[str, JsonValue]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        raise ModelProviderError(
            ModelErrorCode.RESPONSE_INVALID,
            "model tool arguments must be a JSON object",
            provider_id=provider_id,
        )
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError as exc:
        raise ModelProviderError(
            ModelErrorCode.RESPONSE_INVALID,
            "model returned invalid tool argument JSON",
            provider_id=provider_id,
        ) from exc
    if not isinstance(parsed, dict):
        raise ModelProviderError(
            ModelErrorCode.RESPONSE_INVALID,
            "model tool arguments must decode to an object",
            provider_id=provider_id,
        )
    return parsed


def finish_reason(value: Any) -> FinishReason:
    mapping = {
        "stop": FinishReason.STOP,
        "end_turn": FinishReason.STOP,
        "tool_calls": FinishReason.TOOL_CALLS,
        "tool_use": FinishReason.TOOL_CALLS,
        "length": FinishReason.LENGTH,
        "max_tokens": FinishReason.LENGTH,
        "content_filter": FinishReason.CONTENT_FILTER,
        "safety": FinishReason.CONTENT_FILTER,
    }
    return mapping.get(str(value or "").lower(), FinishReason.UNKNOWN)


def response_id(value: Any) -> str:
    candidate = str(value or "").strip()
    if candidate:
        # Provider ids occasionally contain characters outside our stable public id grammar.
        safe = "".join(char if char.isalnum() or char in "._:/-" else "_" for char in candidate)
        if not safe or not safe[0].isalnum():
            safe = f"response-{safe}"
        return safe[:192]
    return f"response-{uuid.uuid4().hex}"


def data_url_bytes(value: str) -> tuple[str, str]:
    """Return mime/base64 from a data URL, useful for native providers."""

    prefix, marker, payload = value.partition(",")
    if not marker or not prefix.startswith("data:") or ";base64" not in prefix:
        raise ModelProviderError(ModelErrorCode.INVALID_REQUEST, "image data URL must be base64")
    mime = prefix[5:].split(";", 1)[0]
    try:
        base64.b64decode(payload, validate=True)
    except ValueError as exc:
        raise ModelProviderError(ModelErrorCode.INVALID_REQUEST, "invalid image base64") from exc
    return mime, payload


def require_user_or_assistant(message: ModelMessage) -> None:
    if message.role not in {ModelRole.USER, ModelRole.ASSISTANT}:
        raise ModelProviderError(
            ModelErrorCode.UNSUPPORTED_FEATURE,
            f"unsupported message role in native provider: {message.role.value}",
        )
