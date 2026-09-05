"""Restart-safe channel event deduplication for webhook and socket ingress."""

from __future__ import annotations

import asyncio
import secrets
import weakref
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from .database import SQLiteDatabase
from .errors import PersistenceCapacityError
from .models import ChannelEventScope, utc_now

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS suiteharness_channel_event_claims (
        scope_kind TEXT NOT NULL CHECK(scope_kind IN ('deployment', 'product')),
        deployment_id TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        product_id TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        claimed_at_us INTEGER NOT NULL,
        expires_at_us INTEGER NOT NULL,
        PRIMARY KEY (
            scope_kind, deployment_id, tenant_id, product_id, channel_id, event_id
        ),
        CHECK (
            (scope_kind='deployment' AND deployment_id<>'' AND tenant_id='' AND product_id='')
            OR
            (scope_kind='product' AND deployment_id='' AND tenant_id<>'' AND product_id<>'')
        )
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS suiteharness_channel_event_claims_by_expiry
    ON suiteharness_channel_event_claims(expires_at_us)
    """,
)

_OWNER_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS suiteharness_channel_event_claim_owners (
        scope_kind TEXT NOT NULL,
        deployment_id TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        product_id TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        owner_token TEXT NOT NULL,
        PRIMARY KEY (
            scope_kind, deployment_id, tenant_id, product_id, channel_id, event_id
        ),
        FOREIGN KEY (
            scope_kind, deployment_id, tenant_id, product_id, channel_id, event_id
        ) REFERENCES suiteharness_channel_event_claims(
            scope_kind, deployment_id, tenant_id, product_id, channel_id, event_id
        ) ON DELETE CASCADE
    )
    """,
)


