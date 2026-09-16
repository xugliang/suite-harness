"""Feishu control commands stay on the authenticated channel boundary."""

import asyncio
import json
from datetime import UTC, datetime

from suiteharness.channels import (
    AuthenticatedPrincipal,
    ChannelKind,
    InboundMessage,
    OutboundEvent,
    OutboundEventKind,
)
from suiteharness.channels.dispatch import FeishuDispatcher
from suiteharness.channels.feishu import FeishuEventOutcome
from suiteharness.channels.feishu.processor import FeishuEventProcessor
from suiteharness.channels.gateway import EnterpriseChannelGateway, ProductRouter
from suiteharness.execution.in_memory import InMemoryCapabilityAuthority, InMemoryToolRegistry
from suiteharness.server.access import ConfiguredGrantIssuer, ToolAccessTemplate
from suiteharness.server.application import ChannelApplication
from suiteharness.sessions import InMemorySessionStore, RunStopOutcome, SessionRunClaimState


def _payload(
    *,
    event_id: str = "event-1",
    message_id: str = "command-1",
    text: str,
    chat_id: str = "chat-1",
    chat_type: str = "p2p",
    root_id: str | None = None,
    sender_external_id: str = "employee-1",
) -> dict:
    message = {
        "message_type": "text",
        "message_id": message_id,
        "chat_id": chat_id,
        "chat_type": chat_type,
        "content": json.dumps({"text": text}),
    }
    if root_id is not None:
        message["root_id"] = root_id
    if chat_type != "p2p":
        message["mentions"] = [{"id": {"open_id": "bot-1"}, "key": "@bot"}]
    return {
        "header": {
            "event_type": "im.message.receive_v1",
            "event_id": event_id,
            "create_time": "1700000000000",
        },
        "event": {
            "sender": {"sender_id": {"open_id": sender_external_id}},
            "message": message,
        },
    }


def test_target_stop_command_emits_one_terminal_reply_without_dispatching_text():
    """Removing command handling would dispatch the control text into the model."""

    class Dispatcher(FeishuDispatcher):
        def __init__(self) -> None:
            self.dispatched: list[InboundMessage] = []
            self.stops: list[tuple[InboundMessage, str | None]] = []

        def dispatch(self, message: InboundMessage):
            self.dispatched.append(message)

            async def source():
                yield OutboundEvent(
                    kind=OutboundEventKind.COMPLETED,
                    request_id="ordinary-request",
                    correlation_id="ordinary-correlation",
                    payload={"output": "ordinary"},
                )

            return source()

        async def stop(
            self, message: InboundMessage, target_message_id: str | None = None
        ) -> RunStopOutcome:
            self.stops.append((message, target_message_id))
            return RunStopOutcome.REQUESTED

    async def check() -> None:
        dispatcher = Dispatcher()
        delivered: list[tuple[InboundMessage, OutboundEvent]] = []
        processor = FeishuEventProcessor(
            dispatcher,
            bot_open_id="bot-1",
            outbound_sink=lambda message, event: _deliver(delivered, message, event),
        )

        outcome = await processor.process_payload(_payload(text="/停止 origin-message-42"))

        assert outcome is FeishuEventOutcome.DISPATCHED
        assert dispatcher.dispatched == []
        assert [(message.message_id, target) for message, target in dispatcher.stops] == [
            ("command-1", "origin-message-42")
        ]
        assert len(delivered) == 1
        assert delivered[0][1].kind is OutboundEventKind.COMPLETED
        assert delivered[0][1].payload == {"output": "已请求停止。"}

    asyncio.run(check())


async def _deliver(
    delivered: list[tuple[InboundMessage, OutboundEvent]],
    message: InboundMessage,
    event: OutboundEvent,
) -> None:
    delivered.append((message, event))


class _RecordingDispatcher(FeishuDispatcher):
    def __init__(self, outcomes: list[object] | None = None) -> None:
        self.dispatched: list[InboundMessage] = []
        self.stops: list[tuple[InboundMessage, str | None]] = []
        self._outcomes = outcomes or [RunStopOutcome.REQUESTED]

    def dispatch(self, message: InboundMessage):
        self.dispatched.append(message)

        async def source():
            yield OutboundEvent(
                kind=OutboundEventKind.COMPLETED,
                request_id="ordinary-request",
                correlation_id="ordinary-correlation",
                payload={"output": "ordinary"},
            )

        return source()

    async def stop(
        self, message: InboundMessage, target_message_id: str | None = None
    ) -> RunStopOutcome:
        self.stops.append((message, target_message_id))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, RunStopOutcome)
        return outcome


