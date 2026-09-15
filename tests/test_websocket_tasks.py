"""Shared connection task ownership and unchanged standard approval flow."""

from __future__ import annotations

import asyncio
import json

import pytest


def test_connection_task_group_rejects_work_without_constructing_it_and_waits_for_cleanup():
    from suiteharness.channels.websocket.tasks import ConnectionTaskGroup

    async def check():
        group = ConnectionTaskGroup(max_concurrent=1)
        started, stopped = asyncio.Event(), asyncio.Event()

        async def slow():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                stopped.set()

        assert group.try_start(slow, name="same-operation")
        await started.wait()

        def forbidden_factory():
            pytest.fail("rejected work must not construct an unawaited coroutine")

        assert not group.try_start(forbidden_factory)
        assert group.active_count == 1
        await group.aclose()
        assert stopped.is_set()
        assert group.active_count == 0
        await group.aclose()
        with pytest.raises(RuntimeError, match="closed"):
            group.try_start(forbidden_factory)

    asyncio.run(check())


def test_connection_task_completion_cannot_remove_a_new_task_with_the_same_name():
    from suiteharness.channels.websocket.tasks import ConnectionTaskGroup

    async def check():
        group = ConnectionTaskGroup(max_concurrent=1)
        first_finished, second_started = asyncio.Event(), asyncio.Event()

        async def first():
            first_finished.set()

        async def second():
            second_started.set()
            await asyncio.Event().wait()

        group.try_start(first, name="same-id")
        await first_finished.wait()
        # The first task is done, but its registered done callback can still be queued.
        assert group.try_start(second, name="same-id")
        await second_started.wait()
        assert group.active_count == 1
        await group.aclose()

    asyncio.run(check())


@pytest.mark.parametrize("observe", ["start", "close"])
def test_connection_task_failures_are_reported_instead_of_silently_consumed(observe):
    from suiteharness.channels.websocket.tasks import ConnectionTaskGroup

    async def check():
        group = ConnectionTaskGroup(max_concurrent=1)
        failed = asyncio.Event()

        async def broken():
            failed.set()
            raise ValueError("worker failed")

        group.try_start(broken)
        await failed.wait()
        with pytest.raises(ValueError, match="worker failed"):
            if observe == "start":
                group.try_start(broken)
            else:
                await group.aclose()
        await group.aclose()

    asyncio.run(check())


def test_connection_factory_error_propagates_without_occupying_capacity():
    from suiteharness.channels.websocket.tasks import ConnectionTaskGroup

    async def check():
        group = ConnectionTaskGroup(max_concurrent=1)

        def broken():
            raise ValueError("factory failed")

        with pytest.raises(ValueError, match="factory failed"):
            group.try_start(broken)
        assert group.active_count == 0
        await group.aclose()

    asyncio.run(check())


@pytest.mark.parametrize("disconnect", [False, True])
def test_standard_websocket_keeps_approval_live_or_cancels_it_on_disconnect(disconnect):
    from suiteharness.channels import AuthenticatedPrincipal, OutboundEvent, OutboundEventKind
    from suiteharness.channels.approvals import InteractiveApprovalCoordinator
    from suiteharness.channels.websocket import WebSocketApprovalHub, WebSocketHandshake
    from suiteharness.channels.websocket.models import WebSocketPacket, WebSocketPacketKind
    from suiteharness.channels.websocket.server import WebSocketSession
    from suiteharness.execution import ApprovalTarget, RunRequest, ToolEffect, ToolIdentity, ToolSpec
    from suiteharness.runtime import RequestScope, ScopePath

    async def check():
        principal = AuthenticatedPrincipal(tenant_id="company", principal_id="seller")
        hub = WebSocketApprovalHub()
        approvals = InteractiveApprovalCoordinator(hub.publish, timeout_seconds=2)
        scope = RequestScope(path=ScopePath.agent("company", "product", "default", "chat1"),
            principal_id="seller", session_owner_id="seller", channel_id="web",
            request_id="request1", correlation_id="correlation1")
        request = RunRequest(run_id="run1", scope=scope, grant_id="grant1", input="approve")
        target = ApprovalTarget(scope=scope.path, principal_id="seller", run_id="run1",
            call_id="call1", tool_identity=ToolIdentity(namespace="product", name="update",
                origin="test:websocket", version="1"), arguments_digest="a" * 64)

        class Connection:
            def __init__(self):
                self.inbound, self.outbound = asyncio.Queue(), asyncio.Queue()

            async def receive(self):
                return await self.inbound.get()

            async def send_text(self, text):
                await self.outbound.put(json.loads(text))

            async def close(self, **kwargs):
                await self.inbound.put(WebSocketPacket(WebSocketPacketKind.DISCONNECT))

            async def command(self, payload):
                await self.inbound.put(WebSocketPacket(WebSocketPacketKind.TEXT, json.dumps(payload)))

        class Authenticator:
            async def authenticate(self, handshake):
                return principal

        class Dispatcher:
            async def dispatch_authenticated(self, message, authenticated):
                binding = await approvals.request_approval(request, target,
                    ToolSpec(name="update", effects=frozenset({ToolEffect.WRITE})), {"value": 1})
                assert binding is not None and binding.target == target
                yield OutboundEvent(kind=OutboundEventKind.COMPLETED,
                    request_id="request1", correlation_id="correlation1", payload={"output": "approved"})

        connection = Connection()
        session = WebSocketSession(connection, connection_id="connection1", principal=principal,
            handshake=WebSocketHandshake(origin="https://example.test"), authenticator=Authenticator(),
            revalidator=None, dispatcher=Dispatcher(), approvals=approvals, approval_hub=hub,
            max_concurrent_messages=1)
        running = asyncio.create_task(session.run())

        async def receive():
            return await asyncio.wait_for(connection.outbound.get(), 1)

        try:
            await connection.command({"type": "message", "client_message_id": "message1",
                "conversation_id": "chat1", "text": "approve"})
            assert (await receive())["type"] == "ack"
            challenge = (await receive())["challenge"]["challenge_id"]
            await connection.command({"type": "message", "client_message_id": "message2",
                "conversation_id": "chat1", "text": "busy"})
            assert (await receive())["code"] == "too_many_active_messages"
            await connection.command({"type": "ping", "nonce": "during-approval"})
            assert await receive() == {"type": "pong", "nonce": "during-approval"}
            assert not await approvals.decide(challenge, principal_id="other", approved=True, approved_by="other")
            if disconnect:
                await connection.inbound.put(WebSocketPacket(WebSocketPacketKind.DISCONNECT))
                await asyncio.wait_for(running, 1)
                assert not await approvals.decide(challenge, principal_id="seller", approved=True, approved_by="seller")
                return
            await connection.command({"type": "approval_decision", "challenge_id": challenge, "approved": True})
            events = [await receive(), await receive()]
            assert {event["type"] for event in events} == {"approval_decision_result", "event"}
            assert next(event for event in events if event["type"] == "approval_decision_result")["accepted"] is True
            assert next(event for event in events if event["type"] == "event")["event"]["payload"]["output"] == "approved"
            await connection.command({"type": "approval_decision", "challenge_id": challenge, "approved": True})
            assert (await receive())["accepted"] is False
            await connection.inbound.put(WebSocketPacket(WebSocketPacketKind.DISCONNECT))
            await asyncio.wait_for(running, 1)
        finally:
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            await approvals.close()

    asyncio.run(check())
