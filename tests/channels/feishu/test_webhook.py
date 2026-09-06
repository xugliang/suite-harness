from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from suiteharness.channels import OutboundEvent, OutboundEventKind
from suiteharness.channels.feishu import (
    FeishuAuthenticationError,
    FeishuEventOutcome,
    FeishuEventProcessor,
    FeishuHeaderSignatureVerifier,
    FeishuWebhookHandler,
    FeishuWebhookResponse,
    create_starlette_feishu_endpoint,
)


class Dispatcher:
    def __init__(self) -> None:
        self.messages = []

    async def dispatch(self, message) -> AsyncIterator[OutboundEvent]:  # type: ignore[no-untyped-def]
        self.messages.append(message)
        yield OutboundEvent(
            kind=OutboundEventKind.COMPLETED,
            request_id="request-1",
            correlation_id="correlation-1",
            payload={"text": "done"},
        )


class Decryptor:
    def __init__(self, plaintext: bytes) -> None:
        self.plaintext = plaintext
        self.calls = 0

    async def decrypt(self, body: bytes) -> bytes:
        self.calls += 1
        assert body == b"encrypted-envelope"
        return self.plaintext


def _event(
    *,
    event_id: str = "evt-1",
    chat_type: str = "p2p",
    sender_type: str = "user",
    sender_open_id: str = "ou-user",
    text: str = "你好",
    mentions: list[object] | None = None,
) -> dict[str, object]:
    message: dict[str, object] = {
        "message_id": "om-1",
        "chat_id": "oc-1",
        "chat_type": chat_type,
        "message_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
        "create_time": "1700000000000",
    }
    if mentions is not None:
        message["mentions"] = mentions
    return {
        "schema": "2.0",
        "header": {
            "event_id": event_id,
            "event_type": "im.message.receive_v1",
            "create_time": "1700000000000",
            "token": "verify-token",
        },
        "event": {
            "sender": {
                "sender_type": sender_type,
                "sender_id": {"open_id": sender_open_id},
            },
            "message": message,
        },
    }


def _headers(body: bytes, *, timestamp: int = 1_700_000_000) -> dict[str, str]:
    nonce = "nonce-1"
    digest = hashlib.sha256(
        str(timestamp).encode() + nonce.encode() + b"encrypt-key" + body
    ).hexdigest()
    return {
        "x-lark-request-timestamp": str(timestamp),
        "X-Lark-Request-Nonce": nonce,
        "X-Lark-Signature": digest,
    }


def _handler(dispatcher: Dispatcher, *, decryptor=None):  # type: ignore[no-untyped-def]
    return FeishuWebhookHandler(
        FeishuEventProcessor(
            dispatcher,
            bot_open_id="ou-bot",
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        ),
        verifier=FeishuHeaderSignatureVerifier(
            "encrypt-key", clock=lambda: 1_700_000_000
        ),
        verification_token="verify-token",
        decryptor=decryptor,
    )


def test_signature_and_decryption_happen_before_event_json_parsing() -> None:
    dispatcher = Dispatcher()
    plaintext = json.dumps(_event(), ensure_ascii=False).encode()
    decryptor = Decryptor(plaintext)
    handler = _handler(dispatcher, decryptor=decryptor)

    response = asyncio.run(handler.handle(_headers(b"encrypted-envelope"), b"encrypted-envelope"))

    assert response.outcome is FeishuEventOutcome.DISPATCHED
    assert decryptor.calls == 1
    assert dispatcher.messages[0].text == "你好"
    assert dispatcher.messages[0].received_at == datetime.fromtimestamp(1_700_000_000, tz=UTC)
    assert dispatcher.messages[0].product_id is None
    assert dispatcher.messages[0].conversation_id == "oc-1"
    assert dispatcher.messages[0].metadata["session_scope_id"] == "ou-user"


def test_trusted_default_product_ignores_forged_event_product() -> None:
    dispatcher = Dispatcher()
    payload = _event(event_id="routed-event")
    payload["product_id"] = "product-b"
    payload["event"]["product_id"] = "product-b"  # type: ignore[index]
    processor = FeishuEventProcessor(
        dispatcher,
        bot_open_id="ou-bot",
        default_product_id="product-a",
    )

    outcome = asyncio.run(processor.process_payload(payload))

    assert outcome is FeishuEventOutcome.DISPATCHED
    assert dispatcher.messages[0].product_id == "product-a"


def test_invalid_signature_never_reaches_decryptor() -> None:
    dispatcher = Dispatcher()
    decryptor = Decryptor(b"{}")
    handler = _handler(dispatcher, decryptor=decryptor)
    headers = _headers(b"encrypted-envelope")
    headers["X-Lark-Signature"] = "0" * 64

    with pytest.raises(FeishuAuthenticationError, match="signature"):
        asyncio.run(handler.handle(headers, b"encrypted-envelope"))
    assert decryptor.calls == 0
    assert dispatcher.messages == []


def test_url_challenge_is_token_verified_and_not_dispatched() -> None:
    dispatcher = Dispatcher()
    body = json.dumps(
        {"type": "url_verification", "token": "verify-token", "challenge": "answer"}
    ).encode()
    response = asyncio.run(_handler(dispatcher).handle(_headers(body), body))
    assert response.outcome is FeishuEventOutcome.CHALLENGE
    assert response.body == {"challenge": "answer"}
    assert dispatcher.messages == []