def _processor(dispatcher: FeishuDispatcher, delivered: list[tuple[InboundMessage, OutboundEvent]]):
    return FeishuEventProcessor(
        dispatcher,
        bot_open_id="bot-1",
        outbound_sink=lambda message, event: _deliver(delivered, message, event),
    )


def test_stop_without_target_and_terminal_outcomes_use_one_completed_reply():
    """Changing outcome mapping would lose a user-visible, idempotent terminal result."""

    async def check() -> None:
        dispatcher = _RecordingDispatcher(
            [
                RunStopOutcome.REQUESTED,
                RunStopOutcome.ALREADY_REQUESTED,
                RunStopOutcome.ALREADY_FINISHED,
            ]
        )
        delivered: list[tuple[InboundMessage, OutboundEvent]] = []
        processor = _processor(dispatcher, delivered)
        for index in range(3):
            assert await processor.process_payload(
                _payload(event_id=f"event-outcome-{index}", text="/停止")
            ) is FeishuEventOutcome.DISPATCHED

        assert [target for _message, target in dispatcher.stops] == [None, None, None]
        assert [event.kind for _message, event in delivered] == [
            OutboundEventKind.COMPLETED,
            OutboundEventKind.COMPLETED,
            OutboundEventKind.COMPLETED,
        ]
        assert [event.payload["output"] for _message, event in delivered] == [
            "已请求停止。",
            "停止请求已存在。",
            "任务已结束。",
        ]

    asyncio.run(check())


def test_stop_command_uses_normalized_group_thread_message_without_client_scope():
    """Changing group normalization could let a control command leave its durable scope."""

    async def check() -> None:
        dispatcher = _RecordingDispatcher()
        delivered: list[tuple[InboundMessage, OutboundEvent]] = []
        processor = _processor(dispatcher, delivered)

        outcome = await processor.process_payload(
            _payload(
                event_id="group-event",
                text="@bot /停止 origin-message-42",
                chat_id="group-1",
                chat_type="group",
                root_id="thread-1",
            )
        )

        assert outcome is FeishuEventOutcome.DISPATCHED
        assert dispatcher.dispatched == []
        message, target = dispatcher.stops[0]
        assert target == "origin-message-42"
        assert message.conversation_id == "group-1"
        assert message.metadata["session_scope_id"] == "group-1:thread-1"
        assert message.metadata["root_id"] == "thread-1"
        assert len(delivered) == 1

    asyncio.run(check())


def test_duplicate_stop_event_neither_repeats_stop_nor_acknowledges_again():
    """Removing deduplication from commands would send a second stop acknowledgement."""

    async def check() -> None:
        dispatcher = _RecordingDispatcher()
        delivered: list[tuple[InboundMessage, OutboundEvent]] = []
        processor = _processor(dispatcher, delivered)
        payload = _payload(text="/停止 origin-message-42")

        assert await processor.process_payload(payload) is FeishuEventOutcome.DISPATCHED
        assert await processor.process_payload(payload) is FeishuEventOutcome.DUPLICATE
        assert len(dispatcher.stops) == 1
        assert len(delivered) == 1

    asyncio.run(check())


def test_stop_rejections_and_dispatcher_errors_emit_failed_terminal_reply():
    """Treating rejection as success would hide an unfulfilled control request."""

    async def check() -> None:
        dispatcher = _RecordingDispatcher(
            [RunStopOutcome.NOT_FOUND, PermissionError(), RuntimeError()]
        )
        delivered: list[tuple[InboundMessage, OutboundEvent]] = []
        processor = _processor(dispatcher, delivered)
        for index in range(3):
            assert await processor.process_payload(
                _payload(event_id=f"event-failure-{index}", text="/停止 origin-message-42")
            ) is FeishuEventOutcome.DISPATCHED

        assert dispatcher.dispatched == []
        assert [event.kind for _message, event in delivered] == [
            OutboundEventKind.FAILED,
            OutboundEventKind.FAILED,
            OutboundEventKind.FAILED,
        ]
        assert [event.payload["failure"]["message"] for _message, event in delivered] == [
            "未找到可停止的任务。",
            "无权停止该任务。",
            "停止请求失败。",
        ]

    asyncio.run(check())


