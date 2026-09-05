"""SQLite reference store for restart-safe single-server deployments."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import JsonValue

from suiteharness.sessions.errors import (
    SessionCapacityError,
    SessionIdempotencyConflictError,
    SessionNotFoundError,
    SessionPayloadTooLargeError,
    SessionRevisionConflictError,
)
from suiteharness.sessions.models import (
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
    utc_now,
)

_KEY_COLUMNS = "tenant_id, product_id, agent_id, session_id, principal_id"
_KEY_PREDICATE = "tenant_id=? AND product_id=? AND agent_id=? AND session_id=? AND principal_id=?"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_OWNER_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,256}$")


def _digest(value: JsonValue) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _key(identity: SessionIdentity) -> tuple[str, str, str, str, str]:
    return (
        identity.tenant_id,
        identity.product_id,
        identity.agent_id,
        identity.session_id,
        identity.principal_id,
    )


class SQLiteSessionStore:
    """Transactional store with durable idempotency and optimistic concurrency."""

    def __init__(
        self,
        database: str | Path,
        *,
        clock: Callable[[], datetime] = utc_now,
        limits: SessionStoreLimits | None = None,
    ) -> None:
        if limits is not None and not isinstance(limits, SessionStoreLimits):
            raise TypeError("limits must be SessionStoreLimits")
        database_text = str(database)
        path: Path | None = None
        if database_text != ":memory:":
            path = Path(database_text).expanduser()
            if not path.is_absolute():
                raise ValueError("SQLite session database path must be absolute")
            path = path.resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            database_text = str(path)
        created = path is not None and not path.exists()
        self._connection = sqlite3.connect(
            database_text,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        if created and os.name != "nt":
            path.chmod(0o600)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=30000")
        if database_text != ":memory:":
            self._connection.execute("PRAGMA journal_mode=WAL")
        self._lock = asyncio.Lock()
        self._closed = False
        self._clock = clock
        self._limits = limits or SessionStoreLimits()
        self._initialize()

    def _initialize(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                tenant_id TEXT NOT NULL,
                product_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                principal_id TEXT NOT NULL,
                status TEXT NOT NULL,
                revision INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, product_id, agent_id, session_id, principal_id)
            );
            CREATE TABLE IF NOT EXISTS transcript_events (
                tenant_id TEXT NOT NULL,
                product_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                principal_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                revision INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_digest TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                idempotency_key TEXT,
                PRIMARY KEY (
                    tenant_id, product_id, agent_id, session_id, principal_id, sequence
                ),
                UNIQUE (
                    tenant_id, product_id, agent_id, session_id, principal_id, event_id
                ),
                UNIQUE (
                    tenant_id, product_id, agent_id, session_id, principal_id,
                    idempotency_key
                )
            );
            CREATE TABLE IF NOT EXISTS session_checkpoints (
                tenant_id TEXT NOT NULL,
                product_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                principal_id TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                workflow_state_json TEXT NOT NULL,
                state_digest TEXT NOT NULL,
                last_event_sequence INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                PRIMARY KEY (
                    tenant_id, product_id, agent_id, session_id, principal_id, revision
                ),
                UNIQUE (
                    tenant_id, product_id, agent_id, session_id, principal_id,
                    checkpoint_id
                ),
                UNIQUE (
                    tenant_id, product_id, agent_id, session_id, principal_id,
                    idempotency_key
                )
            );
            CREATE TABLE IF NOT EXISTS session_run_claims (
                tenant_id TEXT NOT NULL,
                product_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                principal_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                owner_token TEXT NOT NULL,
                claim_state TEXT NOT NULL CHECK(
                    claim_state IN ('active', 'finalizing', 'abandoned')
                ),
                claimed_at_us INTEGER NOT NULL,
                expires_at_us INTEGER NOT NULL,
                PRIMARY KEY (
                    tenant_id, product_id, agent_id, session_id, principal_id, run_id
                ),
                FOREIGN KEY (
                    tenant_id, product_id, agent_id, session_id, principal_id
                ) REFERENCES sessions(
                    tenant_id, product_id, agent_id, session_id, principal_id
                ) ON DELETE CASCADE
            );
            """
        )

    async def create(self, identity: SessionIdentity) -> SessionRecord:
        async with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._connection.execute(
                    f"SELECT 1 FROM sessions WHERE {_KEY_PREDICATE}", _key(identity)
                ).fetchone()
                if existing is None:
                    count = int(
                        self._connection.execute(
                            "SELECT COUNT(*) AS count FROM sessions"
                        ).fetchone()["count"]
                    )
                    if count >= self._limits.max_sessions:
                        raise SessionCapacityError("session store reached max_sessions")
                    now = self._now().isoformat()
                    self._connection.execute(
                        f"INSERT INTO sessions ({_KEY_COLUMNS}, status, revision, "
                        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
                        (*_key(identity), SessionStatus.ACTIVE.value, now, now),
                    )
                record = self._require(identity)
                self._connection.commit()
                return record
            except BaseException:
                self._connection.rollback()
                raise

    async def get(self, identity: SessionIdentity) -> SessionRecord | None:
        async with self._lock:
            row = self._connection.execute(
                f"SELECT * FROM sessions WHERE {_KEY_PREDICATE}", _key(identity)
            ).fetchone()
            return None if row is None else self._record(identity, row)

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
        now_us = self._epoch_us(now)
        expires_at_us = self._epoch_us(now + timedelta(seconds=lease_seconds))
        async with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._require(identity)
                prior = self._connection.execute(
                    f"SELECT owner_token, claim_state, expires_at_us "
                    f"FROM session_run_claims WHERE {_KEY_PREDICATE} AND run_id=?",
                    (*_key(identity), run_id),
                ).fetchone()
                if prior is None:
                    per_session = int(
                        self._connection.execute(
                            f"SELECT COUNT(*) AS count FROM session_run_claims "
                            f"WHERE {_KEY_PREDICATE}",
                            _key(identity),
                        ).fetchone()["count"]
                    )
                    if per_session >= self._limits.max_run_claims_per_session:
                        raise SessionCapacityError(
                            "session reached max_run_claims_per_session"
                        )
                    total = int(
                        self._connection.execute(
                            "SELECT COUNT(*) AS count FROM session_run_claims"
                        ).fetchone()["count"]
                    )
                    if total >= self._limits.max_run_claims_total:
                        raise SessionCapacityError(
                            "session store reached max_run_claims_total"
                        )
                    self._connection.execute(
                        f"INSERT INTO session_run_claims ({_KEY_COLUMNS}, run_id, "
                        "owner_token, claim_state, claimed_at_us, expires_at_us) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                        (*_key(identity), run_id, owner_token, now_us, expires_at_us),
                    )
                    self._connection.commit()
                    return SessionRunClaimState.ACQUIRED
                state = str(prior["claim_state"])
                if state != "abandoned" and int(prior["expires_at_us"]) > now_us:
                    self._connection.commit()
                    return SessionRunClaimState.ACTIVE
                if state != "abandoned":
                    self._connection.execute(
                        f"UPDATE session_run_claims SET claim_state='abandoned' "
                        f"WHERE {_KEY_PREDICATE} AND run_id=?",
                        (*_key(identity), run_id),
                    )
                self._connection.commit()
                return SessionRunClaimState.STALE
            except BaseException:
                self._connection.rollback()
                raise

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
        expires_at_us = self._epoch_us(now + timedelta(seconds=lease_seconds))
        async with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._require(identity)
                cursor = self._connection.execute(
                    f"UPDATE session_run_claims "
                    "SET claim_state='finalizing', expires_at_us=? "
                    f"WHERE {_KEY_PREDICATE} AND run_id=? AND owner_token=? "
                    "AND claim_state='active'",
                    (expires_at_us, *_key(identity), run_id, owner_token),
                )
                self._connection.commit()
                return cursor.rowcount == 1
            except BaseException:
                self._connection.rollback()
                raise

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
        payload_json = canonical_json(payload)
        if len(payload_json.encode("utf-8")) > self._limits.max_transcript_payload_bytes:
            raise SessionPayloadTooLargeError(
                "transcript payload exceeds max_transcript_payload_bytes"
            )
        fingerprint = _digest({"type": event_type.value, "payload": payload})
        timestamp = occurred_at or utc_now()
        async with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                record = self._require(identity)
                if idempotency_key is not None:
                    prior = self._connection.execute(
                        f"SELECT * FROM transcript_events WHERE {_KEY_PREDICATE} "
                        "AND idempotency_key=?",
                        (*_key(identity), idempotency_key),
                    ).fetchone()
                    if prior is not None:
                        if prior["payload_digest"] != fingerprint:
                            raise SessionIdempotencyConflictError(
                                "event idempotency key was reused with different content"
                            )
                        self._connection.commit()
                        return self._event(identity, prior)
                self._check_revision(record, expected_revision)
                per_session = int(
                    self._connection.execute(
                        f"SELECT COUNT(*) AS count FROM transcript_events "
                        f"WHERE {_KEY_PREDICATE}",
                        _key(identity),
                    ).fetchone()["count"]
                )
                if per_session >= self._limits.max_transcript_events_per_session:
                    raise SessionCapacityError(
                        "session reached max_transcript_events_per_session"
                    )
                total = int(
                    self._connection.execute(
                        "SELECT COUNT(*) AS count FROM transcript_events"
                    ).fetchone()["count"]
                )
                if total >= self._limits.max_transcript_events_total:
                    raise SessionCapacityError(
                        "session store reached max_transcript_events_total"
                    )
                sequence_row = self._connection.execute(
                    f"SELECT COALESCE(MAX(sequence), 0) AS value FROM transcript_events "
                    f"WHERE {_KEY_PREDICATE}",
                    _key(identity),
                ).fetchone()
                sequence = int(sequence_row["value"]) + 1
                revision = record.revision + 1
                event = TranscriptEvent(
                    identity=identity,
                    event_id=event_id,
                    sequence=sequence,
                    revision=revision,
                    type=event_type,
                    payload=payload,
                    occurred_at=timestamp,
                    idempotency_key=idempotency_key,
                )
                self._connection.execute(
                    "INSERT INTO transcript_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        *_key(identity),
                        event.event_id,
                        event.sequence,
                        event.revision,
                        event.type.value,
                        payload_json,
                        fingerprint,
                        event.occurred_at.isoformat(),
                        event.idempotency_key,
                    ),
                )
                self._update_session(
                    identity,
                    revision,
                    max(record.updated_at, event.occurred_at),
                    record.status,
                )
                self._connection.commit()
                return event
            except sqlite3.IntegrityError as exc:
                self._connection.rollback()
                raise SessionIdempotencyConflictError(
                    "event_id or idempotency_key already exists in this session"
                ) from exc
            except BaseException:
                self._connection.rollback()
                raise

    async def save_checkpoint(
        self,
        identity: SessionIdentity,
        *,
        checkpoint_id: str,
        workflow_state: JsonValue,
        expected_revision: int,
        idempotency_key: str,
    ) -> SessionCheckpoint:
        state_json = canonical_json(workflow_state)
        if len(state_json.encode("utf-8")) > self._limits.max_checkpoint_state_bytes:
            raise SessionPayloadTooLargeError(
                "checkpoint state exceeds max_checkpoint_state_bytes"
            )
        state_digest = _digest(workflow_state)
        async with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                record = self._require(identity)
                prior = self._connection.execute(
                    f"SELECT * FROM session_checkpoints WHERE {_KEY_PREDICATE} "
                    "AND idempotency_key=?",
                    (*_key(identity), idempotency_key),
                ).fetchone()
                if prior is not None:
                    if prior["state_digest"] != state_digest:
                        raise SessionIdempotencyConflictError(
                            "checkpoint idempotency key was reused with different state"
                        )
                    self._connection.commit()
                    return self._checkpoint(identity, prior)
                self._check_revision(record, expected_revision)
                sequence_row = self._connection.execute(
                    f"SELECT COALESCE(MAX(sequence), 0) AS value FROM transcript_events "
                    f"WHERE {_KEY_PREDICATE}",
                    _key(identity),
                ).fetchone()
                now = utc_now()
                checkpoint = SessionCheckpoint(
                    identity=identity,
                    checkpoint_id=checkpoint_id,
                    revision=record.revision + 1,
                    workflow_state=workflow_state,
                    last_event_sequence=int(sequence_row["value"]),
                    created_at=now,
                    idempotency_key=idempotency_key,
                )
                self._connection.execute(
                    "INSERT INTO session_checkpoints VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        *_key(identity),
                        checkpoint.checkpoint_id,
                        checkpoint.revision,
                        state_json,
                        state_digest,
                        checkpoint.last_event_sequence,
                        checkpoint.created_at.isoformat(),
                        checkpoint.idempotency_key,
                    ),
                )
                self._prune_checkpoints(identity)
                self._update_session(identity, checkpoint.revision, now, record.status)
                self._connection.commit()
                return checkpoint
            except sqlite3.IntegrityError as exc:
                self._connection.rollback()
                raise SessionIdempotencyConflictError(
                    "checkpoint_id or idempotency_key already exists in this session"
                ) from exc
            except BaseException:
                self._connection.rollback()
                raise

    def _prune_checkpoints(self, identity: SessionIdentity) -> None:
        """Keep only the newest bounded snapshot window inside the write transaction."""

        key = _key(identity)
        self._connection.execute(
            f"DELETE FROM session_checkpoints WHERE {_KEY_PREDICATE} AND revision NOT IN ("
            f"SELECT revision FROM session_checkpoints WHERE {_KEY_PREDICATE} "
            "ORDER BY revision DESC LIMIT ?)",
            (*key, *key, self._limits.max_checkpoints_per_session),
        )
        while True:
            total = int(
                self._connection.execute(
                    f"SELECT COALESCE(SUM(LENGTH(CAST(workflow_state_json AS BLOB))), 0) "
                    f"AS total FROM session_checkpoints WHERE {_KEY_PREDICATE}",
                    key,
                ).fetchone()["total"]
            )
            if total <= self._limits.max_checkpoint_bytes_per_session:
                return
            oldest = self._connection.execute(
                f"SELECT revision FROM session_checkpoints WHERE {_KEY_PREDICATE} "
                "ORDER BY revision ASC LIMIT 1",
                key,
            ).fetchone()
            if oldest is None:  # pragma: no cover - defensive transaction invariant
                return
            self._connection.execute(
                f"DELETE FROM session_checkpoints WHERE {_KEY_PREDICATE} AND revision=?",
                (*key, int(oldest["revision"])),
            )

    async def set_status(
        self,
        identity: SessionIdentity,
        status: SessionStatus,
        *,
        expected_revision: int,
    ) -> SessionRecord:
        status = SessionStatus(status)
        async with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                record = self._require(identity)
                self._check_revision(record, expected_revision)
                now = utc_now()
                self._update_session(identity, record.revision + 1, now, status)
                self._connection.commit()
                return self._require(identity)
            except BaseException:
                self._connection.rollback()
                raise

    async def resume(self, identity: SessionIdentity) -> SessionSnapshot | None:
        async with self._lock:
            row = self._connection.execute(
                f"SELECT * FROM sessions WHERE {_KEY_PREDICATE}", _key(identity)
            ).fetchone()
            if row is None:
                return None
            checkpoint_row = self._connection.execute(
                f"SELECT * FROM session_checkpoints WHERE {_KEY_PREDICATE} "
                "ORDER BY revision DESC LIMIT 1",
                _key(identity),
            ).fetchone()
            checkpoint = (
                None if checkpoint_row is None else self._checkpoint(identity, checkpoint_row)
            )
            after = 0 if checkpoint is None else checkpoint.last_event_sequence
            event_rows = self._connection.execute(
                f"SELECT * FROM transcript_events WHERE {_KEY_PREDICATE} AND sequence>? "
                "ORDER BY sequence",
                (*_key(identity), after),
            ).fetchall()
            return SessionSnapshot(
                session=self._record(identity, row),
                checkpoint=checkpoint,
                transcript=tuple(self._event(identity, item) for item in event_rows),
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
            rows = self._connection.execute(
                f"SELECT * FROM transcript_events WHERE {_KEY_PREDICATE} "
                "ORDER BY sequence DESC LIMIT ?",
                (*_key(identity), limit),
            ).fetchall()
            return tuple(self._event(identity, item) for item in reversed(rows))

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            self._connection.close()

    async def healthcheck(self) -> None:
        """Fail when the durable session connection is not usable."""

        async with self._lock:
            if self._closed:
                raise RuntimeError("session store is closed")
            row = self._connection.execute("SELECT 1").fetchone()
            if row is None or int(row[0]) != 1:
                raise RuntimeError("SQLite session healthcheck failed")

    def _require(self, identity: SessionIdentity) -> SessionRecord:
        row = self._connection.execute(
            f"SELECT * FROM sessions WHERE {_KEY_PREDICATE}", _key(identity)
        ).fetchone()
        if row is None:
            raise SessionNotFoundError("session was not found for this identity")
        return self._record(identity, row)

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("session clock must return a timezone-aware datetime")
        return now.astimezone(UTC)

    @staticmethod
    def _epoch_us(value: datetime) -> int:
        return int(value.timestamp() * 1_000_000)

    @staticmethod
    def _validate_run_claim(
        run_id: str,
        owner_token: str,
        lease_seconds: float,
    ) -> None:
        if not isinstance(run_id, str) or not _IDENTIFIER.fullmatch(run_id):
            raise ValueError("invalid session run_id")
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

    def _update_session(
        self,
        identity: SessionIdentity,
        revision: int,
        updated_at: datetime,
        status: SessionStatus,
    ) -> None:
        self._connection.execute(
            f"UPDATE sessions SET status=?, revision=?, updated_at=? WHERE {_KEY_PREDICATE}",
            (status.value, revision, updated_at.isoformat(), *_key(identity)),
        )

    @staticmethod
    def _check_revision(record: SessionRecord, expected: int | None) -> None:
        if expected is not None and expected != record.revision:
            raise SessionRevisionConflictError(expected, record.revision)

    @staticmethod
    def _record(identity: SessionIdentity, row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            identity=identity,
            status=SessionStatus(row["status"]),
            revision=int(row["revision"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _event(identity: SessionIdentity, row: sqlite3.Row) -> TranscriptEvent:
        return TranscriptEvent(
            identity=identity,
            event_id=row["event_id"],
            sequence=int(row["sequence"]),
            revision=int(row["revision"]),
            type=TranscriptEventType(row["event_type"]),
            payload=json.loads(row["payload_json"]),
            occurred_at=datetime.fromisoformat(row["occurred_at"]),
            idempotency_key=row["idempotency_key"],
        )

    @staticmethod
    def _checkpoint(identity: SessionIdentity, row: sqlite3.Row) -> SessionCheckpoint:
        return SessionCheckpoint(
            identity=identity,
            checkpoint_id=row["checkpoint_id"],
            revision=int(row["revision"]),
            workflow_state=json.loads(row["workflow_state_json"]),
            last_event_sequence=int(row["last_event_sequence"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            idempotency_key=row["idempotency_key"],
        )
