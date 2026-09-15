"""Official Feishu SDK bridge and reliable, safe outbound terminal delivery.

Company employee directories and business identity mapping belong to deployment.
This reusable adapter depends only on SuiteHarness channel contracts.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import inspect
import json
import math
import re
import threading
import unicodedata
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol, cast

from pydantic import SecretStr
from suiteharness.channels.models import ChannelKind, InboundMessage, OutboundEvent, OutboundEventKind
from .models import FeishuEventOutcome
from .processor import decode_json_object

_AT_TAG = re.compile(r"(?i)<\s*(/\s*)?at(?=\s|>|/)")

class FeishuDeploymentConfigurationError(ValueError):
    """The Feishu adapter cannot be constructed safely."""


class FeishuLongConnectionError(RuntimeError):
    """The official long-connection bridge failed without exposing secrets."""


class FeishuTextClient(Protocol):
    async def send_text(
        self,
        receive_id: str,
        text: str,
        *,
        receive_id_type: str = "chat_id",
        idempotency_key: str | None = None,
    ) -> Any: ...

    async def reply_text(
        self,
        message_id: str,
        text: str,
        *,
        idempotency_key: str | None = None,
        reply_in_thread: bool = True,
    ) -> Any: ...


class SafeFeishuTextFormatter:
    """Convert a terminal SuiteHarness event into bounded Feishu plain text."""

    def __init__(self, *, max_chars: int = 30_000) -> None:
        if not 256 <= max_chars <= 100_000:
            raise ValueError("max_chars must be between 256 and 100000")
        self._maximum = max_chars

    def format(self, event: OutboundEvent) -> str:
        if event.kind is OutboundEventKind.COMPLETED:
            text = self._completed_text(event.payload.get("output"))
        elif event.kind is OutboundEventKind.FAILED:
            failure = event.payload.get("failure")
            retryable = isinstance(failure, dict) and failure.get("retryable") is True
            text = (
                "当前请求暂未完成，请稍后重试。"
                if retryable
                else f"当前请求处理失败，请联系管理员并提供请求编号：{event.request_id}。"
            )
        else:
            raise ValueError("only terminal SuiteHarness events can be formatted")
        return self._sanitize(text)

    @staticmethod
    def _completed_text(output: Any) -> str:
        if isinstance(output, str):
            return output
        if isinstance(output, dict):
            for key in ("answer", "presentation", "text", "message"):
                candidate = output.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate
        if output is None:
            return "查询已完成，但没有可显示的结果。"
        try:
            return json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2)
        except (TypeError, ValueError, RecursionError):
            return "查询已完成，但结果无法安全显示。"

    def _sanitize(self, text: str) -> str:
        normalized = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
        cleaned = "".join(
            character
            for character in normalized
            if character in {"\n", "\t"}
            or unicodedata.category(character) not in {"Cc", "Cf", "Cs"}
        ).strip()
        # Feishu text messages understand <at ...> markup.  Model output must
        # remain plain text and must not be able to mention employees or @all.
        cleaned = _AT_TAG.sub(lambda match: "＜" + match.group(0)[1:], cleaned)
        if not cleaned:
            cleaned = "查询已完成，但没有可显示的结果。"
        if len(cleaned) <= self._maximum:
            return cleaned
        suffix = "\n（内容过长，已安全截断。）"
        return cleaned[: self._maximum - len(suffix)].rstrip() + suffix


class FinalReplySink:
    """Send at most one terminal plain-text answer for one inbound message.

    SuiteHarness' durable inbound-event deduplicator is the cross-process fence.
    This sink adds a concurrency-safe process-local fence so replayed terminal
    events and concurrent callbacks never produce a second HTTP attempt.  A
    failed attempt is removed from the local fence so a later provider retry can
    make another HTTP attempt with the same stable Feishu ``uuid``. Feishu's
    server-side idempotency then resolves an ambiguous first response without a
    second customer-visible answer.
    """

    def __init__(
        self,
        client: FeishuTextClient,
        *,
        formatter: SafeFeishuTextFormatter | None = None,
        max_delivery_keys: int = 100_000,
        reply_in_threads: bool = False,
    ) -> None:
        if not callable(getattr(client, "send_text", None)):
            raise TypeError("client must implement async send_text")
        if max_delivery_keys <= 0:
            raise ValueError("max_delivery_keys must be positive")
        if not isinstance(reply_in_threads, bool):
            raise TypeError("reply_in_threads must be a boolean")
        if reply_in_threads and not callable(getattr(client, "reply_text", None)):
            raise TypeError("thread replies require client.reply_text")
        self._client = client
        self._formatter = formatter or SafeFeishuTextFormatter()
        self._maximum = max_delivery_keys
        self._reply_in_threads = reply_in_threads
        self._attempts: OrderedDict[str, asyncio.Task[None]] = OrderedDict()
        self._lock = asyncio.Lock()

    @staticmethod
    def _delivery_key(message: InboundMessage, event: OutboundEvent) -> str:
        material = "\x00".join(
            (
                message.event_id,
                message.message_id,
                message.conversation_id,
                event.request_id,
                event.correlation_id,
            )
        )
        # Feishu's uuid field is limited to 50 characters.  A 128-bit prefix is
        # sufficient for this per-deployment idempotency namespace.
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

    async def __call__(self, message: InboundMessage, event: OutboundEvent) -> None:
        if message.channel is not ChannelKind.FEISHU:
            raise ValueError("Feishu outbound sink received another channel")
        if event.kind not in {OutboundEventKind.COMPLETED, OutboundEventKind.FAILED}:
            return
        key = self._delivery_key(message, event)
        async with self._lock:
            task = self._attempts.get(key)
            if task is None:
                text = self._formatter.format(event)
                task = asyncio.create_task(
                    self._send_attempt(message, text, key),
                    name=f"feishu-final-{key[:12]}",
                )
                self._attempts[key] = task
                while len(self._attempts) > self._maximum:
                    oldest_key, oldest = next(iter(self._attempts.items()))
                    if not oldest.done():
                        break
                    self._attempts.pop(oldest_key, None)
            else:
                self._attempts.move_to_end(key)
        await asyncio.shield(task)

    async def _send_attempt(self, message: InboundMessage, text: str, key: str) -> None:
        current = asyncio.current_task()
        try:
            await self._send(message, text, key)
        except BaseException:
            # Remove only our own failed task. Concurrent callers still await
            # this same attempt; the next distinct retry creates one new task.
            async with self._lock:
                if self._attempts.get(key) is current:
                    self._attempts.pop(key, None)
            raise

    async def _send(self, message: InboundMessage, text: str, key: str) -> None:
        trusted_thread_event = (
            message.metadata.get("chat_type") == "group"
            or isinstance(message.metadata.get("root_id"), str)
            or isinstance(message.metadata.get("parent_id"), str)
        )
        if self._reply_in_threads and trusted_thread_event:
            await self._client.reply_text(
                message.message_id,
                text,
                idempotency_key=key,
                reply_in_thread=True,
            )
            return
        await self._client.send_text(
            message.conversation_id,
            text,
            receive_id_type="chat_id",
            idempotency_key=key,
        )


class LarkLongConnectionClient(Protocol):
    """The tiny synchronous surface exposed by ``lark_oapi.ws.Client``."""

    def start(self) -> None: ...


SdkEventHandler = Callable[[Any], None]
LarkClientFactory = Callable[[str, str, SdkEventHandler], LarkLongConnectionClient]
AsyncCloser = Callable[[], Awaitable[None]]


class _LoopOwnedLarkClient:
    """Keep the official SDK's module-level event loop on its owning thread."""

    def __init__(self, client: Any, loop: asyncio.AbstractEventLoop) -> None:
        self._client = client
        self._loop = loop

    def start(self) -> None:
        try:
            self._client.start()
        finally:
            if not self._loop.is_closed():
                self._loop.close()


