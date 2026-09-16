"""Application-owned stop fencing and display history for one isolated session."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from suiteharness.agents.react import ReActWorkflow
from suiteharness.channels import ChannelKind, InboundMessage, OutboundEventKind
from suiteharness.execution.events import RunEvent
from suiteharness.execution.in_memory import (
    InMemoryApprovalStore,
    InMemoryAuditJournal,
    InMemoryCapabilityAuthority,
    InMemoryToolRegistry,
)
from suiteharness.execution.models import (
    RunResult,
    RunResumeState,
    RunStatus,
    ToolEffect,
    ToolIdentity,
    ToolSpec,
)
from suiteharness.execution.protocols import ExecutionStrategies
from suiteharness.execution.runner import ExecutionRunner
from suiteharness.models import FinishReason, ModelStreamEvent, ModelStreamEventType
from suiteharness.runtime import RequestScope, ScopePath
from suiteharness.server.access import ConfiguredGrantIssuer, ToolAccessTemplate
from suiteharness.server.application import ChannelApplication
from suiteharness.sessions import (
    InMemorySessionStore,
    RunStopOutcome,
    SessionIdentity,
    TranscriptEventType,
)


def _fixture():
    sessions = InMemorySessionStore()
    registry = InMemoryToolRegistry()
    authority = InMemoryCapabilityAuthority()
    grants = ConfiguredGrantIssuer(
        registry=registry,
        authority=authority,
        templates=[ToolAccessTemplate(channel_id="web", product_id="product")],
    )
    scope = RequestScope(
        path=ScopePath.agent("company", "product", "assistant", "conversation"),
        principal_id="seller",
        channel_id="web",
        request_id="request1",
        correlation_id="correlation1",
    )
    message = InboundMessage(
        channel=ChannelKind.WEB,
        event_id="event1",
        message_id="client-message1",
        conversation_id="conversation",
        sender_external_id="seller",
        text="请帮我看一下",
        product_id="product",
        received_at=datetime.now(UTC),
    )
    identity = SessionIdentity(
        tenant_id="company", product_id="product", agent_id="assistant",
        session_id="conversation", principal_id="seller",
    )
    return sessions, grants, scope, message, identity


def test_cross_application_stop_fences_late_success_and_displays_cancel_without_memory():
    async def check():
        sessions, grants, scope, message, identity = _fixture()
        activation = _LateSuccessActivation()
        worker = ChannelApplication(activation=activation, sessions=sessions, grants=grants)
        controller = ChannelApplication(activation=activation, sessions=sessions, grants=grants)
        source = worker.handle(message, scope)
        assert (await anext(source)).kind is OutboundEventKind.STARTED
        # The internal checkpoint must never become an outward STATUS frame.
        delta = await anext(source)
        assert delta.kind is OutboundEventKind.DELTA
        assert delta.payload["text"] == "尚未完成"
        point = await sessions.get_resume_point(identity, run_id=delta.payload["run_id"])
        assert point is not None
        assert point.payload["resume_state"]["source_run_id"] == delta.payload["run_id"]
        terminal_task = asyncio.create_task(anext(source))
        await activation.waiting.wait()
        outcome = await controller.stop_message(scope, target_message_id=message.message_id)
        assert outcome is RunStopOutcome.REQUESTED
        terminal = await asyncio.wait_for(terminal_task, timeout=3)
        assert terminal.kind is OutboundEventKind.FAILED
        assert terminal.payload["run_status"] == "cancelled"
        assert terminal.payload["partial_output"] == "尚未完成"
        assert terminal.payload["incomplete"] is True
        await source.aclose()
        assert activation.cancelled == [delta.payload["run_id"]]
        transcript = await sessions.recent_transcript(identity, limit=20)
        assert transcript[-1].type is TranscriptEventType.CANCEL
        assert all(item.type is not TranscriptEventType.ASSISTANT_MESSAGE for item in transcript)
        normal = await worker.history(scope, limit=20)
        assert [item.type for item in normal] == [TranscriptEventType.USER_MESSAGE]
        shown = await worker.history(scope, limit=20, include_cancelled=True)
        assert [item.type for item in shown] == [
            TranscriptEventType.USER_MESSAGE, TranscriptEventType.CANCEL,
        ]
        assert shown[-1].payload["incomplete"] is True
    asyncio.run(check())


def test_cancelled_initial_checkpoint_continues_in_a_new_run_without_replaying_old_id():
    async def check():
        sessions, grants, scope, message, identity = _fixture()
        activation = _LateSuccessActivation()
        app = ChannelApplication(activation=activation, sessions=sessions, grants=grants)
        original = app.handle(message, scope)
        started = await anext(original)
        assert started.kind is OutboundEventKind.STARTED
        assert (await anext(original)).kind is OutboundEventKind.DELTA
        pending = asyncio.create_task(anext(original))
        await activation.waiting.wait()
        assert await app.stop_message(scope, target_message_id=message.message_id) is RunStopOutcome.REQUESTED
        assert (await asyncio.wait_for(pending, 3)).payload["run_status"] == "cancelled"
        await original.aclose()

        continuation = message.model_copy(update={
            "event_id": "continue-event1", "message_id": "continue-message1", "text": "",
        })
        events = [event async for event in app.continue_task(
            continuation, scope, target_message_id=message.message_id,
        )]
        assert [event.kind for event in events] == [
            OutboundEventKind.STARTED, OutboundEventKind.COMPLETED,
        ]
        assert events[0].payload["run_id"] != started.payload["run_id"]
        assert events[-1].payload["output"] == "安全续跑回答"
        assert len(activation.requests) == 2
        assert activation.requests[-1].resume is not None
        assert activation.requests[-1].resume.source_run_id == started.payload["run_id"]
        assert activation.requests[-1].input == activation.requests[0].input
        assert activation.requests[-1].conversation == activation.requests[0].conversation
        replay = [event async for event in app.continue_task(
            continuation, scope, target_message_id=message.message_id,
        )]
        assert replay[-1].payload["replayed"] is True
        assert len(activation.requests) == 2
        transcript = await sessions.recent_transcript(identity, limit=20)
        assert [item.type for item in transcript].count(TranscriptEventType.ASSISTANT_MESSAGE) == 1
    asyncio.run(check())


def test_tool_started_after_checkpoint_marks_cancelled_run_unsafe_to_continue():
    async def check():
        sessions, grants, scope, message, _ = _fixture()
        activation = _UnsafeAfterCheckpointActivation()
        app = ChannelApplication(activation=activation, sessions=sessions, grants=grants)
        source = app.handle(message, scope)
        assert (await anext(source)).kind is OutboundEventKind.STARTED
        tool_status = await anext(source)
        assert tool_status.payload["type"] == "tool_started"
        pending = asyncio.create_task(anext(source))
        await activation.waiting.wait()
        assert await app.stop_message(scope, target_message_id=message.message_id) is RunStopOutcome.REQUESTED
        terminal = await asyncio.wait_for(pending, 3)
        assert terminal.payload["resume_unsafe"] is True
        await source.aclose()
        continuation = message.model_copy(update={
            "event_id": "continue-event2", "message_id": "continue-message2", "text": "",
        })
        try:
            _ = [item async for item in app.continue_task(
                continuation, scope, target_message_id=message.message_id,
            )]
        except Exception as exc:
            assert "safe continuation point" in str(exc)
        else:
            raise AssertionError("unsafe tool boundary was accepted")
    asyncio.run(check())


def test_stop_observer_retries_until_runner_registration_exists():
    async def check():
        sessions, grants, scope, message, _ = _fixture()
        activation = _LateSuccessActivation(cancel_misses=1)
        app = ChannelApplication(activation=activation, sessions=sessions, grants=grants)
        source = app.handle(message, scope)
        assert (await anext(source)).kind is OutboundEventKind.STARTED
        assert (await anext(source)).kind is OutboundEventKind.DELTA
        pending = asyncio.create_task(anext(source))
        await activation.waiting.wait()
        assert await app.stop_message(scope, target_message_id=message.message_id) is RunStopOutcome.REQUESTED
        assert (await asyncio.wait_for(pending, 3)).payload["run_status"] == "cancelled"
        assert activation.cancel_attempts >= 2
        await source.aclose()
    asyncio.run(check())


def test_retry_recovers_terminal_transcript_written_before_terminal_checkpoint():
    async def check():
        _, grants, scope, message, identity = _fixture()
        sessions = _CrashAfterTerminalStore()
        activation = _ImmediateSuccessActivation()
        app = ChannelApplication(activation=activation, sessions=sessions, grants=grants)
        try:
            _ = [event async for event in app.handle(message, scope)]
        except RuntimeError as exc:
            assert "simulated checkpoint crash" in str(exc)
        else:
            raise AssertionError("terminal checkpoint crash was not injected")
        transcript = await sessions.recent_transcript(identity, limit=10)
        assert transcript[-1].type is TranscriptEventType.ASSISTANT_MESSAGE
        replay = [event async for event in app.handle(message, scope)]
        assert replay[-1].kind is OutboundEventKind.COMPLETED
        assert replay[-1].payload["output"] == "已完成"
        assert replay[-1].payload["replayed"] is True
        assert activation.calls == 1
    asyncio.run(check())


def test_newer_durable_user_blocks_continuing_an_older_cancelled_run():
    async def check():
        sessions, grants, scope, message, identity = _fixture()
        activation = _LateSuccessActivation()
        app = ChannelApplication(activation=activation, sessions=sessions, grants=grants)
        source = app.handle(message, scope)
        assert (await anext(source)).kind is OutboundEventKind.STARTED
        assert (await anext(source)).kind is OutboundEventKind.DELTA
        pending = asyncio.create_task(anext(source))
        await activation.waiting.wait()
        assert await app.stop_message(scope, target_message_id=message.message_id) is RunStopOutcome.REQUESTED
        assert (await asyncio.wait_for(pending, 3)).payload["run_status"] == "cancelled"
        await source.aclose()
        current = await sessions.get(identity)
        await sessions.append(
            identity, event_id="newer-user-before-started-checkpoint",
            event_type=TranscriptEventType.USER_MESSAGE,
            payload={
                "schema": "suiteharness.channel-user-message.v1",
                "authenticated_principal_id": "seller",
                "message": message.model_copy(update={"message_id": "message2"}).model_dump(mode="json"),
            },
            expected_revision=current.revision,
        )
        continuation = message.model_copy(update={
            "event_id": "continue-event3", "message_id": "continue-message3", "text": "",
        })
        try:
            _ = [item async for item in app.continue_task(
                continuation, scope, target_message_id=message.message_id,
            )]
        except Exception as exc:
            assert "safe continuation point" in str(exc)
        else:
            raise AssertionError("older cancelled run continued past a newer durable input")
    asyncio.run(check())


@pytest.mark.parametrize("interleave", ["source", "user"])
def test_continuation_rechecks_preflight_after_entering_execution(interleave):
    async def check():
        _, grants, scope, message, identity = _fixture()
        sessions = _InterleavingStore()
        activation = _LateSuccessActivation()
        app = ChannelApplication(activation=activation, sessions=sessions, grants=grants)
        source = app.handle(message, scope)
        assert (await anext(source)).kind is OutboundEventKind.STARTED
        assert (await anext(source)).kind is OutboundEventKind.DELTA
        pending = asyncio.create_task(anext(source))
        await activation.waiting.wait()
        assert await app.stop_message(scope, target_message_id=message.message_id) is RunStopOutcome.REQUESTED
        assert (await asyncio.wait_for(pending, 3)).payload["run_status"] == "cancelled"
        await source.aclose()
        sessions.arm(interleave, message)
        continuation = message.model_copy(update={
            "event_id": f"continue-{interleave}",
            "message_id": f"continue-{interleave}", "text": "",
        })
        with pytest.raises(Exception, match="conversation changed"):
            _ = [item async for item in app.continue_task(
                continuation, scope, target_message_id=message.message_id,
            )]
        assert len(activation.requests) == 1
        assert (await sessions.get(identity)) is not None
    asyncio.run(check())


def test_real_runner_continuation_narrows_a_mixed_grant_to_read_only():
    async def check():
        sessions, _, scope, message, _ = _fixture()
        registry = InMemoryToolRegistry()
        authority = InMemoryCapabilityAuthority()
        read_identity = ToolIdentity(
            namespace="suiteharness", name="suiteharness.fs.read",
            origin="suiteharness.builtin.fs", version="1",
        )
        write_identity = ToolIdentity(
            namespace="suiteharness", name="suiteharness.fs.write",
            origin="suiteharness.builtin.fs", version="1",
        )

        async def handler(context, arguments):
            return {"ok": True}

        registry.register_protected(
            ToolSpec(name=read_identity.name, effects=frozenset({ToolEffect.READ}),
                     input_schema={"type": "object"}),
            handler, identity=read_identity,
        )
        registry.register_protected(
            ToolSpec(name=write_identity.name, effects=frozenset({ToolEffect.WRITE}),
                     input_schema={"type": "object"}),
            handler, identity=write_identity,
        )
        grants = ConfiguredGrantIssuer(
            registry=registry, authority=authority,
            templates=[ToolAccessTemplate(
                channel_id="web", product_id="product",
                read_aliases=(read_identity.name,), write_aliases=(write_identity.name,),
            )],
        )
        runner = ExecutionRunner(
            tools=registry, capabilities=authority,
            approvals=InMemoryApprovalStore(), journal=InMemoryAuditJournal(),
        )
        model = _PauseThenAnswerModel()
        strategies = ExecutionStrategies(workflow=ReActWorkflow(model, "chat"))

        class Activation:
            tenant_id = "company"

            def stream(self, request):
                return runner.stream(request, strategies)

            async def cancel(self, request_scope, run_id):
                return await runner.cancel(request_scope, run_id)

        app = ChannelApplication(activation=Activation(), sessions=sessions, grants=grants)
        source = app.handle(message, scope)
        assert (await anext(source)).kind is OutboundEventKind.STARTED
        terminal = asyncio.create_task(anext(source))
        await asyncio.wait_for(model.waiting.wait(), 1)
        assert await app.stop_message(scope, target_message_id=message.message_id) is RunStopOutcome.REQUESTED
        assert (await asyncio.wait_for(terminal, 3)).payload["run_status"] == "cancelled"
        await source.aclose()

        continuation = message.model_copy(update={
            "event_id": "mixed-continue", "message_id": "mixed-continue", "text": "",
        })
        events = [event async for event in app.continue_task(
            continuation, scope, target_message_id=message.message_id,
        )]
        assert events[-1].kind is OutboundEventKind.COMPLETED
        assert events[-1].payload["output"] == "continued"
        assert model.request_tool_names[-1] == ("suiteharness.fs.read",)
    asyncio.run(check())


class _LateSuccessActivation:
    tenant_id = "company"

    def __init__(self, *, cancel_misses: int = 0) -> None:
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled: list[str] = []
        self.requests: list = []
        self.cancel_misses = cancel_misses
        self.cancel_attempts = 0

    async def cancel(self, scope: RequestScope, run_id: str) -> bool:
        self.cancel_attempts += 1
        if self.cancel_attempts <= self.cancel_misses:
            return False
        self.cancelled.append(run_id)
        self.release.set()
        return True

    async def stream(self, request):
        self.requests.append(request)
        now = datetime.now(UTC)
        yield RunEvent(run_id=request.run_id, sequence=1, kind="started")
        if request.resume is not None:
            yield RunEvent(
                run_id=request.run_id, sequence=2, kind="completed",
                result=RunResult(
                    run_id=request.run_id, status=RunStatus.SUCCEEDED,
                    output="安全续跑回答", iterations_used=1, tool_calls_used=0,
                    started_at=now, finished_at=datetime.now(UTC),
                ),
            )
            return
        point = RunResumeState(
            source_run_id=request.run_id, iterations_used=0, tool_calls_used=0,
            prompt_digest="a" * 64, route_key="test-model-v1",
        )
        yield RunEvent(
            run_id=request.run_id, sequence=2, kind="checkpoint",
            data={
                "resume_state": point.model_dump(mode="json"),
                "input": request.input,
                "conversation": request.conversation.model_dump(mode="json"),
            },
        )
        yield RunEvent(run_id=request.run_id, sequence=3, kind="delta", text="尚未完成")
        self.waiting.set()
        await self.release.wait()
        # Even if a non-cooperative model returns success after cancellation,
        # the durable stop intent must prevent success output from committing.
        yield RunEvent(
            run_id=request.run_id, sequence=4, kind="completed",
            result=RunResult(
                run_id=request.run_id, status=RunStatus.SUCCEEDED,
                output="不应保存的迟到回答", iterations_used=1, tool_calls_used=0,
                started_at=now, finished_at=datetime.now(UTC),
            ),
        )


class _UnsafeAfterCheckpointActivation(_LateSuccessActivation):
    async def stream(self, request):
        self.requests.append(request)
        now = datetime.now(UTC)
        yield RunEvent(run_id=request.run_id, sequence=1, kind="started")
        point = RunResumeState(
            source_run_id=request.run_id, iterations_used=0, tool_calls_used=0,
            prompt_digest="b" * 64, route_key="test-model-v1",
        )
        yield RunEvent(
            run_id=request.run_id, sequence=2, kind="checkpoint",
            data={
                "resume_state": point.model_dump(mode="json"),
                "input": request.input,
                "conversation": request.conversation.model_dump(mode="json"),
            },
        )
        yield RunEvent(run_id=request.run_id, sequence=3, kind="tool_started")
        self.waiting.set()
        await self.release.wait()
        yield RunEvent(
            run_id=request.run_id, sequence=4, kind="completed",
            result=RunResult(
                run_id=request.run_id, status=RunStatus.SUCCEEDED,
                output="不应保存", iterations_used=1, tool_calls_used=1,
                started_at=now, finished_at=datetime.now(UTC),
            ),
        )


class _ImmediateSuccessActivation:
    tenant_id = "company"

    def __init__(self) -> None:
        self.calls = 0

    async def cancel(self, scope, run_id):
        return False

    async def stream(self, request):
        self.calls += 1
        now = datetime.now(UTC)
        yield RunEvent(run_id=request.run_id, sequence=1, kind="started")
        yield RunEvent(
            run_id=request.run_id, sequence=2, kind="completed",
            result=RunResult(
                run_id=request.run_id, status=RunStatus.SUCCEEDED, output="已完成",
                iterations_used=1, tool_calls_used=0,
                started_at=now, finished_at=datetime.now(UTC),
            ),
        )


class _CrashAfterTerminalStore(InMemorySessionStore):
    def __init__(self) -> None:
        super().__init__()
        self._terminal_appended = False
        self._crashed = False

    async def append(self, identity, **kwargs):
        event = await super().append(identity, **kwargs)
        if kwargs["event_type"] in {
            TranscriptEventType.ASSISTANT_MESSAGE,
            TranscriptEventType.ERROR,
            TranscriptEventType.CANCEL,
        }:
            self._terminal_appended = True
        return event

    async def save_checkpoint(self, identity, **kwargs):
        if self._terminal_appended and not self._crashed:
            self._crashed = True
            raise RuntimeError("simulated checkpoint crash")
        return await super().save_checkpoint(identity, **kwargs)


class _InterleavingStore(InMemorySessionStore):
    def __init__(self) -> None:
        super().__init__()
        self._interleave = None
        self._message = None

    def arm(self, interleave, message) -> None:
        self._interleave = interleave
        self._message = message

    async def create(self, identity):
        record = await super().create(identity)
        interleave, self._interleave = self._interleave, None
        if interleave == "source":
            await super().append(
                identity, event_id="interleaved-source",
                event_type=TranscriptEventType.MEMORY_INVALIDATED,
                payload={
                    "schema": "suiteharness.memory-invalidation.v1",
                    "reason": "source changed", "source_namespace": "crm",
                    "source_version": 1,
                },
                expected_revision=record.revision,
            )
        elif interleave == "user":
            await super().append(
                identity, event_id="interleaved-user",
                event_type=TranscriptEventType.USER_MESSAGE,
                payload={
                    "schema": "suiteharness.channel-user-message.v1",
                    "authenticated_principal_id": "seller",
                    "message": self._message.model_copy(
                        update={"message_id": "newer-message"}
                    ).model_dump(mode="json"),
                },
                expected_revision=record.revision,
            )
        return record


class _PauseThenAnswerModel:
    def __init__(self) -> None:
        self.waiting = asyncio.Event()
        self.calls = 0
        self.request_tool_names = []

    async def stream(self, route, request):
        self.calls += 1
        self.request_tool_names.append(tuple(tool.name for tool in request.tools))
        if self.calls == 1:
            self.waiting.set()
            await asyncio.Event().wait()
        yield ModelStreamEvent(type=ModelStreamEventType.TEXT_DELTA, text_delta="continued")
        yield ModelStreamEvent(
            type=ModelStreamEventType.MESSAGE_END, finish_reason=FinishReason.STOP,
        )
