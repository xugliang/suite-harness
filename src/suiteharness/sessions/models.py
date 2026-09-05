"""Provider-neutral durable session and transcript vocabulary."""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def utc_now() -> datetime:
    return datetime.now(UTC)


def canonical_json(value: JsonValue) -> str:
    """Serialize state deterministically and reject non-JSON floating-point values."""

    def validate(item: object) -> None:
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("JSON values must not contain NaN or infinity")
        if isinstance(item, dict):
            for child in item.values():
                validate(child)
        elif isinstance(item, list):
            for child in item:
                validate(child)

    validate(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _identifier(value: str, label: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def _utc(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SessionStoreLimits(_FrozenModel):
    """Hard capacity limits shared by the in-memory and SQLite stores.

    Transcript events and run claims carry durable idempotency information, so
    silently pruning them would make an old channel message executable again.
    Those collections therefore fail closed at capacity.  Checkpoints are
    replaceable snapshots and retain only a bounded newest window.
    """

    max_sessions: int = Field(default=100_000, ge=1, le=1_000_000)
    max_transcript_payload_bytes: int = Field(
        default=4 * 1024 * 1024,
        ge=1_024,
        le=32 * 1024 * 1024,
    )
    max_transcript_events_per_session: int = Field(
        default=50_000,
        ge=1,
        le=1_000_000,
    )
    max_transcript_events_total: int = Field(
        default=1_000_000,
        ge=1,
        le=10_000_000,
    )
    max_run_claims_per_session: int = Field(
        default=50_000,
        ge=1,
        le=1_000_000,
    )
    max_run_claims_total: int = Field(
        default=1_000_000,
        ge=1,
        le=10_000_000,
    )
    max_checkpoint_state_bytes: int = Field(
        default=4 * 1024 * 1024,
        ge=1_024,
        le=32 * 1024 * 1024,
    )
    max_checkpoints_per_session: int = Field(default=8, ge=1, le=256)
    max_checkpoint_bytes_per_session: int = Field(
        default=16 * 1024 * 1024,
        ge=1_024,
        le=128 * 1024 * 1024,
    )

    @model_validator(mode="after")
    def internally_consistent(self) -> SessionStoreLimits:
        if self.max_transcript_events_total < self.max_transcript_events_per_session:
            raise ValueError(
                "max_transcript_events_total must cover the per-session limit"
            )
        if self.max_run_claims_total < self.max_run_claims_per_session:
            raise ValueError("max_run_claims_total must cover the per-session limit")
        if self.max_checkpoint_bytes_per_session < self.max_checkpoint_state_bytes:
            raise ValueError(
                "max_checkpoint_bytes_per_session must cover one checkpoint state"
            )
        return self


class SessionStatus(str, Enum):
    ACTIVE = "active"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SessionRunClaimState(str, Enum):
    """Outcome of an atomic durable attempt to own one logical run."""

    ACQUIRED = "acquired"
    ACTIVE = "active"
    STALE = "stale"


class TranscriptEventType(str, Enum):
    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_INTENT = "tool_intent"
    TOOL_RESULT = "tool_result"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    STATUS = "status"
    ERROR = "error"
    CANCEL = "cancel"


class SessionIdentity(_FrozenModel):
    """Complete isolation key; it intentionally contains no deployment generation."""

    tenant_id: str
    product_id: str
    agent_id: str
    session_id: str
    principal_id: str

    @field_validator("tenant_id", "product_id", "agent_id", "session_id", "principal_id")
    @classmethod
    def validate_ids(cls, value: str, info: object) -> str:
        return _identifier(value, getattr(info, "field_name", "identifier"))


class SessionRecord(_FrozenModel):
    identity: SessionIdentity
    status: SessionStatus = SessionStatus.ACTIVE
    revision: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at", "updated_at")
    @classmethod
    def validate_times(cls, value: datetime, info: object) -> datetime:
        return _utc(value, getattr(info, "field_name", "timestamp"))

    @model_validator(mode="after")
    def valid_window(self) -> SessionRecord:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        return self


class TranscriptEvent(_FrozenModel):
    identity: SessionIdentity
    event_id: str
    sequence: int = Field(ge=1)
    revision: int = Field(ge=1)
    type: TranscriptEventType
    payload: JsonValue = None
    occurred_at: datetime = Field(default_factory=utc_now)
    idempotency_key: str | None = None

    @field_validator("event_id")
    @classmethod
    def validate_event_id(cls, value: str) -> str:
        return _identifier(value, "event_id")

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, value: str | None) -> str | None:
        return None if value is None else _identifier(value, "idempotency_key")

    @field_validator("payload")
    @classmethod
    def validate_payload(cls, value: JsonValue) -> JsonValue:
        canonical_json(value)
        return value

    @field_validator("occurred_at")
    @classmethod
    def validate_occurred_at(cls, value: datetime) -> datetime:
        return _utc(value, "occurred_at")


class SessionCheckpoint(_FrozenModel):
    identity: SessionIdentity
    checkpoint_id: str
    revision: int = Field(ge=1)
    workflow_state: JsonValue = None
    last_event_sequence: int = Field(ge=0)
    created_at: datetime = Field(default_factory=utc_now)
    idempotency_key: str

    @field_validator("checkpoint_id", "idempotency_key")
    @classmethod
    def validate_ids(cls, value: str, info: object) -> str:
        return _identifier(value, getattr(info, "field_name", "identifier"))

    @field_validator("workflow_state")
    @classmethod
    def validate_state(cls, value: JsonValue) -> JsonValue:
        canonical_json(value)
        return value

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        return _utc(value, "created_at")


class SessionSnapshot(_FrozenModel):
    """Resume view: latest checkpoint plus events produced after that checkpoint."""

    session: SessionRecord
    checkpoint: SessionCheckpoint | None = None
    transcript: tuple[TranscriptEvent, ...] = ()
