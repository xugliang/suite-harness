"""CAS-protected tenant/product JSON state for plugins and resumable protocols."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from pydantic import JsonValue

from .database import SQLiteDatabase
from .errors import (
    PersistenceCapacityError,
    StateIdempotencyConflictError,
    StatePayloadTooLargeError,
    StateRevisionConflictError,
)
from .models import ProductStateKey, StateRecord, StateWriteResult, canonical_json, utc_now

_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS suiteharness_product_state (
        tenant_id TEXT NOT NULL,
        product_id TEXT NOT NULL,
        namespace TEXT NOT NULL,
        state_key TEXT NOT NULL,
        value_json TEXT NOT NULL,
        value_digest TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision > 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (tenant_id, product_id, namespace, state_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS suiteharness_product_state_by_namespace
    ON suiteharness_product_state(tenant_id, product_id, namespace, state_key)
    """,
    """
    CREATE TABLE IF NOT EXISTS suiteharness_state_idempotency (
        tenant_id TEXT NOT NULL,
        product_id TEXT NOT NULL,
        namespace TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        state_key TEXT NOT NULL,
        command_digest TEXT NOT NULL,
        result_value_json TEXT NOT NULL,
        result_value_digest TEXT NOT NULL,
        result_revision INTEGER NOT NULL CHECK(result_revision > 0),
        result_created_at TEXT NOT NULL,
        result_updated_at TEXT NOT NULL,
        expires_at_us INTEGER NOT NULL,
        PRIMARY KEY (tenant_id, product_id, namespace, idempotency_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS suiteharness_state_idempotency_by_expiry
    ON suiteharness_state_idempotency(expires_at_us)
    """,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _epoch_us(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("persistence clock must return a timezone-aware datetime")
    return int(value.astimezone(UTC).timestamp() * 1_000_000)


def _reject_approval_credentials(value: JsonValue) -> None:
    """Keep one-shot approval bearer material out of generic durable state."""

    if isinstance(value, dict):
        for key, child in value.items():
            normalized = "".join(character for character in key.lower() if character.isalnum())
            if normalized in {"approvaltoken", "approvalcredential"}:
                raise ValueError("approval credentials must not be persisted")
            _reject_approval_credentials(child)
    elif isinstance(value, list):
        for child in value:
            _reject_approval_credentials(child)


class SQLiteProductStateStore:
    """Durable, non-secret product state with mandatory CAS and idempotency.

    Revision zero means a key does not yet exist. Every mutation requires both
    ``expected_revision`` and an idempotency key, making unsafe blind writes
    impossible through this interface. Idempotency records have bounded
    retention; callers should retry within that configured window.
    """

    def __init__(
        self,
        database: SQLiteDatabase,
        *,
        max_value_bytes: int = 1_048_576,
        max_records_per_namespace: int = 10_000,
        max_idempotency_records_per_namespace: int = 100_000,
        idempotency_ttl_seconds: float = 604_800,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if max_value_bytes < 1 or max_value_bytes > 64 * 1_048_576:
            raise ValueError("max_value_bytes must be in [1, 67108864]")
        if max_records_per_namespace < 1:
            raise ValueError("max_records_per_namespace must be positive")
        if max_idempotency_records_per_namespace < 1:
            raise ValueError("max_idempotency_records_per_namespace must be positive")
        if idempotency_ttl_seconds <= 0:
            raise ValueError("idempotency_ttl_seconds must be positive")
        self._database = database
        self._max_value_bytes = max_value_bytes
        self._max_records = max_records_per_namespace
        self._max_idempotency_records = max_idempotency_records_per_namespace
        self._idempotency_ttl = timedelta(seconds=idempotency_ttl_seconds)
        self._clock = clock
        database.install_schema("product_state", 1, _SCHEMA)

    async def get(self, state_key: ProductStateKey) -> StateRecord | None:
        self._require_key(state_key)
        async with self._database.transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM suiteharness_product_state
                WHERE tenant_id=? AND product_id=? AND namespace=? AND state_key=?
                """,
                self._key(state_key),
            ).fetchone()
        return None if row is None else self._record(state_key, row)

    async def compare_and_set(
        self,
        state_key: ProductStateKey,
        value: JsonValue,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> StateWriteResult:
        """Create or replace one value if its revision still matches.

        Use ``expected_revision=0`` for initial creation. Replaying the same
        command returns its original result even if a later command has already
        advanced the current state.
        """

        self._require_key(state_key)
        if expected_revision < 0:
            raise ValueError("expected_revision must be non-negative")
        if not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise ValueError("invalid idempotency_key")
        _reject_approval_credentials(value)
        value_json = canonical_json(value)
        if len(value_json.encode("utf-8")) > self._max_value_bytes:
            raise StatePayloadTooLargeError(
                f"serialized state exceeds {self._max_value_bytes} bytes"
            )
        value_digest = _digest(value_json)
        command_digest = _digest(
            canonical_json(
                {
                    "state_key": state_key.key,
                    "value_digest": value_digest,
                    "expected_revision": expected_revision,
                }
            )
        )
        now = self._clock()
        now_us = _epoch_us(now)
        now_text = now.astimezone(UTC).isoformat()
        expires_at_us = _epoch_us(now + self._idempotency_ttl)

        async with self._database.transaction(write=True) as connection:
            connection.execute(
                """
                DELETE FROM suiteharness_state_idempotency
                WHERE tenant_id=? AND product_id=? AND namespace=? AND expires_at_us<=?
                """,
                (*self._scope(state_key), now_us),
            )
            replay = connection.execute(
                """
                SELECT * FROM suiteharness_state_idempotency
                WHERE tenant_id=? AND product_id=? AND namespace=? AND idempotency_key=?
                """,
                (*self._scope(state_key), idempotency_key),
            ).fetchone()
            if replay is not None:
                if replay["command_digest"] != command_digest:
                    raise StateIdempotencyConflictError(
                        "idempotency_key was already used for a different state command"
                    )
                return StateWriteResult(
                    record=self._idempotent_record(state_key, replay), replayed=True
                )

            row = connection.execute(
                """
                SELECT * FROM suiteharness_product_state
                WHERE tenant_id=? AND product_id=? AND namespace=? AND state_key=?
                """,
                self._key(state_key),
            ).fetchone()
            actual_revision = 0 if row is None else int(row["revision"])
            if actual_revision != expected_revision:
                raise StateRevisionConflictError(
                    expected_revision=expected_revision,
                    actual_revision=actual_revision,
                )

            if row is None:
                count = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) AS count FROM suiteharness_product_state
                        WHERE tenant_id=? AND product_id=? AND namespace=?
                        """,
                        self._scope(state_key),
                    ).fetchone()["count"]
                )
                if count >= self._max_records:
                    raise PersistenceCapacityError(
                        "product state namespace has reached its record limit"
                    )

            idempotency_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM suiteharness_state_idempotency
                    WHERE tenant_id=? AND product_id=? AND namespace=?
                    """,
                    self._scope(state_key),
                ).fetchone()["count"]
            )
            if idempotency_count >= self._max_idempotency_records:
                raise PersistenceCapacityError(
                    "state idempotency namespace has reached its record limit"
                )

            revision = actual_revision + 1
            created_at = now_text if row is None else str(row["created_at"])
            if row is None:
                connection.execute(
                    """
                    INSERT INTO suiteharness_product_state(
                        tenant_id, product_id, namespace, state_key, value_json,
                        value_digest, revision, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        *self._key(state_key),
                        value_json,
                        value_digest,
                        revision,
                        created_at,
                        now_text,
                    ),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE suiteharness_product_state
                    SET value_json=?, value_digest=?, revision=?, updated_at=?
                    WHERE tenant_id=? AND product_id=? AND namespace=? AND state_key=?
                        AND revision=?
                    """,
                    (
                        value_json,
                        value_digest,
                        revision,
                        now_text,
                        *self._key(state_key),
                        expected_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    raise StateRevisionConflictError(
                        expected_revision=expected_revision,
                        actual_revision=actual_revision,
                    )

            connection.execute(
                """
                INSERT INTO suiteharness_state_idempotency(
                    tenant_id, product_id, namespace, idempotency_key, state_key,
                    command_digest, result_value_json, result_value_digest,
                    result_revision, result_created_at, result_updated_at, expires_at_us
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    *self._scope(state_key),
                    idempotency_key,
                    state_key.key,
                    command_digest,
                    value_json,
                    value_digest,
                    revision,
                    created_at,
                    now_text,
                    expires_at_us,
                ),
            )
            record = StateRecord(
                state_key=state_key,
                value=value,
                revision=revision,
                digest=value_digest,
                created_at=datetime.fromisoformat(created_at),
                updated_at=datetime.fromisoformat(now_text),
            )
            return StateWriteResult(record=record)

    async def list_namespace(
        self,
        *,
        tenant_id: str,
        product_id: str,
        namespace: str,
        after_key: str | None = None,
        limit: int = 100,
    ) -> tuple[StateRecord, ...]:
        """List one isolated namespace in stable key order."""

        marker = ProductStateKey(
            tenant_id=tenant_id,
            product_id=product_id,
            namespace=namespace,
            key=after_key or "_",
        )
        if limit < 1 or limit > 1000:
            raise ValueError("state list limit must be in [1, 1000]")
        cursor = after_key or ""
        async with self._database.transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM suiteharness_product_state
                WHERE tenant_id=? AND product_id=? AND namespace=? AND state_key>?
                ORDER BY state_key LIMIT ?
                """,
                (*self._scope(marker), cursor, limit),
            ).fetchall()
        return tuple(
            self._record(marker.model_copy(update={"key": str(row["state_key"])}), row)
            for row in rows
        )

    @staticmethod
    def _require_key(state_key: ProductStateKey) -> None:
        if not isinstance(state_key, ProductStateKey):
            raise TypeError("state_key must be ProductStateKey")

    @staticmethod
    def _scope(state_key: ProductStateKey) -> tuple[str, str, str]:
        return state_key.tenant_id, state_key.product_id, state_key.namespace

    @classmethod
    def _key(cls, state_key: ProductStateKey) -> tuple[str, str, str, str]:
        return (*cls._scope(state_key), state_key.key)

    @staticmethod
    def _record(state_key: ProductStateKey, row: object) -> StateRecord:
        return StateRecord(
            state_key=state_key,
            value=json.loads(row["value_json"]),  # type: ignore[index]
            revision=int(row["revision"]),  # type: ignore[index]
            digest=str(row["value_digest"]),  # type: ignore[index]
            created_at=datetime.fromisoformat(str(row["created_at"])),  # type: ignore[index]
            updated_at=datetime.fromisoformat(str(row["updated_at"])),  # type: ignore[index]
        )

    @staticmethod
    def _idempotent_record(state_key: ProductStateKey, row: object) -> StateRecord:
        replay_key = state_key.model_copy(update={"key": str(row["state_key"])})  # type: ignore[index]
        return StateRecord(
            state_key=replay_key,
            value=json.loads(row["result_value_json"]),  # type: ignore[index]
            revision=int(row["result_revision"]),  # type: ignore[index]
            digest=str(row["result_value_digest"]),  # type: ignore[index]
            created_at=datetime.fromisoformat(str(row["result_created_at"])),  # type: ignore[index]
            updated_at=datetime.fromisoformat(str(row["result_updated_at"])),  # type: ignore[index]
        )


__all__ = ["SQLiteProductStateStore"]
