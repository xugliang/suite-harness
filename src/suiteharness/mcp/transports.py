"""JSON-RPC correlation and guarded MCP transport implementations.

No class in this module opens a process or socket directly. Stdio receives a
persistent session created through ``SandboxBackend``. HTTP receives a bounded
exchange created by the platform network layer after egress authorization.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

from pydantic import JsonValue, TypeAdapter, ValidationError

from suiteharness.runtime.scopes import RequestScope
from suiteharness.sandbox import SandboxBackend

from .models import (
    HttpResumeState,
    JsonRpcErrorObject,
    JsonRpcMessage,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
    LegacySseCompatibility,
    McpErrorCode,
    McpProtocolError,
    McpServerConfig,
    McpTransportError,
    McpTransportKind,
)
from .protocols import (
    HttpRequest,
    HttpResponse,
    InboundNotificationHandler,
    InboundRequestHandler,
    LegacySseAdapter,
    McpEgressPolicy,
    McpHttpExchange,
    McpTokenProvider,
    McpTransport,
    SandboxedStdioSession,
    SandboxedStdioSessionFactory,
    ServerSentEvent,
)

_MESSAGE_ADAPTER = TypeAdapter(JsonRpcMessage)
_DEFAULT_MAX_MESSAGE_BYTES = 4_194_304
_DEFAULT_MAX_STREAM_EVENTS = 256
_MAX_HTTP_HEADER_BYTES = 65_536
_MAX_HTTP_HEADER_COUNT = 128
_HTTP_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def encode_message(message: JsonRpcRequest | JsonRpcNotification | JsonRpcResponse) -> bytes:
    """Encode exactly one compact UTF-8 JSON-RPC message."""

    if isinstance(message, JsonRpcResponse):
        payload: dict[str, object] = {"jsonrpc": "2.0", "id": message.id}
        if message.error is not None:
            payload["error"] = message.error.model_dump(mode="json", exclude_none=True)
        else:
            payload["result"] = message.result
    else:
        payload = message.model_dump(mode="json", exclude_none=True)
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def decode_message(payload: bytes | str) -> JsonRpcRequest | JsonRpcNotification | JsonRpcResponse:
    """Decode one non-batch JSON-RPC message and reject ambiguous envelopes."""

    try:
        raw = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise McpProtocolError("invalid JSON-RPC JSON", code=McpErrorCode.PARSE_ERROR) from exc
    if not isinstance(raw, dict):
        raise McpProtocolError("MCP does not use JSON-RPC batch messages", code=-32600)
    is_response = "id" in raw and ("result" in raw or "error" in raw) and "method" not in raw
    is_request = "id" in raw and "method" in raw
    is_notification = "id" not in raw and "method" in raw
    if sum((is_response, is_request, is_notification)) != 1:
        raise McpProtocolError("ambiguous JSON-RPC envelope", code=-32600)
    try:
        return _MESSAGE_ADAPTER.validate_python(raw)
    except ValidationError as exc:
        raise McpProtocolError("invalid JSON-RPC envelope", code=-32600) from exc


def _wire_size(payload: bytes | str, label: str) -> int:
    if not isinstance(payload, bytes | str):
        raise McpProtocolError(f"{label} must be bytes or text")
    if isinstance(payload, bytes):
        return len(payload)
    try:
        return len(payload.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise McpProtocolError(f"{label} is not valid UTF-8 text") from exc


def _validate_sse_event(
    event: ServerSentEvent,
    *,
    max_message_bytes: int,
) -> None:
    if not isinstance(event, ServerSentEvent):
        raise McpProtocolError("MCP HTTP stream returned an invalid SSE event")
    if not isinstance(event.data, str):
        raise McpProtocolError("SSE event data must be text")
    if _wire_size(event.data, "SSE event data") > max_message_bytes:
        raise McpProtocolError("SSE event data exceeds max_message_bytes")
    for label, value in (("event", event.event), ("event id", event.event_id)):
        if value is not None and (
            not isinstance(value, str)
            or _wire_size(value, f"SSE {label}") > 4_096
            or "\x00" in value
            or "\r" in value
            or "\n" in value
        ):
            raise McpProtocolError(f"invalid SSE {label}")
    if event.retry_ms is not None and (
        isinstance(event.retry_ms, bool)
        or not isinstance(event.retry_ms, int)
        or event.retry_ms < 0
        or event.retry_ms > 86_400_000
    ):
        raise McpProtocolError("invalid SSE retry value")


def parse_sse(
    payload: bytes | str,
    *,
    max_message_bytes: int = _DEFAULT_MAX_MESSAGE_BYTES,
    max_events: int = _DEFAULT_MAX_STREAM_EVENTS,
) -> tuple[ServerSentEvent, ...]:
    """Parse a finite SSE response body (Streamable HTTP, not legacy SSE)."""

    if max_message_bytes < 1 or max_events < 1:
        raise ValueError("SSE bounds must be positive")
    if _wire_size(payload, "SSE payload") > max_message_bytes:
        raise McpProtocolError("SSE payload exceeds max_message_bytes")
    try:
        text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
    except UnicodeDecodeError as exc:
        raise McpProtocolError("SSE payload is not UTF-8") from exc
    events: list[ServerSentEvent] = []
    data: list[str] = []
    event: str | None = None
    event_id: str | None = None
    retry_ms: int | None = None

    def commit() -> None:
        nonlocal data, event, event_id, retry_ms
        if data:
            if len(events) >= max_events:
                raise McpProtocolError("SSE response exceeds max_stream_events")
            item = ServerSentEvent(
                data="\n".join(data),
                event=event,
                event_id=event_id,
                retry_ms=retry_ms,
            )
            _validate_sse_event(item, max_message_bytes=max_message_bytes)
            events.append(item)
        data, event, event_id, retry_ms = [], None, None, None

    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line == "":
            commit()
            continue
        if line.startswith(":"):
            continue
        field, _, raw_value = line.partition(":")
        value = raw_value[1:] if raw_value.startswith(" ") else raw_value
        if field == "data":
            data.append(value)
        elif field == "event":
            event = value
        elif field == "id" and "\x00" not in value:
            event_id = value
        elif field == "retry" and value.isdigit():
            if len(value) > 8:
                raise McpProtocolError("invalid SSE retry value")
            retry_ms = int(value)
    commit()
    return tuple(events)


def _validated_http_response(
    response: HttpResponse,
    *,
    max_message_bytes: int,
) -> HttpResponse:
    if not isinstance(response, HttpResponse):
        raise McpProtocolError("MCP HTTP exchange returned an invalid response")
    if (
        isinstance(response.status, bool)
        or not isinstance(response.status, int)
        or not 100 <= response.status <= 599
    ):
        raise McpProtocolError("MCP HTTP response has an invalid status")
    if not isinstance(response.headers, Mapping):
        raise McpProtocolError("MCP HTTP response headers must be a mapping")
    header_bytes = 0
    header_count = 0
    seen_header_names: set[str] = set()
    for name, value in response.headers.items():
        header_count += 1
        if header_count > _MAX_HTTP_HEADER_COUNT:
            raise McpProtocolError("MCP HTTP response has too many headers")
        if (
            not isinstance(name, str)
            or not _HTTP_HEADER_NAME.fullmatch(name)
            or not isinstance(value, str)
            or "\x00" in value
            or "\r" in value
            or "\n" in value
        ):
            raise McpProtocolError("MCP HTTP response contains an invalid header")
        normalized_name = name.lower()
        if normalized_name in seen_header_names:
            raise McpProtocolError("MCP HTTP response contains duplicate headers")
        seen_header_names.add(normalized_name)
        header_bytes += len(name.encode("ascii")) + _wire_size(value, "HTTP header value") + 4
        if header_bytes > _MAX_HTTP_HEADER_BYTES:
            raise McpProtocolError("MCP HTTP response headers exceed the byte limit")
    if not isinstance(response.body, bytes):
        raise McpProtocolError("MCP HTTP response body must be bytes")
    if len(response.body) > max_message_bytes:
        raise McpProtocolError("MCP HTTP response body exceeds max_message_bytes")
    return response


class SandboxedStdioTransport:
    """Persistent newline-delimited stdio transport launched inside a sandbox."""

    def __init__(
        self,
        config: McpServerConfig,
        *,
        sandbox: SandboxBackend,
        session_factory: SandboxedStdioSessionFactory,
        product_workspace: Path,
    ) -> None:
        if config.transport is not McpTransportKind.STDIO:
            raise ValueError("SandboxedStdioTransport requires stdio configuration")
        root = Path(product_workspace)
        if not root.is_absolute() or not root.exists() or not root.is_dir():
            raise ValueError("product_workspace must be an existing absolute directory")
        self._config = config
        self._sandbox = sandbox
        self._factory = session_factory
        self._root = root.resolve(strict=True)
        self._session: SandboxedStdioSession | None = None
        self._request_handler: InboundRequestHandler | None = None
        self._notification_handler: InboundNotificationHandler | None = None
        self._reader: asyncio.Task[None] | None = None
        self._pending: dict[str | int, asyncio.Future[JsonRpcResponse]] = {}
        self._abandoned: set[str | int] = set()
        self._counter = 0
        self._request_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._active_context: RequestScope | None = None
        self._reader_failure: BaseException | None = None
        self._closed = False

    def set_protocol_version(self, version: str) -> None:
        """Stdio negotiation is carried by initialize; no HTTP header is needed."""

        del version

    async def start(
        self,
        request_handler: InboundRequestHandler,
        notification_handler: InboundNotificationHandler,
    ) -> None:
        if self._closed:
            raise McpTransportError("stdio transport is closed")
        if self._session is not None:
            return
        availability = await self._sandbox.availability()
        if not availability.available:
            raise McpTransportError(f"sandbox unavailable: {availability.detail}")
        if self._config.require_production_sandbox and not self._sandbox.production_safe:
            raise McpTransportError("production MCP stdio requires a production-safe sandbox")
        self._request_handler = request_handler
        self._notification_handler = notification_handler
        self._session = await self._factory.open(
            self._config,
            sandbox=self._sandbox,
            product_workspace=self._root,
        )
        self._reader = asyncio.create_task(
            self._read_loop(), name=f"mcp-stdio:{self._config.server_id}"
        )

    async def request(
        self,
        method: str,
        params: dict[str, JsonValue] | None,
        *,
        context: RequestScope | None,
        timeout_seconds: float,
    ) -> JsonValue:
        self._require_started()
        async with self._request_lock:
            self._counter += 1
            request_id = f"suiteharness-{self._counter}"
            loop = asyncio.get_running_loop()
            future: asyncio.Future[JsonRpcResponse] = loop.create_future()
            self._pending[request_id] = future
            self._active_context = context
            try:
                await self._send(JsonRpcRequest(id=request_id, method=method, params=params))
                response = await asyncio.wait_for(future, timeout=timeout_seconds)
            except TimeoutError as exc:
                self._abandoned.add(request_id)
                if len(self._abandoned) > 1024:
                    self._abandoned.pop()
                with contextlib.suppress(BaseException):
                    await self.notify(
                        "notifications/cancelled",
                        {"requestId": request_id, "reason": "client timeout"},
                        context=context,
                    )
                raise McpTransportError(f"MCP request timed out: {method}") from exc
            finally:
                self._pending.pop(request_id, None)
                self._active_context = None
            return _response_result(response)

    async def notify(
        self,
        method: str,
        params: dict[str, JsonValue] | None = None,
        *,
        context: RequestScope | None = None,
    ) -> None:
        del context
        self._require_started()
        await self._send(JsonRpcNotification(method=method, params=params))

    async def _send(
        self, message: JsonRpcRequest | JsonRpcNotification | JsonRpcResponse
    ) -> None:
        session = self._require_started()
        payload = encode_message(message)
        if len(payload) > self._config.max_message_bytes:
            raise McpProtocolError("outbound MCP message exceeds max_message_bytes")
        async with self._write_lock:
            await session.send_line(payload)

    def _require_started(self) -> SandboxedStdioSession:
        if self._closed:
            raise McpTransportError("stdio transport is closed")
        if self._session is None:
            raise McpTransportError("stdio transport has not been started")
        if self._reader_failure is not None:
            raise McpTransportError("MCP stdio reader previously failed") from self._reader_failure
        return self._session

    async def _read_loop(self) -> None:
        assert self._session is not None
        failure: BaseException | None = None
        try:
            while True:
                line = await self._session.receive_line()
                if line is None:
                    raise McpTransportError("MCP stdio server closed its output")
                if not isinstance(line, bytes):
                    raise McpProtocolError("MCP stdio receive_line must return bytes")
                if len(line) > self._config.max_message_bytes:
                    raise McpProtocolError("MCP stdio message exceeds max_message_bytes")
                if not line.strip():
                    continue
                message = decode_message(line)
                if isinstance(message, JsonRpcResponse):
                    future = self._pending.get(message.id)
                    if future is None:
                        if message.id in self._abandoned:
                            self._abandoned.discard(message.id)
                            continue
                        raise McpProtocolError("received response for an unknown request id")
                    if not future.done():
                        future.set_result(message)
                elif isinstance(message, JsonRpcRequest):
                    await self._handle_request(message, self._active_context)
                else:
                    await self._handle_notification(message, self._active_context)
        except asyncio.CancelledError:
            return
        except BaseException as exc:
            failure = exc
        finally:
            if failure is not None:
                self._reader_failure = failure
                for future in tuple(self._pending.values()):
                    if not future.done():
                        future.set_exception(failure)

    async def _handle_request(
        self, request: JsonRpcRequest, context: RequestScope | None
    ) -> None:
        if self._request_handler is None:
            response = JsonRpcResponse(
                id=request.id,
                error=JsonRpcErrorObject(code=-32601, message="client request handler unavailable"),
            )
        else:
            try:
                response = await self._request_handler(request, context)
            except McpProtocolError as exc:
                response = JsonRpcResponse(
                    id=request.id,
                    error=JsonRpcErrorObject(code=exc.code, message=str(exc), data=exc.data),
                )
            except BaseException:
                response = JsonRpcResponse(
                    id=request.id,
                    error=JsonRpcErrorObject(code=-32603, message="client callback failed"),
                )
        await self._send(response)

    async def _handle_notification(
        self, notification: JsonRpcNotification, context: RequestScope | None
    ) -> None:
        if self._notification_handler is not None:
            await self._notification_handler(notification, context)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        if self._session is not None:
            await self._session.close()
        error = McpTransportError("stdio transport closed")
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(error)
        self._pending.clear()


class StreamableHttpTransport:
    """MCP Streamable HTTP orchestrator over an injected guarded exchange."""

    def __init__(
        self,
        config: McpServerConfig,
        *,
        tenant_id: str,
        product_id: str,
        egress_policy: McpEgressPolicy,
        exchange: McpHttpExchange,
        token_provider: McpTokenProvider | None = None,
        resume: HttpResumeState | None = None,
    ) -> None:
        if config.transport is not McpTransportKind.STREAMABLE_HTTP:
            raise ValueError("StreamableHttpTransport requires Streamable HTTP config")
        if config.legacy_sse is not LegacySseCompatibility.DISABLED:
            raise ValueError("legacy SSE requires an explicit LegacySseAdapter")
        self._config = config
        self._tenant_id = tenant_id
        self._product_id = product_id
        self._policy = egress_policy
        self._exchange = exchange
        self._token_provider = token_provider
        self._endpoint: str | None = None
        self._session_id = None if resume is None else resume.session_id
        self._last_event_id = None if resume is None else resume.last_event_id
        self._request_handler: InboundRequestHandler | None = None
        self._notification_handler: InboundNotificationHandler | None = None
        self._counter = 0
        self._lock = asyncio.Lock()
        self._closed = False
        self._protocol_version: str | None = None

    @property
    def resume_state(self) -> HttpResumeState:
        return HttpResumeState(session_id=self._session_id, last_event_id=self._last_event_id)

    def set_protocol_version(self, version: str) -> None:
        if version not in self._config.protocol_versions:
            raise McpProtocolError("cannot set an unconfigured MCP protocol version")
        self._protocol_version = version

    async def start(
        self,
        request_handler: InboundRequestHandler,
        notification_handler: InboundNotificationHandler,
    ) -> None:
        if self._closed:
            raise McpTransportError("HTTP transport is closed")
        if self._endpoint is not None:
            return
        assert self._config.endpoint is not None
        approved = await self._policy.authorize(
            self._config.endpoint,
            tenant_id=self._tenant_id,
            product_id=self._product_id,
        )
        if approved != self._config.endpoint:
            raise McpTransportError("egress policy must not rewrite the MCP endpoint")
        parsed = urlparse(approved)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise McpTransportError("egress policy returned an unsafe MCP endpoint")
        self._endpoint = approved
        self._request_handler = request_handler
        self._notification_handler = notification_handler

    async def request(
        self,
        method: str,
        params: dict[str, JsonValue] | None,
        *,
        context: RequestScope | None,
        timeout_seconds: float,
    ) -> JsonValue:
        self._require_started()
        async with self._lock:
            self._counter += 1
            request_id = f"suiteharness-{self._counter}"
            message = JsonRpcRequest(id=request_id, method=method, params=params)
            response = await self._post(message, context=context, timeout_seconds=timeout_seconds)
            if response is None:
                response = await self._wait_event_response(
                    request_id, context=context, timeout_seconds=timeout_seconds
                )
            return _response_result(response)

    async def notify(
        self,
        method: str,
        params: dict[str, JsonValue] | None = None,
        *,
        context: RequestScope | None = None,
    ) -> None:
        self._require_started()
        response = await self._post(
            JsonRpcNotification(method=method, params=params),
            context=context,
            timeout_seconds=self._config.timeout_seconds,
        )
        if response is not None:
            raise McpProtocolError("notification unexpectedly produced a JSON-RPC response")

    async def _headers(self, content_type: str) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": content_type,
        }
        if self._session_id is not None:
            headers["Mcp-Session-Id"] = self._session_id
        if self._protocol_version is not None:
            headers["MCP-Protocol-Version"] = self._protocol_version
        if self._config.credential_ref is not None:
            if self._token_provider is None:
                raise McpTransportError("configured MCP credential has no token provider")
            endpoint = self._require_started()
            value = await self._token_provider.authorization_header(
                self._config.credential_ref, endpoint=endpoint
            )
            if "\r" in value or "\n" in value or not value.strip():
                raise McpTransportError("token provider returned an invalid authorization header")
            headers["Authorization"] = value
        return headers

    async def _post(
        self,
        message: JsonRpcRequest | JsonRpcNotification,
        *,
        context: RequestScope | None,
        timeout_seconds: float,
    ) -> JsonRpcResponse | None:
        endpoint = self._require_started()
        body = encode_message(message)
        if len(body) > self._config.max_message_bytes:
            raise McpProtocolError("outbound MCP message exceeds max_message_bytes")
        response = _validated_http_response(
            await self._exchange.send(
                HttpRequest(
                    method="POST",
                    url=endpoint,
                    headers=await self._headers("application/json"),
                    body=body,
                    timeout_seconds=timeout_seconds,
                )
            ),
            max_message_bytes=self._config.max_message_bytes,
        )
        if (
            response.status == 401
            and self._config.credential_ref is not None
            and self._token_provider is not None
        ):
            invalidate = getattr(self._token_provider, "invalidate", None)
            if callable(invalidate):
                outcome = invalidate(self._config.credential_ref, endpoint=endpoint)
                if inspect.isawaitable(outcome):
                    await outcome
                response = _validated_http_response(
                    await self._exchange.send(
                        HttpRequest(
                            method="POST",
                            url=endpoint,
                            headers=await self._headers("application/json"),
                            body=body,
                            timeout_seconds=timeout_seconds,
                        )
                    ),
                    max_message_bytes=self._config.max_message_bytes,
                )
        self._capture_session(response.headers)
        if response.status == 202:
            return None
        if not 200 <= response.status < 300:
            raise McpTransportError(f"MCP HTTP request failed with status {response.status}")
        content_type = _header(response.headers, "content-type").split(";", 1)[0].strip().lower()
        if content_type == "application/json":
            decoded = decode_message(response.body)
            return await self._consume(decoded, context=context)
        if content_type == "text/event-stream":
            expected = message.id if isinstance(message, JsonRpcRequest) else None
            return await self._consume_events(
                parse_sse(
                    response.body,
                    max_message_bytes=self._config.max_message_bytes,
                    max_events=self._config.max_stream_events,
                ),
                expected,
                context,
            )
        raise McpProtocolError(f"unsupported MCP HTTP content type: {content_type!r}")

    async def _consume_events(
        self,
        events: tuple[ServerSentEvent, ...],
        expected_id: str | int | None,
        context: RequestScope | None,
    ) -> JsonRpcResponse | None:
        found: JsonRpcResponse | None = None
        for event in events:
            if event.event_id is not None:
                self._last_event_id = event.event_id
            decoded = decode_message(event.data)
            response = await self._consume(decoded, context=context)
            if response is not None:
                if expected_id is None or response.id != expected_id:
                    raise McpProtocolError("SSE carried a response for an unexpected request")
                if found is not None:
                    raise McpProtocolError("SSE carried duplicate JSON-RPC responses")
                found = response
        return found

    async def _consume(
        self,
        message: JsonRpcRequest | JsonRpcNotification | JsonRpcResponse,
        *,
        context: RequestScope | None,
    ) -> JsonRpcResponse | None:
        if isinstance(message, JsonRpcResponse):
            return message
        if isinstance(message, JsonRpcNotification):
            if self._notification_handler is not None:
                await self._notification_handler(message, context)
            return None
        if self._request_handler is None:
            raise McpProtocolError("server sent a client request without a callback handler")
        callback_response = await self._request_handler(message, context)
        # Streamable HTTP client responses to server requests are separate POSTs.
        callback_body = encode_message(callback_response)
        if len(callback_body) > self._config.max_message_bytes:
            raise McpProtocolError("outbound MCP callback exceeds max_message_bytes")
        callback_response = _validated_http_response(
            await self._exchange.send(
                HttpRequest(
                    method="POST",
                    url=self._require_started(),
                    headers=await self._headers("application/json"),
                    body=callback_body,
                    timeout_seconds=self._config.timeout_seconds,
                )
            ),
            max_message_bytes=self._config.max_message_bytes,
        )
        self._capture_session(callback_response.headers)
        if not 200 <= callback_response.status < 300:
            raise McpTransportError(
                "MCP HTTP callback response failed with status "
                f"{callback_response.status}"
            )
        return None

    async def _wait_event_response(
        self,
        request_id: str | int,
        *,
        context: RequestScope | None,
        timeout_seconds: float,
    ) -> JsonRpcResponse:
        headers = await self._headers("application/json")
        headers.pop("Content-Type", None)
        if self._last_event_id is not None:
            headers["Last-Event-ID"] = self._last_event_id
        request = HttpRequest(
            method="GET",
            url=self._require_started(),
            headers=headers,
            body=None,
            timeout_seconds=timeout_seconds,
        )

        async def receive() -> JsonRpcResponse:
            event_count = 0
            async for event in self._exchange.open_events(request):
                event_count += 1
                if event_count > self._config.max_stream_events:
                    raise McpProtocolError("MCP HTTP stream exceeds max_stream_events")
                _validate_sse_event(
                    event,
                    max_message_bytes=self._config.max_message_bytes,
                )
                result = await self._consume_events((event,), request_id, context)
                if result is not None:
                    return result
            raise McpTransportError("MCP HTTP event stream ended before its response")

        try:
            async with asyncio.timeout(timeout_seconds):
                return await receive()
        except TimeoutError as exc:
            raise McpTransportError("MCP HTTP event stream timed out") from exc

    def _capture_session(self, headers: Mapping[str, str]) -> None:
        value = _header(headers, "mcp-session-id")
        if value:
            if len(value.encode("utf-8")) > 4_096 or "\r" in value or "\n" in value:
                raise McpProtocolError("invalid MCP session id header")
            self._session_id = value

    def _require_started(self) -> str:
        if self._closed:
            raise McpTransportError("HTTP transport is closed")
        if self._endpoint is None:
            raise McpTransportError("HTTP transport has not been started")
        return self._endpoint

    async def close(self) -> None:
        if self._closed:
            return
        endpoint = self._endpoint
        session_id = self._session_id
        if endpoint is not None and session_id is not None:
            with contextlib.suppress(BaseException):
                headers = await self._headers("application/json")
                headers.pop("Content-Type", None)
                _validated_http_response(
                    await self._exchange.send(
                        HttpRequest(
                            method="DELETE",
                            url=endpoint,
                            headers=headers,
                            body=None,
                            timeout_seconds=min(self._config.timeout_seconds, 10.0),
                        )
                    ),
                    max_message_bytes=self._config.max_message_bytes,
                )
        self._closed = True

    async def close_for_resume(self) -> None:
        """Suspend locally while preserving the remote resumable session."""

        self._closed = True
        self._request_handler = None
        self._notification_handler = None


def _header(headers: Mapping[str, str], name: str) -> str:
    lowered = name.lower()
    return next((value for key, value in headers.items() if key.lower() == lowered), "")


def _response_result(response: JsonRpcResponse) -> JsonValue:
    if response.error is not None:
        raise McpProtocolError(
            response.error.message,
            code=response.error.code,
            data=response.error.data,
        )
    return cast(JsonValue, response.result)


class SecureMcpTransportFactory:
    """Select guarded transports; unsupported legacy SSE fails explicitly."""

    def __init__(
        self,
        *,
        sandbox: SandboxBackend | None = None,
        stdio_sessions: SandboxedStdioSessionFactory | None = None,
        egress_policy: McpEgressPolicy | None = None,
        http_exchange: McpHttpExchange | None = None,
        token_provider: McpTokenProvider | None = None,
        legacy_sse_adapter: LegacySseAdapter | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._stdio_sessions = stdio_sessions
        self._egress_policy = egress_policy
        self._http_exchange = http_exchange
        self._token_provider = token_provider
        self._legacy = legacy_sse_adapter

    def create(
        self,
        config: McpServerConfig,
        *,
        tenant_id: str,
        product_id: str,
        product_workspace: Path,
        resume: HttpResumeState | None = None,
    ) -> McpTransport:
        if config.transport is McpTransportKind.STDIO:
            if self._sandbox is None or self._stdio_sessions is None:
                raise McpTransportError(
                    "stdio MCP requires SandboxBackend and SandboxedStdioSessionFactory"
                )
            return SandboxedStdioTransport(
                config,
                sandbox=self._sandbox,
                session_factory=self._stdio_sessions,
                product_workspace=product_workspace,
            )
        if config.legacy_sse is LegacySseCompatibility.ADAPTER_REQUIRED:
            if self._legacy is None:
                raise McpTransportError("legacy SSE was declared but no adapter was installed")
            return self._legacy.create_transport(
                config,
                tenant_id=tenant_id,
                product_id=product_id,
                resume=resume,
            )
        if self._egress_policy is None or self._http_exchange is None:
            raise McpTransportError(
                "Streamable HTTP requires McpEgressPolicy and McpHttpExchange"
            )
        return StreamableHttpTransport(
            config,
            tenant_id=tenant_id,
            product_id=product_id,
            egress_policy=self._egress_policy,
            exchange=self._http_exchange,
            token_provider=self._token_provider,
            resume=resume,
        )


__all__ = [
    "SandboxedStdioTransport",
    "SecureMcpTransportFactory",
    "StreamableHttpTransport",
    "decode_message",
    "encode_message",
    "parse_sse",
]
