"""Strict JSON frame vocabulary for the company WebSocket channel."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    field_serializer,
    field_validator,
    model_validator,
)

from suiteharness.channels.approvals import ApprovalChallenge
from suiteharness.channels.models import ChannelAttachment, OutboundEvent

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class WebSocketProtocolError(RuntimeError):
    """Safe protocol error carrying the RFC 6455 close code."""

    def __init__(self, code: str, message: str, *, close_code: int = 1008) -> None:
        super().__init__(message)
        self.code = code
        self.close_code = close_code


class _Frame(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class MessageFrame(_Frame):
    type: Literal["message"]
    client_message_id: str
    conversation_id: str
    text: str = Field(default="", max_length=1_000_000)
    product_id: str | None = None
    attachments: tuple[ChannelAttachment, ...] = ()

    @field_validator("client_message_id")
    @classmethod
    def validate_client_message_id(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("invalid client_message_id")
        return value

    @field_validator("conversation_id")
    @classmethod
    def validate_conversation_id(cls, value: str) -> str:
        if not value or len(value) > 1024 or "\x00" in value:
            raise ValueError("invalid conversation_id")
        return value

    @field_validator("product_id")
    @classmethod
    def validate_product_id(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(
            r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*", value
        ):
            raise ValueError("invalid product_id")
        return value

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("message text must be NUL-free")
        return value

    @model_validator(mode="after")
    def require_content(self) -> MessageFrame:
        if not self.text.strip() and not self.attachments:
            raise ValueError("message requires non-blank text or attachments")
        return self


class ApprovalDecisionFrame(_Frame):
    type: Literal["approval_decision"]
    challenge_id: str
    approved: bool

    @field_validator("challenge_id")
    @classmethod
    def validate_challenge_id(cls, value: str) -> str:
        if not value or len(value) > 256 or "\x00" in value:
            raise ValueError("invalid challenge_id")
        return value


class PingFrame(_Frame):
    type: Literal["ping"]
    nonce: str | None = Field(default=None, max_length=128)

    @field_validator("nonce")
    @classmethod
    def validate_nonce(cls, value: str | None) -> str | None:
        if value is not None and "\x00" in value:
            raise ValueError("invalid ping nonce")
        return value


class StopFrame(_Frame):
    type: Literal["stop"]
    stop_id: str
    target_client_message_id: str
    conversation_id: str
    product_id: str | None = None

    @field_validator("stop_id", "target_client_message_id")
    @classmethod
    def validate_ids(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("invalid control identifier")
        return value

    @field_validator("conversation_id")
    @classmethod
    def validate_conversation_id(cls, value: str) -> str:
        return MessageFrame.validate_conversation_id(value)

    @field_validator("product_id")
    @classmethod
    def validate_product_id(cls, value: str | None) -> str | None:
        return MessageFrame.validate_product_id(value)


class HistoryFrame(_Frame):
    type: Literal["history"]
    history_id: str
    conversation_id: str
    product_id: str | None = None
    limit: int = Field(ge=1, le=1_000)
    before_sequence: int | None = Field(default=None, ge=1)

    @field_validator("history_id")
    @classmethod
    def validate_history_id(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("invalid history_id")
        return value

    @field_validator("conversation_id")
    @classmethod
    def validate_conversation_id(cls, value: str) -> str:
        return MessageFrame.validate_conversation_id(value)

    @field_validator("product_id")
    @classmethod
    def validate_product_id(cls, value: str | None) -> str | None:
        return MessageFrame.validate_product_id(value)


class ContinueFrame(_Frame):
    type: Literal["continue"]
    client_message_id: str
    target_client_message_id: str
    conversation_id: str
    product_id: str | None = None

    @field_validator("client_message_id", "target_client_message_id")
    @classmethod
    def validate_ids(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("invalid control identifier")
        return value

    @field_validator("conversation_id")
    @classmethod
    def validate_conversation_id(cls, value: str) -> str:
        return MessageFrame.validate_conversation_id(value)

    @field_validator("product_id")
    @classmethod
    def validate_product_id(cls, value: str | None) -> str | None:
        return MessageFrame.validate_product_id(value)


ClientFrame = Annotated[
    MessageFrame | ApprovalDecisionFrame | PingFrame | StopFrame | HistoryFrame | ContinueFrame,
    Field(discriminator="type"),
]
_CLIENT_FRAME_ADAPTER: TypeAdapter[ClientFrame] = TypeAdapter(ClientFrame)


class AckFrame(_Frame):
    type: Literal["ack"] = "ack"
    client_message_id: str


class EventFrame(_Frame):
    type: Literal["event"] = "event"
    client_message_id: str
    event: OutboundEvent


class ApprovalRequiredFrame(_Frame):
    type: Literal["approval_required"] = "approval_required"
    challenge: ApprovalChallenge

    @field_serializer("challenge")
    def serialize_challenge(self, value: ApprovalChallenge) -> dict[str, object]:
        # ApprovalChallenge freezes arguments with MappingProxyType; convert at
        # this explicit wire boundary so Pydantic never handles that opaque type.
        return {
            "challenge_id": value.challenge_id,
            "principal_id": value.principal_id,
            "run_id": value.run_id,
            "call_id": value.call_id,
            "tool_name": value.tool_name,
            "tool_identity": value.tool_identity,
            "arguments": dict(value.arguments),
            "expires_at": value.expires_at,
        }


class ApprovalDecisionResultFrame(_Frame):
    type: Literal["approval_decision_result"] = "approval_decision_result"
    challenge_id: str
    accepted: bool


class PongFrame(_Frame):
    type: Literal["pong"] = "pong"
    nonce: str | None = None


class ErrorFrame(_Frame):
    type: Literal["error"] = "error"
    code: str
    message: str
    client_message_id: str | None = None


class StopResultFrame(_Frame):
    type: Literal["stop_result"] = "stop_result"
    stop_id: str
    outcome: Literal["requested", "already_requested", "already_finished", "not_found"]


class HistoryDisplayEvent(_Frame):
    """Deliberately small, display-only projection of a transcript event."""

    kind: Literal["user", "assistant", "cancel"]
    run_id: str | None
    message_id: str | None = None
    occurred_at: datetime
    text: str | None = Field(default=None, max_length=1_000_000)
    output: JsonValue | None = None
    partial_output: str | None = Field(default=None, max_length=16_384)
    incomplete: bool | None = None
    truncated: bool | None = None

    @field_validator("run_id", "message_id")
    @classmethod
    def validate_display_id(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not _ID.fullmatch(value):
            raise ValueError("invalid display identifier")
        return value

    @field_validator("occurred_at")
    @classmethod
    def validate_occurred_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        return value.astimezone(UTC)


class HistoryResultFrame(_Frame):
    type: Literal["history_result"] = "history_result"
    history_id: str
    events: tuple[HistoryDisplayEvent, ...]
    next_cursor: int | None = Field(default=None, ge=1)
    has_more: bool | None = None
    pagination_supported: bool = False
    truncated: bool = False

    @field_serializer("events")
    def serialize_events(self, value: tuple[HistoryDisplayEvent, ...]) -> list[dict[str, object]]:
        serialized: list[dict[str, object]] = []
        for item in value:
            result = item.model_dump(mode="json", exclude_none=True)
            if item.run_id is None:
                result["run_id"] = None
            serialized.append(result)
        return serialized


ServerFrame = (
    AckFrame
    | EventFrame
    | ApprovalRequiredFrame
    | ApprovalDecisionResultFrame
    | PongFrame
    | ErrorFrame
    | StopResultFrame
    | HistoryResultFrame
)


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite WebSocket JSON number")


def parse_client_frame(raw: str) -> ClientFrame:
    try:
        def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
            value: dict[str, object] = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("duplicate WebSocket JSON key")
                value[key] = item
            return value

        payload = json.loads(
            raw,
            object_pairs_hook=object_pairs,
            parse_constant=_reject_json_constant,
        )
        # Re-encode the already duplicate-checked value so strict validation
        # retains JSON semantics (for example, an array is valid for a frozen
        # tuple field) without accepting Python-side coercions.
        checked_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return _CLIENT_FRAME_ADAPTER.validate_json(checked_json, strict=True)
    except (json.JSONDecodeError, RecursionError, UnicodeError, ValueError) as exc:
        raise WebSocketProtocolError("invalid_frame", "invalid WebSocket JSON frame") from exc


class WebSocketPacketKind(str, Enum):
    TEXT = "text"
    BINARY = "binary"
    DISCONNECT = "disconnect"


@dataclass(frozen=True, slots=True)
class WebSocketPacket:
    kind: WebSocketPacketKind
    text: str | None = None
    data: bytes | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.kind is WebSocketPacketKind.TEXT:
            if not isinstance(self.text, str) or self.data is not None:
                raise ValueError("text packet requires only text")
        elif self.kind is WebSocketPacketKind.BINARY:
            if not isinstance(self.data, bytes) or self.text is not None:
                raise ValueError("binary packet requires only bytes")
        elif self.text is not None or self.data is not None:
            raise ValueError("disconnect packet has no payload")


@dataclass(frozen=True, slots=True)
class WebSocketHandshake:
    """Server-observed handshake values; authorization is redacted from repr."""

    origin: str | None
    authorization: str | None = field(default=None, repr=False)
    cookie_header: str | None = field(default=None, repr=False)
    client_host: str | None = None
    requested_subprotocols: tuple[str, ...] = ()


__all__ = [
    "AckFrame",
    "ApprovalDecisionFrame",
    "ApprovalDecisionResultFrame",
    "ApprovalRequiredFrame",
    "ClientFrame",
    "ContinueFrame",
    "ErrorFrame",
    "EventFrame",
    "HistoryDisplayEvent",
    "HistoryFrame",
    "HistoryResultFrame",
    "MessageFrame",
    "PingFrame",
    "PongFrame",
    "ServerFrame",
    "StopFrame",
    "StopResultFrame",
    "WebSocketHandshake",
    "WebSocketPacket",
    "WebSocketPacketKind",
    "WebSocketProtocolError",
    "parse_client_frame",
]
