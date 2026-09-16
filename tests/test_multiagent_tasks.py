"""Durable delegation tasks remain owned, monotonic, and restart-safe."""

import asyncio
from dataclasses import FrozenInstanceError

import pytest

from suiteharness.multiagent.sqlite import SQLiteAgentTaskStore
from suiteharness.multiagent.tasks import AgentTaskIdempotencyConflictError, AgentTaskStatus
from suiteharness.persistence.database import SQLiteDatabase
from suiteharness.runtime.scopes import RequestScope, ScopePath


def scope(
    principal: str = "alice", product: str = "sales", tenant: str = "tenant"
) -> RequestScope:
    return RequestScope(ScopePath.product(tenant, product), principal)


def test_call_lookup_returns_persisted_terminal_record_after_reopen(tmp_path):
    asyncio.run(_call_lookup_returns_persisted_terminal_record_after_reopen(tmp_path))


async def _call_lookup_returns_persisted_terminal_record_after_reopen(tmp_path):
    path = tmp_path / "tasks.sqlite"
    database = SQLiteDatabase(path)
    store = SQLiteAgentTaskStore(database)
    task = await store.create(scope(), "run-a", "call-a", "search", "digest-a")
    await store.transition(
        scope(),
        task.task_id,
        AgentTaskStatus.QUEUED,
        AgentTaskStatus.RUNNING,
        parent_run_id="run-a",
    )
    finished = await store.transition(
        scope(),
        task.task_id,
        AgentTaskStatus.RUNNING,
        AgentTaskStatus.SUCCEEDED,
        parent_run_id="run-a",
        result="done",
    )
    await database.close()

    reopened = SQLiteDatabase(path)
    restarted = SQLiteAgentTaskStore(reopened)
    try:
        assert finished is not None
        assert await restarted.get_by_call(scope(), "run-a", "call-a") == finished
    finally:
        await reopened.close()


def test_call_lookup_uses_every_owner_and_call_key():
    asyncio.run(_call_lookup_uses_every_owner_and_call_key())


async def _call_lookup_uses_every_owner_and_call_key():
    database = SQLiteDatabase(":memory:")
    store = SQLiteAgentTaskStore(database)
    try:
        cases = (
            (scope(), "run-a", "call-a"),
            (scope(tenant="other"), "run-a", "call-a"),
            (scope(product="other"), "run-a", "call-a"),
            (scope(principal="bob"), "run-a", "call-a"),
            (scope(), "run-b", "call-a"),
            (scope(), "run-a", "call-b"),
        )
        created = [
            await store.create(owner, run_id, call_id, "search", "digest-a")
            for owner, run_id, call_id in cases
        ]
        for (owner, run_id, call_id), task in zip(cases, created, strict=True):
            assert await store.get_by_call(owner, run_id, call_id) == task
        assert await store.get_by_call(scope(), "run-a", "unknown") is None
        assert await store.get_by_call(scope(), "unknown", "call-a") is None
    finally:
        await database.close()


def test_create_reuses_only_the_same_scoped_call_and_payload():
    asyncio.run(_create_reuses_only_the_same_scoped_call_and_payload())


async def _create_reuses_only_the_same_scoped_call_and_payload():
    database = SQLiteDatabase(":memory:")
    store = SQLiteAgentTaskStore(database)
    try:
        first = await store.create(scope(), "run-a", "call-a", "search", "digest-a")
        same = await store.create(scope(), "run-a", "call-a", "search", "digest-a")
        assert same == first
        assert first.status is AgentTaskStatus.QUEUED
        assert first.child_run_id != first.parent_run_id
        assert first.child_agent_id != first.child_session_id
        with pytest.raises(FrozenInstanceError):
            first.status = AgentTaskStatus.RUNNING
        with pytest.raises(AgentTaskIdempotencyConflictError):
            await store.create(scope(), "run-a", "call-a", "other", "digest-a")
        with pytest.raises(AgentTaskIdempotencyConflictError):
            await store.create(scope(), "run-a", "call-a", "search", "digest-b")
        assert (
            await store.create(scope(), "run-a", "call-b", "search", "digest-a")
        ).task_id != first.task_id
        assert (
            await store.create(scope("bob"), "run-a", "call-a", "search", "digest-a")
        ).task_id != first.task_id
    finally:
        await database.close()


