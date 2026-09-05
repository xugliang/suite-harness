"""Optional Starlette ASGI bridge for the company WebSocket server."""

from __future__ import annotations

from typing import Any

from .models import WebSocketHandshake, WebSocketPacket, WebSocketPacketKind
from .server import EnterpriseWebSocketServer

_SINGLETON_HANDSHAKE_HEADERS = (
    "origin",
    "authorization",
    "cookie",
    "sec-websocket-protocol",
)


def _require_starlette() -> tuple[type[Any], type[Any]]:
    try:
        from starlette.routing import WebSocketRoute
        from starlette.websockets import WebSocketState
    except ImportError as exc:  # pragma: no cover - deployment diagnostic
        raise RuntimeError(
            "the Starlette WebSocket adapter requires the optional 'starlette' dependency"
        ) from exc
    return WebSocketRoute, WebSocketState


class StarletteWebSocketConnection:
    """Translate Starlette ASGI messages to the transport-neutral socket seam."""

    def __init__(self, websocket: Any) -> None:
        _route, state = _require_starlette()
        self._websocket = websocket
        self._state = state

    async def accept(self, *, subprotocol: str | None = None) -> None:
        await self._websocket.accept(subprotocol=subprotocol)

    async def receive(self) -> WebSocketPacket:
        message = await self._websocket.receive()
        message_type = message.get("type")
        if message_type == "websocket.disconnect":
            return WebSocketPacket(WebSocketPacketKind.DISCONNECT)
        if message_type != "websocket.receive":
            return WebSocketPacket(WebSocketPacketKind.DISCONNECT)
        text = message.get("text")
        if isinstance(text, str):
            return WebSocketPacket(WebSocketPacketKind.TEXT, text=text)
        data = message.get("bytes")
        if isinstance(data, bytes):
            return WebSocketPacket(WebSocketPacketKind.BINARY, data=data)
        return WebSocketPacket(WebSocketPacketKind.DISCONNECT)

    async def send_text(self, text: str) -> None:
        await self._websocket.send_text(text)

    async def close(self, *, code: int, reason: str = "") -> None:
        if self._websocket.application_state != self._state.DISCONNECTED:
            await self._websocket.close(code=code, reason=reason)


def create_starlette_websocket_endpoint(server: EnterpriseWebSocketServer) -> Any:
    """Return an endpoint callable suitable for ``WebSocketRoute``."""

    _require_starlette()

    async def endpoint(websocket: Any) -> None:
        headers: dict[str, str | None] = {}
        for name in _SINGLETON_HANDSHAKE_HEADERS:
            values = _scope_header_values(websocket, name)
            if len(values) > 1:
                await websocket.close(code=4400, reason="invalid_handshake")
                return
            headers[name] = values[0] if values else None
        protocols_header = headers["sec-websocket-protocol"] or ""
        protocols = tuple(
            item.strip() for item in protocols_header.split(",") if item.strip()
        )
        client = getattr(websocket, "client", None)
        handshake = WebSocketHandshake(
            origin=headers["origin"],
            authorization=headers["authorization"],
            cookie_header=headers["cookie"],
            client_host=getattr(client, "host", None),
            requested_subprotocols=protocols,
        )
        await server.serve(StarletteWebSocketConnection(websocket), handshake)

    return endpoint


def _scope_header_values(websocket: Any, name: str) -> tuple[str, ...]:
    """Read raw ASGI headers without collapsing security-sensitive duplicates."""

    target = name.casefold().encode("ascii")
    values: list[str] = []
    for key, value in websocket.scope.get("headers", ()):
        if key.lower() != target:
            continue
        try:
            values.append(value.decode("latin-1"))
        except (AttributeError, UnicodeDecodeError):
            return ("", "")
    return tuple(values)


def create_starlette_websocket_route(path: str, server: EnterpriseWebSocketServer) -> Any:
    """Create a Starlette route without forcing Starlette on core deployments."""

    route_type, _state = _require_starlette()
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("WebSocket route path must be absolute")
    return route_type(path, create_starlette_websocket_endpoint(server))


__all__ = [
    "StarletteWebSocketConnection",
    "create_starlette_websocket_endpoint",
    "create_starlette_websocket_route",
]
