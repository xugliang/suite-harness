from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from suiteharness.channels import OutboundEvent, OutboundEventKind
from suiteharness.channels.feishu import (
    FeishuEventOutcome,
    FeishuEventProcessor,
    InMemoryEventDeduplicator,
)
from suiteharness.persistence import (
    ChannelEventScope,
    EventScopeKind,
    PersistenceCapacityError,
    SQLiteChannelEventDeduplicator,
    SQLiteDatabase,
)


class _Dispatcher:
    def __init__(self) -> None:
        self.calls = 0

    async def dispatch(self, _message: object) -> AsyncIterator[OutboundEvent]:
        self.calls += 1
        yield OutboundEvent(
            kind=OutboundEventKind.COMPLETED,
            request_id="request-1",
            correlation_id="correlation-1",
            payload={},
        )


class _BlockingDispatcher:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def dispatch(self, _message: object) -> AsyncIterator[OutboundEvent]:
        self.started.set()
        await self.release.wait()
        yield OutboundEvent(
            kind=OutboundEventKind.COMPLETED,
            request_id="request-blocked",
            correlation_id="correlation-blocked",
            payload={},
        )


def _feishu_event(event_id: str) -> dict[str, object]:
    return {
        "header": {
            "event_id": event_id,
            "event_type": "im.message.receive_v1",
            "create_time": "1700000000000",
        },
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": "ou-user"}},
            "message": {
                "message_id": "om-1",
                "chat_id": "oc-1",
                "chat_type": "p2p",
                "message_type": "text",
                "content": '{"text":"hello"}',
                "create_time": "1700000000000",
            },
        },
    }


def test_deployment_event_dedupe_plugs_into_feishu_and_survives_restart(
    tmp_path: Path,
) -> None:
    async def exercise() -> tuple[FeishuEventOutcome, FeishuEventOutcome, int]:
        path = (tmp_path / "events.sqlite3").resolve()
        scope = ChannelEventScope.deployment("company-server-1", "feishu")
        first_database = SQLiteDatabase(path)
        first_deduper = SQLiteChannelEventDeduplicator(first_database, scope=scope)
        dispatcher = _Dispatcher()
        first_processor = FeishuEventProcessor(
            dispatcher,
            bot_open_id="ou-bot",
            deduplicator=first_deduper,
        )
        first = await first_processor.process_payload(_feishu_event("evt-1"))
        await first_database.close()

        second_database = SQLiteDatabase(path)
        second_processor = FeishuEventProcessor(
            dispatcher,
            bot_open_id="ou-bot",
            deduplicator=SQLiteChannelEventDeduplicator(second_database, scope=scope),
        )
        duplicate = await second_processor.process_payload(_feishu_event("evt-1"))
        await second_database.close()
        return first, duplicate, dispatcher.calls

    first, duplicate, calls = asyncio.run(exercise())
    assert first is FeishuEventOutcome.DISPATCHED
    assert duplicate is FeishuEventOutcome.DUPLICATE
    assert calls == 1