def _epoch_us(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("persistence clock must return a timezone-aware datetime")
    return int(value.astimezone(UTC).timestamp() * 1_000_000)


class SQLiteChannelEventDeduplicator:
    """Durable bounded implementation of the Feishu ``EventDeduplicator`` protocol.

    Bind one instance to a validated scope, then pass it directly to
    ``FeishuEventProcessor``. A successful claim survives process restarts for
    the retention period; ``release`` removes only that exact scoped event when
    dispatch fails and the upstream channel should retry it.
    """

    def __init__(
        self,
        database: SQLiteDatabase,
        *,
        scope: ChannelEventScope,
        ttl_seconds: float = 86_400,
        max_entries: int = 100_000,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not isinstance(scope, ChannelEventScope):
            raise TypeError("scope must be ChannelEventScope")
        if ttl_seconds <= 0:
            raise ValueError("dedupe ttl_seconds must be positive")
        if max_entries < 1:
            raise ValueError("dedupe max_entries must be positive")
        self._database = database
        self._scope = scope
        self._ttl = timedelta(seconds=ttl_seconds)
        self._max_entries = max_entries
        self._clock = clock
        # ``EventDeduplicator`` predates ownership handles in its public API.
        # Keep the unguessable owner token task-local so a delayed release can
        # compare-and-delete its own generation without changing channel code.
        self._owned_claims: weakref.WeakKeyDictionary[
            asyncio.Task[object], dict[str, tuple[str, int]]
        ] = weakref.WeakKeyDictionary()
        database.install_schema("channel_event_dedupe", 1, _SCHEMA)
        database.install_schema("channel_event_dedupe_owners", 1, _OWNER_SCHEMA)

    async def claim(self, event_id: str) -> bool:
        """Atomically reserve an event, returning false for a retained duplicate."""

        self._validate_event_id(event_id)
        now = self._clock()
        now_us = _epoch_us(now)
        expires_at_us = _epoch_us(now + self._ttl)
        owner_token = secrets.token_urlsafe(32)
        scope_values = self._scope.storage_key()
        async with self._database.transaction(write=True) as connection:
            connection.execute(
                """
                DELETE FROM suiteharness_channel_event_claims
                WHERE scope_kind=? AND deployment_id=? AND tenant_id=? AND product_id=?
                    AND channel_id=? AND expires_at_us<=?
                """,
                (*scope_values, now_us),
            )
            existing = connection.execute(
                """
                SELECT 1 FROM suiteharness_channel_event_claims
                WHERE scope_kind=? AND deployment_id=? AND tenant_id=? AND product_id=?
                    AND channel_id=? AND event_id=?
                """,
                (*scope_values, event_id),
            ).fetchone()
            if existing is not None:
                return False
            count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM suiteharness_channel_event_claims
                    WHERE scope_kind=? AND deployment_id=? AND tenant_id=? AND product_id=?
                        AND channel_id=?
                    """,
                    scope_values,
                ).fetchone()["count"]
            )
            if count >= self._max_entries:
                # Evicting a live claim would silently re-run an event. Failing
                # closed lets the channel retry after capacity/expiry instead.
                raise PersistenceCapacityError(
                    "channel deduplication scope has reached its event limit"
                )
            connection.execute(
                """
                INSERT INTO suiteharness_channel_event_claims(
                    scope_kind, deployment_id, tenant_id, product_id, channel_id,
                    event_id, claimed_at_us, expires_at_us
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*scope_values, event_id, now_us, expires_at_us),
            )
            connection.execute(
                """
                INSERT INTO suiteharness_channel_event_claim_owners(
                    scope_kind, deployment_id, tenant_id, product_id, channel_id,
                    event_id, owner_token
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (*scope_values, event_id, owner_token),
            )
        task = self._current_task()
        owned = self._owned_claims.setdefault(task, {})
        for stale_event_id, (_, owned_until_us) in tuple(owned.items()):
            if owned_until_us <= now_us:
                owned.pop(stale_event_id, None)
        owned[event_id] = (owner_token, expires_at_us)
        return True

    async def release(self, event_id: str) -> None:
        """Release one failed dispatch without affecting another scope."""

        self._validate_event_id(event_id)
        task = self._current_task()
        owned = self._owned_claims.get(task)
        owned_claim = None if owned is None else owned.pop(event_id, None)
        if not owned_claim:
            return
        owner_token, _ = owned_claim
        async with self._database.transaction(write=True) as connection:
            connection.execute(
                """
                DELETE FROM suiteharness_channel_event_claims
                WHERE scope_kind=? AND deployment_id=? AND tenant_id=? AND product_id=?
                    AND channel_id=? AND event_id=?
                    AND EXISTS (
                        SELECT 1 FROM suiteharness_channel_event_claim_owners AS owner
                        WHERE owner.scope_kind=suiteharness_channel_event_claims.scope_kind
                            AND owner.deployment_id=suiteharness_channel_event_claims.deployment_id
                            AND owner.tenant_id=suiteharness_channel_event_claims.tenant_id
                            AND owner.product_id=suiteharness_channel_event_claims.product_id
                            AND owner.channel_id=suiteharness_channel_event_claims.channel_id
                            AND owner.event_id=suiteharness_channel_event_claims.event_id
                            AND owner.owner_token=?
                    )
                """,
                (*self._scope.storage_key(), event_id, owner_token),
            )

    async def purge_expired(self) -> int:
        """Remove expired rows in this scope and return the affected row count."""

        now_us = _epoch_us(self._clock())
        async with self._database.transaction(write=True) as connection:
            cursor = connection.execute(
                """
                DELETE FROM suiteharness_channel_event_claims
                WHERE scope_kind=? AND deployment_id=? AND tenant_id=? AND product_id=?
                    AND channel_id=? AND expires_at_us<=?
                """,
                (*self._scope.storage_key(), now_us),
            )
            return cursor.rowcount

    @staticmethod
    def _validate_event_id(event_id: str) -> None:
        if (
            not isinstance(event_id, str)
            or not event_id
            or len(event_id) > 1024
            or "\x00" in event_id
        ):
            raise ValueError("invalid channel event_id")

    @staticmethod
    def _current_task() -> asyncio.Task[object]:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("channel event claims require an asyncio task")
        return task


__all__ = ["SQLiteChannelEventDeduplicator"]
