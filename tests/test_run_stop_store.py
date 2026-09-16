import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from suiteharness.sessions import (
    InMemorySessionStore,
    RunStopOutcome,
    SessionIdentity,
    SessionRunClaimState,
    SQLiteSessionStore,
)

OWNER_TOKEN = "o" * 32


def identity(*, principal_id="owner"):
    return SessionIdentity(
        tenant_id="company",
        product_id="product",
        agent_id="agent",
        session_id="session",
        principal_id=principal_id,
    )


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 9, 16, tzinfo=UTC)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def test_sqlite_stop_request_is_visible_to_a_second_connection(tmp_path):
    async def check():
        database = tmp_path / "sessions.sqlite"
        writer = SQLiteSessionStore(database)
        reader = SQLiteSessionStore(database)
        session = identity()
        try:
            await writer.create(session)
            assert await writer.claim_run(
                session, run_id="run-visible", owner_token=OWNER_TOKEN, lease_seconds=60
            ) is SessionRunClaimState.ACQUIRED

            assert await reader.request_stop(
                session, run_id="run-visible"
            ) is RunStopOutcome.REQUESTED
            assert await writer.stop_requested(session, run_id="run-visible") is True
        finally:
            await reader.close()
            await writer.close()

    asyncio.run(check())


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_stop_request_is_idempotent_and_rejects_another_identity(tmp_path, store_kind):
    async def check():
        store = (
            InMemorySessionStore()
            if store_kind == "memory"
            else SQLiteSessionStore(tmp_path / "sessions.sqlite")
        )
        owner = identity()
        intruder = identity(principal_id="intruder")
        try:
            await store.create(owner)
            await store.create(intruder)
            assert await store.claim_run(
                owner, run_id="run-owned", owner_token=OWNER_TOKEN, lease_seconds=60
            ) is SessionRunClaimState.ACQUIRED

            assert await store.request_stop(
                intruder, run_id="run-owned"
            ) is RunStopOutcome.NOT_FOUND
            assert await store.stop_requested(intruder, run_id="run-owned") is False
            assert await store.request_stop(
                owner, run_id="run-owned"
            ) is RunStopOutcome.REQUESTED
            assert await store.request_stop(
                owner, run_id="run-owned"
            ) is RunStopOutcome.ALREADY_REQUESTED
        finally:
            if isinstance(store, SQLiteSessionStore):
                await store.close()

    asyncio.run(check())


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_stop_request_rejects_finalizing_and_expired_claims(tmp_path, store_kind):
    async def check():
        clock = MutableClock()
        store = (
            InMemorySessionStore(clock=clock)
            if store_kind == "memory"
            else SQLiteSessionStore(tmp_path / "sessions.sqlite", clock=clock)
        )
        session = identity()
        try:
            await store.create(session)
            assert await store.claim_run(
                session, run_id="run-finalizing", owner_token=OWNER_TOKEN, lease_seconds=60
            ) is SessionRunClaimState.ACQUIRED
            assert await store.finalize_run(
                session, run_id="run-finalizing", owner_token=OWNER_TOKEN, lease_seconds=60
            ) is True
            assert await store.request_stop(
                session, run_id="run-finalizing"
            ) is RunStopOutcome.ALREADY_FINISHED

            assert await store.claim_run(
                session, run_id="run-expired", owner_token=OWNER_TOKEN, lease_seconds=1
            ) is SessionRunClaimState.ACQUIRED
            clock.advance(2)
            assert await store.request_stop(
                session, run_id="run-expired"
            ) is RunStopOutcome.ALREADY_FINISHED
        finally:
            if isinstance(store, SQLiteSessionStore):
                await store.close()

    asyncio.run(check())


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_run_stop_queries_recheck_expiry_after_waiting_for_the_store_lock(tmp_path, store_kind):
    async def check():
        clock = MutableClock()
        store = (
            InMemorySessionStore(clock=clock)
            if store_kind == "memory"
            else SQLiteSessionStore(tmp_path / "sessions.sqlite", clock=clock)
        )
        session = identity()
        try:
            await store.create(session)
            for run_id in ("run-stop", "run-list"):
                assert await store.claim_run(
                    session, run_id=run_id, owner_token=OWNER_TOKEN, lease_seconds=1
                ) is SessionRunClaimState.ACQUIRED

            async with store._lock:
                stop_task = asyncio.create_task(store.request_stop(session, run_id="run-stop"))
                active_task = asyncio.create_task(store.active_run_ids(session))
                await asyncio.sleep(0)
                clock.advance(2)

            assert await stop_task is RunStopOutcome.ALREADY_FINISHED
            assert await active_task == ()
        finally:
            if isinstance(store, SQLiteSessionStore):
                await store.close()

    asyncio.run(check())


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_active_run_ids_returns_only_unexpired_active_claims_for_its_identity(tmp_path, store_kind):
    async def check():
        clock = MutableClock()
        store = (
            InMemorySessionStore(clock=clock)
            if store_kind == "memory"
            else SQLiteSessionStore(tmp_path / "sessions.sqlite", clock=clock)
        )
        owner = identity()
        other = identity(principal_id="other")
        try:
            await store.create(owner)
            await store.create(other)
            for run_id, lease_seconds in (("run-active-1", 60), ("run-finalizing", 60), ("run-expired", 1)):
                assert await store.claim_run(
                    owner, run_id=run_id, owner_token=OWNER_TOKEN, lease_seconds=lease_seconds
                ) is SessionRunClaimState.ACQUIRED
            assert await store.finalize_run(
                owner, run_id="run-finalizing", owner_token=OWNER_TOKEN, lease_seconds=60
            ) is True
            assert await store.claim_run(
                other, run_id="run-other", owner_token=OWNER_TOKEN, lease_seconds=60
            ) is SessionRunClaimState.ACQUIRED

            clock.advance(2)
            assert await store.claim_run(
                owner, run_id="run-active-2", owner_token=OWNER_TOKEN, lease_seconds=60
            ) is SessionRunClaimState.ACQUIRED

            assert await store.active_run_ids(owner) == ("run-active-1", "run-active-2")
            assert await store.active_run_ids(owner, limit=1) == ("run-active-1",)
            assert await store.active_run_ids(other) == ("run-other",)
        finally:
            if isinstance(store, SQLiteSessionStore):
                await store.close()

    asyncio.run(check())
