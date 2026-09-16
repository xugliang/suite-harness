"""Provider-neutral durable session and transcript vocabulary."""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
RESUME_POINT_TTL_SECONDS = 300


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


def memory_invalidation_source(payload: JsonValue) -> tuple[str, int] | None:
    """Validate an optional ordered business-source stamp, independent of session identity."""
    if not isinstance(payload, dict):
        return None
    namespace, version = payload.get('source_namespace'), payload.get('source_version')
    if namespace is None and version is None:
        return None
    if (not isinstance(namespace, str) or not _IDENTIFIER.fullmatch(namespace)
            or type(version) is not int or version < 1):
        raise ValueError('source namespace and positive integer version must be supplied together')
    return namespace, version


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
    # Recovery points contain reusable execution data and form a separate cache
    # from ordinary session snapshots. Budgets include the full UTF-8 envelope;
    # expiration and eviction never remove durable claims or transcript events.
    max_resume_point_bytes: int = Field(default=512 * 1024, ge=1024, le=512 * 1024)
    max_resume_points_per_session: int = Field(default=8, ge=1, le=256)
    max_resume_bytes_per_session: int = Field(default=2 * 1024 * 1024, ge=1024, le=128 * 1024 * 1024)
    max_resume_points_total: int = Field(default=10_000, ge=1, le=1_000_000)
    max_resume_bytes_total: int = Field(default=128 * 1024 * 1024, ge=1024, le=1024 * 1024 * 1024)

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
        if self.max_resume_bytes_per_session < self.max_resume_point_bytes:
            raise ValueError("max_resume_bytes_per_session must cover one resume point")
        if self.max_resume_points_total < self.max_resume_points_per_session:
            raise ValueError("max_resume_points_total must cover the per-session limit")
        if self.max_resume_bytes_total < self.max_resume_bytes_per_session:
            raise ValueError("max_resume_bytes_total must cover the per-session limit")
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


class RunStopOutcome(str, Enum):
    """Result of atomically recording a request to stop one active run."""

    REQUESTED = "requested"
    ALREADY_REQUESTED = "already_requested"
    ALREADY_FINISHED = "already_finished"
    NOT_FOUND = "not_found"


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
    MEMORY_INVALIDATED = "memory_invalidated"


def finished_resume_run_id(event_type: TranscriptEventType, payload: JsonValue) -> str | None:
    """Identify terminal results whose execution state is no longer reusable."""
    if (event_type not in {TranscriptEventType.ASSISTANT_MESSAGE, TranscriptEventType.ERROR}
            or not isinstance(payload, dict) or payload.get("run_status") == "cancelled"):
        return None
    run_id = payload.get("run_id")
    return run_id if isinstance(run_id, str) and _IDENTIFIER.fullmatch(run_id) else None


def validate_transcript_page(limit: int, before_sequence: int | None) -> None:
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("transcript page limit must be between 1 and 1000")
    if before_sequence is not None and (type(before_sequence) is not int or before_sequence < 1):
        raise ValueError("before_sequence must be a positive integer")


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
    memory_invalidated_revision: int = Field(default=0, ge=0)
    memory_source_versions: dict[str, int] = Field(default_factory=dict)
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


class RunResumePoint(_FrozenModel):
    """Trusted, complete execution checkpoint; never a client-supplied resume request."""

    identity: SessionIdentity
    run_id: str
    payload: JsonValue
    memory_invalidated_revision: int = Field(default=0, ge=0)
    memory_source_versions: dict[str, int] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("run_id")
    @classmethod
    def validate_run_id(cls, value: str) -> str:
        return _identifier(value, "run_id")

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        return _utc(value, "created_at")

    @field_validator("memory_source_versions")
    @classmethod
    def validate_source_versions(cls, value: dict[str, int]) -> dict[str, int]:
        for namespace, version in value.items():
            _identifier(namespace, "source_namespace")
            if type(version) is not int or version < 1:
                raise ValueError("source versions must be positive integers")
        return value


