from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

from suiteharness.channels import (
    ApprovalChallenge,
    AuthenticatedPrincipal,
    InteractiveApprovalCoordinator,
    OutboundEvent,
    OutboundEventKind,
)
from suiteharness.channels.websocket import (
    EnterpriseWebSocketServer,
    OriginPolicy,
    WebSocketApprovalHub,
    WebSocketHandshake,
    WebSocketPacket,
    WebSocketPacketKind,
)
from suiteharness.execution import (
    ApprovalTarget,
    RunRequest,
    ToolEffect,
    ToolIdentity,
    ToolSpec,
    arguments_digest,
)
from suiteharness.runtime import RequestScope, ScopePath


class Authenticator:
    async def authenticate(self, handshake):  # type: ignore[no-untyped-def]
        return AuthenticatedPrincipal(tenant_id="acme", principal_id="employee-1")


class ApprovalDispatcher:
    def __init__(self, approvals: InteractiveApprovalCoordinator) -> None:
        self.approvals = approvals
        self.binding = None

    async def dispatch_authenticated(
        self, message, principal  # type: ignore[no-untyped-def]
    ):
        assert principal.principal_id == "employee-1"
        request = RunRequest(
            run_id="run-approval",
            grant_id="grant-approval",
            input=message.text,
            scope=RequestScope(
                ScopePath.agent("acme", "sales", "default", "session-approval"),
                "employee-1",
                channel_id="web",
            ),
        )
        target = ApprovalTarget(
            scope=request.scope.path,
            principal_id="employee-1",
            run_id=request.run_id,
            call_id="call-write",
            tool_identity=ToolIdentity(
                namespace="suiteharness",
                name="suiteharness.fs.write",
                origin="suiteharness.builtin.fs",
                version="1",
            ),
            arguments_digest=arguments_digest({"path": "exports/a.md"}),
        )
        self.binding = await self.approvals.request_approval(
            request,
            target,
            ToolSpec(name="suiteharness.fs.write", effects=frozenset({ToolEffect.WRITE})),
            {"path": "exports/a.md"},
        )
        yield OutboundEvent(
            kind=OutboundEventKind.COMPLETED,
            request_id="request-approval",
            correlation_id="correlation-approval",
        )


class ApprovalConnection:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[WebSocketPacket] = asyncio.Queue()
        self.incoming.put_nowait(
            WebSocketPacket(
                WebSocketPacketKind.TEXT,
                text=json.dumps(
                    {
                        "type": "message",
                        "client_message_id": "client-approval",
                        "conversation_id": "conversation-1",
                        "product_id": "sales",
                        "text": "write report",
                    }
                ),
            )
        )
        self.sent = []

    async def accept(self, *, subprotocol=None):  # type: ignore[no-untyped-def]
        pass

    async def receive(self):  # type: ignore[no-untyped-def]
        return await self.incoming.get()

    async def send_text(self, text: str) -> None:
        frame = json.loads(text)
        self.sent.append(frame)
        if frame["type"] == "approval_required":
            self.incoming.put_nowait(
                WebSocketPacket(
                    WebSocketPacketKind.TEXT,
                    text=json.dumps(
                        {
                            "type": "approval_decision",
                            "challenge_id": frame["challenge"]["challenge_id"],
                            "approved": True,
                        }
                    ),
                )
            )
        elif frame["type"] == "event":
            self.incoming.put_nowait(WebSocketPacket(WebSocketPacketKind.DISCONNECT))

    async def close(self, *, code: int, reason: str = "") -> None:
        pass


def test_authenticated_socket_decision_resolves_interactive_coordinator() -> None:
    async def exercise():
        hub = WebSocketApprovalHub()
        approvals = InteractiveApprovalCoordinator(hub.publish, timeout_seconds=1)
        dispatcher = ApprovalDispatcher(approvals)
        server = EnterpriseWebSocketServer(
            tenant_id="acme",
            authenticator=Authenticator(),
            origin_policy=OriginPolicy(("https://assistant.acme.cn",)),
            dispatcher=dispatcher,
            approvals=approvals,
            approval_hub=hub,
        )
        connection = ApprovalConnection()
        await server.serve(
            connection,
            WebSocketHandshake(origin="https://assistant.acme.cn"),
        )
        return dispatcher, connection

    dispatcher, connection = asyncio.run(exercise())
    assert dispatcher.binding is not None
    assert dispatcher.binding.approved_by == "employee-1"
    assert [frame["type"] for frame in connection.sent] == [
        "ack",
        "approval_required",
        "approval_decision_result",
        "event",
    ]
    assert connection.sent[2]["accepted"] is True


def test_approval_hub_never_crosses_tenant_boundary() -> None:
    async def exercise() -> None:
        hub = WebSocketApprovalHub()

        async def sender(frame):  # type: ignore[no-untyped-def]
            raise AssertionError(f"cross-tenant delivery: {frame}")

        await hub.register("other-tenant", "employee-1", "connection-1", sender)
        request = RunRequest(
            run_id="run-tenant",
            grant_id="grant-tenant",
            input="test",
            scope=RequestScope(
                ScopePath.agent("acme", "sales", "default", "session-tenant"),
                "employee-1",
                channel_id="web",
            ),
        )
        challenge = ApprovalChallenge(
            challenge_id="challenge-tenant",
            principal_id="employee-1",
            run_id="run-tenant",
            call_id="call-tenant",
            tool_name="suiteharness.fs.write",
            tool_identity="suiteharness:suiteharness.fs.write@suiteharness.builtin.fs#1",
            arguments={"path": "a.md"},
            expires_at=datetime.now(UTC),
        )
        with pytest.raises(RuntimeError, match="no authenticated WebSocket"):
            await hub.publish(request, challenge)

    asyncio.run(exercise())
