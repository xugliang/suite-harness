"""Strict, provider-facing models for the Feishu company channel."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

_EVENT_ID = re.compile(r"^[^\x00]{1,1024}$")


class FeishuEventOutcome(str, Enum):
    """Result of accepting a verified callback or long-connection event."""

    CHALLENGE = "challenge"
    DISPATCHED = "dispatched"
    DUPLICATE = "duplicate"
    IGNORED = "ignored"


class FeishuChannelError(RuntimeError):
    """Safe error at the Feishu protocol boundary."""


class FeishuAuthenticationError(FeishuChannelError):
    """The callback could not be authenticated."""


class FeishuPayloadError(FeishuChannelError):
    """The authenticated payload is malformed or unsupported."""


class FeishuApiError(FeishuChannelError):
    """A Feishu Open Platform API call failed without leaking its response body."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class FeishuWebhookResponse(_WireModel):
    status_code: int = Field(default=200, ge=100, le=599)
    body: dict[str, str] = Field(default_factory=dict)
    outcome: FeishuEventOutcome


class FeishuSendResult(_WireModel):
    message_id: str

    @field_validator("message_id")
    @classmethod
    def validate_message_id(cls, value: str) -> str:
        if not _EVENT_ID.fullmatch(value):
            raise ValueError("invalid Feishu message id")
        return value


@dataclass(frozen=True, slots=True)
class FeishuHttpResponse:
    """Small transport-neutral buffered HTTP response."""

    status_code: int
    body: bytes


__all__ = [
    "FeishuApiError",
    "FeishuAuthenticationError",
    "FeishuChannelError",
    "FeishuEventOutcome",
    "FeishuHttpResponse",
    "FeishuPayloadError",
    "FeishuSendResult",
    "FeishuWebhookResponse",
]
