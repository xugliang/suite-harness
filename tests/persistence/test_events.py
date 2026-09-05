from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from suiteharness.channels import OutboundEvent, OutboundEventKind
from suiteharness.channels.feishu import FeishuEventOutcome, FeishuEventProcessor
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
