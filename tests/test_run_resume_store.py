"""Resume records must not cross identity, expiry, or incomplete-state boundaries."""

import asyncio
import copy
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from suiteharness import sessions
from suiteharness.sessions import InMemorySessionStore, SessionIdentity, SQLiteSessionStore


class Clock:
    def __init__(self):
        self.value = datetime(2026, 9, 16, tzinfo=UTC)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def identity(**changes):
    return SessionIdentity(
        **dict(
            tenant_id="tenant",
            product_id="product",
            agent_id="agent",
            session_id="customer",
            principal_id="seller",
        )
        | changes
    )


def payload():
    return {
        "message_digest": "a" * 64,
        "input": "inspect customer",
        "conversation": {"messages": [{"role": "user", "content": "previous question"}]},
        "resume_state": {
            "source_run_id": "old-run",
            "workflow_state": {"step": "complete"},
            "observations": [
                {
                    "call_id": "read-1",
                    "tool_name": "suiteharness.fs.read",
                    "tool_identity": {
                        "namespace": "suiteharness",
                        "name": "suiteharness.fs.read",
                        "origin": "suiteharness.builtin.fs",
                        "version": "1",
                    },
                    "status": "succeeded",
                    "result": {"text": "complete evidence"},
                    "failure": None,
                    "started_at": "2026-09-16T00:00:00Z",
                    "finished_at": "2026-09-16T00:00:00Z",
                }
            ],
            "iterations_used": 1,
            "tool_calls_used": 1,
            "call_ids": ["read-1"],
            "prompt_digest": "b" * 64,
            "route_key": "chat-route",
        },
    }


def point(clock, *, data=None, **changes):
    # getattr makes the first RED a missing public API assertion, not an import failure.
    point_type = getattr(sessions, "RunResumePoint", None)
    assert point_type is not None, "sessions must export RunResumePoint"
    return point_type(
        **dict(
            identity=identity(),
            run_id="old-run",
            payload=payload() if data is None else data,
            memory_invalidated_revision=0,
            memory_source_versions={},
            created_at=clock(),
        )
        | changes
    )


@asynccontextmanager
async def opened(tmp_path, kind, clock, *, lease=1000):
    store = (
        InMemorySessionStore(clock=clock)
        if kind == "memory"
        else SQLiteSessionStore(tmp_path / "resume.sqlite", clock=clock)
    )
    try:
        await store.create(identity())
        await store.claim_run(
            identity(), run_id="old-run", owner_token="o" * 32, lease_seconds=lease
        )
        yield store
    finally:
        if isinstance(store, SQLiteSessionStore):
            await store.close()


