"""Strict JSON frame vocabulary for the company WebSocket channel."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_serializer,
    field_validator,
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
    text: str = Field(min_length=1, max_length=1_000_000)
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
        if "\x00" in value or not value.strip():
            raise ValueError("message text must be non-blank and NUL-free")
        return value


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


ClientFrame = Annotated[
    MessageFrame | ApprovalDecisionFrame | PingFrame,
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


ServerFrame = (
    AckFrame
    | EventFrame
    | ApprovalRequiredFrame
    | ApprovalDecisionResultFrame
    | PongFrame
    | ErrorFrame
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
    "ErrorFrame",
    "EventFrame",
    "MessageFrame",
    "PingFrame",
    "PongFrame",
    "ServerFrame",
    "WebSocketHandshake",
    "WebSocketPacket",
    "WebSocketPacketKind",
    "WebSocketProtocolError",
    "parse_client_frame",
]