def test_incomplete_processing_claim_is_recoverable_after_restart_lease(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        current = datetime(2026, 1, 1, tzinfo=UTC)

        def clock() -> datetime:
            return current

        path = (tmp_path / "events.sqlite3").resolve()
        scope = ChannelEventScope.deployment("company-server-1", "feishu")
        first_database = SQLiteDatabase(path)
        first = SQLiteChannelEventDeduplicator(
            first_database,
            scope=scope,
            processing_lease_seconds=10,
            clock=clock,
        )
        # Simulate a process crash after reservation but before the reply and
        # completion marker. Closing the database deliberately does not release.
        assert await first.claim("evt-crashed") is True
        await first_database.close()

        second_database = SQLiteDatabase(path)
        second = SQLiteChannelEventDeduplicator(
            second_database,
            scope=scope,
            processing_lease_seconds=10,
            clock=clock,
        )
        assert await second.claim("evt-crashed") is False
        current += timedelta(seconds=11)
        assert await second.claim("evt-crashed") is True
        await second.complete("evt-crashed")
        await second_database.close()

        third_database = SQLiteDatabase(path)
        third = SQLiteChannelEventDeduplicator(third_database, scope=scope, clock=clock)
        assert await third.claim("evt-crashed") is False
        await third_database.close()

    asyncio.run(exercise())


def test_event_release_expiry_and_product_scope_isolation() -> None:
    async def exercise() -> None:
        current = datetime(2026, 1, 1, tzinfo=UTC)

        def clock() -> datetime:
            return current

        database = SQLiteDatabase(":memory:")
        alpha = SQLiteChannelEventDeduplicator(
            database,
            scope=ChannelEventScope.product("tenant-a", "alpha", "web"),
            ttl_seconds=10,
            clock=clock,
        )
        beta = SQLiteChannelEventDeduplicator(
            database,
            scope=ChannelEventScope.product("tenant-a", "beta", "web"),
            ttl_seconds=10,
            clock=clock,
        )
        assert await alpha.claim("evt") is True
        assert await alpha.claim("evt") is False
        assert await beta.claim("evt") is True
        await alpha.release("evt")
        assert await alpha.claim("evt") is True
        current += timedelta(seconds=11)
        assert await alpha.claim("evt") is True
        assert await beta.purge_expired() == 1
        await database.close()

    asyncio.run(exercise())


def test_event_capacity_fails_closed_without_evicting_live_claim() -> None:
    async def exercise() -> None:
        database = SQLiteDatabase(":memory:")
        deduper = SQLiteChannelEventDeduplicator(
            database,
            scope=ChannelEventScope.deployment("server", "feishu"),
            max_entries=1,
        )
        assert await deduper.claim("first") is True
        with pytest.raises(PersistenceCapacityError):
            await deduper.claim("second")
        assert await deduper.claim("first") is False
        await database.close()

    asyncio.run(exercise())


def test_in_memory_mixed_expiries_recover_processing_without_evicting_completed() -> None:
    async def exercise() -> None:
        current = 0.0

        def clock() -> float:
            return current

        deduper = InMemoryEventDeduplicator(
            ttl_seconds=1_000,
            processing_lease_seconds=10,
            max_entries=2,
            clock=clock,
        )
        assert await deduper.claim("completed") is True
        await deduper.complete("completed")
        current = 1.0
        assert await deduper.claim("crashed") is True
        current = 12.0
        # The long-lived completed marker is older in insertion order, but it
        # must not hide the expired processing lease behind it.
        assert await deduper.claim("crashed") is True
        with pytest.raises(RuntimeError, match="capacity"):
            await deduper.claim("third")
        assert await deduper.claim("completed") is False

    asyncio.run(exercise())


def test_expired_event_claim_release_cannot_delete_new_owner_generation() -> None:
    async def exercise() -> None:
        current = datetime(2026, 1, 1, tzinfo=UTC)

        def clock() -> datetime:
            return current

        database = SQLiteDatabase(":memory:")
        deduper = SQLiteChannelEventDeduplicator(
            database,
            scope=ChannelEventScope.deployment("server", "feishu"),
            ttl_seconds=10,
            clock=clock,
        )
        first_claimed = asyncio.Event()
        release_first = asyncio.Event()

        async def first_owner() -> None:
            assert await deduper.claim("event-1") is True
            first_claimed.set()
            await release_first.wait()
            await deduper.release("event-1")

        first = asyncio.create_task(first_owner())
        await first_claimed.wait()
        current += timedelta(seconds=11)
        assert await deduper.claim("event-1") is True
        release_first.set()
        await first
        # The delayed release carried generation A's token and must not
        # delete the live generation B claim.
        assert await deduper.claim("event-1") is False
        await database.close()

    asyncio.run(exercise())


def test_event_scope_shape_is_explicit() -> None:
    with pytest.raises(ValueError, match="deployment event scope"):
        ChannelEventScope(
            kind=EventScopeKind.DEPLOYMENT,
            deployment_id="server",
            tenant_id="tenant-a",
            channel_id="feishu",
        )
    with pytest.raises(ValueError, match="product event scope"):
        ChannelEventScope(
            kind=EventScopeKind.PRODUCT,
            tenant_id="tenant-a",
            channel_id="web",
        )


def test_in_memory_processing_lease_outlives_the_processing_timeout() -> None:
    async def exercise() -> None:
        current = 0.0

        def clock() -> float:
            return current

        deduper = InMemoryEventDeduplicator(
            processing_lease_seconds=660,
            clock=clock,
        )
        dispatcher = _BlockingDispatcher()
        processor = FeishuEventProcessor(
            dispatcher,
            bot_open_id="ou-bot",
            deduplicator=deduper,
            processing_timeout_seconds=600,
        )
        first = asyncio.create_task(processor.process_payload(_feishu_event("evt-live")))
        await dispatcher.started.wait()
        current = 600.0
        try:
            assert await deduper.claim("evt-live") is False
        finally:
            dispatcher.release.set()
            assert await first is FeishuEventOutcome.DISPATCHED

    asyncio.run(exercise())


def test_sqlite_processing_lease_outlives_the_processing_timeout(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        current = datetime(2026, 1, 1, tzinfo=UTC)

        def clock() -> datetime:
            return current

        path = (tmp_path / "active-event.sqlite3").resolve()
        scope = ChannelEventScope.deployment("server", "feishu")
        first_database = SQLiteDatabase(path)
        second_database = SQLiteDatabase(path)
        first_deduper = SQLiteChannelEventDeduplicator(
            first_database,
            scope=scope,
            processing_lease_seconds=660,
            clock=clock,
        )
        second_deduper = SQLiteChannelEventDeduplicator(
            second_database,
            scope=scope,
            processing_lease_seconds=660,
            clock=clock,
        )
        dispatcher = _BlockingDispatcher()
        processor = FeishuEventProcessor(
            dispatcher,
            bot_open_id="ou-bot",
            deduplicator=first_deduper,
            processing_timeout_seconds=600,
        )
        first = asyncio.create_task(processor.process_payload(_feishu_event("evt-live")))
        await dispatcher.started.wait()
        current += timedelta(seconds=600)
        try:
            assert await second_deduper.claim("evt-live") is False
        finally:
            dispatcher.release.set()
            assert await first is FeishuEventOutcome.DISPATCHED
            await second_database.close()
            await first_database.close()

    asyncio.run(exercise())


def test_processor_rejects_a_lease_that_cannot_cover_its_timeout(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        database = SQLiteDatabase((tmp_path / "short-lease.sqlite3").resolve())
        implementations = (
            InMemoryEventDeduplicator(processing_lease_seconds=600),
            SQLiteChannelEventDeduplicator(
                database,
                scope=ChannelEventScope.deployment("server", "feishu"),
                processing_lease_seconds=600,
            ),
        )
        for deduper in implementations:
            with pytest.raises(ValueError, match="strictly greater"):
                FeishuEventProcessor(
                    _Dispatcher(),
                    bot_open_id="ou-bot",
                    deduplicator=deduper,
                    processing_timeout_seconds=600,
                )
        await database.close()

    asyncio.run(exercise())


def test_processing_timeout_releases_the_claim_for_provider_retry() -> None:
    async def exercise() -> None:
        deduper = InMemoryEventDeduplicator(processing_lease_seconds=0.1)
        dispatcher = _BlockingDispatcher()
        processor = FeishuEventProcessor(
            dispatcher,
            bot_open_id="ou-bot",
            deduplicator=deduper,
            processing_timeout_seconds=0.01,
        )
        with pytest.raises(TimeoutError):
            await processor.process_payload(_feishu_event("evt-timeout"))
        assert await deduper.claim("evt-timeout") is True
        await deduper.release("evt-timeout")

    asyncio.run(exercise())


@pytest.mark.parametrize("invalid", [True, 1.5, "1"])
def test_event_deduplication_capacity_requires_an_integer(
    tmp_path: Path,
    invalid: object,
) -> None:
    with pytest.raises(ValueError, match="max_entries"):
        InMemoryEventDeduplicator(max_entries=invalid)  # type: ignore[arg-type]

    database = SQLiteDatabase((tmp_path / "invalid-capacity.sqlite3").resolve())
    try:
        with pytest.raises(ValueError, match="max_entries"):
            SQLiteChannelEventDeduplicator(
                database,
                scope=ChannelEventScope.deployment("server", "feishu"),
                max_entries=invalid,  # type: ignore[arg-type]
            )
    finally:
        asyncio.run(database.close())
