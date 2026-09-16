"""Durable run controls across independent SQLite connections and application locks."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from suiteharness.channels import ChannelKind, InboundMessage, OutboundEventKind
from suiteharness.execution.events import RunEvent
from suiteharness.execution.in_memory import InMemoryCapabilityAuthority, InMemoryToolRegistry
from suiteharness.execution.models import RunResult, RunResumeState, RunStatus
from suiteharness.runtime import RequestScope, ScopePath
from suiteharness.server.access import ConfiguredGrantIssuer, ToolAccessTemplate
from suiteharness.server.application import ChannelApplication
from suiteharness.sessions import RunStopOutcome, SessionIdentity, TranscriptEventType
from suiteharness.sessions.errors import SessionNotFoundError
from suiteharness.sessions.sqlite import SQLiteSessionStore


def _request():
    scope = RequestScope(
        path=ScopePath.agent("company", "product", "assistant", "conversation"),
        principal_id="seller", channel_id="web", request_id="request1",
        correlation_id="correlation1",
    )
    message = InboundMessage(
        channel=ChannelKind.WEB, event_id="event1", message_id="message1",
        conversation_id="conversation", sender_external_id="seller",
        text="请继续分析", product_id="product", received_at=datetime.now(UTC),
    )
    identity = SessionIdentity(
        tenant_id="company", product_id="product", agent_id="assistant",
        session_id="conversation", principal_id="seller",
    )
    return scope, message, identity


@asynccontextmanager
async def _applications(database, activation):
    worker_store = SQLiteSessionStore(database)
    control_store = SQLiteSessionStore(database)
    grants = ConfiguredGrantIssuer(
        registry=InMemoryToolRegistry(), authority=InMemoryCapabilityAuthority(),
        templates=[ToolAccessTemplate(channel_id="web", product_id="product")],
    )
    try:
        yield (
            worker_store, control_store,
            ChannelApplication(activation=activation, sessions=worker_store, grants=grants),
            ChannelApplication(activation=activation, sessions=control_store, grants=grants),
        )
    finally:
        await worker_store.close()
        await control_store.close()


class _ControlledActivation:
    """Only the external runner is fake; stop persistence and fencing remain real."""

    tenant_id = "company"

    def __init__(self, partial="", *, hold_continuation=False):
        self.partial = partial
        self.hold_continuation = hold_continuation
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()
        self.continuing = asyncio.Event()
        self.finish_continuation = asyncio.Event()
        self.requests = []
        self.cancelled = []

    async def cancel(self, scope, run_id):
        if not self.requests or run_id != self.requests[0].run_id:
            return False
        assert scope == self.requests[0].scope
        self.cancelled.append(run_id)
        self.release.set()
        return True

    async def stream(self, request):
        self.requests.append(request)
        started_at = datetime.now(UTC)
        yield RunEvent(run_id=request.run_id, sequence=1, kind="started")
        if request.resume is None:
            point = RunResumeState(
                source_run_id=request.run_id, iterations_used=0, tool_calls_used=0,
                prompt_digest="a" * 64, route_key="test-model-v1",
            )
            yield RunEvent(
                run_id=request.run_id, sequence=2, kind="checkpoint",
                data={"resume_state": point.model_dump(mode="json"), "input": request.input,
                      "conversation": request.conversation.model_dump(mode="json")},
            )
            if self.partial:
                yield RunEvent(run_id=request.run_id, sequence=3, kind="delta", text=self.partial)
            self.waiting.set()
            # No further delta is available; only cancellation releases this wait.
            await self.release.wait()
            output = "迟到成功不可提交"
        else:
            self.continuing.set()
            if self.hold_continuation:
                await self.finish_continuation.wait()
            output = "安全续跑回答"
        yield RunEvent(
            run_id=request.run_id, sequence=4, kind="completed",
            result=RunResult(
                run_id=request.run_id, status=RunStatus.SUCCEEDED, output=output,
                iterations_used=1, tool_calls_used=0,
                started_at=started_at, finished_at=datetime.now(UTC),
            ),
        )


async def _stop_original(worker, controller, store, activation, scope, message, identity):
    source = worker.handle(message, scope)
    try:
        started = await anext(source)
        assert started.kind is OutboundEventKind.STARTED
        run_id = started.payload["run_id"]
        if activation.partial:
            assert (await anext(source)).kind is OutboundEventKind.DELTA
        pending = asyncio.create_task(anext(source))
        try:
            await asyncio.wait_for(activation.waiting.wait(), 3)
            assert await store.active_run_ids(identity, limit=2) == (run_id,)
            assert not pending.done()
            assert await controller.stop_message(
                scope, target_message_id=message.message_id,
            ) is RunStopOutcome.REQUESTED
            terminal = await asyncio.wait_for(pending, 3)
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        assert terminal.kind is OutboundEventKind.FAILED
        assert terminal.payload["run_status"] == "cancelled"
        assert terminal.payload["resume_unsafe"] is False
        assert activation.cancelled == [run_id]
        return run_id
    finally:
        await source.aclose()


@pytest.mark.parametrize("partial", ["", "中" * 50_000], ids=["no-delta", "bounded-utf8"])
def test_sqlite_cross_instance_stop_survives_reopen_and_continues_safe_point(tmp_path, partial):
    async def check():
        database = tmp_path / "sessions.sqlite3"
        scope, message, identity = _request()
        activation = _ControlledActivation(partial)
        async with _applications(database, activation) as (_, store, worker, controller):
            run_id = await _stop_original(
                worker, controller, store, activation, scope, message, identity,
            )
            assert [item.type for item in await store.recent_transcript(identity, limit=20)] == [
                TranscriptEventType.USER_MESSAGE, TranscriptEventType.CANCEL,
            ]
        # Both original connections are closed: resume state and cancellation
        # must come from disk, not either application's lock or in-memory state.
        resumed_activation = _ControlledActivation()
        async with _applications(database, resumed_activation) as (_, store, worker, controller):
            shown = await controller.history(scope, include_cancelled=True)
            assert [item.type for item in shown] == [
                TranscriptEventType.USER_MESSAGE, TranscriptEventType.CANCEL,
            ]
            cancelled = shown[-1].payload
            assert cancelled["run_id"] == run_id
            assert cancelled["run_status"] == "cancelled"
            assert "迟到成功不可提交" not in str(cancelled)
            if partial:
                assert cancelled["incomplete"] is True
                assert cancelled["partial_truncated"] is True
                assert cancelled["partial_output"] == "中" * 43_690
                assert len(cancelled["partial_output"].encode("utf-8")) <= 131_072
            else:
                assert not cancelled.get("partial_output")
            assert [item.type for item in await controller.history(scope)] == [
                TranscriptEventType.USER_MESSAGE,
            ]
            point = await store.get_resume_point(identity, run_id=run_id)
            assert point is not None and point.identity == identity
            assert point.payload["resume_state"]["source_run_id"] == run_id
            for field in ("tenant_id", "product_id", "agent_id", "session_id", "principal_id"):
                other = identity.model_copy(update={field: "other"})
                assert await store.get_resume_point(other, run_id=run_id) is None
                with pytest.raises(SessionNotFoundError):
                    await store.recent_transcript(other, limit=20)
                assert await store.request_stop(other, run_id=run_id) is RunStopOutcome.NOT_FOUND
            continuation = message.model_copy(update={
                "event_id": "continue-event", "message_id": "continue-message", "text": "",
            })
            events = [event async for event in worker.continue_task(
                continuation, scope, target_message_id=message.message_id,
            )]
            assert [event.kind for event in events] == [
                OutboundEventKind.STARTED, OutboundEventKind.COMPLETED,
            ]
            assert events[0].payload["run_id"] != run_id
            assert events[-1].payload["output"] == "安全续跑回答"
            request = resumed_activation.requests[0]
            assert request.resume.source_run_id == run_id
            assert request.input == activation.requests[0].input
            assert request.conversation == activation.requests[0].conversation
            assert request.read_only is True
            assert [item.type for item in await controller.history(scope, include_cancelled=True)] == [
                TranscriptEventType.USER_MESSAGE, TranscriptEventType.CANCEL,
                TranscriptEventType.ASSISTANT_MESSAGE,
            ]
    asyncio.run(check())


def test_sqlite_second_application_retries_active_continuation_without_executing_twice(tmp_path):
    async def check():
        scope, message, identity = _request()
        activation = _ControlledActivation(hold_continuation=True)
        async with _applications(tmp_path / "sessions.sqlite3", activation) as (
            _, store, worker, controller,
        ):
            original_run_id = await _stop_original(
                worker, controller, store, activation, scope, message, identity,
            )
            continuation = message.model_copy(update={
                "event_id": "continue-event", "message_id": "continue-message", "text": "",
            })
            source = worker.continue_task(continuation, scope, target_message_id=message.message_id)
            try:
                started = await anext(source)
                continuation_run_id = started.payload["run_id"]
                assert continuation_run_id != original_run_id
                pending = asyncio.create_task(anext(source))
                try:
                    await asyncio.wait_for(activation.continuing.wait(), 3)
                    retry = [event async for event in controller.continue_task(
                        continuation, scope, target_message_id=message.message_id,
                    )]
                    assert retry[-1].kind is OutboundEventKind.FAILED
                    assert retry[-1].payload["failure"]["code"] == "run_in_progress"
                    assert retry[-1].payload["replayed"] is True
                    assert retry[-1].payload["run_id"] == continuation_run_id
                    assert not pending.done()
                    assert len(activation.requests) == 2
                    activation.finish_continuation.set()
                    assert (await asyncio.wait_for(pending, 3)).kind is OutboundEventKind.COMPLETED
                finally:
                    if not pending.done():
                        pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
            finally:
                await source.aclose()
            replay = [event async for event in controller.continue_task(
                continuation, scope, target_message_id=message.message_id,
            )]
            assert replay[-1].kind is OutboundEventKind.COMPLETED
            assert replay[-1].payload["replayed"] is True
            assert replay[-1].payload["output"] == "安全续跑回答"
            assert len(activation.requests) == 2
            transcript = await store.recent_transcript(identity, limit=20)
            assert [item.type for item in transcript].count(TranscriptEventType.ASSISTANT_MESSAGE) == 1
    asyncio.run(check())