def safe_resume_point(point: RunResumePoint) -> RunResumePoint | None:
    """Copy and fail closed on oversized, incomplete or credential-bearing JSON.

    This only checks the durable envelope. The runner still revalidates the
    live registration, effects, permissions, prompt and route before execution.
    """
    forbidden = {
        "resultid", "resultreference", "approvaltoken", "approvalbinding", "approvalcredential",
        "accesstoken", "refreshtoken", "apikey", "authorization", "password", "secret",
        "clientsecret", "credential", "credentials", "grantid", "ownertoken", "privatekey",
        "approval", "token", "cookie",
    }

    def unsafe(value: object) -> bool:
        if isinstance(value, dict):
            return any(
                not isinstance(key, str)
                or re.sub(r"[^a-z0-9]", "", key.lower()) in forbidden
                or unsafe(child) for key, child in value.items()
            )
        if isinstance(value, list):
            return any(unsafe(child) for child in value)
        return type(value) not in (str, int, float, bool, type(None))

    try:
        if not isinstance(point, RunResumePoint) or unsafe(point.payload):
            return None
        encoded = canonical_json(point.payload)
        if len(encoded.encode("utf-8")) > 512 * 1024:
            return None
        # Revalidation handles model_copy/model_construct and nested mutation.
        checked = RunResumePoint.model_validate_json(point.model_dump_json())
        data = checked.payload
        if (not isinstance(data, dict) or not {"message_digest", "input", "conversation", "resume_state"} <= data.keys()
                or not isinstance(data["message_digest"], str)
                or re.fullmatch(r"[0-9a-f]{64}", data["message_digest"]) is None
                or not isinstance(data["conversation"], dict)
                or not isinstance(data["conversation"].get("messages"), list)):
            return None
        state = data["resume_state"]
        required = {"source_run_id", "workflow_state", "observations", "iterations_used",
                    "tool_calls_used", "call_ids", "prompt_digest", "route_key"}
        if (not isinstance(state, dict) or not required <= state.keys()
                or state["source_run_id"] != checked.run_id
                or type(state["iterations_used"]) is not int or not 0 <= state["iterations_used"] <= 10_000
                or type(state["tool_calls_used"]) is not int
                or not isinstance(state["observations"], list) or not isinstance(state["call_ids"], list)
                or not isinstance(state["prompt_digest"], str)
                or re.fullmatch(r"[0-9a-f]{64}", state["prompt_digest"]) is None
                or not isinstance(state["route_key"], str) or not 1 <= len(state["route_key"]) <= 4096):
            return None
        observations, call_ids = state["observations"], state["call_ids"]
        if (not all(isinstance(value, str) and _IDENTIFIER.fullmatch(value) for value in call_ids)
                or len(set(call_ids)) != len(call_ids)
                or state["tool_calls_used"] != len(observations) or len(call_ids) != len(observations)
                or len(observations) > 1024
                or (observations and (state["iterations_used"] == 0 or state["workflow_state"] is None))):
            return None
        observed_ids = []
        for observation in observations:
            if (not isinstance(observation, dict) or observation.get("status") != "succeeded"
                    or observation.get("failure") is not None or "result" not in observation):
                return None
            name = observation.get("tool_name")
            family = ("fs" if name in {f"suiteharness.fs.{part}" for part in ("list", "read", "glob", "grep")}
                      else "web" if name in {"suiteharness.web.search", "suiteharness.web.fetch"} else None)
            if family is None or observation.get("tool_identity") != {
                "namespace": "suiteharness", "name": name,
                "origin": f"suiteharness.builtin.{family}", "version": "1",
            }:
                return None
            start = _utc(datetime.fromisoformat(observation["started_at"]), "started_at")
            finish = _utc(datetime.fromisoformat(observation["finished_at"]), "finished_at")
            if finish < start:
                return None
            observed_ids.append(observation.get("call_id"))
        if sorted(observed_ids) != sorted(call_ids):
            return None
        return checked
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return None


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
