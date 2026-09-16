"""Concurrency-safe in-memory session store for tests and ephemeral servers."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import math
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from pydantic import JsonValue

from suiteharness.sessions.errors import (
    SessionCapacityError,
    SessionIdempotencyConflictError,
    SessionNotFoundError,
    SessionPayloadTooLargeError,
    SessionRevisionConflictError,
)
from suiteharness.sessions.models import (
    RESUME_POINT_TTL_SECONDS,
    RunResumePoint,
    RunStopOutcome,
    SessionCheckpoint,
    SessionIdentity,
    SessionRecord,
    SessionRunClaimState,
    SessionSnapshot,
    SessionStatus,
    SessionStoreLimits,
    TranscriptEvent,
    TranscriptEventType,
    canonical_json,
    finished_resume_run_id,
    memory_invalidation_source,
    safe_resume_point,
    utc_now,
    validate_transcript_page,
)
from suiteharness.sessions.protocols import SessionStore

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_OWNER_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,256}$")


def _digest(value: JsonValue) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class InMemorySessionStore(SessionStore):
    """Reference implementation with atomic append and checkpoint CAS."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = utc_now,
        limits: SessionStoreLimits | None = None,
    ) -> None:
        if limits is not None and not isinstance(limits, SessionStoreLimits):
            raise TypeError("limits must be SessionStoreLimits")
        self._sessions: dict[SessionIdentity, SessionRecord] = {}
        self._events: dict[SessionIdentity, list[TranscriptEvent]] = {}
        self._checkpoints: dict[SessionIdentity, list[SessionCheckpoint]] = {}
        self._event_idempotency: dict[tuple[SessionIdentity, str], tuple[str, TranscriptEvent]] = {}
        self._event_ids: dict[tuple[SessionIdentity, str], TranscriptEvent] = {}
        self._checkpoint_idempotency: dict[
            tuple[SessionIdentity, str], tuple[str, SessionCheckpoint]
        ] = {}
        self._run_claims: dict[
            tuple[SessionIdentity, str], tuple[str, str, datetime]
        ] = {}
        self._run_stops: set[tuple[SessionIdentity, str]] = set()
        self._resume_points: dict[tuple[SessionIdentity, str], RunResumePoint] = {}
        self._resume_point_bytes: dict[tuple[SessionIdentity, str], int] = {}
        self._clock = clock
        self._limits = limits or SessionStoreLimits()
        self._lock = asyncio.Lock()

    async def create(self, identity: SessionIdentity) -> SessionRecord:
        async with self._lock:
            record = self._sessions.get(identity)
            if record is None:
                if len(self._sessions) >= self._limits.max_sessions:
                    raise SessionCapacityError("session store reached max_sessions")
                record = SessionRecord(identity=identity)
                self._sessions[identity] = record
                self._events[identity] = []
                self._checkpoints[identity] = []
            return record.model_copy(deep=True)

    async def get(self, identity: SessionIdentity) -> SessionRecord | None:
        async with self._lock:
            record = self._sessions.get(identity)
            return None if record is None else record.model_copy(deep=True)

    async def claim_run(
        self,
        identity: SessionIdentity,
        *,
        run_id: str,
        owner_token: str,
        lease_seconds: float,
    ) -> SessionRunClaimState:
        self._validate_run_claim(run_id, owner_token, lease_seconds)
        now = self._now()
        async with self._lock:
            self._require(identity)
            key = (identity, run_id)
            prior = self._run_claims.get(key)
            if prior is None:
                per_session = sum(
                    1 for claim_identity, _run_id in self._run_claims if claim_identity == identity
                )
                if per_session >= self._limits.max_run_claims_per_session:
                    raise SessionCapacityError(
                        "session reached max_run_claims_per_session"
                    )
                if len(self._run_claims) >= self._limits.max_run_claims_total:
                    raise SessionCapacityError("session store reached max_run_claims_total")
                self._run_claims[key] = (
                    owner_token,
                    "active",
                    now + timedelta(seconds=lease_seconds),
                )
                return SessionRunClaimState.ACQUIRED
            _, state, expires_at = prior
            if state != "abandoned" and expires_at > now:
                return SessionRunClaimState.ACTIVE
            if state != "abandoned":
                self._run_claims[key] = (prior[0], "abandoned", expires_at)
            return SessionRunClaimState.STALE

    async def finalize_run(
        self,
        identity: SessionIdentity,
        *,
        run_id: str,
        owner_token: str,
        lease_seconds: float,
    ) -> bool:
        self._validate_run_claim(run_id, owner_token, lease_seconds)
        now = self._now()
        async with self._lock:
            self._require(identity)
            key = (identity, run_id)
            prior = self._run_claims.get(key)
            if prior is None or prior[0] != owner_token or prior[1] != "active":
                return False
            # Keep a short finalization fence while terminal transcript and
            # checkpoint rows are persisted. A crash becomes recoverable as
            # uncertain after this deadline, never executable by a new owner.
            self._run_claims[key] = (
                owner_token,
                "finalizing",
                now + timedelta(seconds=lease_seconds),
            )
            return True

    async def request_stop(
        self,
        identity: SessionIdentity,
        *,
        run_id: str,
    ) -> RunStopOutcome:
        self._validate_run_id(run_id)
        async with self._lock:
            now = self._now()
            claim = self._run_claims.get((identity, run_id))
            if claim is None:
                return RunStopOutcome.NOT_FOUND
            if claim[1] != "active" or claim[2] <= now:
                return RunStopOutcome.ALREADY_FINISHED
            key = (identity, run_id)
            if key in self._run_stops:
                return RunStopOutcome.ALREADY_REQUESTED
            self._run_stops.add(key)
            return RunStopOutcome.REQUESTED

    async def stop_requested(
        self,
        identity: SessionIdentity,
        *,
        run_id: str,
    ) -> bool:
        self._validate_run_id(run_id)
        async with self._lock:
            return (identity, run_id) in self._run_stops

    async def active_run_ids(
        self,
        identity: SessionIdentity,
        *,
        limit: int = 4,
    ) -> tuple[str, ...]:
        self._validate_active_run_limit(limit)
        async with self._lock:
            now = self._now()
            return tuple(
                run_id
                for (claim_identity, run_id), (_, state, expires_at) in self._run_claims.items()
                if claim_identity == identity and state == "active" and expires_at > now
            )[:limit]

    async def save_resume_point(
        self, identity: SessionIdentity, point: RunResumePoint,
    ) -> RunResumePoint | None:
        checked = safe_resume_point(point)
        if checked is None or checked.identity != identity:
            return None
        point_bytes = len(checked.model_dump_json().encode("utf-8"))
        if point_bytes > self._limits.max_resume_point_bytes:
            return None
        async with self._lock:
            now = self._now()
            self._expire_resume_points(now)
            key = (identity, checked.run_id)
            claim = self._run_claims.get(key)
            record = self._sessions.get(identity)
            prior = self._resume_points.get(key)
            if (claim is None or claim[1] != "active" or claim[2] <= now
                    or key in self._run_stops or record is None
                    or not 0 <= (now - checked.created_at).total_seconds() < RESUME_POINT_TTL_SECONDS
                    or checked.memory_invalidated_revision != record.memory_invalidated_revision
                    or checked.memory_source_versions != record.memory_source_versions
                    or (prior is not None and checked.created_at < prior.created_at)):
                return None
            self._resume_points[key] = checked
            self._resume_point_bytes[key] = point_bytes
            self._trim_resume_points(identity, key)
            return checked.model_copy(deep=True)

    async def get_resume_point(
        self, identity: SessionIdentity, *, run_id: str,
    ) -> RunResumePoint | None:
        self._validate_run_id(run_id)
        async with self._lock:
            now = self._now()
            self._expire_resume_points(now)
            point = self._resume_points.get((identity, run_id))
            if point is None or not 0 <= (now - point.created_at).total_seconds() < RESUME_POINT_TTL_SECONDS:
                return None
            return point.model_copy(deep=True)

    def _discard_resume_point(self, key: tuple[SessionIdentity, str]) -> None:
        self._resume_points.pop(key, None)
        self._resume_point_bytes.pop(key, None)

    def _expire_resume_points(self, now: datetime) -> None:
        expired = [key for key, point in self._resume_points.items()
                   if (now - point.created_at).total_seconds() >= RESUME_POINT_TTL_SECONDS]
        for key in expired:
            self._discard_resume_point(key)

    def _trim_resume_points(
        self, identity: SessionIdentity, keep: tuple[SessionIdentity, str],
    ) -> None:
        for scope, max_count, max_bytes in (
            (identity, self._limits.max_resume_points_per_session, self._limits.max_resume_bytes_per_session),
            (None, self._limits.max_resume_points_total, self._limits.max_resume_bytes_total),
        ):
            keys = sorted((key for key in self._resume_points if scope is None or key[0] == scope),
                          key=lambda key: self._resume_points[key].created_at)
            count, size = len(keys), sum(self._resume_point_bytes[key] for key in keys)
            for key in keys:
                if count <= max_count and size <= max_bytes:
                    break
                if key != keep:
                    count -= 1
                    size -= self._resume_point_bytes[key]
                    self._discard_resume_point(key)

    async def append(
        self,
        identity: SessionIdentity,
        *,
        event_id: str,
        event_type: TranscriptEventType,
        payload: JsonValue = None,
        idempotency_key: str | None = None,
        expected_revision: int | None = None,
        occurred_at: datetime | None = None,
    ) -> TranscriptEvent:
        event_type = TranscriptEventType(event_type)
        payload_copy = copy.deepcopy(payload)
        source = (memory_invalidation_source(payload_copy)
                  if event_type is TranscriptEventType.MEMORY_INVALIDATED else None)
        payload_bytes = canonical_json(payload_copy).encode("utf-8")
        if len(payload_bytes) > self._limits.max_transcript_payload_bytes:
            raise SessionPayloadTooLargeError(
                "transcript payload exceeds max_transcript_payload_bytes"
            )
        fingerprint = _digest({"type": event_type.value, "payload": payload_copy})
        async with self._lock:
            record = self._require(identity)
            if idempotency_key is not None:
                prior = self._event_idempotency.get((identity, idempotency_key))
                if prior is not None:
                    if prior[0] != fingerprint:
                        raise SessionIdempotencyConflictError(
                            "event idempotency key was reused with different content"
                        )
                    return prior[1].model_copy(deep=True)
            if source is not None and source[1] <= record.memory_source_versions.get(source[0], 0):
                # The comparison and watermark update share this store lock;
                # a delayed older outbox delivery cannot invalidate newer work.
                return next(event.model_copy(deep=True) for event in self._events[identity]
                            if event.revision == record.memory_invalidated_revision)
            if (identity, event_id) in self._event_ids:
                raise SessionIdempotencyConflictError("event_id already exists in this session")
            self._check_revision(record, expected_revision)
            events = self._events[identity]
            if len(events) >= self._limits.max_transcript_events_per_session:
                raise SessionCapacityError(
                    "session reached max_transcript_events_per_session"
                )
            if sum(len(items) for items in self._events.values()) >= (
                self._limits.max_transcript_events_total
            ):
                raise SessionCapacityError(
                    "session store reached max_transcript_events_total"
                )
            revision = record.revision + 1
            event = TranscriptEvent(
                identity=identity,
                event_id=event_id,
                sequence=len(events) + 1,
                revision=revision,
                type=event_type,
                payload=payload_copy,
                occurred_at=occurred_at or utc_now(),
                idempotency_key=idempotency_key,
            )
            events.append(event)
            self._event_ids[(identity, event_id)] = event
            if idempotency_key is not None:
                self._event_idempotency[(identity, idempotency_key)] = (fingerprint, event)
            self._sessions[identity] = record.model_copy(
                update={
                    "revision": revision,
                    "memory_invalidated_revision": (
                        revision if event_type is TranscriptEventType.MEMORY_INVALIDATED
                        else record.memory_invalidated_revision
                    ),
                    "memory_source_versions": (record.memory_source_versions if source is None else
                                               {**record.memory_source_versions, source[0]: source[1]}),
                    "updated_at": max(record.updated_at, event.occurred_at),
                }
            )
            self._expire_resume_points(self._now())
            finished_run = finished_resume_run_id(event_type, payload_copy)
            if finished_run is not None:
                self._discard_resume_point((identity, finished_run))
            return event.model_copy(deep=True)

    async def save_checkpoint(
        self,
        identity: SessionIdentity,
        *,
        checkpoint_id: str,
        workflow_state: JsonValue,
        expected_revision: int,
        idempotency_key: str,
    ) -> SessionCheckpoint:
        state_copy = copy.deepcopy(workflow_state)
        state_bytes = canonical_json(state_copy).encode("utf-8")
        if len(state_bytes) > self._limits.max_checkpoint_state_bytes:
            raise SessionPayloadTooLargeError(
                "checkpoint state exceeds max_checkpoint_state_bytes"
            )
        fingerprint = _digest(state_copy)
        async with self._lock:
            record = self._require(identity)
            prior = self._checkpoint_idempotency.get((identity, idempotency_key))
            if prior is not None:
                if prior[0] != fingerprint:
                    raise SessionIdempotencyConflictError(
                        "checkpoint idempotency key was reused with different state"
                    )
                return prior[1].model_copy(deep=True)
            if any(item.checkpoint_id == checkpoint_id for item in self._checkpoints[identity]):
                raise SessionIdempotencyConflictError(
                    "checkpoint_id already exists in this session"
                )
            self._check_revision(record, expected_revision)
            now = utc_now()
            checkpoint = SessionCheckpoint(
                identity=identity,
                checkpoint_id=checkpoint_id,
                revision=record.revision + 1,
                workflow_state=state_copy,
                last_event_sequence=len(self._events[identity]),
                created_at=now,
                idempotency_key=idempotency_key,
            )
            self._checkpoints[identity].append(checkpoint)
            self._checkpoint_idempotency[(identity, idempotency_key)] = (
                fingerprint,
                checkpoint,
            )
            self._prune_checkpoints(identity)
            self._sessions[identity] = record.model_copy(
                update={"revision": checkpoint.revision, "updated_at": now}
            )
            return checkpoint.model_copy(deep=True)

    def _prune_checkpoints(self, identity: SessionIdentity) -> None:
        checkpoints = self._checkpoints[identity]

        def retained_bytes() -> int:
            return sum(
                len(canonical_json(item.workflow_state).encode("utf-8"))
                for item in checkpoints
            )

        while (
            len(checkpoints) > self._limits.max_checkpoints_per_session
            or retained_bytes() > self._limits.max_checkpoint_bytes_per_session
        ):
            removed = checkpoints.pop(0)
            key = (identity, removed.idempotency_key)
            prior = self._checkpoint_idempotency.get(key)
            if prior is not None and prior[1].checkpoint_id == removed.checkpoint_id:
                self._checkpoint_idempotency.pop(key, None)

    async def set_status(
        self,
        identity: SessionIdentity,
        status: SessionStatus,
        *,
        expected_revision: int,
    ) -> SessionRecord:
        status = SessionStatus(status)
        async with self._lock:
            record = self._require(identity)
            self._check_revision(record, expected_revision)
            updated = record.model_copy(
                update={
                    "status": status,
                    "revision": record.revision + 1,
                    "updated_at": utc_now(),
                }
            )
            self._sessions[identity] = updated
            return updated.model_copy(deep=True)

    async def resume(self, identity: SessionIdentity) -> SessionSnapshot | None:
        async with self._lock:
            record = self._sessions.get(identity)
            if record is None:
                return None
            checkpoints = self._checkpoints[identity]
            checkpoint = checkpoints[-1] if checkpoints else None
            after = checkpoint.last_event_sequence if checkpoint is not None else 0
            transcript = tuple(
                item.model_copy(deep=True)
                for item in self._events[identity]
                if item.sequence > after
            )
            return SessionSnapshot(
                session=record.model_copy(deep=True),
                checkpoint=(None if checkpoint is None else checkpoint.model_copy(deep=True)),
                transcript=transcript,
            )

    async def recent_transcript(
        self,
        identity: SessionIdentity,
        *,
        limit: int,
    ) -> tuple[TranscriptEvent, ...]:
        if limit < 1 or limit > 1000:
            raise ValueError("recent transcript limit must be between 1 and 1000")
        async with self._lock:
            self._require(identity)
            return tuple(
                item.model_copy(deep=True) for item in self._events[identity][-limit:]
            )

    async def transcript_page(
        self, identity: SessionIdentity, *, limit: int, before_sequence: int | None = None,
    ) -> tuple[TranscriptEvent, ...]:
        validate_transcript_page(limit, before_sequence)
        async with self._lock:
            self._require(identity)
            events = self._events[identity]
            # Durable transcript sequences are contiguous and never pruned.
            end = len(events) if before_sequence is None else min(len(events), before_sequence - 1)
            return tuple(item.model_copy(deep=True) for item in events[max(0, end - limit):end])

    def _require(self, identity: SessionIdentity) -> SessionRecord:
        try:
            return self._sessions[identity]
        except KeyError as exc:
            raise SessionNotFoundError("session was not found for this identity") from exc

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("session clock must return a timezone-aware datetime")
        return now.astimezone(UTC)

    @staticmethod
    def _validate_run_claim(
        run_id: str,
        owner_token: str,
        lease_seconds: float,
    ) -> None:
        InMemorySessionStore._validate_run_id(run_id)
        if not isinstance(owner_token, str) or not _OWNER_TOKEN.fullmatch(owner_token):
            raise ValueError("invalid session run owner_token")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int | float)
            or not math.isfinite(lease_seconds)
            or lease_seconds <= 0
            or lease_seconds > 172_800
        ):
            raise ValueError("session run lease_seconds must be in (0, 172800]")

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        if not isinstance(run_id, str) or not _IDENTIFIER.fullmatch(run_id):
            raise ValueError("invalid session run_id")

    @staticmethod
    def _validate_active_run_limit(limit: int) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1_000:
            raise ValueError("active run limit must be between 1 and 1000")

    @staticmethod
    def _check_revision(record: SessionRecord, expected: int | None) -> None:
        if expected is not None and expected != record.revision:
            raise SessionRevisionConflictError(expected, record.revision)
