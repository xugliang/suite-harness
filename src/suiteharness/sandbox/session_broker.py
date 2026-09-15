"""Bounded product-owned persistent sessions behind the private broker socket."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field, replace

from .models import SandboxConfigurationError, SandboxUnavailable
from .remote import (
    MAX_REQUEST_BYTES,
    MAX_STDIN_BYTES,
    fields,
    parse_json,
    string,
    unbytes,
)

SESSION_IDLE_SECONDS = 60.0
RECEIVE_WAIT_SECONDS = 25.0


@dataclass
class _Record:
    session: object
    output_limit: int
    expires: float
    touched: float = field(default_factory=time.monotonic)
    receive_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    watcher: object = None
    closing: object = None


class SessionBroker:
    def __init__(self, policy, backend, other_busy):
        self.policy, self.backend, self.other_busy = policy, backend, other_busy
        self._records = {}
        self._opening = 0
        self._operations = set()
        self._closed = False

    @property
    def count(self):
        return len(self._records) + self._opening

    async def _body(self, request):
        data = bytearray()
        async with asyncio.timeout(10):
            async for chunk in request.stream():
                if len(data) + len(chunk) > MAX_REQUEST_BYTES:
                    raise SandboxConfigurationError("request exceeds limit")
                data.extend(chunk)
        return parse_json(data)

    async def _finish(self, token, record):
        try:
            async with asyncio.timeout(30):
                await record.session.close()
        finally:
            self._records.pop(token, None)

    async def _close_record(self, token):
        record = self._records.get(token)
        if record is None:
            return
        if record.closing is None:
            record.closing = asyncio.create_task(self._finish(token, record))
        await asyncio.shield(record.closing)
        if record.watcher is not None and record.watcher is not asyncio.current_task():
            record.watcher.cancel()
            await asyncio.gather(record.watcher, return_exceptions=True)

    async def _watch(self, token, record):
        try:
            while token in self._records:
                remaining = min(
                    record.expires - time.monotonic(),
                    record.touched + SESSION_IDLE_SECONDS - time.monotonic(),
                )
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(record.session.wait_closed(), remaining)
                    break
                except TimeoutError:
                    continue
        finally:
            await self._close_record(token)

    async def dispatch(self, request):
        from starlette.responses import JSONResponse, Response

        action = request.url.path.rsplit("/", 1)[-1]
        token = None
        try:
            if self._closed:
                return JSONResponse({"error": "broker closing"}, status_code=503)
            data = await self._body(request)
            if action == "open":
                if self.count + self.other_busy() >= 4:
                    return JSONResponse({"error": "sandbox busy"}, status_code=429)
                self._opening += 1
                try:
                    command = self.policy.decode(data, persistent=True)
                    internal = hashlib.sha256(
                        (self.policy.namespace + ":" + command.request_id).encode()
                    ).hexdigest()
                    session = await self.backend.open_session(replace(command, request_id=internal))
                    token = secrets.token_hex(32)
                    record = _Record(
                        session,
                        command.limits.output_bytes,
                        time.monotonic() + command.limits.timeout_seconds,
                    )
                    self._records[token] = record
                    record.watcher = asyncio.create_task(self._watch(token, record))
                    return JSONResponse({"request_id": command.request_id, "session_id": token})
                finally:
                    self._opening -= 1
            fields(data, "session_id payload" if action == "send" else "session_id")
            token = string(data["session_id"], 128)
            if action == "close":
                await self._close_record(token)
                return JSONResponse({"closed": True})
            record = self._records.get(token)
            if record is None or record.closing is not None:
                return JSONResponse({"error": "unknown session"}, status_code=404)
            record.touched = time.monotonic()
            if action == "send":
                payload = unbytes(data["payload"], min(record.output_limit, MAX_STDIN_BYTES))
                if b"\n" in payload or b"\r" in payload:
                    raise SandboxConfigurationError("persistent messages must be one line")
                if record.send_lock.locked():
                    return JSONResponse({"error": "session send busy"}, status_code=409)
                async with record.send_lock, asyncio.timeout(15):
                    await record.session.send_line(payload)
                return JSONResponse({"sent": True})
            if action != "receive":
                return JSONResponse({"error": "unknown operation"}, status_code=404)
            if record.receive_lock.locked():
                return JSONResponse({"error": "session receive busy"}, status_code=409)
            async with record.receive_lock:

                async def read():
                    try:
                        return await asyncio.wait_for(
                            record.session.receive_line(), RECEIVE_WAIT_SECONDS
                        )
                    except TimeoutError:
                        return ...

                async def disconnected():
                    while (await request.receive())["type"] != "http.disconnect":
                        pass

                operation = asyncio.create_task(read())
                disconnect = asyncio.create_task(disconnected())
                self._operations.add(operation)
                try:
                    done, _ = await asyncio.wait(
                        (operation, disconnect), return_when=asyncio.FIRST_COMPLETED
                    )
                    if operation not in done:
                        operation.cancel()
                        await asyncio.gather(operation, return_exceptions=True)
                        await self._close_record(token)
                        return Response(status_code=499)
                    payload = await operation
                finally:
                    disconnect.cancel()
                    operation.cancel()
                    await asyncio.gather(disconnect, operation, return_exceptions=True)
                    self._operations.discard(operation)
                record.touched = time.monotonic()
                if payload is ...:
                    return JSONResponse({"payload": None, "pending": True, "closed": False})
                if payload is None:
                    await self._close_record(token)
                    return JSONResponse({"payload": None, "pending": False, "closed": True})
                if not isinstance(payload, bytes) or len(payload) > record.output_limit:
                    raise SandboxUnavailable("persistent output exceeds limit")
                return JSONResponse(
                    {
                        "payload": base64.b64encode(payload).decode("ascii"),
                        "pending": False,
                        "closed": False,
                    }
                )
        except (SandboxConfigurationError, ValueError, TypeError, KeyError):
            return JSONResponse({"error": "invalid sandbox session request"}, status_code=400)
        except asyncio.CancelledError:
            if token is not None:
                await self._close_record(token)
            raise
        except (SandboxUnavailable, TimeoutError, OSError):
            if token is not None:
                await self._close_record(token)
            return JSONResponse({"error": "sandbox session unavailable"}, status_code=503)

    async def close(self):
        self._closed = True
        for task in tuple(self._operations):
            task.cancel()
        await asyncio.gather(*tuple(self._operations), return_exceptions=True)
        results = await asyncio.gather(
            *(self._close_record(token) for token in tuple(self._records)), return_exceptions=True
        )
        failures = [item for item in results if isinstance(item, BaseException)]
        if failures:
            raise BaseExceptionGroup("persistent sandbox shutdown failed", failures)