def test_task_lookup_and_transition_deny_foreign_scope_and_parent_run():
    asyncio.run(_task_lookup_and_transition_deny_foreign_scope_and_parent_run())


async def _task_lookup_and_transition_deny_foreign_scope_and_parent_run():
    database = SQLiteDatabase(":memory:")
    store = SQLiteAgentTaskStore(database)
    try:
        task = await store.create(scope(), "run-a", "call-a", "search", "digest-a")
        for foreign in (scope("bob"), scope(product="other")):
            assert await store.get_owned(foreign, task.task_id, parent_run_id="run-a") is None
            assert (
                await store.transition(
                    foreign,
                    task.task_id,
                    AgentTaskStatus.QUEUED,
                    AgentTaskStatus.RUNNING,
                    parent_run_id="run-a",
                )
                is None
            )
        assert await store.get_owned(scope(), task.task_id, parent_run_id="run-b") is None
        assert (
            await store.transition(
                scope(),
                task.task_id,
                AgentTaskStatus.QUEUED,
                AgentTaskStatus.RUNNING,
                parent_run_id="run-b",
            )
            is None
        )
        assert await store.get_owned(scope(), "unknown", parent_run_id="run-a") is None
        assert (
            await store.get_owned(scope(), task.task_id, parent_run_id="run-a")
        ).status is AgentTaskStatus.QUEUED
    finally:
        await database.close()


def test_parent_run_id_is_required_for_lookup_and_transition():
    asyncio.run(_parent_run_id_is_required_for_lookup_and_transition())


async def _parent_run_id_is_required_for_lookup_and_transition():
    database = SQLiteDatabase(":memory:")
    store = SQLiteAgentTaskStore(database)
    try:
        task = await store.create(scope(), "run-a", "call-a", "search", "digest-a")
        with pytest.raises(TypeError):
            await store.get_owned(scope(), task.task_id)
        with pytest.raises(TypeError):
            await store.transition(
                scope(), task.task_id, AgentTaskStatus.QUEUED, AgentTaskStatus.RUNNING
            )
        assert (
            await store.get_owned(scope(), task.task_id, parent_run_id="run-a")
        ).status is AgentTaskStatus.QUEUED
        assert await store.get_owned(scope(), task.task_id, parent_run_id="run-b") is None
    finally:
        await database.close()


def test_transition_is_terminal_compare_and_set_with_bounded_result():
    asyncio.run(_transition_is_terminal_compare_and_set_with_bounded_result())


async def _transition_is_terminal_compare_and_set_with_bounded_result():
    database = SQLiteDatabase(":memory:")
    store = SQLiteAgentTaskStore(database)
    try:
        task = await store.create(scope(), "run-a", "call-a", "search", "digest-a")
        assert (
            await store.transition(
                scope(),
                task.task_id,
                AgentTaskStatus.RUNNING,
                AgentTaskStatus.SUCCEEDED,
                parent_run_id="run-a",
            )
            is None
        )
        running = await store.transition(
            scope(),
            task.task_id,
            AgentTaskStatus.QUEUED,
            AgentTaskStatus.RUNNING,
            parent_run_id="run-a",
        )
        assert running is not None and running.status is AgentTaskStatus.RUNNING
        queued_failure = await store.create(scope(), "run-a", "call-b", "search", "digest-b")
        assert (
            await store.transition(
                scope(),
                queued_failure.task_id,
                AgentTaskStatus.QUEUED,
                AgentTaskStatus.FAILED,
                parent_run_id="run-a",
                failure="grant issuance failed before start",
            )
        ).status is AgentTaskStatus.FAILED
        finished = await store.transition(
            scope(),
            task.task_id,
            AgentTaskStatus.RUNNING,
            AgentTaskStatus.SUCCEEDED,
            parent_run_id="run-a",
            result="done",
        )
        assert finished is not None and finished.result == "done"
        assert finished.finished_at is not None
        assert (
            await store.transition(
                scope(),
                task.task_id,
                AgentTaskStatus.SUCCEEDED,
                AgentTaskStatus.FAILED,
                parent_run_id="run-a",
                failure="late",
            )
            is None
        )
        assert (await store.get_owned(scope(), task.task_id, parent_run_id="run-a")) == finished
    finally:
        await database.close()


