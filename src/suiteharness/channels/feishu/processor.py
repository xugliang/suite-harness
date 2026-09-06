"""One normalized Feishu event path shared by webhook and long connection."""

from __future__ import annotations

import asyncio
import heapq
import hmac
import json
import math
import re
import secrets
import time
import unicodedata
import weakref
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol, TypeAlias

from suiteharness.channels.models import (
    ChannelKind,
    InboundMessage,
    OutboundEvent,
)

from .models import (
    FeishuAuthenticationError,
    FeishuEventOutcome,
    FeishuPayloadError,
    FeishuWebhookResponse,
)
from .security import BoundedDecryptor, IdentityDecryptor, RawEventDecryptor, RawSignatureVerifier

JsonObject: TypeAlias = dict[str, Any]
OutboundSink: TypeAlias = Callable[[InboundMessage, OutboundEvent], Awaitable[None]]


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> JsonObject:
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise FeishuPayloadError("Feishu JSON contains duplicate object keys")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise FeishuPayloadError("Feishu JSON contains a non-finite number")


def _constant_text_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def decode_json_object(body: bytes) -> JsonObject:
    """Decode strict UTF-8 JSON and reject duplicate keys and non-finite numbers."""

    try:
        text = body.decode("utf-8")
        parsed = json.loads(
            text,
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=_reject_json_constant,
        )
    except FeishuPayloadError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise FeishuPayloadError("Feishu event is not valid UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise FeishuPayloadError("Feishu event JSON must be an object")
    return parsed


def _object(parent: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise FeishuPayloadError(f"Feishu event field {key!r} must be an object")
    return value


def _string(
    parent: Mapping[str, Any],
    key: str,
    *,
    maximum: int = 1024,
    required: bool = True,
) -> str | None:
    value = parent.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        raise FeishuPayloadError(f"Feishu event field {key!r} is invalid")
    return value


class FeishuDispatcher(Protocol):
    """Usually implemented by :class:`EnterpriseChannelGateway`."""

    def dispatch(self, message: InboundMessage) -> AsyncIterator[OutboundEvent]: ...


class EventDeduplicator(Protocol):
    @property
    def processing_lease_seconds(self) -> float: ...

    async def claim(self, event_id: str) -> bool: ...

    async def complete(self, event_id: str) -> None: ...

    async def release(self, event_id: str) -> None: ...


class InMemoryEventDeduplicator:
    """Atomic process-local event reservation with bounded retention."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 86_400,
        processing_lease_seconds: float | None = None,
        max_entries: int = 100_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int | float)
            or not math.isfinite(ttl_seconds)
            or ttl_seconds <= 0
        ):
            raise ValueError("dedupe ttl_seconds must be positive")
        if processing_lease_seconds is not None and (
            isinstance(processing_lease_seconds, bool)
            or not isinstance(processing_lease_seconds, int | float)
            or not math.isfinite(processing_lease_seconds)
            or processing_lease_seconds <= 0
        ):
            raise ValueError("dedupe processing_lease_seconds must be positive")
        if (
            isinstance(max_entries, bool)
            or not isinstance(max_entries, int)
            or max_entries <= 0
        ):
            raise ValueError("dedupe max_entries must be positive")
        self._ttl = ttl_seconds
        self._processing_lease = (
            min(ttl_seconds, 660.0)
            if processing_lease_seconds is None
            else float(processing_lease_seconds)
        )
        self._maximum = max_entries
        self._clock = clock
        self._entries: OrderedDict[str, tuple[float, str, bool]] = OrderedDict()
        self._expirations: list[tuple[float, str, str]] = []
        self._owned_claims: weakref.WeakKeyDictionary[asyncio.Task[object], dict[str, str]] = (
            weakref.WeakKeyDictionary()
        )
        self._lock = asyncio.Lock()

    @property
    def processing_lease_seconds(self) -> float:
        return self._processing_lease

    def _purge_expired(self, now: float) -> None:
        while self._expirations and self._expirations[0][0] <= now:
            expires_at, event_id, owner_token = heapq.heappop(self._expirations)
            current = self._entries.get(event_id)
            if current is not None and current[:2] == (expires_at, owner_token):
                self._entries.pop(event_id, None)

    async def claim(self, event_id: str) -> bool:
        now = self._clock()
        async with self._lock:
            self._purge_expired(now)
            if event_id in self._entries:
                return False
            if len(self._entries) >= self._maximum:
                # Evicting a live marker would allow a duplicate request to run.
                raise RuntimeError("event deduplication capacity has been reached")
            owner_token = secrets.token_urlsafe(32)
            expires_at = now + self._processing_lease
            self._entries[event_id] = (
                expires_at,
                owner_token,
                False,
            )
            heapq.heappush(self._expirations, (expires_at, event_id, owner_token))
            task = self._current_task()
            self._owned_claims.setdefault(task, {})[event_id] = owner_token
            return True

    async def complete(self, event_id: str) -> None:
        now = self._clock()
        task = self._current_task()
        async with self._lock:
            owned = self._owned_claims.get(task)
            owner_token = None if owned is None else owned.get(event_id)
            current = self._entries.get(event_id)
            if owner_token is None or current is None or current[1] != owner_token:
                raise RuntimeError("event claim ownership expired before completion")
            expires_at = now + self._ttl
            self._entries[event_id] = (expires_at, owner_token, True)
            heapq.heappush(self._expirations, (expires_at, event_id, owner_token))
            self._entries.move_to_end(event_id)
            owned.pop(event_id, None)

    async def release(self, event_id: str) -> None:
        task = self._current_task()
        async with self._lock:
            owned = self._owned_claims.get(task)
            owner_token = None if owned is None else owned.pop(event_id, None)
            current = self._entries.get(event_id)
            if owner_token is not None and current is not None and current[1] == owner_token:
                self._entries.pop(event_id, None)

    @staticmethod
    def _current_task() -> asyncio.Task[object]:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("channel event claims require an asyncio task")
        return task


class FeishuEventProcessor:
    """Normalize supported message events and dispatch them exactly once per process."""

    def __init__(
        self,
        dispatcher: FeishuDispatcher,
        *,
        bot_open_id: str | None,
        default_product_id: str | None = None,
        deduplicator: EventDeduplicator | None = None,
        outbound_sink: OutboundSink | None = None,
        clock: Callable[[], datetime] | None = None,
        processing_timeout_seconds: float = 600.0,
    ) -> None:
        if bot_open_id is not None and (not bot_open_id or "\x00" in bot_open_id):
            raise ValueError("bot_open_id is invalid")
        if default_product_id is not None and not re.fullmatch(
            r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*", default_product_id
        ):
            raise ValueError("default_product_id is invalid")
        if (
            isinstance(processing_timeout_seconds, bool)
            or not isinstance(processing_timeout_seconds, int | float)
            or not math.isfinite(processing_timeout_seconds)
            or not (0 < processing_timeout_seconds <= 3_600)
        ):
            raise ValueError("processing_timeout_seconds must be in (0, 3600]")
        selected_deduplicator = deduplicator or InMemoryEventDeduplicator(
            processing_lease_seconds=float(processing_timeout_seconds) + 60.0
        )
        processing_lease_seconds = getattr(
            selected_deduplicator,
            "processing_lease_seconds",
            None,
        )
        if (
            isinstance(processing_lease_seconds, bool)
            or not isinstance(processing_lease_seconds, int | float)
            or not math.isfinite(processing_lease_seconds)
            or processing_lease_seconds <= processing_timeout_seconds
        ):
            raise ValueError(
                "event deduplicator processing lease must be strictly greater than "
                "processing_timeout_seconds"
            )
        self._dispatcher = dispatcher
        self._bot_open_id = bot_open_id
        self._default_product_id = default_product_id
        self._deduplicator = selected_deduplicator
        self._outbound_sink = outbound_sink
        self._clock = clock or (lambda: datetime.now(UTC))
        self._processing_timeout = float(processing_timeout_seconds)

    @property
    def processing_timeout_seconds(self) -> float:
        return self._processing_timeout

    async def accept_verified_json(self, body: bytes) -> FeishuEventOutcome:
        """Accept JSON bytes already authenticated by webhook or the official SDK."""

        return await self.process_payload(decode_json_object(body))

    async def process_payload(self, payload: Mapping[str, Any]) -> FeishuEventOutcome:
        header = _object(payload, "header")
        event_type = _string(header, "event_type")
        if event_type != "im.message.receive_v1":
            return FeishuEventOutcome.IGNORED
        event_id = _string(header, "event_id")
        assert event_id is not None
        if not await self._deduplicator.claim(event_id):
            return FeishuEventOutcome.DUPLICATE
        try:
            async with asyncio.timeout(self._processing_timeout):
                message = self._normalize_message(payload, event_id=event_id)
                if message is None:
                    await self._deduplicator.complete(event_id)
                    return FeishuEventOutcome.IGNORED
                async for outbound in self._dispatcher.dispatch(message):
                    if self._outbound_sink is not None:
                        await self._outbound_sink(message, outbound)
                await self._deduplicator.complete(event_id)
        except BaseException:
            await self._deduplicator.release(event_id)
            raise
        return FeishuEventOutcome.DISPATCHED

    def _normalize_message(
        self,
        payload: Mapping[str, Any],
        *,
        event_id: str,
    ) -> InboundMessage | None:
        header = _object(payload, "header")
        event = _object(payload, "event")
        sender = _object(event, "sender")
        sender_type = _string(sender, "sender_type", required=False)
        if sender_type in {"app", "bot"}:
            return None
        sender_ids = _object(sender, "sender_id")
        sender_external_id = _first_id(sender_ids, ("open_id", "user_id", "union_id"))
        if self._bot_open_id is not None and _constant_text_equal(
            sender_external_id, self._bot_open_id
        ):
            return None

        raw_message = _object(event, "message")
        if _string(raw_message, "message_type") != "text":
            return None
        message_id = _string(raw_message, "message_id")
        chat_id = _string(raw_message, "chat_id")
        chat_type = _string(raw_message, "chat_type")
        assert message_id is not None and chat_id is not None and chat_type is not None

        mention_keys: tuple[str, ...] = ()
        if chat_type != "p2p":
            if self._bot_open_id is None:
                return None
            mention_keys = self._matching_bot_mention_keys(raw_message)
            if not mention_keys:
                return None

        raw_content = _string(raw_message, "content", maximum=1_000_000)
        assert raw_content is not None
        content = decode_json_object(raw_content.encode("utf-8"))
        text = content.get("text")
        if not isinstance(text, str) or len(text) > 1_000_000 or "\x00" in text:
            raise FeishuPayloadError("Feishu text message content is invalid")
        for key in mention_keys:
            text = text.replace(key, " ")
        text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n")).strip()
        if not text:
            return None

        received_at = _event_time(header, raw_message, self._clock)
        root_id = _string(raw_message, "root_id", required=False)
        parent_id = _string(raw_message, "parent_id", required=False)
        metadata: dict[str, str] = {"chat_type": chat_type}
        if root_id is not None:
            metadata["root_id"] = root_id
        if parent_id is not None:
            metadata["parent_id"] = parent_id
        # Keep delivery addressing (chat_id) separate from the durable session
        # scope. A legacy deployment may key a plain private chat by the employee
        # open_id, so an offline import can resume the same logical history
        # without ever sending a reply to an open_id as though it were a chat.
        thread_id = root_id or parent_id
        if thread_id is not None:
            metadata["session_scope_id"] = f"{chat_id}:{thread_id}"
        elif chat_type == "p2p":
            metadata["session_scope_id"] = sender_external_id
        else:
            metadata["session_scope_id"] = chat_id
        return InboundMessage(
            channel=ChannelKind.FEISHU,
            event_id=event_id,
            message_id=message_id,
            conversation_id=chat_id,
            sender_external_id=sender_external_id,
            text=text,
            # Product routing is supplied only by the trusted host adapter.  A
            # similarly named field in the provider event is intentionally ignored.
            product_id=self._default_product_id,
            received_at=received_at,
            metadata=metadata,
        )

    def _matching_bot_mention_keys(self, message: Mapping[str, Any]) -> tuple[str, ...]:
        mentions = message.get("mentions", [])
        if not isinstance(mentions, list):
            raise FeishuPayloadError("Feishu message mentions must be an array")
        matching: list[str] = []
        for mention in mentions:
            if not isinstance(mention, dict):
                raise FeishuPayloadError("Feishu message mention must be an object")
            mention_id = mention.get("id")
            if not isinstance(mention_id, dict):
                continue
            open_id = mention_id.get("open_id")
            if open_id != self._bot_open_id:
                continue
            key = mention.get("key")
            if isinstance(key, str) and key and len(key) <= 256 and "\x00" not in key:
                matching.append(key)
        return tuple(matching)


def _first_id(values: Mapping[str, Any], names: tuple[str, ...]) -> str:
    for name in names:
        value = values.get(name)
        if isinstance(value, str) and value and len(value) <= 1024 and "\x00" not in value:
            return value
    raise FeishuPayloadError("Feishu sender has no usable company identity")


def _event_time(
    header: Mapping[str, Any],
    message: Mapping[str, Any],
    clock: Callable[[], datetime],
) -> datetime:
    value = header.get("create_time", message.get("create_time"))
    if value is None:
        result = clock()
    else:
        if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
            raise FeishuPayloadError("Feishu event create_time is invalid")
        try:
            result = datetime.fromtimestamp(int(value) / 1000, tz=UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise FeishuPayloadError("Feishu event create_time is out of range") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise FeishuPayloadError("Feishu event clock returned a naive datetime")
    return result.astimezone(UTC)


class FeishuWebhookHandler:
    """Authenticate raw bytes, decrypt, validate token, then dispatch.

    This ordering is intentional: no untrusted event object reaches a JSON
    parser or application dispatcher before raw-body signature verification.
    """

    def __init__(
        self,
        processor: FeishuEventProcessor,
        *,
        verifier: RawSignatureVerifier,
        verification_token: str,
        decryptor: RawEventDecryptor | None = None,
        max_body_bytes: int = 1_048_576,
    ) -> None:
        if not verification_token:
            raise ValueError("verification_token must not be blank")
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")
        self._processor = processor
        self._verifier = verifier
        self._verification_token = verification_token
        self._decryptor = BoundedDecryptor(
            decryptor or IdentityDecryptor(), max_plaintext_bytes=max_body_bytes
        )
        self._max_body_bytes = max_body_bytes

    @property
    def max_body_bytes(self) -> int:
        """Maximum authenticated callback size, also enforced by ASGI streaming."""

        return self._max_body_bytes

    async def handle(
        self,
        headers: Mapping[str, str],
        body: bytes,
    ) -> FeishuWebhookResponse:
        if not isinstance(body, bytes):
            raise FeishuPayloadError("Feishu callback body must be bytes")
        if len(body) > self._max_body_bytes:
            raise FeishuPayloadError("Feishu callback is too large")
        self._verifier.verify(headers, body)
        plaintext = await self._decryptor.decrypt(body)
        payload = decode_json_object(plaintext)
        self._verify_token(payload)

        if payload.get("type") == "url_verification":
            challenge = _string(payload, "challenge", maximum=4096)
            assert challenge is not None
            return FeishuWebhookResponse(
                body={"challenge": challenge},
                outcome=FeishuEventOutcome.CHALLENGE,
            )
        outcome = await self._processor.process_payload(payload)
        return FeishuWebhookResponse(body={}, outcome=outcome)

    def _verify_token(self, payload: Mapping[str, Any]) -> None:
        candidate = payload.get("token")
        header = payload.get("header")
        if isinstance(header, dict):
            candidate = header.get("token", candidate)
        if not isinstance(candidate, str) or not _constant_text_equal(
            candidate, self._verification_token
        ):
            raise FeishuAuthenticationError("invalid Feishu verification token")


__all__ = [
    "EventDeduplicator",
    "FeishuDispatcher",
    "FeishuEventProcessor",
    "FeishuWebhookHandler",
    "InMemoryEventDeduplicator",
    "OutboundSink",
    "decode_json_object",
]
