"""PostgreSQL session store sharing the proven session state-machine operations."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from suiteharness.persistence.postgres import PostgresDatabase, PostgresStoreAdapter, _OperationLock
from .models import SessionStoreLimits, utc_now
from .protocols import SessionStore
from .sqlite import SQLSessionOperations


class _SessionOperations(SQLSessionOperations):
    def __init__(self, database, clock, limits):
        self._connection = database._connection
        self._lock = _OperationLock()
        self._closed = False
        self._clock = clock
        self._limits = limits or SessionStoreLimits()


class PostgresSessionStore(PostgresStoreAdapter, SessionStore):
    """Pooled sessions with the same CAS, run ownership and clear tombstone rules.

    The shared operation instance is constructed without invoking its SQLite
    initializer. It owns no database file or connection. Each public operation
    borrows a PostgreSQL transaction and executes entirely on a worker thread.
    """
    def __init__(self, database: PostgresDatabase, *, clock: Callable[[], datetime] = utc_now,
                 limits: SessionStoreLimits | None = None):
        if limits is not None and not isinstance(limits, SessionStoreLimits):
            raise TypeError("limits must be SessionStoreLimits")
        database.install_schema("sessions", 1, ())
        self._setup(database, _SessionOperations(database, clock, limits))

    async def create(self, identity):
        return await self._invoke("create", identity)
    async def get(self, identity):
        return await self._invoke("get", identity, write=False)
    async def clear_conversation(self, identity, *, idempotency_key):
        return await self._invoke("clear_conversation", identity, idempotency_key=idempotency_key)
    async def pending_clear(self, identity):
        return await self._invoke("pending_clear", identity, write=False)
    async def finish_clear(self, identity, *, idempotency_key, failed_layers=()):
        return await self._invoke("finish_clear", identity, idempotency_key=idempotency_key, failed_layers=failed_layers)
    async def claim_run(self, identity, *, run_id, owner_token, lease_seconds):
        return await self._invoke("claim_run", identity, run_id=run_id, owner_token=owner_token, lease_seconds=lease_seconds)
    async def finalize_run(self, identity, *, run_id, owner_token, lease_seconds):
        return await self._invoke("finalize_run", identity, run_id=run_id, owner_token=owner_token, lease_seconds=lease_seconds)
    async def append(self, identity, **kwargs):
        return await self._invoke("append", identity, **kwargs)
    async def save_checkpoint(self, identity, **kwargs):
        return await self._invoke("save_checkpoint", identity, **kwargs)
    async def set_status(self, identity, status, *, expected_revision):
        return await self._invoke("set_status", identity, status, expected_revision=expected_revision)
    async def resume(self, identity):
        return await self._invoke("resume", identity, write=False)
    async def recent_transcript(self, identity, *, limit):
        return await self._invoke("recent_transcript", identity, limit=limit, write=False)


__all__ = ["PostgresSessionStore"]