def test_event_is_deduplicated_and_group_requires_bot_mention() -> None:
    async def exercise():
        dispatcher = Dispatcher()
        processor = FeishuEventProcessor(dispatcher, bot_open_id="ou-bot")
        direct = _event(event_id="same")
        assert await processor.process_payload(direct) is FeishuEventOutcome.DISPATCHED
        assert await processor.process_payload(direct) is FeishuEventOutcome.DUPLICATE

        unmentioned = _event(event_id="group-1", chat_type="group")
        assert await processor.process_payload(unmentioned) is FeishuEventOutcome.IGNORED

        mentioned = _event(
            event_id="group-2",
            chat_type="group",
            text="@_user_1  请总结",
            mentions=[{"key": "@_user_1", "id": {"open_id": "ou-bot"}}],
        )
        assert await processor.process_payload(mentioned) is FeishuEventOutcome.DISPATCHED
        return dispatcher.messages

    messages = asyncio.run(exercise())
    assert [item.text for item in messages] == ["你好", "请总结"]
    assert messages[1].metadata["session_scope_id"] == "oc-1"


def test_thread_uses_chat_and_root_only_for_durable_session_scope() -> None:
    dispatcher = Dispatcher()
    payload = _event(event_id="thread-event", chat_type="group")
    payload["event"]["message"]["root_id"] = "om-root"  # type: ignore[index]
    payload["event"]["message"]["mentions"] = [  # type: ignore[index]
        {"key": "@bot", "id": {"open_id": "ou-bot"}}
    ]
    payload["event"]["message"]["content"] = json.dumps({"text": "@bot 问题"})  # type: ignore[index]

    outcome = asyncio.run(
        FeishuEventProcessor(dispatcher, bot_open_id="ou-bot").process_payload(payload)
    )

    assert outcome is FeishuEventOutcome.DISPATCHED
    assert dispatcher.messages[0].conversation_id == "oc-1"
    assert dispatcher.messages[0].metadata["session_scope_id"] == "oc-1:om-root"


@pytest.mark.parametrize(
    ("sender_type", "sender_id"),
    [("bot", "some-bot"), ("app", "some-app"), ("user", "ou-bot")],
)
def test_bot_self_echo_is_ignored(sender_type: str, sender_id: str) -> None:
    dispatcher = Dispatcher()
    payload = _event(sender_type=sender_type, sender_open_id=sender_id)
    outcome = asyncio.run(
        FeishuEventProcessor(dispatcher, bot_open_id="ou-bot").process_payload(payload)
    )
    assert outcome is FeishuEventOutcome.IGNORED
    assert dispatcher.messages == []


def test_failed_dispatch_releases_event_for_provider_retry() -> None:
    class Failing:
        def __init__(self) -> None:
            self.calls = 0

        async def dispatch(self, message):  # type: ignore[no-untyped-def]
            self.calls += 1
            if False:
                yield message
            raise RuntimeError("failure")

    async def exercise() -> int:
        dispatcher = Failing()
        processor = FeishuEventProcessor(dispatcher, bot_open_id="ou-bot")
        for _ in range(2):
            with pytest.raises(RuntimeError, match="failure"):
                await processor.process_payload(_event(event_id="retry-event"))
        return dispatcher.calls

    assert asyncio.run(exercise()) == 2


def test_asgi_webhook_streams_and_rejects_undeclared_oversized_body() -> None:
    class Handler:
        max_body_bytes = 4

        def __init__(self) -> None:
            self.calls = 0

        async def handle(self, headers, body):  # type: ignore[no-untyped-def]
            del headers, body
            self.calls += 1
            return FeishuWebhookResponse(body={}, outcome=FeishuEventOutcome.IGNORED)

    class Request:
        headers: dict[str, str] = {}

        def __init__(self, chunks: tuple[bytes, ...], declared: bytes | None) -> None:
            self.scope = {
                "headers": ()
                if declared is None
                else ((b"content-length", declared),)
            }
            self._chunks = chunks
            self.read_chunks = 0

        async def stream(self):  # type: ignore[no-untyped-def]
            for chunk in self._chunks:
                self.read_chunks += 1
                yield chunk

    for declared in (None, b"1"):
        handler = Handler()
        request = Request((b"ab", b"cde", b"must-not-be-read"), declared)
        endpoint = create_starlette_feishu_endpoint(handler)  # type: ignore[arg-type]

        response = asyncio.run(endpoint(request))

        assert response.status_code == 413
        assert request.read_chunks == 2
        assert handler.calls == 0


def test_asgi_webhook_accepts_exact_limit_and_rejects_bad_length_header() -> None:
    class Handler:
        max_body_bytes = 4

        def __init__(self) -> None:
            self.calls = 0

        async def handle(self, headers, body):  # type: ignore[no-untyped-def]
            del headers
            self.calls += 1
            assert body == b"abcd"
            return FeishuWebhookResponse(body={}, outcome=FeishuEventOutcome.IGNORED)

    class Request:
        headers: dict[str, str] = {}

        def __init__(self, headers: tuple[tuple[bytes, bytes], ...]) -> None:
            self.scope = {"headers": headers}
            self.streamed = False

        async def stream(self):  # type: ignore[no-untyped-def]
            self.streamed = True
            yield b"abcd"

    handler = Handler()
    endpoint = create_starlette_feishu_endpoint(handler)  # type: ignore[arg-type]
    accepted = Request(((b"content-length", b"4"),))
    response = asyncio.run(endpoint(accepted))
    assert response.status_code == 200
    assert handler.calls == 1

    duplicate = Request(
        ((b"content-length", b"1"), (b"content-length", b"1"))
    )
    response = asyncio.run(endpoint(duplicate))
    assert response.status_code == 400
    assert not duplicate.streamed
    assert handler.calls == 1
