"""Recovery caches are bounded; durable execution fences and history are not pruned."""

import asyncio
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from suiteharness.sessions import (
    InMemorySessionStore,
    RunResumePoint,
    RunStopOutcome,
    SessionIdentity,
    SessionRunClaimState,
    SessionStoreLimits,
    SQLiteSessionStore,
    TranscriptEventType,
)
from suiteharness.sessions.errors import SessionNotFoundError
from suiteharness.sessions.protocols import SessionStore


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 16, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def identity(seller="seller"):
    return SessionIdentity(tenant_id="tenant", product_id="product", agent_id="agent",
                           session_id="session", principal_id=seller)


def point(clock, scope, run_id, *, text="read"):
    return RunResumePoint(identity=scope, run_id=run_id, created_at=clock(), payload={
        "message_digest": "a" * 64, "input": text, "conversation": {"messages": []},
        "resume_state": {"source_run_id": run_id, "workflow_state": None, "observations": [],
                         "iterations_used": 0, "tool_calls_used": 0, "call_ids": [],
                         "prompt_digest": "b" * 64, "route_key": "chat"},
    })


@asynccontextmanager
async def opened(tmp_path, kind, clock, limits=None):
    store = (InMemorySessionStore(clock=clock, limits=limits) if kind == "memory" else
             SQLiteSessionStore(tmp_path / "sessions.sqlite3", clock=clock, limits=limits))
    try:
        yield store
    finally:
        if isinstance(store, SQLiteSessionStore):
            await store.close()


async def save(store, clock, scope, run_id, *, text="read"):
    await store.create(scope)
    await store.claim_run(scope, run_id=run_id, owner_token="o" * 32, lease_seconds=1000)
    return await store.save_resume_point(scope, point(clock, scope, run_id, text=text))


def retained_count(store):
    if isinstance(store, SQLiteSessionStore):
        return store._connection.execute("SELECT COUNT(*) FROM session_run_resume_points").fetchone()[0]
    return len(store._resume_points)


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
@pytest.mark.parametrize("operation", ["read", "write"])
def test_expired_points_are_physically_reclaimed_without_releasing_claims(tmp_path, kind, operation):
    async def check():
        clock = Clock()
        async with opened(tmp_path, kind, clock) as store:
            await save(store, clock, identity(), "expired")
            await save(store, clock, identity("other"), "also-expired")
            await store.request_stop(identity(), run_id="expired")
            clock.advance(300)
            if operation == "read":
                assert await store.get_resume_point(identity(), run_id="unknown") is None
                assert retained_count(store) == 0
            else:
                assert await save(store, clock, identity(), "fresh") is not None
                assert retained_count(store) == 1
            assert await store.claim_run(identity(), run_id="expired", owner_token="n" * 32,
                                         lease_seconds=1000) is SessionRunClaimState.ACTIVE
            assert await store.stop_requested(identity(), run_id="expired") is True
    asyncio.run(check())


