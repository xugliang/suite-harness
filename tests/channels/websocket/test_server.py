from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from suiteharness.channels import (
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


class Authenticator:
    def __init__(self) -> None:
        self.calls = 0

    async def authenticate(self, handshake):  # type: ignore[no-untyped-def]
        self.calls += 1
        if handshake.authorization != "Bearer company-session":
            return None
        return AuthenticatedPrincipal(
            tenant_id="acme", principal_id="employee-1", roles=frozenset({"employee"})
        )


class Dispatcher:
    def __init__(self) -> None:
        self.messages = []
        self.principals = []

    async def dispatch_authenticated(
        self, message, principal  # type: ignore[no-untyped-def]
    ) -> AsyncIterator[OutboundEvent]:
        self.messages.append(message)
        self.principals.append(principal)
        await asyncio.sleep(0)
        yield OutboundEvent(
            kind=OutboundEventKind.DELTA,
            request_id="request-1",
            correlation_id="correlation-1",
            payload={"text": "part"},
        )
        yield OutboundEvent(
            kind=OutboundEventKind.COMPLETED,
            request_id="request-1",
            correlation_id="correlation-1",
            payload={"text": "done"},
        )


class Connection:
    def __init__(self, incoming: list[WebSocketPacket]) -> None:
        self.incoming: asyncio.Queue[WebSocketPacket] = asyncio.Queue()
        for packet in incoming:
            self.incoming.put_nowait(packet)
        self.accepted = False
        self.subprotocol = None
        self.sent: list[dict[str, object]] = []
        self.closed = None

    async def accept(self, *, subprotocol=None):  # type: ignore[no-untyped-def]
        self.accepted = True
        self.subprotocol = subprotocol

    async def receive(self) -> WebSocketPacket:
        return await self.incoming.get()

    async def send_text(self, text: str) -> None:
        value = json.loads(text)
        self.sent.append(value)
        if value.get("type") == "event" and value["event"]["kind"] == "completed":  # type: ignore[index]
            self.incoming.put_nowait(WebSocketPacket(WebSocketPacketKind.DISCONNECT))

    async def close(self, *, code: int, reason: str = "") -> None:
        self.closed = (code, reason)


def _message_packet() -> WebSocketPacket:
    return WebSocketPacket(
        WebSocketPacketKind.TEXT,
        text=json.dumps(
            {
                "type": "message",
                "client_message_id": "client-1",
                "conversation_id": "conversation-1",
                "product_id": "sales",
                "text": "hello",
            }
        ),
    )


def _server(authenticator, dispatcher):  # type: ignore[no-untyped-def]
    hub = WebSocketApprovalHub()
    approvals = InteractiveApprovalCoordinator(hub.publish, timeout_seconds=1)
    return (
        EnterpriseWebSocketServer(
            tenant_id="acme",
            authenticator=authenticator,
            origin_policy=OriginPolicy(("https://assistant.acme.cn",)),
            dispatcher=dispatcher,
            approvals=approvals,
            approval_hub=hub,
        ),
        approvals,
    )


def test_server_authenticates_then_streams_application_events() -> None:
    async def exercise():
        authenticator = Authenticator()
        dispatcher = Dispatcher()
        server, _approvals = _server(authenticator, dispatcher)
        connection = Connection([_message_packet()])
        await server.serve(
            connection,
            WebSocketHandshake(
                origin="https://assistant.acme.cn",
                authorization="Bearer company-session",
                requested_subprotocols=("suiteharness.v1",),
            ),
        )
        return authenticator, dispatcher, connection

    authenticator, dispatcher, connection = asyncio.run(exercise())
    assert authenticator.calls == 1
    assert connection.accepted
    assert connection.subprotocol == "suiteharness.v1"
    assert [item["type"] for item in connection.sent] == ["ack", "event", "event"]
    assert [item["event"]["kind"] for item in connection.sent[1:]] == [  # type: ignore[index]
        "delta",
        "completed",
    ]
    message = dispatcher.messages[0]
    assert message.sender_external_id == "employee-1"
    assert message.product_id == "sales"
    assert message.channel.value == "web"
    assert dispatcher.principals == [
        AuthenticatedPrincipal(
            tenant_id="acme",
            principal_id="employee-1",
            roles=frozenset({"employee"}),
        )
    ]


def test_origin_is_rejected_before_authentication_and_accept() -> None:
    async def exercise():
        authenticator = Authenticator()
        server, _approvals = _server(authenticator, Dispatcher())
        connection = Connection([])
        await server.serve(
            connection,
            WebSocketHandshake(
                origin="https://evil.example", authorization="Bearer company-session"
            ),
        )
        return authenticator, connection

    authenticator, connection = asyncio.run(exercise())
    assert authenticator.calls == 0
    assert not connection.accepted
    assert connection.closed == (4403, "origin_not_allowed")


def test_authenticated_identity_from_another_tenant_is_rejected() -> None:
    class WrongTenant:
        async def authenticate(self, handshake):  # type: ignore[no-untyped-def]
            return AuthenticatedPrincipal(
                tenant_id="other-tenant", principal_id="employee-1"
            )

    async def exercise():
        server, _approvals = _server(WrongTenant(), Dispatcher())
        connection = Connection([])
        await server.serve(
            connection,
            WebSocketHandshake(origin="https://assistant.acme.cn"),
        )
        return connection

    connection = asyncio.run(exercise())
    assert not connection.accepted
    assert connection.closed == (4403, "tenant_not_allowed")


def test_binary_and_oversized_frames_are_rejected() -> None:
    async def exercise(packet: WebSocketPacket, maximum: int):
        auth = Authenticator()
        dispatcher = Dispatcher()
        hub = WebSocketApprovalHub()
        approvals = InteractiveApprovalCoordinator(hub.publish, timeout_seconds=1)
        server = EnterpriseWebSocketServer(
            tenant_id="acme",
            authenticator=auth,
            origin_policy=OriginPolicy(("https://assistant.acme.cn",)),
            dispatcher=dispatcher,
            approvals=approvals,
            approval_hub=hub,
            max_frame_bytes=maximum,
        )
        connection = Connection([packet])
        await server.serve(
            connection,
            WebSocketHandshake(
                origin="https://assistant.acme.cn", authorization="Bearer company-session"
            ),
        )
        return connection

    binary = asyncio.run(
        exercise(WebSocketPacket(WebSocketPacketKind.BINARY, data=b"{}"), 1024)
    )
    assert binary.closed == (1003, "binary_not_supported")
    oversized = asyncio.run(exercise(_message_packet(), 10))
    assert oversized.closed == (1009, "frame_too_large")
