"""Contracts and opt-in PostgreSQL integration checks for the foundation stores."""
from __future__ import annotations

import asyncio
import importlib
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

_CREATED_DATABASES = []


@pytest.fixture(autouse=True)
def cleanup_owned_integration_schemas():
    yield
    while _CREATED_DATABASES:
        database = _CREATED_DATABASES.pop()
        asyncio.run(database.close())
        from psycopg import sql
        # Only the fresh UUID schema allocated by this test may be removed.
        assert database.schema.startswith("pgtest_foundation_")
        with database._connect() as connection:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(database.schema)))


def pg():
    return importlib.import_module("suiteharness.persistence.postgres")


def integration_database():
    dsn = os.environ.get("SUITEHARNESS_TEST_POSTGRES_DSN") or os.environ.get("SUITE_POSTGRES_TEST_DSN")
    if not dsn:
        pytest.skip("set SUITE_POSTGRES_TEST_DSN for a dedicated integration database")
    database = pg().PostgresDatabase(dsn, schema="pgtest_foundation_" + uuid4().hex[:16])
    _CREATED_DATABASES.append(database)
    return database


def test_postgres_backend_rejects_non_postgresql_dsn_without_sqlite_fallback(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("PostgreSQL must never create a SQLite connection")
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    with pytest.raises(ValueError, match="PostgreSQL"):
        pg().PostgresDatabase("sqlite:///tmp/fallback.db")


def test_schema_name_is_validated_before_connecting():
    with pytest.raises(ValueError, match="schema"):
        pg().PostgresDatabase("postgresql://localhost/test", schema="public; DROP DATABASE x")


def test_sql_parameters_do_not_rewrite_literal_question_marks():
    sql = pg().postgres_statement("SELECT '?' AS marker, ? AS value, '100%' AS ratio")
    assert sql == "SELECT '?' AS marker, %s AS value, '100%%' AS ratio"


def test_store_constructor_is_io_free_but_operations_require_open():
    database = pg().PostgresDatabase("postgresql://localhost/test")
    store = pg().PostgresProductStateStore(database)
    from suiteharness.persistence.models import ProductStateKey
    with pytest.raises(RuntimeError, match="open|migrat|schema"):
        asyncio.run(store.get(ProductStateKey(tenant_id="t", product_id="p", namespace="test", key="k")))


def test_migration_catalog_contains_bigint_epoch_columns_and_clear_receipts():
    catalog = pg().postgres_migrations()
    assert catalog["sessions"][0] == 1
    sql = "\n".join(statement for _, statements in catalog.values() for statement in statements)
    assert "session_clear_receipts" in sql
    assert "expires_at_us BIGINT" in sql
    assert "AUTOINCREMENT" not in sql
    assert "memory_invalidated_revision" in sql


def test_postgres_session_revision_clear_and_replay(monkeypatch):
    from suiteharness.sessions.models import SessionIdentity, TranscriptEventType
    from suiteharness.sessions.postgres import PostgresSessionStore
    from suiteharness.sessions.errors import SessionIdempotencyConflictError

    async def scenario():
        database = integration_database()
        await database.migrate()
        await database.open()
        store = PostgresSessionStore(database)
        identity = SessionIdentity(tenant_id="test", product_id="test", agent_id="a",
                                   session_id="clear-replay", principal_id="p")
        await store.create(identity)
        event = await store.append(identity, event_id="e1", event_type=TranscriptEventType.USER_MESSAGE,
                                   payload={"text": "confidential"}, idempotency_key="delivery-1")
        checkpoint = await store.save_checkpoint(identity, checkpoint_id="c1", workflow_state={"secret": "memory"},
                                                 expected_revision=event.revision, idempotency_key="checkpoint-1")
        assert checkpoint.revision == event.revision + 1
        receipt = await store.clear_conversation(identity, idempotency_key="clear-1")
        await store.finish_clear(identity, idempotency_key="clear-1")
        replay = await store.append(identity, event_id="e1", event_type=TranscriptEventType.USER_MESSAGE,
                                    payload={"text": "confidential"}, idempotency_key="delivery-1")
        assert replay.payload is None
        with pytest.raises(SessionIdempotencyConflictError):
            await store.append(identity, event_id="e1", event_type=TranscriptEventType.USER_MESSAGE,
                               payload={"text": "different"}, idempotency_key="delivery-1")
        assert receipt.revision == checkpoint.revision + 1
        assert (await store.get(identity)).memory_invalidated_revision == receipt.revision
        await store.close()
        await database.healthcheck()
        await database.close()
    asyncio.run(scenario())


def test_postgres_state_cas_scope_and_lease_ownership():
    from suiteharness.persistence.models import ProductStateKey, ChannelEventScope
    from suiteharness.persistence.errors import StateRevisionConflictError, StateIdempotencyConflictError

    async def scenario():
        database = integration_database()
        await database.migrate()
        await database.open()
        store = pg().PostgresProductStateStore(database)
        key = ProductStateKey(tenant_id="t", product_id="p", namespace="plugin.state", key="k")
        first = await store.compare_and_set(key, {"value": 1}, expected_revision=0, idempotency_key="state-1")
        replay = await store.compare_and_set(key, {"value": 1}, expected_revision=0, idempotency_key="state-1")
        assert first.record.revision == replay.record.revision == 1 and replay.replayed
        with pytest.raises(StateRevisionConflictError):
            await store.compare_and_set(key, 2, expected_revision=0, idempotency_key="state-2")
        with pytest.raises(StateIdempotencyConflictError):
            await store.compare_and_set(key, 2, expected_revision=0, idempotency_key="state-1")
        assert await store.get(key.model_copy(update={"product_id": "other"})) is None
        clock = [datetime.now(UTC)]
        dedupe = pg().PostgresChannelEventDeduplicator(database, scope=ChannelEventScope.product("t", "p", "f"),
                                                       processing_lease_seconds=1, clock=lambda: clock[0])
        assert await dedupe.claim("event")
        await dedupe.release("event")
        assert await dedupe.claim("event")
        await dedupe.complete("event")
        assert not await dedupe.claim("event")
        await database.close()
    asyncio.run(scenario())


def test_postgres_tool_results_scope_quota_and_clear_tombstones(monkeypatch):
    from suiteharness.execution.postgres import PostgresToolResultStore
    from suiteharness.runtime import RequestScope, ScopePath
    def scope(principal="p", session="s"):
        return RequestScope(path=ScopePath.agent("t", "p", "a", session), principal_id=principal)
    async def scenario():
        database = integration_database()
        await database.migrate()
        await database.open()
        def forbidden(*args, **kwargs):
            pytest.fail("PostgreSQL operations must not touch SQLite")
        monkeypatch.setattr(sqlite3, "connect", forbidden)
        results = PostgresToolResultStore(database, max_total_bytes=30)
        reference = await results.put(scope(), "run", "中文" * 4)
        first = await results.read(scope(), "run", reference["result_id"], limit=3)
        second = await results.read(scope(), "run", reference["result_id"], offset=first["next_offset"], limit=20)
        assert first["content"] + second["content"] == "中文" * 4
        for principal, session, run in (("other", "s", "run"), ("p", "other", "run"), ("p", "s", "other")):
            with pytest.raises(LookupError):
                await results.read(scope(principal, session), run, reference["result_id"])
        with pytest.raises(ValueError, match="quota"):
            await results.put(scope(), "run", "1234567")
        assert await results.clear_runs(scope(), ("run",)) == 1
        with pytest.raises(LookupError):
            await PostgresToolResultStore(database).put(scope(), "run", "late result")
        with pytest.raises(LookupError):
            await results.read(scope(), "run", reference["result_id"])
        await database.close()
    asyncio.run(scenario())


def test_postgres_session_concurrent_revision_and_lease_owner():
    from suiteharness.sessions.models import SessionIdentity, TranscriptEventType, SessionRunClaimState
    from suiteharness.sessions.postgres import PostgresSessionStore
    from suiteharness.sessions.errors import SessionRevisionConflictError
    async def scenario():
        database = integration_database()
        await database.migrate()
        await database.open()
        clock = [datetime.now(UTC)]
        store = PostgresSessionStore(database, clock=lambda: clock[0])
        other = PostgresSessionStore(database, clock=lambda: clock[0])
        identity = SessionIdentity(tenant_id="t", product_id="p", agent_id="a", session_id="s", principal_id="p")
        await store.create(identity)
        writes = await asyncio.gather(*[
            instance.append(identity, event_id="event-" + str(index), event_type=TranscriptEventType.USER_MESSAGE,
                            payload=index, expected_revision=0)
            for index, instance in enumerate((store, other))], return_exceptions=True)
        assert sum(isinstance(value, SessionRevisionConflictError) for value in writes) == 1
        assert (await store.get(identity)).revision == 1
        assert await store.claim_run(identity, run_id="run", owner_token="a" * 32, lease_seconds=1) == SessionRunClaimState.ACQUIRED
        assert await other.claim_run(identity, run_id="run", owner_token="b" * 32, lease_seconds=1) == SessionRunClaimState.ACTIVE
        assert not await other.finalize_run(identity, run_id="run", owner_token="b" * 32, lease_seconds=1)
        assert await store.finalize_run(identity, run_id="run", owner_token="a" * 32, lease_seconds=1)
        clock[0] += timedelta(seconds=2)
        assert await other.claim_run(identity, run_id="run", owner_token="b" * 32, lease_seconds=1) == SessionRunClaimState.STALE
        await database.close()
    asyncio.run(scenario())


def test_postgres_event_stale_owner_cannot_release_new_lease():
    from suiteharness.persistence.models import ChannelEventScope
    async def scenario():
        database = integration_database()
        await database.migrate()
        await database.open()
        clock = [datetime.now(UTC)]
        dedupe = pg().PostgresChannelEventDeduplicator(database, scope=ChannelEventScope.product("t", "p", "f"),
                                                       processing_lease_seconds=1, clock=lambda: clock[0])
        old_claimed, new_claimed, allow_old_release, allow_new_complete = (asyncio.Event() for _ in range(4))
        async def old_owner():
            assert await dedupe.claim("event")
            old_claimed.set()
            await allow_old_release.wait()
            await dedupe.release("event")
        async def new_owner():
            await old_claimed.wait()
            clock[0] += timedelta(seconds=2)
            assert await dedupe.claim("event")
            new_claimed.set()
            await allow_new_complete.wait()
            await dedupe.complete("event")
        first, second = asyncio.create_task(old_owner()), asyncio.create_task(new_owner())
        await new_claimed.wait()
        allow_old_release.set()
        await first
        assert not await dedupe.claim("event")
        allow_new_complete.set()
        await second
        assert not await dedupe.claim("event")
        await database.close()
    asyncio.run(scenario())


def test_postgres_pool_work_does_not_block_event_loop_and_write_rolls_back():
    async def scenario():
        database = integration_database()
        await database.migrate()
        await database.open()
        started = __import__("threading").Event()
        def slow(connection):
            started.set()
            connection.execute("SELECT pg_sleep(?)", (0.2,)).fetchone()
        pending = asyncio.create_task(database.run(slow))
        while not started.is_set():
            await asyncio.sleep(0.005)
        ticks = 0
        while not pending.done():
            await asyncio.sleep(0.01)
            ticks += 1
        await pending
        assert ticks >= 3
        def failed(connection):
            connection.execute("INSERT INTO cleared_result_owners VALUES (?)", ("rollback-owner",))
            raise ValueError("rollback")
        with pytest.raises(ValueError, match="rollback"):
            await database.run(failed)
        assert await database.run(lambda c: c.execute("SELECT COUNT(*) FROM cleared_result_owners WHERE owner=?", ("rollback-owner",)).fetchone()[0], write=False) == 0
        await database.close()
    asyncio.run(scenario())


def test_postgres_open_is_readonly_and_expected_database_guard():
    async def scenario():
        database = integration_database()
        with pytest.raises(Exception):
            await database.open()
        # Failed runtime startup must leave no schema or table behind.
        def exists():
            with database._connect() as connection:
                return connection.execute("SELECT COUNT(*) FROM pg_namespace WHERE nspname=%s", (database.schema,)).fetchone()[0]
        assert await asyncio.to_thread(exists) == 0
        wrong = pg().PostgresDatabase(database._dsn, schema=database.schema, expected_database="definitely_wrong_database")
        with pytest.raises(RuntimeError, match="unexpected database"):
            await wrong.migrate()
        assert await asyncio.to_thread(exists) == 0
        await database.close()
    asyncio.run(scenario())


def test_postgres_audit_duplicate_rollback_filter_and_pool_ownership():
    from suiteharness.execution.postgres import PostgresAuditJournal
    from suiteharness.execution.sqlite import AuditConflictError
    from suiteharness.execution.models import AuditEvent, AuditEventType
    async def scenario():
        database = integration_database()
        await database.migrate()
        await database.open()
        journal = PostgresAuditJournal(database)
        event = AuditEvent(event_id="e1", event_type=AuditEventType.RUN_STARTED, occurred_at=datetime.now(UTC),
                           tenant_id="t", product_id="p", principal_id="u", request_id="r", correlation_id="c",
                           purpose="test", run_id="run", data={"nested": {"text": "中文"}})
        await journal.append(event)
        with pytest.raises(AuditConflictError):
            await journal.append(event)
        await journal.append(event.model_copy(update={"event_id": "e2", "product_id": "other"}))
        assert [item.event_id for item in await journal.events(tenant_id="t", product_id="p")] == ["e1"]
        assert len(await journal.events(tenant_id="t")) == 2
        await journal.close()
        await database.healthcheck()
        await database.close()
    asyncio.run(scenario())


def test_postgres_mcp_cas_endpoint_scope_and_quarantine_owner():
    from suiteharness.mcp.models import HttpResumeState
    from suiteharness.persistence.errors import StateRevisionConflictError
    async def scenario():
        database = integration_database()
        await database.migrate()
        await database.open()
        state = pg().PostgresProductStateStore(database)
        resume = pg().PostgresMcpHttpResumeStore(state)
        params = dict(tenant_id="t", product_id="p", server_id="server", endpoint="https://example.test/mcp")
        assert await resume.load(**params) is None
        saved = await resume.save(**params, state=HttpResumeState(session_id="session", last_event_id="event"),
                                  expected_revision=0, idempotency_key="resume-1")
        loaded = await resume.load(**params)
        assert loaded.revision == saved.revision == 1
        assert loaded.state.session_id == "session"
        assert await resume.load(**{**params, "endpoint": "https://other.test/mcp"}) is None
        with pytest.raises(StateRevisionConflictError):
            await resume.save(**params, state=HttpResumeState(session_id="session2"), expected_revision=0,
                              idempotency_key="resume-2")
        quarantine = pg().PostgresSandboxQuarantineStore(database, deployment_id="test")
        container = "suiteharness-sandbox-" + "a" * 24
        assert await quarantine.claim(container, "active", owner_token="a" * 32)
        assert not await quarantine.claim(container, "active", owner_token="b" * 32)
        assert not await quarantine.clear(container, owner_token="b" * 32)
        assert (await quarantine.load())[container] == "active"
        assert await quarantine.clear(container, owner_token="a" * 32)
        assert await quarantine.load() == {}
        await quarantine.mark(container, "failed")
        assert await quarantine.clear(container)
        await database.close()
    asyncio.run(scenario())


def test_postgres_close_waits_for_inflight_atomic_write():
    import threading
    async def scenario():
        database = integration_database()
        await database.migrate()
        await database.open()
        started = threading.Event()
        def slow(connection):
            started.set()
            connection.execute("SELECT pg_sleep(?)", (0.2,)).fetchone()
            connection.execute("INSERT INTO cleared_result_owners VALUES (?)", ("inflight",))
        pending = asyncio.create_task(database.run(slow))
        while not started.is_set():
            await asyncio.sleep(0.005)
        closing = asyncio.create_task(database.close())
        await asyncio.sleep(0.02)
        assert not closing.done()
        await pending
        await closing
        def persisted():
            with database._connect() as connection:
                return connection.execute('SELECT COUNT(*) FROM "' + database.schema + '".cleared_result_owners WHERE owner=%s', ("inflight",)).fetchone()[0]
        assert await asyncio.to_thread(persisted) == 1
    asyncio.run(scenario())


def test_postgres_beijing_columns_match_existing_sqlite_virtual_semantics():
    from suiteharness.sessions.postgres import PostgresSessionStore
    from suiteharness.sessions.models import SessionIdentity, TranscriptEventType
    async def scenario():
        database = integration_database()
        await database.migrate()
        await database.open()
        store = PostgresSessionStore(database, clock=lambda: datetime(2026, 10, 20, 1, 2, 3, 999500, tzinfo=UTC))
        identity = SessionIdentity(tenant_id="t", product_id="p", agent_id="a", session_id="s", principal_id="u")
        await store.create(identity)
        result = await database.run(lambda c: c.execute("SELECT created_at_beijing FROM sessions").fetchone()[0], write=False)
        assert result == "2026-10-20 09:02:04+08:00"
        await store.append(identity, event_id="e", event_type=TranscriptEventType.USER_MESSAGE,
                           payload="test", occurred_at=datetime(2026, 10, 20, 2, 3, 4, tzinfo=UTC))
        result = await database.run(lambda c: c.execute("SELECT occurred_at_beijing FROM transcript_events").fetchone()[0], write=False)
        assert result == "2026-10-20 10:03:04+08:00"
        with pytest.raises(Exception):
            await database.run(lambda c: c.execute("UPDATE sessions SET created_at_beijing=?", ("forged",)))
        # Bad source dates yield NULL, as the old SQLite datetime() expression did.
        await database.run(lambda c: c.execute("UPDATE sessions SET created_at=?", ("not-a-date",)))
        assert await database.run(lambda c: c.execute("SELECT created_at_beijing FROM sessions").fetchone()[0], write=False) is None
        await database.close()
    asyncio.run(scenario())


def test_postgres_open_rejects_missing_tables_despite_version_metadata():
    from psycopg import sql
    from suiteharness.sessions.postgres import PostgresSessionStore
    async def scenario():
        database = integration_database()
        await database.migrate()
        with database._connect() as connection:
            connection.execute(sql.SQL("DROP TABLE {}.sessions CASCADE").format(sql.Identifier(database.schema)))
        PostgresSessionStore(database)
        with pytest.raises(RuntimeError, match="schema|table|migration"):
            await database.open()
        await database.close()
    asyncio.run(scenario())