def test_old_dispatcher_and_ordinary_text_keep_safe_fallbacks():
    """Removing either branch would break backward compatibility or model messages."""

    class OldDispatcher(FeishuDispatcher):
        def __init__(self) -> None:
            self.dispatched: list[InboundMessage] = []

        def dispatch(self, message: InboundMessage):
            self.dispatched.append(message)

            async def source():
                yield OutboundEvent(
                    kind=OutboundEventKind.COMPLETED,
                    request_id="ordinary-request",
                    correlation_id="ordinary-correlation",
                    payload={"output": "ordinary"},
                )

            return source()

    async def check() -> None:
        old_dispatcher = OldDispatcher()
        old_delivered: list[tuple[InboundMessage, OutboundEvent]] = []
        old_processor = _processor(old_dispatcher, old_delivered)
        assert await old_processor.process_payload(
            _payload(event_id="old-command", text="/停止")
        ) is FeishuEventOutcome.DISPATCHED
        assert old_dispatcher.dispatched == []
        assert old_delivered[0][1].kind is OutboundEventKind.FAILED
        assert old_delivered[0][1].payload["failure"]["message"] == "停止功能暂不可用。"

        dispatcher = _RecordingDispatcher()
        delivered: list[tuple[InboundMessage, OutboundEvent]] = []
        processor = _processor(dispatcher, delivered)
        assert await processor.process_payload(
            _payload(event_id="ordinary-text", text="请帮我处理这个问题")
        ) is FeishuEventOutcome.DISPATCHED
        assert [message.text for message in dispatcher.dispatched] == ["请帮我处理这个问题"]
        assert dispatcher.stops == []
        assert delivered[0][1].payload == {"output": "ordinary"}

    asyncio.run(check())


class _Directory:
    def __init__(self) -> None:
        self._principals = {
            "employee-1": AuthenticatedPrincipal(
                tenant_id="company", principal_id="employee-a"
            ),
            "employee-2": AuthenticatedPrincipal(
                tenant_id="company", principal_id="employee-b"
            ),
        }

    async def authenticate(self, message: InboundMessage) -> AuthenticatedPrincipal | None:
        return self._principals.get(message.sender_external_id)


class _NoModelActivation:
    tenant_id = "company"

    def __init__(self) -> None:
        self.stream_calls = 0

    async def stream(self, _request: object):
        self.stream_calls += 1
        if False:
            yield None


def _gateway_fixture() -> tuple[
    EnterpriseChannelGateway,
    ChannelApplication,
    InMemorySessionStore,
    _NoModelActivation,
]:
    sessions = InMemorySessionStore()
    registry = InMemoryToolRegistry()
    authority = InMemoryCapabilityAuthority()
    grants = ConfiguredGrantIssuer(
        registry=registry,
        authority=authority,
        templates=[ToolAccessTemplate(channel_id="feishu", product_id="product")],
    )
    activation = _NoModelActivation()
    application = ChannelApplication(
        activation=activation,
        sessions=sessions,
        grants=grants,
    )
    gateway = EnterpriseChannelGateway(
        tenant_id="company",
        authenticator=_Directory(),
        router=ProductRouter(("product",)),
        application=application,
        agent_ids={"product": "assistant"},
    )
    return gateway, application, sessions, activation


def _message(
    *,
    event_id: str,
    message_id: str,
    sender_external_id: str,
    conversation_id: str,
    session_scope_id: str,
) -> InboundMessage:
    return InboundMessage(
        channel=ChannelKind.FEISHU,
        event_id=event_id,
        message_id=message_id,
        conversation_id=conversation_id,
        sender_external_id=sender_external_id,
        text="ordinary message",
        received_at=datetime.now(UTC),
        metadata={"session_scope_id": session_scope_id},
    )