def _sdk_event_to_json_bytes(event: Any) -> bytes:
    if isinstance(event, bytes):
        body = event
    elif isinstance(event, Mapping):
        try:
            body = json.dumps(
                event,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError) as exc:
            raise FeishuLongConnectionError(
                "official Feishu SDK callback produced an invalid event"
            ) from exc
    else:
        try:
            import lark_oapi as lark

            marshalled = lark.JSON.marshal(event)
        except (ImportError, AttributeError, TypeError, ValueError) as exc:
            raise FeishuLongConnectionError(
                "official Feishu SDK callback could not be serialized"
            ) from exc
        if not isinstance(marshalled, str):
            raise FeishuLongConnectionError("official Feishu SDK callback produced no event JSON")
        body = marshalled.encode("utf-8")
    # Validate the reconstructed wire object before it crosses the trusted SDK
    # boundary.  SuiteHarness validates it again at the processor boundary.
    decode_json_object(body)
    return body


def default_lark_client_factory(
    app_id: str,
    app_secret: str,
    on_event: SdkEventHandler,
) -> LarkLongConnectionClient:
    try:
        import lark_oapi as lark
    except ImportError as exc:  # pragma: no cover - production diagnostic
        raise FeishuDeploymentConfigurationError(
            "Feishu long connection requires the official lark_oapi package"
        ) from exc
    # lark_oapi.ws.client stores its event loop in a module-level variable and
    # Client.start() calls run_until_complete on it.  Bind a fresh loop here;
    # this factory is intentionally invoked by our SDK thread, never by the
    # already-running SuiteHarness event loop.
    import lark_oapi.ws.client as lark_ws_client

    sdk_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(sdk_loop)
    lark_ws_client.loop = sdk_loop
    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_event)
        .build()
    )
    return cast(
        LarkLongConnectionClient,
        _LoopOwnedLarkClient(
            lark.ws.Client(
                app_id,
                app_secret,
                event_handler=event_handler,
                log_level=lark.LogLevel.WARNING,
                auto_reconnect=True,
            ),
            sdk_loop,
        ),
    )