def test_sqlite_reopen_reclaims_all_expired_recovery_rows(tmp_path):
    async def check():
        clock = Clock()
        async with opened(tmp_path, "sqlite", clock) as store:
            await save(store, clock, identity(), "expired")
        clock.advance(300)
        async with opened(tmp_path, "sqlite", clock) as reopened:
            assert retained_count(reopened) == 0
            assert await reopened.request_stop(identity(), run_id="expired") is RunStopOutcome.REQUESTED
    asyncio.run(check())


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
@pytest.mark.parametrize("event_type,status,remaining", [
    (TranscriptEventType.ASSISTANT_MESSAGE, "succeeded", False),
    (TranscriptEventType.ERROR, "failed", False),
    (TranscriptEventType.ERROR, "timed_out", False),
    (TranscriptEventType.ERROR, "budget_exceeded", False),
    (TranscriptEventType.CANCEL, "cancelled", True),
    (TranscriptEventType.ERROR, "cancelled", True),
])
def test_terminal_append_discards_only_finished_run_recovery(tmp_path, kind, event_type, status, remaining):
    async def check():
        clock = Clock()
        async with opened(tmp_path, kind, clock) as store:
            await save(store, clock, identity(), "run")
            await save(store, clock, identity("other"), "run")
            event = await store.append(identity(), event_id="terminal", event_type=event_type,
                                       payload={"run_id": "run", "run_status": status},
                                       idempotency_key="terminal")
            assert (await store.get_resume_point(identity(), run_id="run") is not None) is remaining
            assert await store.get_resume_point(identity("other"), run_id="run") is not None
            assert await store.append(identity(), event_id="terminal", event_type=event_type,
                                       payload={"run_id": "run", "run_status": status},
                                       idempotency_key="terminal") == event
            assert await store.claim_run(identity(), run_id="run", owner_token="n" * 32,
                                         lease_seconds=1000) is SessionRunClaimState.ACTIVE
    asyncio.run(check())


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
@pytest.mark.parametrize("budget,expected_count", [
    ("session-count", 2), ("session-bytes", 1), ("total-count", 2), ("total-bytes", 1),
])
def test_resume_cache_evicts_oldest_points_to_independent_count_and_byte_budgets(tmp_path, kind, budget, expected_count):
    async def check():
        assert "max_resume_points_per_session" in SessionStoreLimits.model_fields
        settings = {"max_resume_point_bytes": 2048}
        if budget == "session-count":
            settings["max_resume_points_per_session"] = 2
        elif budget == "session-bytes":
            settings["max_resume_bytes_per_session"] = 2500
        elif budget == "total-count":
            settings.update(max_resume_points_per_session=2, max_resume_points_total=2)
        else:
            settings.update(max_resume_bytes_per_session=3000, max_resume_bytes_total=3000)
        # Ordinary session checkpoints have a separate, deliberately tiny budget.
        limits = SessionStoreLimits(max_checkpoint_state_bytes=1024,
                                    max_checkpoint_bytes_per_session=1024, **settings)
        clock = Clock()
        async with opened(tmp_path, kind, clock, limits) as store:
            for number in range(3):
                scope = identity(str(number)) if budget.startswith("total") else identity()
                assert await save(store, clock, scope, f"run-{number}", text="x" * 1000) is not None
                clock.advance(1)
            assert retained_count(store) == expected_count
            last_scope = identity("2") if budget.startswith("total") else identity()
            assert await store.get_resume_point(last_scope, run_id="run-2") is not None
            first_scope = identity("0") if budget.startswith("total") else identity()
            assert await store.get_resume_point(first_scope, run_id="run-0") is None
            assert await store.claim_run(first_scope, run_id="run-0", owner_token="n" * 32,
                                         lease_seconds=1000) is SessionRunClaimState.ACTIVE
    asyncio.run(check())


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_resume_point_limit_measures_utf8_envelope_without_destroying_prior(tmp_path, kind):
    async def check():
        assert "max_resume_point_bytes" in SessionStoreLimits.model_fields
        clock = Clock()
        limits = SessionStoreLimits(max_resume_point_bytes=2048)
        async with opened(tmp_path, kind, clock, limits) as store:
            prior = await save(store, clock, identity(), "run")
            assert prior is not None
            oversized = point(clock, identity(), "run", text="界" * 700)
            assert await store.save_resume_point(identity(), oversized) is None
            assert await store.get_resume_point(identity(), run_id="run") == prior
    asyncio.run(check())


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_sequence_pages_reach_old_history_without_duplicates_or_cross_identity(tmp_path, kind):
    async def check():
        clock = Clock()
        async with opened(tmp_path, kind, clock) as store:
            assert callable(getattr(store, "transcript_page", None))
            await store.create(identity())
            await store.create(identity("other"))
            for number in range(1005):
                await store.append(identity(), event_id=f"event-{number}",
                                   event_type=TranscriptEventType.USER_MESSAGE, payload={"text": str(number)})
            page = await store.transcript_page(identity(), limit=1000)
            assert [item.sequence for item in page] == list(range(6, 1006))
            older = await store.transcript_page(identity(), limit=1000, before_sequence=page[0].sequence)
            assert [item.sequence for item in older] == [1, 2, 3, 4, 5]
            assert await store.transcript_page(identity(), limit=10, before_sequence=1) == ()
            assert await store.transcript_page(identity("other"), limit=10) == ()
            with pytest.raises(SessionNotFoundError):
                await store.transcript_page(identity("missing"), limit=10)
            older[0].payload["text"] = "changed"
            assert (await store.transcript_page(identity(), limit=1, before_sequence=2))[0].payload == {"text": "0"}
            for cursor in (0, -1, True, "2"):
                with pytest.raises(ValueError):
                    await store.transcript_page(identity(), limit=10, before_sequence=cursor)
    asyncio.run(check())