async def _claim_message_run(
    gateway: EnterpriseChannelGateway,
    application: ChannelApplication,
    sessions: InMemorySessionStore,
    message: InboundMessage,
) -> tuple[object, str]:
    principal = await _Directory().authenticate(message)
    assert principal is not None
    scope = await gateway._scope_for(message, principal)
    identity = application._session_identity(scope)
    await sessions.create(identity)
    run_id = application._stable_id(
        "run",
        identity.tenant_id,
        identity.product_id,
        identity.agent_id,
        identity.session_id,
        identity.principal_id,
        scope.principal_id,
        scope.channel_id,
        message.message_id,
    )
    assert await sessions.claim_run(
        identity,
        run_id=run_id,
        owner_token="o" * 32,
        lease_seconds=60,
    ) is SessionRunClaimState.ACQUIRED
    return identity, run_id


def test_gateway_pipeline_does_not_allow_employee_b_to_stop_employee_a_run():
    """Removing gateway-derived identity isolation would stop a different employee's claim."""

    async def check() -> None:
        gateway, application, sessions, activation = _gateway_fixture()
        target = _message(
            event_id="target-a-event",
            message_id="target-a-message",
            sender_external_id="employee-1",
            conversation_id="private-a",
            session_scope_id="employee-1",
        )
        identity, run_id = await _claim_message_run(
            gateway, application, sessions, target
        )
        delivered: list[tuple[InboundMessage, OutboundEvent]] = []
        processor = _processor(gateway, delivered)

        assert await processor.process_payload(
            _payload(
                event_id="employee-b-stop",
                text="/停止 target-a-message",
                sender_external_id="employee-2",
            )
        ) is FeishuEventOutcome.DISPATCHED

        assert not await sessions.stop_requested(identity, run_id=run_id)
        assert activation.stream_calls == 0
        assert len(delivered) == 1
        assert delivered[0][1].kind is OutboundEventKind.FAILED

    asyncio.run(check())


def test_gateway_pipeline_does_not_stop_a_different_group_thread_run():
    """Removing trusted thread scope from routing would cross-stop a sibling thread."""

    async def check() -> None:
        gateway, application, sessions, activation = _gateway_fixture()
        target = _message(
            event_id="thread-a-event",
            message_id="thread-a-message",
            sender_external_id="employee-1",
            conversation_id="group-1",
            session_scope_id="group-1:thread-a",
        )
        identity, run_id = await _claim_message_run(
            gateway, application, sessions, target
        )
        delivered: list[tuple[InboundMessage, OutboundEvent]] = []
        processor = _processor(gateway, delivered)

        assert await processor.process_payload(
            _payload(
                event_id="thread-b-stop",
                text="@bot /停止 thread-a-message",
                chat_id="group-1",
                chat_type="group",
                root_id="thread-b",
            )
        ) is FeishuEventOutcome.DISPATCHED

        assert not await sessions.stop_requested(identity, run_id=run_id)
        assert activation.stream_calls == 0
        assert len(delivered) == 1
        assert delivered[0][1].kind is OutboundEventKind.FAILED

    asyncio.run(check())


def test_gateway_pipeline_rejects_bare_stop_when_scope_has_multiple_runs():
    """Removing the unique-active-run check would stop an arbitrary authorized claim."""

    async def check() -> None:
        gateway, application, sessions, activation = _gateway_fixture()
        command = _message(
            event_id="ambiguous-stop",
            message_id="command-message",
            sender_external_id="employee-1",
            conversation_id="chat-1",
            session_scope_id="employee-1",
        )
        principal = await _Directory().authenticate(command)
        assert principal is not None
        scope = await gateway._scope_for(command, principal)
        identity = application._session_identity(scope)
        await sessions.create(identity)
        for run_id in ("active-run-a", "active-run-b"):
            assert await sessions.claim_run(
                    identity,
                    run_id=run_id,
                    owner_token="o" * 32,
                lease_seconds=60,
            ) is SessionRunClaimState.ACQUIRED
        delivered: list[tuple[InboundMessage, OutboundEvent]] = []
        processor = _processor(gateway, delivered)

        assert await processor.process_payload(
            _payload(event_id="ambiguous-stop", text="/停止")
        ) is FeishuEventOutcome.DISPATCHED

        assert not await sessions.stop_requested(identity, run_id="active-run-a")
        assert not await sessions.stop_requested(identity, run_id="active-run-b")
        assert activation.stream_calls == 0
        assert len(delivered) == 1
        assert delivered[0][1].kind is OutboundEventKind.FAILED

    asyncio.run(check())
