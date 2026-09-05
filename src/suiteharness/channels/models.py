"""Provider-neutral messages at the authenticated company-channel boundary."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ChannelKind(str, Enum):
    WEB = "web"
    FEISHU = "feishu"


class OutboundEventKind(str, Enum):
    STARTED = "started"
    DELTA = "delta"
    APPROVAL_REQUIRED = "approval_required"
    COMPLETED = "completed"
    FAILED = "failed"


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ChannelAttachment(_WireModel):
    attachment_id: str
    media_type: str
    name: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)

    @field_validator("attachment_id")
    @classmethod
    def validate_attachment_id(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("invalid attachment_id")
        return value

    @field_validator("media_type")
    @classmethod
    def validate_media_type(cls, value: str) -> str:
        if "/" not in value or len(value) > 255 or any(char.isspace() for char in value):
            raise ValueError("invalid media_type")
        return value.lower()


class InboundMessage(_WireModel):
    """Verified but not yet company-authenticated inbound message."""

    channel: ChannelKind
    event_id: str
    message_id: str
    conversation_id: str
    sender_external_id: str
    text: str = Field(max_length=1_000_000)
    product_id: str | None = None
    attachments: tuple[ChannelAttachment, ...] = ()
    received_at: datetime
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("event_id", "message_id", "conversation_id", "sender_external_id")
    @classmethod
    def validate_external_ids(cls, value: str) -> str:
        if not value or len(value) > 1024 or "\x00" in value:
            raise ValueError("external channel identifiers must be bounded NUL-free strings")
        return value

    @field_validator("product_id")
    @classmethod
    def validate_product_id(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*", value):
            raise ValueError("invalid product_id")
        return value

    @field_validator("received_at")
    @classmethod
    def validate_received_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("received_at must be timezone-aware")
        return value.astimezone(UTC)


class AuthenticatedPrincipal(_WireModel):
    tenant_id: str
    principal_id: str
    roles: frozenset[str] = frozenset()

    @field_validator("tenant_id", "principal_id")
    @classmethod
    def validate_ids(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("invalid authenticated identifier")
        return value

    @field_validator("roles")
    @classmethod
    def validate_roles(cls, value: frozenset[str]) -> frozenset[str]:
        if any(not _IDENTIFIER.fullmatch(role) for role in value):
            raise ValueError("invalid authenticated role")
        return value


class OutboundEvent(_WireModel):
    kind: OutboundEventKind
    request_id: str
    correlation_id: str
    payload: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("request_id", "correlation_id")
    @classmethod
    def validate_ids(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("invalid outbound identifier")
        return value


__all__ = [
    "AuthenticatedPrincipal",
    "ChannelAttachment",
    "ChannelKind",
    "InboundMessage",
    "OutboundEvent",
    "OutboundEventKind",
]
