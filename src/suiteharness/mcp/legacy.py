"""Pre-Streamable-HTTP SSE, restricted to an administrator's HTTPS origin."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from urllib.parse import urljoin

from .models import McpProtocolError, McpTransportError
from .protocols import HttpRequest
from .transports import _SessionRpcTransport


class _LegacySession:
    def __init__(self, config, exchange, tokens):
        self.config, self.exchange, self.tokens = config, exchange, tokens
        self.endpoint = None
        self.queue = asyncio.Queue(maxsize=16)
        self.ready = asyncio.get_running_loop().create_future()
        self.failure = None
        self.closed = False
        self.pump = asyncio.create_task(self._pump())

    async def headers(self):
        headers = {"Accept": "text/event-stream", "Content-Type": "application/json"}
        if self.config.credential_ref:
            if self.tokens is None:
                raise McpTransportError("MCP configured credential has no token provider")
            value = await self.tokens.authorization_header(
                self.config.credential_ref, endpoint=self.config.endpoint
            )
            if not value.strip() or "\r" in value or "\n" in value:
                raise McpTransportError("invalid MCP authorization header")
            headers["Authorization"] = value
        return headers

    async def _pump(self):
        try:
            request = HttpRequest(
                "GET",
                self.config.endpoint,
                await self.headers(),
                None,
                self.config.session_lifetime_seconds,
            )
            async with aclosing(self.exchange.open_events(request)) as events:
                async for event in events:
                    if event.event == "endpoint":
                        if self.endpoint is not None:
                            raise McpProtocolError("legacy SSE endpoint changed during a session")
                        endpoint = urljoin(self.config.endpoint, event.data)
                        self.exchange.allow_legacy_endpoint(endpoint)
                        self.endpoint = endpoint
                        self.ready.set_result(None)
                    elif event.event in (None, "message") and event.data:
                        if self.endpoint is None:
                            raise McpProtocolError(
                                "legacy SSE message preceded endpoint negotiation"
                            )
                        await self.queue.put(event.data.encode("utf-8"))
            raise McpTransportError("legacy SSE event stream ended")
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self.failure = exc
            if not self.ready.done():
                self.ready.set_exception(exc)
        finally:
            if not self.queue.full():
                self.queue.put_nowait(None)

    async def send_line(self, payload):
        if self.closed or self.failure is not None or self.endpoint is None:
            raise McpTransportError("legacy SSE session is unavailable")
        response = await self.exchange.send(
            HttpRequest(
                "POST", self.endpoint, await self.headers(), payload, self.config.timeout_seconds
            )
        )
        if not 200 <= response.status < 300:
            raise McpTransportError(f"legacy SSE POST failed with status {response.status}")

    async def receive_line(self):
        if self.failure is not None and self.queue.empty():
            raise self.failure
        item = await self.queue.get()
        if item is None and self.failure is not None:
            raise self.failure
        return item

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.pump.cancel()
        await asyncio.gather(self.pump, return_exceptions=True)


class LegacySseTransport(_SessionRpcTransport):
    def __init__(self, config, exchange, tokens=None):
        super().__init__(config)
        self.exchange, self.tokens = exchange, tokens

    async def start(self, request_handler, notification_handler):
        if self._closed:
            raise McpTransportError("legacy SSE transport is closed")
        if self._session is not None:
            return
        session = _LegacySession(self._config, self.exchange, self.tokens)
        try:
            await asyncio.wait_for(session.ready, self._config.timeout_seconds)
        except BaseException:
            await session.close()
            raise
        self._start_reader(session, request_handler, notification_handler)
