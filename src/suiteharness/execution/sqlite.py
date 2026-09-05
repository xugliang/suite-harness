"""Durable append-only audit journal for a single-server deployment."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path

from suiteharness.execution.models import AuditEvent


class AuditConflictError(RuntimeError):
    """An immutable audit event identifier was already committed."""


class SQLiteAuditJournal:
    """Persist detached audit events; the public API intentionally has no update/delete."""

    def __init__(self, database: str | Path) -> None:
        path = Path(database)
        if not path.is_absolute():
            raise ValueError("audit database path must be absolute")
        path.parent.mkdir(parents=True, exist_ok=True)
        created = not path.exists()
        self._connection = sqlite3.connect(
            str(path),
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        if created and os.name != "nt":
            path.chmod(0o600)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout=30000")
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS audit_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                occurred_at TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                product_id TEXT NOT NULL,
                principal_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                event_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS audit_by_tenant_time
                ON audit_events(tenant_id, occurred_at, sequence);
            CREATE INDEX IF NOT EXISTS audit_by_run
                ON audit_events(tenant_id, product_id, run_id, sequence);
            """
        )
        self._lock = asyncio.Lock()
        self._closed = False

    async def append(self, event: AuditEvent) -> None:
        if not isinstance(event, AuditEvent):
            raise TypeError("event must be an AuditEvent")
        payload = event.model_dump_json()
        async with self._lock:
            self._require_open()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._connection.execute(
                    "INSERT INTO audit_events "
                    "(event_id, occurred_at, tenant_id, product_id, principal_id, "
                    "channel_id, run_id, event_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event.event_id,
                        event.occurred_at.isoformat(),
                        event.tenant_id,
                        event.product_id,
                        event.principal_id,
                        event.channel_id,
                        event.run_id,
                        payload,
                    ),
                )
                self._connection.commit()
            except sqlite3.IntegrityError as exc:
                self._connection.rollback()
                raise AuditConflictError("audit event_id already exists") from exc
            except BaseException:
                self._connection.rollback()
                raise

    async def events(
        self,
        *,
        tenant_id: str | None = None,
        product_id: str | None = None,
        run_id: str | None = None,
        after_sequence: int = 0,
        limit: int = 1000,
    ) -> tuple[AuditEvent, ...]:
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        if limit < 1 or limit > 10_000:
            raise ValueError("audit query limit must be in [1, 10000]")
        predicates = ["sequence > ?"]
        values: list[object] = [after_sequence]
        for column, value in (
            ("tenant_id", tenant_id),
            ("product_id", product_id),
            ("run_id", run_id),
        ):
            if value is not None:
                predicates.append(f"{column} = ?")
                values.append(value)
        values.append(limit)
        statement = (
            "SELECT event_json FROM audit_events WHERE "
            + " AND ".join(predicates)
            + " ORDER BY sequence LIMIT ?"
        )
        async with self._lock:
            self._require_open()
            rows = self._connection.execute(statement, values).fetchall()
        return tuple(AuditEvent.model_validate_json(row["event_json"]) for row in rows)

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            self._connection.close()

    async def healthcheck(self) -> None:
        """Fail when the append-only audit connection is not usable."""

        async with self._lock:
            self._require_open()
            row = self._connection.execute("SELECT 1").fetchone()
            if row is None or int(row[0]) != 1:
                raise RuntimeError("SQLite audit healthcheck failed")

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("audit journal is closed")


__all__ = ["AuditConflictError", "SQLiteAuditJournal"]