class OfficialLarkLongConnectionSdk:
    """Thread bridge from the official synchronous SDK to SuiteHarness async IO.

    The registered official-SDK handler blocks on ``run_coroutine_threadsafe``;
    therefore the SDK does not acknowledge the event until SuiteHarness returns
    a :class:`FeishuEventOutcome`.  The current official Python SDK has no public
    ``stop`` method.  In that case ``close`` safely closes this callback bridge
    and marks :attr:`process_replacement_required`; the supervisor must replace
    the whole process before another Feishu receiver takes over.
    """

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str | SecretStr,
        client_factory: LarkClientFactory = default_lark_client_factory,
        callback_timeout_seconds: float = 600.0,
        close_timeout_seconds: float = 10.0,
        owned_closers: tuple[AsyncCloser, ...] = (),
    ) -> None:
        checked_id, checked_secret = validate_feishu_credentials(app_id, app_secret)
        for name, value, maximum in (
            ("callback_timeout_seconds", callback_timeout_seconds, 7_205.0),
            ("close_timeout_seconds", close_timeout_seconds, 300.0),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or not 0 < value <= maximum
            ):
                raise ValueError(f"Feishu SDK {name} is outside its safe range")
        if not callable(client_factory):
            raise TypeError("client_factory must be callable")
        if any(not callable(closer) for closer in owned_closers):
            raise TypeError("owned_closers must be callable")
        self._app_id = checked_id
        self._app_secret = SecretStr(checked_secret)
        self._factory = client_factory
        self._callback_timeout = callback_timeout_seconds
        self._close_timeout = close_timeout_seconds
        self._owned_closers = owned_closers
        self._state_lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._callback: Callable[[bytes], Awaitable[FeishuEventOutcome]] | None = None
        self._client: LarkLongConnectionClient | None = None
        self._thread: threading.Thread | None = None
        self._client_ready = threading.Event()
        self._done: asyncio.Future[None] | None = None
        self._pending: set[concurrent.futures.Future[FeishuEventOutcome]] = set()
        self._accepting = False
        self._closed = False
        self._process_replacement_required = False
        self._closers_closed = False

    def __repr__(self) -> str:
        return f"{type(self).__name__}(app_id=<redacted>, app_secret=SecretStr('**********'))"

    @property
    def process_replacement_required(self) -> bool:
        with self._state_lock:
            return self._process_replacement_required

    async def run(
        self,
        callback: Callable[[bytes], Awaitable[FeishuEventOutcome]],
    ) -> None:
        if not callable(callback):
            raise TypeError("callback must be callable")
        loop = asyncio.get_running_loop()
        with self._state_lock:
            if self._closed:
                raise RuntimeError("Feishu long-connection adapter is closed")
            if self._thread is not None:
                raise RuntimeError("Feishu long-connection adapter may run only once")
            done = loop.create_future()
            self._loop = loop
            self._callback = callback
            self._done = done
            self._accepting = True
            thread = threading.Thread(
                target=self._run_client,
                name="suiteharness-feishu-sdk",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        await done

    def _on_sdk_event(self, event: Any) -> None:
        body = _sdk_event_to_json_bytes(event)
        with self._state_lock:
            loop = self._loop
            callback = self._callback
            if not self._accepting or loop is None or callback is None or loop.is_closed():
                raise FeishuLongConnectionError(
                    "Feishu callback bridge is no longer accepting events"
                )
            future = asyncio.run_coroutine_threadsafe(callback(body), loop)
            self._pending.add(future)
        try:
            outcome = future.result(timeout=self._callback_timeout)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise FeishuLongConnectionError(
                "SuiteHarness did not finish the Feishu event before the callback timeout"
            ) from exc
        except BaseException:
            raise FeishuLongConnectionError(
                "SuiteHarness rejected the verified Feishu SDK event"
            ) from None
        finally:
            with self._state_lock:
                self._pending.discard(future)
        if not isinstance(outcome, FeishuEventOutcome):
            raise FeishuLongConnectionError("SuiteHarness returned an invalid Feishu event outcome")

    def _run_client(self) -> None:
        failure: FeishuLongConnectionError | None = None
        try:
            client = self._factory(
                self._app_id,
                self._app_secret.get_secret_value(),
                self._on_sdk_event,
            )
            if not callable(getattr(client, "start", None)):
                raise TypeError("SDK client has no start method")
            with self._state_lock:
                self._client = client
                closed = self._closed
            self._client_ready.set()
            if closed:
                return
            client.start()
        except BaseException:
            failure = FeishuLongConnectionError(
                "official Feishu SDK long connection exited with an error"
            )
        finally:
            self._client_ready.set()
        with self._state_lock:
            self._accepting = False
            loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._complete_run, failure)

    def _complete_run(self, failure: BaseException | None) -> None:
        done = self._done
        if done is None or done.done():
            return
        if failure is None:
            done.set_result(None)
        else:
            done.set_exception(failure)

    async def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._accepting = False
            pending = tuple(self._pending)
            client = self._client
            thread = self._thread
        for future in pending:
            future.cancel()

        failures: list[BaseException] = []
        if thread is not None and thread.is_alive() and client is None:
            await asyncio.to_thread(self._client_ready.wait, self._close_timeout)
            with self._state_lock:
                client = self._client
        stopper = None
        if client is not None:
            stopper = getattr(client, "stop", None)
            if not callable(stopper):
                stopper = getattr(client, "close", None)
        if callable(stopper):
            try:
                result = await asyncio.to_thread(stopper)
                if inspect.isawaitable(result):
                    await result
            except BaseException as exc:
                failures.append(exc)

        if thread is not None and thread.is_alive() and callable(stopper):
            await asyncio.to_thread(thread.join, self._close_timeout)
        elif (
            thread is not None
            and thread.is_alive()
            and self._done is not None
            and self._done.done()
        ):
            # A short-lived/test client schedules completion just before its
            # SDK thread returns.  Let that already-completed thread cross its
            # final instruction before deciding that process replacement is
            # required; a genuinely blocking SDK still remains bounded.
            await asyncio.to_thread(thread.join, min(self._close_timeout, 0.1))
        thread_alive = thread is not None and thread.is_alive()
        if thread_alive:
            with self._state_lock:
                self._process_replacement_required = True

        self._complete_run(None)
        if not self._closers_closed:
            self._closers_closed = True
            for closer in reversed(self._owned_closers):
                try:
                    await closer()
                except BaseException as exc:
                    failures.append(exc)
        if failures:
            raise BaseExceptionGroup(
                "Feishu long-connection adapter shutdown reported failures",
                failures,
            )


def validate_feishu_credentials(
    app_id: str,
    app_secret: str | SecretStr,
) -> tuple[str, str]:
    if (
        not isinstance(app_id, str)
        or not app_id
        or app_id != app_id.strip()
        or len(app_id) > 256
        or "\x00" in app_id
    ):
        raise FeishuDeploymentConfigurationError("Feishu app_id is invalid")
    secret = app_secret.get_secret_value() if isinstance(app_secret, SecretStr) else app_secret
    if not isinstance(secret, str) or not secret or len(secret) > 4096 or "\x00" in secret:
        raise FeishuDeploymentConfigurationError("Feishu app_secret is invalid")
    return app_id, secret


__all__ = [
    "AsyncCloser", "FeishuDeploymentConfigurationError", "FeishuLongConnectionError",
    "FeishuTextClient", "FinalReplySink", "LarkClientFactory", "LarkLongConnectionClient",
    "OfficialLarkLongConnectionSdk", "SafeFeishuTextFormatter", "SdkEventHandler",
    "default_lark_client_factory", "validate_feishu_credentials",
]
