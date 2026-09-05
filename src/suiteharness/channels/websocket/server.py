"""Authenticated, bounded and streaming WebSocket company channel."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlsplit

from suiteharness.channels.approvals import ApprovalChallenge, InteractiveApprovalCoordinator
from suiteharness.channels.models import (
    AuthenticatedPrincipal,
    ChannelKind,
    InboundMessage,
    OutboundEvent,
)
from suiteharness.execution import RunRequest

from .models import (
    AckFrame,
    ApprovalDecisionFrame,
    ApprovalDecisionResultFrame,
    ApprovalRequiredFrame,
    ErrorFrame,
    EventFrame,
    MessageFrame,
    PingFrame,
    PongFrame,
    ServerFrame,
    WebSocketHandshake,
    WebSocketPacket,
    WebSocketPacketKind,
    WebSocketProtocolError,
    parse_client_frame,
)


class WebSocketConnection(Protocol):
    async def accept(self, *, subprotocol: str | None = None) -> None: ...

    async def receive(self) -> WebSocketPacket: ...

    async def send_text(self, text: str) -> None: ...

    async def close(self, *, code: int, reason: str = "") -> None: ...


class WebSocketAuthenticator(Protocol):
    """Authenticate only company-managed server credentials from the handshake."""

    async def authenticate(
        self, handshake: WebSocketHandshake
    ) -> AuthenticatedPrincipal | None: ...


class WebSocketDispatcher(Protocol):
    def dispatch_authenticated(
        self,
        message: InboundMessage,
        principal: AuthenticatedPrincipal,
    ) -> AsyncIterator[OutboundEvent]: ...


def _canonical_origin(value: str) -> str:
    if not value or len(value) > 2048 or "\x00" in value:
        raise ValueError("invalid WebSocket origin")
    parsed = urlsplit(value)
    if parsed.scheme.casefold() not in {"http", "https"} or parsed.hostname is None:
        raise ValueError("invalid WebSocket origin")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("invalid WebSocket origin")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("invalid WebSocket origin")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid WebSocket origin") from exc
    scheme = parsed.scheme.casefold()
    host = parsed.hostname.casefold()
    if ":" in host:
        host = f"[{host}]"
    default_port = 80 if scheme == "http" else 443
    authority = host if port in {None, default_port} else f"{host}:{port}"
    return f"{scheme}://{authority}"


class OriginPolicy:
    """Exact allowlist; wildcard and reflected origins are intentionally unsupported."""

    def __init__(self, allowed_origins: tuple[str, ...], *, allow_missing: bool = False) -> None:
        if not allowed_origins:
            raise ValueError("allowed_origins must not be empty")
        canonical: list[str] = []
        for origin in allowed_origins:
            if "*" in origin:
                raise ValueError("wildcard WebSocket origins are not supported")
            canonical.append(_canonical_origin(origin))
        if len(canonical) != len(set(canonical)):
            raise ValueError("allowed_origins contains duplicates")
        self._allowed = frozenset(canonical)
        self._allow_missing = allow_missing

    def permits(self, origin: str | None) -> bool:
        if origin is None:
            return self._allow_missing
        try:
            return _canonical_origin(origin) in self._allowed
        except ValueError:
            return False


FrameSender = Callable[[ServerFrame], Awaitable[None]]


class WebSocketApprovalHub:
    """Route exact-call approval challenges to active sockets of that principal."""

    def __init__(self) -> None:
        self._connections: dict[tuple[str, str, str], FrameSender] = {}
        self._lock = asyncio.Lock()

    async def register(
        self,
        tenant_id: str,
        principal_id: str,
        connection_id: str,
        sender: FrameSender,
    ) -> None:
        async with self._lock:
            key = (tenant_id, principal_id, connection_id)
            if key in self._connections:
                raise RuntimeError("duplicate WebSocket connection registration")
            self._connections[key] = sender

    async def unregister(
        self, tenant_id: str, principal_id: str, connection_id: str
    ) -> None:
        async with self._lock:
            self._connections.pop((tenant_id, principal_id, connection_id), None)

    async def publish(self, request: RunRequest, challenge: ApprovalChallenge) -> None:
        if request.scope.channel_id != "web":
            raise RuntimeError("interactive approval is available only to the Web channel")
        if request.scope.principal_id != challenge.principal_id:
            raise RuntimeError("approval challenge principal does not match its request")
        async with self._lock:
            senders = tuple(
                sender
                for (tenant_id, principal_id, _connection_id), sender in self._connections.items()
                if tenant_id == request.scope.tenant_id
                and principal_id == challenge.principal_id
            )
        if not senders:
            raise RuntimeError("no authenticated WebSocket is available for approval")
        results = await asyncio.gather(
            *(sender(ApprovalRequiredFrame(challenge=challenge)) for sender in senders),
            return_exceptions=True,
        )
        if all(isinstance(result, BaseException) for result in results):
            raise RuntimeError("approval challenge could not be delivered")


class WebSocketSession:
    """One authenticated socket; receive stays live while agent runs stream events."""

    def __init__(
        self,
        connection: WebSocketConnection,
        *,
        connection_id: str,
        principal: AuthenticatedPrincipal,
        dispatcher: WebSocketDispatcher,
        approvals: InteractiveApprovalCoordinator,
        approval_hub: WebSocketApprovalHub,
        max_frame_bytes: int = 1_048_576,
        max_outbound_bytes: int = 1_048_576,
        max_concurrent_messages: int = 4,
        max_messages_per_connection: int = 10_000,
    ) -> None:
        if max_frame_bytes <= 0 or max_outbound_bytes <= 0:
            raise ValueError("WebSocket frame limits must be positive")
        if max_concurrent_messages <= 0 or max_messages_per_connection <= 0:
            raise ValueError("WebSocket message limits must be positive")
        self._connection = connection
        self._connection_id = connection_id
        self._principal = principal
        self._dispatcher = dispatcher
        self._approvals = approvals
        self._approval_hub = approval_hub
        self._max_frame_bytes = max_frame_bytes
        self._max_outbound_bytes = max_outbound_bytes
        self._max_concurrent = max_concurrent_messages
        self._max_messages = max_messages_per_connection
        self._send_lock = asyncio.Lock()
        self._seen_message_ids: set[str] = set()
        self._runs: dict[str, asyncio.Task[None]] = {}

    async def run(self) -> None:
        await self._approval_hub.register(
            self._principal.tenant_id,
            self._principal.principal_id,
            self._connection_id,
            self._send_frame,
        )
        try:
            while True:
                packet = await self._connection.receive()
                if packet.kind is WebSocketPacketKind.DISCONNECT:
                    break
                if packet.kind is WebSocketPacketKind.BINARY:
                    raise WebSocketProtocolError(
                        "binary_not_supported",
                        "binary WebSocket frames are not supported",
                        close_code=1003,
                    )
                assert packet.text is not None
                if len(packet.text.encode("utf-8")) > self._max_frame_bytes:
                    raise WebSocketProtocolError(
                        "frame_too_large", "WebSocket frame is too large", close_code=1009
                    )
                frame = parse_client_frame(packet.text)
                await self._handle_frame(frame)
        except WebSocketProtocolError as exc:
            try:
                await self._send_frame(ErrorFrame(code=exc.code, message=str(exc)))
            finally:
                await self._connection.close(code=exc.close_code, reason=exc.code)
        finally:
            await self._approval_hub.unregister(
                self._principal.tenant_id,
                self._principal.principal_id,
                self._connection_id,
            )
            running = tuple(self._runs.values())
            for task in running:
                task.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)
            self._runs.clear()

    async def _handle_frame(
        self,
        frame: MessageFrame | ApprovalDecisionFrame | PingFrame,
    ) -> None:
        if isinstance(frame, PingFrame):
            await self._send_frame(PongFrame(nonce=frame.nonce))
            return
        if isinstance(frame, ApprovalDecisionFrame):
            accepted = await self._approvals.decide(
                frame.challenge_id,
                principal_id=self._principal.principal_id,
                approved=frame.approved,
                approved_by=self._principal.principal_id,
            )
            await self._send_frame(
                ApprovalDecisionResultFrame(
                    challenge_id=frame.challenge_id,
                    accepted=accepted,
                )
            )
            return

        client_id = frame.client_message_id
        if client_id in self._seen_message_ids:
            await self._send_frame(
                ErrorFrame(
                    code="duplicate_message",
                    message="client_message_id was already used on this connection",
                    client_message_id=client_id,
                )
            )
            return
        if len(self._seen_message_ids) >= self._max_messages:
            raise WebSocketProtocolError(
                "connection_message_limit",
                "WebSocket connection message limit was reached",
            )
        if len(self._runs) >= self._max_concurrent:
            await self._send_frame(
                ErrorFrame(
                    code="too_many_active_messages",
                    message="too many agent messages are active on this connection",
                    client_message_id=client_id,
                )
            )
            return
        self._seen_message_ids.add(client_id)
        await self._send_frame(AckFrame(client_message_id=client_id))
        task = asyncio.create_task(self._dispatch(frame), name=f"suiteharness-web-{client_id}")
        self._runs[client_id] = task
        task.add_done_callback(lambda finished, key=client_id: self._run_finished(key, finished))

    def _run_finished(self, client_id: str, task: asyncio.Task[None]) -> None:
        self._runs.pop(client_id, None)
        if not task.cancelled():
            task.exception()

    async def _dispatch(self, frame: MessageFrame) -> None:
        message = InboundMessage(
            channel=ChannelKind.WEB,
            event_id=f"{self._connection_id}:{frame.client_message_id}",
            message_id=frame.client_message_id,
            conversation_id=frame.conversation_id,
            sender_external_id=self._principal.principal_id,
            text=frame.text,
            product_id=frame.product_id,
            attachments=frame.attachments,
            received_at=datetime.now(UTC),
            metadata={"connection_id": self._connection_id},
        )
        try:
            async for event in self._dispatcher.dispatch_authenticated(
                message,
                self._principal,
            ):
                await self._send_frame(
                    EventFrame(client_message_id=frame.client_message_id, event=event)
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._send_frame(
                ErrorFrame(
                    code="application_error",
                    message="the agent request failed",
                    client_message_id=frame.client_message_id,
                )
            )

    async def _send_frame(self, frame: ServerFrame) -> None:
        raw = frame.model_dump_json()
        if len(raw.encode("utf-8")) > self._max_outbound_bytes:
            raise WebSocketProtocolError(
                "outbound_frame_too_large",
                "server WebSocket frame is too large",
                close_code=1011,
            )
        async with self._send_lock:
            await self._connection.send_text(raw)


class EnterpriseWebSocketServer:
    """Origin-check and authenticate before accepting a company WebSocket."""

    def __init__(
        self,
        *,
        tenant_id: str,
        authenticator: WebSocketAuthenticator,
        origin_policy: OriginPolicy,
        dispatcher: WebSocketDispatcher,
        approvals: InteractiveApprovalCoordinator,
        approval_hub: WebSocketApprovalHub,
        subprotocol: str = "suiteharness.v1",
        max_frame_bytes: int = 1_048_576,
        max_outbound_bytes: int = 1_048_576,
        max_concurrent_messages: int = 4,
        max_messages_per_connection: int = 10_000,
    ) -> None:
        if not tenant_id or len(tenant_id) > 128 or "\x00" in tenant_id:
            raise ValueError("invalid WebSocket deployment tenant_id")
        if not subprotocol or len(subprotocol) > 128 or any(char.isspace() for char in subprotocol):
            raise ValueError("invalid WebSocket subprotocol")
        self._authenticator = authenticator
        self._tenant_id = tenant_id
        self._origin_policy = origin_policy
        self._dispatcher = dispatcher
        self._approvals = approvals
        self._approval_hub = approval_hub
        self._subprotocol = subprotocol
        self._limits = (
            max_frame_bytes,
            max_outbound_bytes,
            max_concurrent_messages,
            max_messages_per_connection,
        )

    async def serve(
        self,
        connection: WebSocketConnection,
        handshake: WebSocketHandshake,
    ) -> None:
        if not _bounded_handshake(handshake):
            await connection.close(code=4400, reason="invalid_handshake")
            return
        if not self._origin_policy.permits(handshake.origin):
            await connection.close(code=4403, reason="origin_not_allowed")
            return
        try:
            principal = await self._authenticator.authenticate(handshake)
        except Exception:
            await connection.close(code=1011, reason="authentication_unavailable")
            return
        if principal is None:
            await connection.close(code=4401, reason="authentication_failed")
            return
        if principal.tenant_id != self._tenant_id:
            await connection.close(code=4403, reason="tenant_not_allowed")
            return
        requested = handshake.requested_subprotocols
        if requested and self._subprotocol not in requested:
            await connection.close(code=4406, reason="subprotocol_not_supported")
            return
        selected = self._subprotocol if self._subprotocol in requested else None
        await connection.accept(subprotocol=selected)
        connection_id = secrets.token_urlsafe(18)
        session = WebSocketSession(
            connection,
            connection_id=connection_id,
            principal=principal,
            dispatcher=self._dispatcher,
            approvals=self._approvals,
            approval_hub=self._approval_hub,
            max_frame_bytes=self._limits[0],
            max_outbound_bytes=self._limits[1],
            max_concurrent_messages=self._limits[2],
            max_messages_per_connection=self._limits[3],
        )
        await session.run()


def _bounded_handshake(handshake: WebSocketHandshake) -> bool:
    sensitive = (handshake.authorization, handshake.cookie_header)
    if any(
        value is not None
        and (len(value) > 16_384 or "\x00" in value or "\r" in value or "\n" in value)
        for value in sensitive
    ):
        return False
    if len(handshake.requested_subprotocols) > 32:
        return False
    return all(
        bool(item)
        and len(item) <= 128
        and not any(char.isspace() for char in item)
        and "\x00" not in item
        for item in handshake.requested_subprotocols
    )


__all__ = [
    "EnterpriseWebSocketServer",
    "FrameSender",
    "OriginPolicy",
    "WebSocketApprovalHub",
    "WebSocketAuthenticator",
    "WebSocketConnection",
    "WebSocketDispatcher",
    "WebSocketSession",
]