def test_cancellation_fences_new_children_and_late_completion():
    asyncio.run(_cancellation_fences_new_children_and_late_completion())


async def _cancellation_fences_new_children_and_late_completion():
    database = SQLiteDatabase(":memory:")
    store = SQLiteAgentTaskStore(database)
    try:
        queued = await store.create(scope(), "run-a", "call-a", "search", "digest-a")
        running = await store.create(scope(), "run-a", "call-b", "search", "digest-b")
        unrelated = await store.create(scope(), "run-b", "call-a", "search", "digest-a")
        await store.transition(
            scope(),
            running.task_id,
            AgentTaskStatus.QUEUED,
            AgentTaskStatus.RUNNING,
            parent_run_id="run-a",
        )
        assert await store.cancel_children(scope(), "run-a") == 2
        assert (
            await store.get_owned(scope(), queued.task_id, parent_run_id="run-a")
        ).status is AgentTaskStatus.CANCELLED
        assert (
            await store.get_owned(scope(), running.task_id, parent_run_id="run-a")
        ).status is AgentTaskStatus.CANCELLED
        assert (
            await store.get_owned(scope(), unrelated.task_id, parent_run_id="run-b")
        ).status is AgentTaskStatus.QUEUED
        assert (
            await store.transition(
                scope(),
                running.task_id,
                AgentTaskStatus.RUNNING,
                AgentTaskStatus.SUCCEEDED,
                parent_run_id="run-a",
                result="late",
            )
            is None
        )
        with pytest.raises(RuntimeError, match="cancel"):
            await store.create(scope(), "run-a", "call-c", "search", "digest-c")
        assert (
            await store.create(scope(), "run-a", "call-a", "search", "digest-a")
        ).status is AgentTaskStatus.CANCELLED
        assert await store.cancel_children(scope(), "run-a") == 0
    finally:
        await database.close()


def test_restart_marks_uncertain_queued_and_running_failed_without_rerun(tmp_path):
    asyncio.run(_restart_marks_uncertain_running_failed_without_rerun(tmp_path))


async def _restart_marks_uncertain_running_failed_without_rerun(tmp_path):
    path = tmp_path / "tasks.sqlite"
    database = SQLiteDatabase(path)
    store = SQLiteAgentTaskStore(database)
    queued = await store.create(scope(), "run-a", "call-a", "search", "digest-a")
    running = await store.create(scope(), "run-a", "call-b", "search", "digest-b")
    await store.transition(
        scope(),
        running.task_id,
        AgentTaskStatus.QUEUED,
        AgentTaskStatus.RUNNING,
        parent_run_id="run-a",
    )
    await database.close()

    reopened = SQLiteDatabase(path)
    restarted = SQLiteAgentTaskStore(reopened)
    try:
        assert await restarted.recover_interrupted() == 2
        recovered = await restarted.get_owned(scope(), running.task_id, parent_run_id="run-a")
        assert recovered is not None and recovered.status is AgentTaskStatus.FAILED
        assert recovered.failure == "interrupted by service restart"
        assert (
            await restarted.get_owned(scope(), queued.task_id, parent_run_id="run-a")
        ).status is AgentTaskStatus.FAILED
        assert await restarted.recover_interrupted() == 0
        assert (
            await restarted.create(scope(), "run-a", "call-b", "search", "digest-b")
        ) == recovered
        assert (
            await restarted.transition(
                scope(),
                running.task_id,
                AgentTaskStatus.RUNNING,
                AgentTaskStatus.SUCCEEDED,
                parent_run_id="run-a",
            )
            is None
        )
    finally:
        await reopened.close()
