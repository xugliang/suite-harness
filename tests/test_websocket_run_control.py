"""Authenticated WebSocket stop, history, and continuation controls."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest


def test_control_frames_require_bounded_stable_identifiers_and_a_positive_limit():
    from suiteharness.channels.websocket.models import (
        ContinueFrame,
        HistoryFrame,
        StopFrame,
        WebSocketProtocolError,
        parse_client_frame,
    )

    assert isinstance(
        parse_client_frame(
            '{"type":"stop","stop_id":"stop-1","target_client_message_id":"message-1",'
            '"conversation_id":"conversation-1","product_id":"product-1"}'
        ),
        StopFrame,
    )
    assert isinstance(
        parse_client_frame(
            '{"type":"history","history_id":"history-1","conversation_id":"conversation-1",'
            '"limit":20}'
        ),
        HistoryFrame,
    )
    assert isinstance(
        parse_client_frame(
            '{"type":"continue","client_message_id":"message-2",'
            '"target_client_message_id":"message-1","conversation_id":"conversation-1"}'
        ),
        ContinueFrame,
    )

    for invalid in (
        '{"type":"stop","stop_id":"stop-1","target_client_message_id":"bad id",'
        '"conversation_id":"conversation-1"}',
        '{"type":"history","history_id":"history-1","conversation_id":"conversation-1",'
        '"limit":0}',
        '{"type":"continue","client_message_id":"message-2",'
        '"target_client_message_id":"message-1","conversation_id":"conversation-1","extra":true}',
    ):
        with pytest.raises(WebSocketProtocolError) as error:
            parse_client_frame(invalid)
        assert error.value.code == "invalid_frame"


def test_stop_history_and_continue_use_authenticated_dispatcher_without_leaking_audit_payloads():
    from suiteharness.channels import AuthenticatedPrincipal, OutboundEvent, OutboundEventKind
    from suiteharness.channels.approvals import InteractiveApprovalCoordinator
    from suiteharness.channels.websocket import WebSocketApprovalHub, WebSocketHandshake
    from suiteharness.channels.websocket.models import WebSocketPacket, WebSocketPacketKind
    from suiteharness.channels.websocket.server import WebSocketSession
    from suiteharness.sessions import SessionIdentity, TranscriptEvent, TranscriptEventType

    async def check():
        principal = AuthenticatedPrincipal(tenant_id="company", principal_id="seller")

        class Connection:
            def __init__(self):
                self.inbound: asyncio.Queue[WebSocketPacket] = asyncio.Queue()
                self.outbound: asyncio.Queue[dict[str, object]] = asyncio.Queue()

            async def receive(self):
                return await self.inbound.get()

            async def send_text(self, text):
                await self.outbound.put(json.loads(text))

            async def close(self, **_kwargs):
                await self.inbound.put(WebSocketPacket(WebSocketPacketKind.DISCONNECT))

            async def command(self, payload):
                await self.inbound.put(WebSocketPacket(WebSocketPacketKind.TEXT, json.dumps(payload)))

        class Authenticator:
            async def authenticate(self, _handshake):
                return principal

        identity = SessionIdentity(
            tenant_id="company",
            product_id="product",
            agent_id="agent",
            session_id="session",
            principal_id="seller",
        )
        transcript = (
            TranscriptEvent(
                identity=identity,
                event_id="user-event",
                sequence=1,
                revision=1,
                type=TranscriptEventType.USER_MESSAGE,
                payload={
                    "authenticated_principal_id": "seller",
                    "message": {"message_id": "message-1", "text": "question", "metadata": {"secret": "no"}},
                },
                occurred_at=datetime(2026, 9, 16, tzinfo=UTC),
            ),
            TranscriptEvent(
                identity=identity,
                event_id="assistant-event",
                sequence=2,
                revision=2,
                type=TranscriptEventType.ASSISTANT_MESSAGE,
                payload={
                    "run_id": "run-1",
                    "output": {"summary": "answer"},
                    "tool_result": "secret",
                },
                occurred_at=datetime(2026, 9, 16, 0, 0, 1, tzinfo=UTC),
            ),
            TranscriptEvent(
                identity=identity,
                event_id="cancel-event",
                sequence=3,
                revision=3,
                type=TranscriptEventType.CANCEL,
                payload={
                    "run_id": "run-1",
                    "partial_output": "unfinished",
                    "incomplete": True,
                    "internal": "secret",
                },
                occurred_at=datetime(2026, 9, 16, 0, 0, 2, tzinfo=UTC),
            ),
            TranscriptEvent(
                identity=identity,
                event_id="tool-event",
                sequence=4,
                revision=4,
                type=TranscriptEventType.TOOL_RESULT,
                payload={"run_id": "run-1", "tool_result": "must not display"},
                occurred_at=datetime(2026, 9, 16, 0, 0, 3, tzinfo=UTC),
            ),
        )

        class Dispatcher:
            def __init__(self):
                self.stops: list[tuple[str, str, str]] = []
                self.continues: list[str] = []

            async def dispatch_authenticated(self, message, authenticated):
                assert authenticated == principal
                yield OutboundEvent(
                    kind=OutboundEventKind.COMPLETED,
                    request_id="request-message",
                    correlation_id="correlation-message",
                    payload={"output": message.text},
                )

            async def stop_authenticated(self, message, authenticated, target_message_id):
                assert authenticated == principal
                assert message.channel.value == "web"
                self.stops.append((message.message_id, message.conversation_id, target_message_id))
                return "requested"

            async def history_authenticated(self, message, authenticated, limit):
                assert authenticated == principal
                assert message.message_id == "history-1"
                assert limit == 20
                return transcript

            async def continue_authenticated(self, message, authenticated, target_message_id):
                assert authenticated == principal
                self.continues.append(target_message_id)
                yield OutboundEvent(
                    kind=OutboundEventKind.STARTED,
                    request_id="request-continue",
                    correlation_id="correlation-continue",
                    payload={"run_id": "new-run"},
                )

        connection, dispatcher = Connection(), Dispatcher()
        approvals = InteractiveApprovalCoordinator(WebSocketApprovalHub().publish)
        session = WebSocketSession(
            connection,
            connection_id="connection-1",
            principal=principal,
            handshake=WebSocketHandshake(origin="https://example.test"),
            authenticator=Authenticator(),
            revalidator=None,
            dispatcher=dispatcher,
            approvals=approvals,
            approval_hub=WebSocketApprovalHub(),
            max_concurrent_messages=1,
        )
        running = asyncio.create_task(session.run())

        async def receive():
            return await asyncio.wait_for(connection.outbound.get(), 1)

        try:
            await connection.command({"type": "stop", "stop_id": "stop-1", "target_client_message_id": "message-1", "conversation_id": "conversation-1"})
            assert await receive() == {"type": "stop_result", "stop_id": "stop-1", "outcome": "requested"}
            assert dispatcher.stops == [("stop-1", "conversation-1", "message-1")]

            await connection.command({"type": "history", "history_id": "history-1", "conversation_id": "conversation-1", "limit": 20})
            history = await receive()
            assert history["type"] == "history_result"
            assert history["history_id"] == "history-1"
            assert history["events"] == [
                {"kind": "user", "run_id": None, "message_id": "message-1", "occurred_at": "2026-09-16T00:00:00Z", "text": "question"},
                {"kind": "assistant", "run_id": "run-1", "occurred_at": "2026-09-16T00:00:01Z", "output": {"summary": "answer"}},
                {"kind": "cancel", "run_id": "run-1", "occurred_at": "2026-09-16T00:00:02Z", "partial_output": "unfinished", "incomplete": True},
            ]
            assert "secret" not in json.dumps(history)

            await connection.command({"type": "continue", "client_message_id": "message-2", "target_client_message_id": "message-1", "conversation_id": "conversation-1"})
            assert (await receive())["type"] == "ack"
            continuation = await receive()
            assert continuation["type"] == "event"
            assert continuation["client_message_id"] == "message-2"
            assert dispatcher.continues == ["message-1"]

            await connection.command({"type": "message", "client_message_id": "message-3", "conversation_id": "conversation-1", "text": "still usable"})
            assert (await receive())["type"] == "ack"
            assert (await receive())["event"]["payload"]["output"] == "still usable"
        finally:
            await connection.inbound.put(WebSocketPacket(WebSocketPacketKind.DISCONNECT))
            await asyncio.wait_for(running, 1)
            await approvals.close()

    asyncio.run(check())


def test_control_errors_are_safe_and_do_not_close_the_connection():
    from suiteharness.channels import AuthenticatedPrincipal, OutboundEvent, OutboundEventKind
    from suiteharness.channels.approvals import InteractiveApprovalCoordinator
    from suiteharness.channels.websocket import WebSocketApprovalHub, WebSocketHandshake
    from suiteharness.channels.websocket.models import WebSocketPacket, WebSocketPacketKind
    from suiteharness.channels.websocket.server import WebSocketSession

    async def check():
        principal = AuthenticatedPrincipal(tenant_id="company", principal_id="seller")

        class Connection:
            def __init__(self):
                self.inbound, self.outbound = asyncio.Queue(), asyncio.Queue()

            async def receive(self): return await self.inbound.get()
            async def send_text(self, text): await self.outbound.put(json.loads(text))
            async def close(self, **_kwargs): await self.inbound.put(WebSocketPacket(WebSocketPacketKind.DISCONNECT))
            async def command(self, payload): await self.inbound.put(WebSocketPacket(WebSocketPacketKind.TEXT, json.dumps(payload)))

        class Authenticator:
            async def authenticate(self, _handshake): return principal

        class Dispatcher:
            async def dispatch_authenticated(self, message, _principal):
                yield OutboundEvent(kind=OutboundEventKind.COMPLETED, request_id="request-1", correlation_id="correlation-1", payload={"output": message.text})
            async def history_authenticated(self, *_args):
                raise PermissionError("another customer")
            async def continue_authenticated(self, *_args):
                if False:
                    yield None
                raise PermissionError("another customer")

        connection = Connection()
        approvals = InteractiveApprovalCoordinator(WebSocketApprovalHub().publish)
        session = WebSocketSession(connection, connection_id="connection-1", principal=principal,
            handshake=WebSocketHandshake(origin="https://example.test"), authenticator=Authenticator(), revalidator=None,
            dispatcher=Dispatcher(), approvals=approvals, approval_hub=WebSocketApprovalHub())
        running = asyncio.create_task(session.run())
        async def receive(): return await asyncio.wait_for(connection.outbound.get(), 1)
        try:
            await connection.command({"type": "history", "history_id": "history-1", "conversation_id": "wrong-customer", "limit": 1})
            assert (await receive())["code"] == "application_error"
            await connection.command({"type": "continue", "client_message_id": "message-1", "target_client_message_id": "other-message", "conversation_id": "wrong-customer"})
            assert (await receive())["type"] == "ack"
            assert (await receive())["code"] == "application_error"
            await connection.command({"type": "message", "client_message_id": "message-2", "conversation_id": "conversation-1", "text": "works"})
            assert (await receive())["type"] == "ack"
            assert (await receive())["event"]["payload"]["output"] == "works"
        finally:
            await connection.inbound.put(WebSocketPacket(WebSocketPacketKind.DISCONNECT))
            await asyncio.wait_for(running, 1)
            await approvals.close()

    asyncio.run(check())


def test_reconnected_stop_targets_only_its_run_and_controls_share_message_capacity():
    """Stop is durable dispatcher work, while continue consumes a stream slot."""
    from suiteharness.channels import AuthenticatedPrincipal, OutboundEvent, OutboundEventKind
    from suiteharness.channels.approvals import InteractiveApprovalCoordinator
    from suiteharness.channels.websocket import WebSocketApprovalHub, WebSocketHandshake
    from suiteharness.channels.websocket.models import WebSocketPacket, WebSocketPacketKind
    from suiteharness.channels.websocket.server import WebSocketSession

    async def check():
        principal = AuthenticatedPrincipal(tenant_id="company", principal_id="seller")

        class Connection:
            def __init__(self):
                self.inbound, self.outbound = asyncio.Queue(), asyncio.Queue()

            async def receive(self):
                return await self.inbound.get()

            async def send_text(self, text):
                await self.outbound.put(json.loads(text))

            async def close(self, **_kwargs):
                await self.inbound.put(WebSocketPacket(WebSocketPacketKind.DISCONNECT))

            async def command(self, payload):
                await self.inbound.put(WebSocketPacket(WebSocketPacketKind.TEXT, json.dumps(payload)))

        class Authenticator:
            async def authenticate(self, _handshake):
                return principal

        class Dispatcher:
            def __init__(self):
                self.active: dict[str, asyncio.Event] = {}
                self.started_two = asyncio.Event()
                self.continue_started = asyncio.Event()
                self.continue_release = asyncio.Event()
                self.stops: list[tuple[str, str]] = []
                self.outcomes: dict[str, str] = {}

            async def dispatch_authenticated(self, message, authenticated):
                assert authenticated == principal
                if message.message_id == "after-stop":
                    yield OutboundEvent(kind=OutboundEventKind.COMPLETED, request_id="request-after", correlation_id="correlation-after", payload={"output": "usable"})
                    return
                release = asyncio.Event()
                self.active[message.message_id] = release
                if {"message-1", "message-2"}.issubset(self.active):
                    self.started_two.set()
                await release.wait()
                yield OutboundEvent(kind=OutboundEventKind.COMPLETED, request_id=f"request-{message.message_id}", correlation_id=f"correlation-{message.message_id}", payload={"output": message.message_id})

            async def stop_authenticated(self, message, authenticated, target_message_id):
                assert authenticated == principal
                self.stops.append((message.metadata["connection_id"], target_message_id))
                outcome = self.outcomes.setdefault(target_message_id, "requested")
                if outcome == "requested":
                    self.outcomes[target_message_id] = "already_requested"
                    self.active[target_message_id].set()
                return outcome

            async def continue_authenticated(self, message, authenticated, target_message_id):
                assert authenticated == principal
                assert target_message_id == "message-1"
                self.continue_started.set()
                await self.continue_release.wait()
                yield OutboundEvent(kind=OutboundEventKind.COMPLETED, request_id="request-continue", correlation_id="correlation-continue", payload={"output": "continued"})

        async def receive(connection):
            return await asyncio.wait_for(connection.outbound.get(), 1)

        connection_a, connection_b = Connection(), Connection()
        dispatcher, hub = Dispatcher(), WebSocketApprovalHub()
        approvals = InteractiveApprovalCoordinator(hub.publish)
        session_a = WebSocketSession(connection_a, connection_id="connection-a", principal=principal,
            handshake=WebSocketHandshake(origin="https://example.test"), authenticator=Authenticator(), revalidator=None,
            dispatcher=dispatcher, approvals=approvals, approval_hub=hub, max_concurrent_messages=2)
        session_b = WebSocketSession(connection_b, connection_id="connection-b", principal=principal,
            handshake=WebSocketHandshake(origin="https://example.test"), authenticator=Authenticator(), revalidator=None,
            dispatcher=dispatcher, approvals=approvals, approval_hub=hub, max_concurrent_messages=2)
        running_a, running_b = asyncio.create_task(session_a.run()), asyncio.create_task(session_b.run())
        try:
            for message_id in ("message-1", "message-2"):
                await connection_a.command({"type": "message", "client_message_id": message_id, "conversation_id": "conversation-1", "text": message_id})
                assert (await receive(connection_a))["type"] == "ack"
            await asyncio.wait_for(dispatcher.started_two.wait(), 1)

            await connection_a.command({"type": "continue", "client_message_id": "continue-1", "target_client_message_id": "message-1", "conversation_id": "conversation-1"})
            assert (await receive(connection_a))["code"] == "too_many_active_messages"

            # Stop is a control frame and must bypass this connection's two
            # occupied message slots. Completion and stop_result may race.
            await connection_a.command({"type": "stop", "stop_id": "stop-1", "target_client_message_id": "message-1", "conversation_id": "conversation-1"})
            first_pair = [await receive(connection_a), await receive(connection_a)]
            assert {item["type"] for item in first_pair} == {"stop_result", "event"}
            stop_result = next(item for item in first_pair if item["type"] == "stop_result")
            assert stop_result == {"type": "stop_result", "stop_id": "stop-1", "outcome": "requested"}
            completed = next(item for item in first_pair if item["type"] == "event")
            assert completed["client_message_id"] == "message-1"

            # A newly connected authenticated socket addresses the same stable
            # client message and sees the idempotent stop result.
            await connection_b.command({"type": "stop", "stop_id": "stop-2", "target_client_message_id": "message-1", "conversation_id": "conversation-1"})
            assert await receive(connection_b) == {"type": "stop_result", "stop_id": "stop-2", "outcome": "already_requested"}
            assert dispatcher.stops == [("connection-a", "message-1"), ("connection-b", "message-1")]

            await connection_a.command({"type": "continue", "client_message_id": "continue-1", "target_client_message_id": "message-1", "conversation_id": "conversation-1"})
            assert (await receive(connection_a))["type"] == "ack"
            await asyncio.wait_for(dispatcher.continue_started.wait(), 1)
            await connection_a.command({"type": "message", "client_message_id": "after-stop", "conversation_id": "conversation-1", "text": "after"})
            assert (await receive(connection_a))["code"] == "too_many_active_messages"

            await connection_b.command({"type": "stop", "stop_id": "stop-3", "target_client_message_id": "message-2", "conversation_id": "conversation-1"})
            assert (await receive(connection_b))["outcome"] == "requested"
            assert (await receive(connection_a))["client_message_id"] == "message-2"

            await connection_a.command({"type": "message", "client_message_id": "after-stop", "conversation_id": "conversation-1", "text": "after"})
            assert (await receive(connection_a))["type"] == "ack"
            assert (await receive(connection_a))["event"]["payload"]["output"] == "usable"
        finally:
            dispatcher.continue_release.set()
            for connection in (connection_a, connection_b):
                await connection.inbound.put(WebSocketPacket(WebSocketPacketKind.DISCONNECT))
            await asyncio.wait_for(asyncio.gather(running_a, running_b), 1)
            await approvals.close()

    asyncio.run(check())
