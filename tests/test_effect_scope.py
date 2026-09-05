"""EffectScope owns every side effect and always unwinds it exactly once."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from suiteharness.runtime.effects import (
    EffectCleanupError,
    EffectScope,
    EffectScopeClosedError,
    EffectScopeState,
)


def test_effect_scope_is_lifo_and_idempotent() -> None:
    async def exercise() -> list[str]:
        events: list[str] = []
        scope = EffectScope("test")
        scope.callback("first", lambda: events.append("first"))

        async def second() -> None:
            await asyncio.sleep(0)
            events.append("second")

        scope.callback("second", second)
        await scope.close()
        await scope.close()
        assert scope.state is EffectScopeState.CLOSED
        return events

    assert asyncio.run(exercise()) == ["second", "first"]


def test_effect_scope_continues_after_cleanup_failure() -> None:
    async def exercise() -> tuple[list[str], EffectCleanupError]:
        events: list[str] = []
        scope = EffectScope("broken")
        scope.callback("oldest", lambda: events.append("oldest"))

        def fail() -> None:
            events.append("failed")
            raise RuntimeError("boom")

        scope.callback("failure", fail)
        scope.callback("newest", lambda: events.append("newest"))
        with pytest.raises(EffectCleanupError) as captured:
            await scope.close()
        with pytest.raises(EffectCleanupError) as repeated:
            await scope.close()
        assert repeated.value is captured.value
        return events, captured.value

    events, error = asyncio.run(exercise())
    assert events == ["newest", "failed", "oldest"]
    assert [(failure.label, type(failure.error)) for failure in error.failures] == [
        ("failure", RuntimeError)
    ]


def test_concurrent_close_runs_each_disposer_once() -> None:
    async def exercise() -> int:
        calls = 0
        entered = asyncio.Event()
        release = asyncio.Event()
        scope = EffectScope("concurrent")

        async def dispose() -> None:
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()

        scope.callback("resource", dispose)
        owner = asyncio.create_task(scope.close())
        await entered.wait()
        follower = asyncio.create_task(scope.close())
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(owner, follower)
        return calls

    assert asyncio.run(exercise()) == 1


def test_enter_and_child_are_owned_by_parent_in_lifo_order() -> None:
    async def exercise() -> list[str]:
        events: list[str] = []
        scope = EffectScope("parent")
        scope.callback("parent-first", lambda: events.append("parent-first"))

        @asynccontextmanager
        async def resource():
            events.append("enter")
            yield "value"
            events.append("exit")

        assert await scope.enter("resource", resource()) == "value"
        child = scope.child("child")
        child.callback("child-effect", lambda: events.append("child"))
        scope.callback("parent-last", lambda: events.append("parent-last"))
        await scope.close()
        return events

    assert asyncio.run(exercise()) == [
        "enter",
        "parent-last",
        "child",
        "exit",
        "parent-first",
    ]


def test_closed_scope_rejects_new_effects() -> None:
    async def exercise() -> None:
        scope = EffectScope("closed")
        await scope.close()
        with pytest.raises(EffectScopeClosedError):
            scope.callback("late", lambda: None)
        with pytest.raises(EffectScopeClosedError):
            scope.child("late-child")

    asyncio.run(exercise())
