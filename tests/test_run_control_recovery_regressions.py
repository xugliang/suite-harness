"""Cross-instance recovery must preserve newer work and bind continuation identity."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from suiteharness.channels import OutboundEventKind
from suiteharness.execution.events import RunEvent
from suiteharness.execution.models import FailureCode, RunFailure, RunResult, RunStatus
from suiteharness.server.application import ChannelApplication, ChannelMessageConflictError
from suiteharness.sessions import SessionStatus, SQLiteSessionStore
from tests.test_run_control_application import _fixture
from tests.test_run_control_sqlite_integration import _ControlledActivation, _stop_original


class _ImmediateActivation:
    tenant_id = "company"

    async def stream(self, request):
        now = datetime.now(UTC)
        cancelled = request.input["message"] == "cancel newer"
        yield RunEvent(
            run_id=request.run_id, sequence=1,
            kind="cancelled" if cancelled else "completed",
            result=RunResult(
                run_id=request.run_id,
                status=RunStatus.CANCELLED if cancelled else RunStatus.SUCCEEDED,
                failure=RunFailure(code=FailureCode.CANCELLED, message="stopped") if cancelled else None,
                output=None if cancelled else request.input["message"],
                iterations_used=1, tool_calls_used=0, started_at=now, finished_at=now,
            ),
        )

    async def cancel(self, scope, run_id):
        return False


class _InterruptedCheckpointStore(SQLiteSessionStore):
    def __init__(self, path):
        super().__init__(path)
        self.crash_once = True
        self.pause_at = None
        self.paused = asyncio.Event()
        self.release = asyncio.Event()

    async def _pause(self, at):
        if self.pause_at == at:
            self.pause_at = None
            self.paused.set()
            await self.release.wait()

    async def save_checkpoint(self, identity, **kwargs):
        history = kwargs["workflow_state"]["runs"]
        if history and history[-1]["state"] == "completed":
            if self.crash_once:
                self.crash_once = False
                raise RuntimeError("simulated terminal checkpoint crash")
            await self._pause("save")
        return await super().save_checkpoint(identity, **kwargs)

    async def resume(self, identity):
        result = await super().resume(identity)
        await self._pause("snapshot")
        return result


@pytest.mark.parametrize("pause_at", ["snapshot", "save"])
@pytest.mark.parametrize("newer_status", [SessionStatus.COMPLETED, SessionStatus.CANCELLED])
def test_terminal_recovery_does_not_overwrite_newer_run(tmp_path, pause_at, newer_status):
    async def check():
        _, grants, scope, original, identity = _fixture()
        recovering_store = _InterruptedCheckpointStore(tmp_path / "recovery.sqlite3")
        other_store = SQLiteSessionStore(tmp_path / "recovery.sqlite3")
        activation = _ImmediateActivation()
        recovering = ChannelApplication(activation=activation, sessions=recovering_store, grants=grants)
        other = ChannelApplication(activation=activation, sessions=other_store, grants=grants)
        pending = None
        try:
            with pytest.raises(RuntimeError, match="simulated terminal checkpoint crash"):
                _ = [e async for e in recovering.handle(original, scope)]
            recovering_store.pause_at = pause_at

            async def recover():
                return [e async for e in recovering.handle(original, scope)]

            pending = asyncio.create_task(recover())
            await asyncio.wait_for(recovering_store.paused.wait(), 3)
            newer = original.model_copy(update={
                "message_id": "newer-message", "event_id": "newer-event",
                "text": "cancel newer" if newer_status is SessionStatus.CANCELLED else "newer answer",
            })
            new_events = [e async for e in other.handle(newer, scope)]
            before = await other_store.resume(identity)
            assert before.session.status is newer_status
            before_ids = [r["run_id"] for r in before.checkpoint.workflow_state["runs"]]
            recovering_store.release.set()
            old_events = await asyncio.wait_for(pending, 3)
            assert old_events[-1].kind is OutboundEventKind.COMPLETED
            after = await other_store.resume(identity)
            assert [r["run_id"] for r in after.checkpoint.workflow_state["runs"]] == before_ids
            assert after.session.status is newer_status
            replay = [e async for e in other.handle(newer, scope)]
            assert replay[-1].kind == new_events[-1].kind
            assert replay[-1].payload["replayed"] is True
            assert replay[-1].payload.get("failure") == new_events[-1].payload.get("failure")
            assert replay[-1].payload.get("output") == new_events[-1].payload.get("output")
        finally:
            recovering_store.release.set()
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)
            await recovering_store.close()
            await other_store.close()
    asyncio.run(check())


def test_recovery_does_not_invalidate_the_revision_of_a_newer_active_run(tmp_path):
    async def check():
        _, grants, scope, original, identity = _fixture()

        class BlockingActivation(_ImmediateActivation):
            def __init__(self):
                self.waiting, self.finish = asyncio.Event(), asyncio.Event()

            async def stream(self, request):
                if request.input["message"] == "wait newer":
                    self.waiting.set()
                    await self.finish.wait()
                async for event in super().stream(request):
                    yield event

        recovering_store = _InterruptedCheckpointStore(tmp_path / "active.sqlite3")
        other_store = SQLiteSessionStore(tmp_path / "active.sqlite3")
        activation = BlockingActivation()
        recovering = ChannelApplication(activation=activation, sessions=recovering_store, grants=grants)
        other = ChannelApplication(activation=activation, sessions=other_store, grants=grants)
        tasks = []

        async def collect(app, message):
            return [e async for e in app.handle(message, scope)]

        try:
            with pytest.raises(RuntimeError, match="simulated terminal checkpoint crash"):
                await collect(recovering, original)
            recovering_store.pause_at = "snapshot"
            old = asyncio.create_task(collect(recovering, original))
            tasks.append(old)
            await asyncio.wait_for(recovering_store.paused.wait(), 3)
            newer = original.model_copy(update={
                "message_id": "newer", "event_id": "newer", "text": "wait newer",
            })
            running = asyncio.create_task(collect(other, newer))
            tasks.append(running)
            await asyncio.wait_for(activation.waiting.wait(), 3)
            recovering_store.release.set()
            assert (await asyncio.wait_for(old, 3))[-1].kind is OutboundEventKind.COMPLETED
            activation.finish.set()
            assert (await asyncio.wait_for(running, 3))[-1].kind is OutboundEventKind.COMPLETED
            assert (await other_store.get(identity)).status is SessionStatus.COMPLETED
        finally:
            recovering_store.release.set()
            activation.finish.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await recovering_store.close()
            await other_store.close()
    asyncio.run(check())


def test_two_crashed_terminal_checkpoints_can_recover_newest_then_older(tmp_path):
    async def check():
        _, grants, scope, original, identity = _fixture()
        store = _InterruptedCheckpointStore(tmp_path / "two-crashes.sqlite3")
        app = ChannelApplication(activation=_ImmediateActivation(), sessions=store, grants=grants)
        newer = original.model_copy(update={"message_id": "newer", "event_id": "newer"})
        try:
            for message in (original, newer):
                store.crash_once = True
                with pytest.raises(RuntimeError, match="simulated terminal checkpoint crash"):
                    _ = [e async for e in app.handle(message, scope)]
            for message in (newer, original):
                replay = [e async for e in app.handle(message, scope)]
                assert replay[-1].kind is OutboundEventKind.COMPLETED
            snapshot = await store.resume(identity)
            assert snapshot.session.status is SessionStatus.COMPLETED
            assert [r["state"] for r in snapshot.checkpoint.workflow_state["runs"]] == [
                "completed", "completed",
            ]
        finally:
            await store.close()
    asyncio.run(check())


@pytest.mark.parametrize("active", [False, True])
def test_continuation_id_cannot_be_reused_for_another_source(active):
    async def check():
        sessions, grants, scope, message, identity = _fixture()
        activation = _ControlledActivation(hold_continuation=active)
        worker = ChannelApplication(activation=activation, sessions=sessions, grants=grants)
        controller = ChannelApplication(activation=activation, sessions=sessions, grants=grants)
        await _stop_original(worker, controller, sessions, activation, scope, message, identity)
        continuation = message.model_copy(update={
            "message_id": "continue-message", "event_id": "continue-event", "text": "",
        })

        async def collect():
            return [e async for e in worker.continue_task(
                continuation, scope, target_message_id=message.message_id,
            )]

        pending = asyncio.create_task(collect())
        try:
            await asyncio.wait_for(activation.continuing.wait(), 3)
            if not active:
                await asyncio.wait_for(pending, 3)
            with pytest.raises(ChannelMessageConflictError):
                _ = [e async for e in controller.continue_task(
                    continuation, scope, target_message_id="does-not-exist",
                )]
            with pytest.raises(ChannelMessageConflictError):
                _ = [e async for e in controller.handle(continuation, scope)]
            activation.finish_continuation.set()
            await asyncio.wait_for(pending, 3)
            replay = [e async for e in controller.continue_task(
                continuation, scope, target_message_id=message.message_id,
            )]
            assert replay[-1].kind is OutboundEventKind.COMPLETED
            assert replay[-1].payload["replayed"] is True
            assert len(activation.requests) == 2
        finally:
            activation.finish_continuation.set()
            await asyncio.gather(pending, return_exceptions=True)
    asyncio.run(check())
