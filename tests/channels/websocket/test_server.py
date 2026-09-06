from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest

from suiteharness.channels import (
    AuthenticatedPrincipal,
    InteractiveApprovalCoordinator,
    OutboundEvent,
    OutboundEventKind,
)
from suiteharness.channels.websocket import (
    EnterpriseWebSocketServer,
    HmacSessionTokenCodec,
    OriginPolicy,
    ServerSessionWebSocketAuthenticator,
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


class Revalidator:
    def __init__(
        self,
        results: list[AuthenticatedPrincipal | None] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.results = list(results or [])
        self.error = error
        self.calls: list[tuple[object, AuthenticatedPrincipal]] = []

    async def revalidate(self, handshake, established_principal):  # type: ignore[no-untyped-def]
        self.calls.append((handshake, established_principal))
        if self.error is not None:
            raise self.error
        if self.results:
            return self.results.pop(0)
        return established_principal


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


def _text_packet(payload: dict[str, object]) -> WebSocketPacket:
    return WebSocketPacket(WebSocketPacketKind.TEXT, text=json.dumps(payload))


def _server(
    authenticator,
    dispatcher,
    *,
    revalidator=None,
    revalidation_interval=30.0,
):  # type: ignore[no-untyped-def]
    hub = WebSocketApprovalHub()
    approvals = InteractiveApprovalCoordinator(hub.publish, timeout_seconds=1)
    return (
        EnterpriseWebSocketServer(
            tenant_id="acme",
            authenticator=authenticator,
            session_revalidator=revalidator,
            origin_policy=OriginPolicy(("https://assistant.acme.cn",)),
            dispatcher=dispatcher,
            approvals=approvals,
            approval_hub=hub,
            session_revalidation_interval_seconds=revalidation_interval,
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
    assert authenticator.calls == 2
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


def test_ping_and_approval_frames_are_revalidated_before_handling() -> None:
    async def exercise():
        authenticator = Authenticator()
        revalidator = Revalidator()
        server, _approvals = _server(
            authenticator,
            Dispatcher(),
            revalidator=revalidator,
        )
        connection = Connection(
            [
                _text_packet({"type": "ping", "nonce": "fresh"}),
                _text_packet(
                    {
                        "type": "approval_decision",
                        "challenge_id": "challenge-1",
                        "approved": True,
                    }
                ),
                WebSocketPacket(WebSocketPacketKind.DISCONNECT),
            ]
        )
        await server.serve(
            connection,
            WebSocketHandshake(
                origin="https://assistant.acme.cn",
                authorization="Bearer company-session",
            ),
        )
        return authenticator, revalidator, connection

    authenticator, revalidator, connection = asyncio.run(exercise())
    assert authenticator.calls == 3
    assert len(revalidator.calls) == 2
    assert [item["type"] for item in connection.sent] == [
        "pong",
        "approval_decision_result",
    ]
    assert connection.sent[1]["accepted"] is False


@pytest.mark.parametrize(
    "changed_principal",
    [
        AuthenticatedPrincipal(
            tenant_id="other", principal_id="employee-1", roles=frozenset({"employee"})
        ),
        AuthenticatedPrincipal(
            tenant_id="acme", principal_id="employee-2", roles=frozenset({"employee"})
        ),
        AuthenticatedPrincipal(
            tenant_id="acme", principal_id="employee-1", roles=frozenset({"manager"})
        ),
    ],
    ids=("tenant", "principal", "roles"),
)
def test_dynamic_identity_changes_fail_closed(
    changed_principal: AuthenticatedPrincipal,
) -> None:
    async def exercise():
        revalidator = Revalidator([changed_principal])
        server, _approvals = _server(
            Authenticator(),
            Dispatcher(),
            revalidator=revalidator,
        )
        connection = Connection([_text_packet({"type": "ping", "nonce": "ignored"})])
        await server.serve(
            connection,
            WebSocketHandshake(
                origin="https://assistant.acme.cn",
                authorization="Bearer company-session",
            ),
        )
        return connection

    connection = asyncio.run(exercise())
    assert connection.sent == []
    assert connection.closed == (4401, "authentication_changed")


def test_revocation_cancels_an_inflight_run_before_closing() -> None:
    class HangingDispatcher:
        def __init__(self, connection: Connection) -> None:
            self.connection = connection
            self.cancelled = asyncio.Event()

        async def dispatch_authenticated(
            self, message, principal  # type: ignore[no-untyped-def]
        ) -> AsyncIterator[OutboundEvent]:
            del message, principal
            self.connection.incoming.put_nowait(
                _text_packet({"type": "ping", "nonce": "revoked"})
            )
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
            if False:  # pragma: no cover - establishes this as an async iterator
                yield OutboundEvent(  # type: ignore[unreachable]
                    kind=OutboundEventKind.COMPLETED,
                    request_id="never",
                    correlation_id="never",
                )

    async def exercise():
        connection = Connection([_message_packet()])
        dispatcher = HangingDispatcher(connection)
        principal = AuthenticatedPrincipal(
            tenant_id="acme",
            principal_id="employee-1",
            roles=frozenset({"employee"}),
        )
        revalidator = Revalidator([principal, None])
        server, _approvals = _server(
            Authenticator(),
            dispatcher,
            revalidator=revalidator,
        )
        await server.serve(
            connection,
            WebSocketHandshake(
                origin="https://assistant.acme.cn",
                authorization="Bearer company-session",
            ),
        )
        return connection, dispatcher, revalidator

    connection, dispatcher, revalidator = asyncio.run(exercise())
    assert dispatcher.cancelled.is_set()
    assert len(revalidator.calls) == 2
    assert connection.closed == (4401, "authentication_failed")
    assert [item["type"] for item in connection.sent] == ["ack"]


def test_idle_socket_is_revalidated_and_revoked_without_a_client_frame() -> None:
    async def exercise():
        revalidator = Revalidator([None])
        server, _approvals = _server(
            Authenticator(),
            Dispatcher(),
            revalidator=revalidator,
            revalidation_interval=0.01,
        )
        connection = Connection([])
        await asyncio.wait_for(
            server.serve(
                connection,
                WebSocketHandshake(
                    origin="https://assistant.acme.cn",
                    authorization="Bearer company-session",
                ),
            ),
            timeout=1,
        )
        return connection, revalidator

    connection, revalidator = asyncio.run(exercise())
    assert len(revalidator.calls) == 1
    assert connection.sent == []
    assert connection.closed == (4401, "authentication_failed")


def test_hmac_ticket_expiry_is_checked_again_before_a_ping() -> None:
    clock = [1_900_000_000.0]
    codec = HmacSessionTokenCodec(
        b"company-server-session-key-32-bytes-minimum",
        issuer="suiteharness-server:test",
        audience="suiteharness-company-websocket",
        tenant_id="acme",
        clock=lambda: clock[0],
    )
    principal = AuthenticatedPrincipal(
        tenant_id="acme",
        principal_id="employee-1",
        roles=frozenset({"employee"}),
    )
    token = codec.issue(principal, lifetime_seconds=1)

    class ExpiringConnection(Connection):
        async def accept(self, *, subprotocol=None):  # type: ignore[no-untyped-def]
            await super().accept(subprotocol=subprotocol)
            clock[0] += 1

    async def exercise():
        authenticator = ServerSessionWebSocketAuthenticator(codec)
        server, _approvals = _server(authenticator, Dispatcher())
        connection = ExpiringConnection(
            [_text_packet({"type": "ping", "nonce": "expired"})]
        )
        await server.serve(
            connection,
            WebSocketHandshake(
                origin="https://assistant.acme.cn",
                authorization=f"Bearer {token}",
            ),
        )
        return connection

    connection = asyncio.run(exercise())
    assert connection.sent == []
    assert connection.closed == (4401, "authentication_failed")


@pytest.mark.parametrize("dynamic_failure", [False, True], ids=("ticket", "dynamic"))
def test_revalidation_exceptions_close_without_processing(
    dynamic_failure: bool,
) -> None:
    class FailingAuthenticator(Authenticator):
        async def authenticate(self, handshake):  # type: ignore[no-untyped-def]
            principal = await super().authenticate(handshake)
            if self.calls > 1:
                raise RuntimeError("directory unavailable")
            return principal

    async def exercise():
        authenticator = Authenticator() if dynamic_failure else FailingAuthenticator()
        revalidator = (
            Revalidator(error=RuntimeError("revocation store unavailable"))
            if dynamic_failure
            else None
        )
        server, _approvals = _server(
            authenticator,
            Dispatcher(),
            revalidator=revalidator,
        )
        connection = Connection([_text_packet({"type": "ping", "nonce": "ignored"})])
        await server.serve(
            connection,
            WebSocketHandshake(
                origin="https://assistant.acme.cn",
                authorization="Bearer company-session",
            ),
        )
        return connection

    connection = asyncio.run(exercise())
    assert connection.sent == []
    assert connection.closed == (1011, "authentication_unavailable")