def test_legacy_store_explicitly_reports_missing_pagination_support(tmp_path):
    async def check():
        store = InMemorySessionStore()
        await store.create(identity())
        assert callable(getattr(SessionStore, "transcript_page", None))
        with pytest.raises(NotImplementedError):
            await SessionStore.transcript_page(store, identity(), limit=10)
        with pytest.raises(NotImplementedError):
            await SessionStore.transcript_page(store, identity(), limit=10, before_sequence=10)
    asyncio.run(check())


def test_sqlite_old_resume_schema_is_backfilled_without_losing_live_points(tmp_path):
    async def check():
        clock = Clock()
        async with opened(tmp_path, "sqlite", clock) as store:
            original = await save(store, clock, identity(), "run", text="中文证据")
        with sqlite3.connect(tmp_path / "sessions.sqlite3") as legacy:
            legacy.execute("ALTER TABLE session_run_resume_points DROP COLUMN point_bytes")
        async with opened(tmp_path, "sqlite", clock) as upgraded:
            assert await upgraded.get_resume_point(identity(), run_id="run") == original
            row = upgraded._connection.execute(
                "SELECT point_bytes, LENGTH(CAST(point_json AS BLOB)) FROM session_run_resume_points"
            ).fetchone()
            assert row[0] == row[1] and row[0] > 0
    asyncio.run(check())


def test_two_sqlite_writers_share_one_atomic_total_resume_budget(tmp_path):
    clock = Clock()
    limits = SessionStoreLimits(max_resume_points_per_session=1, max_resume_points_total=1)
    barrier = threading.Barrier(2)

    def writer(seller):
        async def check():
            async with opened(tmp_path, "sqlite", clock, limits) as store:
                scope = identity(seller)
                await store.create(scope)
                for number in range(4):
                    run_id = f"run-{number}"
                    await store.claim_run(scope, run_id=run_id, owner_token="o" * 32, lease_seconds=1000)
                    barrier.wait(timeout=5)
                    assert await store.save_resume_point(scope, point(clock, scope, run_id)) is not None
                    assert retained_count(store) == 1
        asyncio.run(check())

    # Initialize WAL/schema before exercising two independent writers.
    async def initialize():
        async with opened(tmp_path, "sqlite", clock, limits):
            pass
    asyncio.run(initialize())
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(writer, seller) for seller in ("seller-a", "seller-b")]
        for future in futures:
            future.result(timeout=20)

    async def verify():
        async with opened(tmp_path, "sqlite", clock, limits) as store:
            assert retained_count(store) == 1
            assert store._connection.execute("SELECT COUNT(*) FROM session_run_claims").fetchone()[0] == 8
    asyncio.run(verify())


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_resume_observation_arguments_remain_subject_to_credential_filter(tmp_path, kind):
    async def check():
        clock = Clock()
        async with opened(tmp_path, kind, clock) as store:
            await save(store, clock, identity(), "run")
            value = point(clock, identity(), "run").model_dump(mode="json")
            state = value["payload"]["resume_state"]
            state.update(workflow_state={"step": "read"}, iterations_used=1, tool_calls_used=1,
                         call_ids=["call"], observations=[{
                             "call_id": "call", "tool_name": "suiteharness.fs.read",
                             "tool_identity": {"namespace": "suiteharness", "name": "suiteharness.fs.read",
                                               "origin": "suiteharness.builtin.fs", "version": "1"},
                             "status": "succeeded", "result": {"text": "evidence"}, "failure": None,
                             "started_at": clock().isoformat(), "finished_at": clock().isoformat(),
                             "arguments": {"path": "notes.txt"},
                         }])
            checked = RunResumePoint.model_validate_json(json.dumps(value))
            assert await store.save_resume_point(identity(), checked) is not None
            state["observations"][0]["arguments"]["nested"] = {"access_token": "unsafe"}
            unsafe = RunResumePoint.model_validate_json(json.dumps(value))
            assert await store.save_resume_point(identity(), unsafe) is None
            assert await store.get_resume_point(identity(), run_id="run") == checked
    asyncio.run(check())
