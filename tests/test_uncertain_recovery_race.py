"""Uncertain-run recovery must merge against the current durable generation."""

import asyncio
import copy
from datetime import UTC, datetime, timedelta

import pytest

from suiteharness.channels import OutboundEventKind
from suiteharness.server.application import ChannelApplication
from suiteharness.sessions import SessionStatus, SQLiteSessionStore, TranscriptEventType
from tests.test_run_control_application import _fixture
from tests.test_run_control_recovery_regressions import _ImmediateActivation


class Clock:
    def __init__(self):
        self.value = datetime.now(UTC)

    def __call__(self):
        return self.value


class PausingStore(SQLiteSessionStore):
    def __init__(self, path, clock):
        super().__init__(path, clock=clock)
        self.crash_started = True
        self.paused = asyncio.Event()
        self.release = asyncio.Event()

    async def save_checkpoint(self, identity, **kwargs):
        checkpoint = await super().save_checkpoint(identity, **kwargs)
        if self.crash_started and kwargs["workflow_state"]["runs"][-1]["state"] == "started":
            self.crash_started = False
            raise RuntimeError("crash after durable started checkpoint")
        return checkpoint

    async def append(self, identity, **kwargs):
        event = await super().append(identity, **kwargs)
        if (event.type is TranscriptEventType.ERROR
                and event.payload.get("failure", {}).get("code") == "run_outcome_uncertain"):
            self.paused.set()
            await self.release.wait()
        return event


@pytest.mark.parametrize("invalidate", [False, True])
def test_uncertain_recovery_preserves_newer_sqlite_run_and_memory(tmp_path, invalidate):
    async def check():
        _, grants, scope, original, identity = _fixture()
        clock = Clock()
        recovering_store = PausingStore(tmp_path / "uncertain.sqlite3", clock)
        other_store = SQLiteSessionStore(tmp_path / "uncertain.sqlite3", clock=clock)
        recovering = ChannelApplication(
            activation=_ImmediateActivation(), sessions=recovering_store, grants=grants,
        )
        other = ChannelApplication(
            activation=_ImmediateActivation(), sessions=other_store, grants=grants,
        )
        pending = None

        async def collect(app, message):
            return [event async for event in app.handle(message, scope)]

        try:
            with pytest.raises(RuntimeError, match="durable started"):
                await collect(recovering, original)
            first = await other_store.resume(identity)
            assert first.checkpoint.workflow_state["runs"][-1]["state"] == "started"
            assert [event.type for event in await other_store.recent_transcript(identity, limit=10)] == [
                TranscriptEventType.USER_MESSAGE,
            ]
            clock.value += timedelta(seconds=1000)
            pending = asyncio.create_task(collect(recovering, original))
            await asyncio.wait_for(recovering_store.paused.wait(), 3)
            if invalidate:
                await other.invalidate_memory(scope, source_namespace="crm", source_version=1)
            newer = original.model_copy(update={
                "message_id": "newer", "event_id": "newer", "text": "new answer",
            })
            assert (await collect(other, newer))[-1].kind is OutboundEventKind.COMPLETED
            before = await other_store.resume(identity)
            state = copy.deepcopy(before.checkpoint.workflow_state)
            state["memory"] = {"summary": "newer summary", "covered_revision": before.session.revision,
                               "degraded": False}
            await other_store.save_checkpoint(
                identity, checkpoint_id="newer-memory", workflow_state=state,
                expected_revision=before.session.revision, idempotency_key="newer-memory",
            )
            before = await other_store.resume(identity)
            recovering_store.release.set()
            outcome = await asyncio.wait_for(pending, 3)
            assert outcome[-1].payload["failure"]["code"] == "run_outcome_uncertain"
            after = await other_store.resume(identity)
            assert [item["run_id"] for item in after.checkpoint.workflow_state["runs"]] == [
                item["run_id"] for item in before.checkpoint.workflow_state["runs"]
            ]
            assert after.checkpoint.workflow_state["runs"][-1] == before.checkpoint.workflow_state["runs"][-1]
            assert after.checkpoint.workflow_state["memory"] == state["memory"]
            assert after.session.status is SessionStatus.COMPLETED
            if invalidate:
                assert after.session.revision == before.session.revision
            replay = await collect(other, newer)
            assert replay[-1].kind is OutboundEventKind.COMPLETED
            assert replay[-1].payload["replayed"] is True
            assert replay[-1].payload["output"] == "new answer"
        finally:
            recovering_store.release.set()
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)
            await recovering_store.close()
            await other_store.close()
    asyncio.run(check())