def test_sqlite_reopen_preserves_complete_state_and_source_watermarks(tmp_path):
    async def check():
        clock = Clock()
        async with opened(tmp_path, "sqlite", clock) as store:
            event = await store.append(
                identity(),
                event_id="source",
                event_type=sessions.TranscriptEventType.MEMORY_INVALIDATED,
                payload={"source_namespace": "crm", "source_version": 4},
            )
            saved = point(
                clock, memory_invalidated_revision=event.revision, memory_source_versions={"crm": 4}
            )
            assert await store.save_resume_point(identity(), saved) == saved
        reopened = SQLiteSessionStore(tmp_path / "resume.sqlite", clock=clock)
        try:
            restored = await reopened.get_resume_point(identity(), run_id="old-run")
            assert restored == saved
            assert restored.payload["resume_state"]["observations"][0]["result"] == {
                "text": "complete evidence"
            }
            assert restored.payload["input"] == "inspect customer"
            assert restored.payload["conversation"]["messages"][0]["content"] == "previous question"
        finally:
            await reopened.close()

    asyncio.run(check())


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_exact_identity_run_and_copy_isolation(tmp_path, kind):
    async def check():
        clock = Clock()
        async with opened(tmp_path, kind, clock) as store:
            original = point(clock)
            saved = await store.save_resume_point(identity(), original)
            assert saved == original
            for field in ("tenant_id", "product_id", "agent_id", "session_id", "principal_id"):
                wrong = identity(**{field: "other"})
                assert await store.get_resume_point(wrong, run_id="old-run") is None
                assert await store.save_resume_point(wrong, original) is None
            assert await store.get_resume_point(identity(), run_id="other-run") is None
            original.payload["input"] = "mutated"
            saved.payload["input"] = "also mutated"
            restored = await store.get_resume_point(identity(), run_id="old-run")
            assert restored.payload["input"] == "inspect customer"
            restored.payload["input"] = "third mutation"
            assert (await store.get_resume_point(identity(), run_id="old-run")).payload[
                "input"
            ] == "inspect customer"

    asyncio.run(check())


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
@pytest.mark.parametrize(
    "condition", ["stopped", "expired", "finalizing", "missing", "stale-source", "stale-version"]
)
def test_save_requires_live_unstopped_claim_and_current_sources(tmp_path, kind, condition):
    async def check():
        clock = Clock()
        async with opened(tmp_path, kind, clock, lease=1) as store:
            item = point(clock)
            if condition == "stopped":
                await store.request_stop(identity(), run_id="old-run")
            elif condition == "expired":
                clock.advance(1)
            elif condition == "finalizing":
                await store.finalize_run(
                    identity(), run_id="old-run", owner_token="o" * 32, lease_seconds=60
                )
            elif condition == "missing":
                item = item.model_copy(update={"run_id": "absent"})
                item.payload["resume_state"]["source_run_id"] = "absent"
            elif condition == "stale-source":
                await store.append(
                    identity(),
                    event_id="invalidated",
                    event_type=sessions.TranscriptEventType.MEMORY_INVALIDATED,
                )
            else:
                item = item.model_copy(update={"memory_source_versions": {"crm": 2}})
            assert await store.save_resume_point(identity(), item) is None
            assert await store.get_resume_point(identity(), run_id=item.run_id) is None

    asyncio.run(check())


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
@pytest.mark.parametrize(
    "mutation",
    [
        "oversize",
        "approval_token",
        "approval",
        "token",
        "Cookie",
        "access_token",
        "api_key",
        "Authorization",
        "password",
        "result_id",
        "result_reference",
        "missing-input",
        "missing-context",
        "missing-digest",
        "missing-state",
        "wrong-run",
        "incomplete",
        "failed",
        "delegate",
        "bad-identity",
        "nan",
    ],
)
def test_unsafe_payload_returns_none_without_changing_prior_point(tmp_path, kind, mutation):
    async def check():
        clock = Clock()
        async with opened(tmp_path, kind, clock) as store:
            prior = point(clock)
            assert await store.save_resume_point(identity(), prior) == prior
            data = payload()
            if mutation == "oversize":
                data["input"] = "界" * (180 * 1024)
            elif mutation.startswith("missing-"):
                data.pop(
                    {
                        "missing-input": "input",
                        "missing-context": "conversation",
                        "missing-digest": "message_digest",
                        "missing-state": "resume_state",
                    }[mutation]
                )
            elif mutation == "wrong-run":
                data["resume_state"]["source_run_id"] = "wrong"
            elif mutation == "incomplete":
                data["resume_state"]["tool_calls_used"] = 2
            elif mutation == "failed":
                data["resume_state"]["observations"][0]["status"] = "failed"
            elif mutation == "delegate":
                data["resume_state"]["observations"][0]["tool_name"] = "agent.delegate"
            elif mutation == "bad-identity":
                data["resume_state"]["observations"][0]["tool_identity"]["origin"] = "plugin.fs"
            elif mutation == "nan":
                data["input"] = float("nan")
            else:
                data["resume_state"]["observations"][0]["result"] = {
                    "nested": [{mutation: "unsafe"}]
                }
            unsafe = prior.model_copy(update={"payload": data})
            assert await store.save_resume_point(identity(), unsafe) is None
            assert await store.get_resume_point(identity(), run_id="old-run") == prior

    asyncio.run(check())


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_initial_runner_state_roundtrips_and_future_points_are_rejected(tmp_path, kind):
    from suiteharness.execution.models import RunResumeState

    async def check():
        clock = Clock()
        async with opened(tmp_path, kind, clock) as store:
            state = RunResumeState(
                source_run_id="old-run",
                workflow_state=None,
                iterations_used=0,
                tool_calls_used=0,
                prompt_digest="c" * 64,
                route_key="chat",
            )
            data = payload()
            data["resume_state"] = state.model_dump(mode="json")
            initial = point(clock, data=data)
            assert await store.save_resume_point(identity(), initial) == initial
            restored = await store.get_resume_point(identity(), run_id="old-run")
            assert restored.payload["resume_state"]["observations"] == []
            assert restored.payload["resume_state"]["workflow_state"] is None
            future = initial.model_copy(update={"created_at": clock() + timedelta(seconds=1)})
            assert await store.save_resume_point(identity(), future) is None

    asyncio.run(check())


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_only_latest_complete_point_is_readable_until_ttl(tmp_path, kind):
    async def check():
        clock = Clock()
        async with opened(tmp_path, kind, clock) as store:
            first = point(clock)
            await store.save_resume_point(identity(), first)
            clock.advance(1)
            data = copy.deepcopy(payload())
            data["resume_state"]["workflow_state"] = {"step": "new complete"}
            latest = point(clock, data=data)
            assert await store.save_resume_point(identity(), latest) == latest
            assert await store.save_resume_point(identity(), first) is None
            await store.request_stop(identity(), run_id="old-run")
            assert await store.get_resume_point(identity(), run_id="old-run") == latest
            clock.advance(299)
            assert await store.get_resume_point(identity(), run_id="old-run") == latest
            clock.advance(1)
            assert await store.get_resume_point(identity(), run_id="old-run") is None

    asyncio.run(check())


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
@pytest.mark.parametrize("operation", ["save-claim", "save-ttl", "get-ttl"])
def test_expiry_is_checked_after_waiting_for_store_lock(tmp_path, kind, operation):
    async def check():
        clock = Clock()
        async with opened(
            tmp_path, kind, clock, lease=1 if operation == "save-claim" else 1000
        ) as store:
            item = point(clock)
            if operation == "get-ttl":
                await store.save_resume_point(identity(), item)
            async with store._lock:
                pending = asyncio.create_task(
                    store.get_resume_point(identity(), run_id="old-run")
                    if operation == "get-ttl"
                    else store.save_resume_point(identity(), item)
                )
                await asyncio.sleep(0)
                clock.advance(1 if operation == "save-claim" else 300)
            assert await pending is None

    asyncio.run(check())


@pytest.mark.parametrize("change", ["expiry", "stop"])
def test_sqlite_rechecks_after_waiting_for_database_write_lock(tmp_path, change):
    async def check():
        clock = Clock()
        async with opened(tmp_path, "sqlite", clock, lease=1) as store:
            item = point(clock)
            blocker = SQLiteSessionStore(tmp_path / "resume.sqlite", clock=clock)
            blocker._connection.execute("BEGIN IMMEDIATE")
            entered = threading.Event()
            store._connection.set_trace_callback(
                lambda sql: entered.set() if sql == "BEGIN IMMEDIATE" else None
            )

            def release():
                assert entered.wait(5)
                if change == "expiry":
                    clock.advance(1)
                else:
                    blocker._connection.execute(
                        "INSERT INTO session_run_stops VALUES (?, ?, ?, ?, ?, ?, ?)",
                        ("tenant", "product", "agent", "customer", "seller", "old-run", 1),
                    )
                blocker._connection.commit()

            thread = threading.Thread(target=release)
            thread.start()
            try:
                assert await store.save_resume_point(identity(), item) is None
            finally:
                thread.join(timeout=5)
                await blocker.close()

    asyncio.run(check())
