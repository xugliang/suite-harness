from __future__ import annotations

import asyncio
import json

import pytest

from suiteharness.channels.websocket import (
    ApprovalDecisionFrame,
    MessageFrame,
    OriginPolicy,
    WebSocketHandshake,
    WebSocketProtocolError,
    create_starlette_websocket_endpoint,
    parse_client_frame,
)


def test_client_frames_are_strict_discriminated_json() -> None:
    message = parse_client_frame(
        json.dumps(
            {
                "type": "message",
                "client_message_id": "client-1",
                "conversation_id": "conversation/external",
                "text": "hello",
                "attachments": [
                    {"attachment_id": "attachment-1", "media_type": "text/plain"}
                ],
            }
        )
    )
    assert isinstance(message, MessageFrame)
    assert message.attachments[0].attachment_id == "attachment-1"

    decision = parse_client_frame(
        json.dumps(
            {"type": "approval_decision", "challenge_id": "challenge", "approved": True}
        )
    )
    assert isinstance(decision, ApprovalDecisionFrame)

    with pytest.raises(WebSocketProtocolError, match="invalid WebSocket JSON frame"):
        parse_client_frame(
            json.dumps(
                {
                    "type": "approval_decision",
                    "challenge_id": "challenge",
                    "approved": "true",
                }
            )
        )
    with pytest.raises(WebSocketProtocolError):
        parse_client_frame(
            json.dumps(
                {
                    "type": "message",
                    "client_message_id": "client-1",
                    "conversation_id": "conversation",
                    "text": "hello",
                    "unexpected": "not allowed",
                }
            )
        )
    with pytest.raises(WebSocketProtocolError):
        parse_client_frame(
            '{"type":"approval_decision","challenge_id":"challenge",'
            '"approved":true,"approved":false}'
        )


def test_origin_policy_is_an_exact_canonical_allowlist() -> None:
    policy = OriginPolicy(("https://Assistant.Acme.CN:443",))
    assert policy.permits("https://assistant.acme.cn")
    assert not policy.permits("https://evil.example")
    assert not policy.permits("https://assistant.acme.cn.evil.example")
    assert not policy.permits(None)
    assert not policy.permits("https://assistant.acme.cn/path")
    with pytest.raises(ValueError, match="wildcard"):
        OriginPolicy(("https://*.acme.cn",))


def test_handshake_repr_redacts_browser_cookie_and_authorization() -> None:
    handshake = WebSocketHandshake(
        origin="https://assistant.acme.cn",
        authorization="Bearer secret-auth",
        cookie_header="suiteharness_session=secret-cookie",
    )
    assert "secret-auth" not in repr(handshake)
    assert "secret-cookie" not in repr(handshake)


def test_starlette_bridge_rejects_duplicate_security_headers_before_authentication() -> None:
    class Server:
        called = False

        async def serve(self, connection, handshake):  # type: ignore[no-untyped-def]
            del connection, handshake
            self.called = True

    class WebSocket:
        scope = {
            "headers": (
                (b"origin", b"https://assistant.acme.cn"),
                (b"authorization", b"Bearer first"),
                (b"Authorization", b"Bearer second"),
            )
        }
        client = None

        def __init__(self) -> None:
            self.closed: tuple[int, str] | None = None

        async def close(self, *, code: int, reason: str) -> None:
            self.closed = (code, reason)

    server = Server()
    websocket = WebSocket()
    endpoint = create_starlette_websocket_endpoint(server)  # type: ignore[arg-type]
    asyncio.run(endpoint(websocket))

    assert websocket.closed == (4400, "invalid_handshake")
    assert not server.called
