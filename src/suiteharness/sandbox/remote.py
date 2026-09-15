"""Bounded sandbox protocol over a product-owned Unix HTTP socket."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
from dataclasses import asdict
from pathlib import Path

from .models import (
    SandboxAvailability,
    SandboxConfigurationError,
    SandboxRequest,
    SandboxResult,
    SandboxUnavailable,
)
from .protocols import SandboxBackend, SandboxSession

MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_STDIN_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024


def fields(value, names):
    if type(value) is not dict or set(value) != set(names.split()):
        raise SandboxConfigurationError("invalid sandbox protocol fields")
    return value


def string(value, maximum=32768):
    if type(value) is not str or len(value) > maximum or "\x00" in value:
        raise SandboxConfigurationError("invalid sandbox protocol string")
    return value


def boolean(value):
    if type(value) is not bool:
        raise SandboxConfigurationError("invalid sandbox protocol boolean")
    return value


def unbytes(value, maximum):
    string(value, ((maximum + 2) // 3) * 4)
    try:
        result = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise SandboxConfigurationError("invalid sandbox binary encoding") from exc
    if len(result) > maximum:
        raise SandboxConfigurationError("sandbox binary value exceeds limit")
    return result


def bounded_json(value, maximum):
    data = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
    if len(data) > maximum:
        raise SandboxConfigurationError("sandbox message exceeds limit")
    return data


def parse_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate field")
            result[key] = value
        return result

    try:
        return json.loads(
            data,
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (ValueError, RecursionError) as exc:
        raise SandboxConfigurationError("invalid sandbox JSON") from exc


def encode_request(request):
    return dict(
        request_id=request.request_id,
        argv=list(request.argv),
        mounts=[
            dict(host_path=str(m.host_path), container_path=m.container_path, read_only=m.read_only)
            for m in request.mounts
        ],
        working_directory=request.working_directory,
        environment=dict(request.environment),
        stdin=None if request.stdin is None else base64.b64encode(request.stdin).decode("ascii"),
        limits=asdict(request.limits),
        network=dict(
            mode=request.network.mode.value, egress_profile=request.network.egress_profile
        ),
    )


def encode_result(result):
    return dict(
        request_id=result.request_id,
        exit_code=result.exit_code,
        stdout=base64.b64encode(result.stdout).decode("ascii"),
        stderr=base64.b64encode(result.stderr).decode("ascii"),
        timed_out=result.timed_out,
        output_truncated=result.output_truncated,
    )


def decode_result(value, request_id, output_bytes):
    fields(value, "request_id exit_code stdout stderr timed_out output_truncated")
    if value["request_id"] != request_id or (
        value["exit_code"] is not None and type(value["exit_code"]) is not int
    ):
        raise SandboxConfigurationError("invalid sandbox result identity or status")
    stdout = unbytes(value["stdout"], output_bytes)
    stderr = unbytes(value["stderr"], output_bytes)
    if len(stdout) + len(stderr) > output_bytes:
        raise SandboxConfigurationError("sandbox output exceeds limit")
    return SandboxResult(
        request_id,
        value["exit_code"],
        stdout,
        stderr,
        boolean(value["timed_out"]),
        boolean(value["output_truncated"]),
    )


class RemoteSandboxBackend(SandboxBackend):
    def __init__(self, socket_path: Path) -> None:
        self.socket_path = Path(socket_path)
        if not self.socket_path.is_absolute():
            raise SandboxConfigurationError("broker socket must be absolute")

    @property
    def backend_id(self) -> str:
        return "docker"

    @property
    def production_safe(self) -> bool:
        return True

    async def _call(self, path, body, timeout, maximum):
        if os.name != "posix":
            raise SandboxUnavailable("sandbox broker requires Unix sockets")
        import httpx

        try:
            transport = httpx.AsyncHTTPTransport(uds=str(self.socket_path))
            async with httpx.AsyncClient(
                transport=transport, trust_env=False, timeout=timeout
            ) as client:
                async with client.stream(
                    "POST" if body is not None else "GET",
                    "http://sandbox" + path,
                    content=body,
                    headers={"content-type": "application/json"},
                ) as response:
                    if response.status_code != 200:
                        raise SandboxUnavailable(
                            "sandbox broker rejected the request or is unavailable"
                        )
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > maximum:
                            raise SandboxUnavailable("sandbox broker response exceeds limit")
                    return parse_json(data)
        except (httpx.HTTPError, OSError, SandboxConfigurationError) as exc:
            raise SandboxUnavailable("sandbox broker unavailable or invalid response") from exc

    async def availability(self) -> SandboxAvailability:
        try:
            value = await self._call("/availability", None, 15, 8192)
            fields(value, "available detail")
            return SandboxAvailability(boolean(value["available"]), string(value["detail"], 4096))
        except (SandboxUnavailable, SandboxConfigurationError):
            return SandboxAvailability(False, "sandbox broker unavailable")

    async def run(self, request: SandboxRequest) -> SandboxResult:
        if request.limits.output_bytes > MAX_OUTPUT_BYTES:
            raise SandboxConfigurationError("sandbox output ceiling exceeds broker protocol")
        body = bounded_json(encode_request(request), MAX_REQUEST_BYTES)
        value = await self._call(
            "/run",
            body,
            request.limits.timeout_seconds + 45,
            ((request.limits.output_bytes + 2) // 3) * 4 + 4096,
        )
        try:
            return decode_result(value, request.request_id, request.limits.output_bytes)
        except SandboxConfigurationError as exc:
            raise SandboxUnavailable("sandbox broker returned an invalid result") from exc

    async def open_session(self, request: SandboxRequest) -> SandboxSession:
        if request.limits.output_bytes > MAX_OUTPUT_BYTES:
            raise SandboxConfigurationError("persistent output ceiling exceeds protocol")
        body = bounded_json(encode_request(request), MAX_REQUEST_BYTES)
        value = await self._call("/session/open", body, 30, 4096)
        fields(value, "request_id session_id")
        if value["request_id"] != request.request_id:
            raise SandboxConfigurationError("persistent session request id mismatch")
        token = string(value["session_id"], 128)
        if len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
            raise SandboxConfigurationError("invalid persistent session identity")
        return _RemoteSession(self, token, request.limits.output_bytes)


class _RemoteSession(SandboxSession):
    def __init__(self, backend, token, output_limit):
        self._backend, self._token, self._limit = backend, token, output_limit
        self._closed = asyncio.Event()
        self._close_task = None

    async def _request(self, action, payload=None):
        value = {"session_id": self._token}
        if action == "send":
            value["payload"] = base64.b64encode(payload).decode("ascii")
        return await self._backend._call(
            "/session/" + action,
            bounded_json(value, MAX_REQUEST_BYTES),
            35,
            ((self._limit + 2) // 3) * 4 + 4096,
        )

    async def send_line(self, payload):
        if self._closed.is_set() or self._close_task is not None:
            raise SandboxUnavailable("persistent session is closed")
        if (
            not isinstance(payload, bytes)
            or len(payload) > min(self._limit, MAX_STDIN_BYTES)
            or b"\n" in payload
            or b"\r" in payload
        ):
            raise SandboxConfigurationError("invalid persistent outbound line")
        result = await self._request("send", payload)
        fields(result, "sent")
        if not boolean(result["sent"]):
            raise SandboxUnavailable("persistent session did not accept message")

    async def receive_line(self):
        while not self._closed.is_set():
            result = await self._request("receive")
            fields(result, "payload pending closed")
            pending, closed = boolean(result["pending"]), boolean(result["closed"])
            if pending or closed:
                if result["payload"] is not None or (pending and closed):
                    raise SandboxConfigurationError("invalid persistent response state")
                if closed:
                    self._closed.set()
                    return None
                continue
            return unbytes(result["payload"], self._limit)
        return None

    async def _finish(self):
        result = await self._request("close")
        fields(result, "closed")
        if not boolean(result["closed"]):
            raise SandboxUnavailable("persistent session cleanup not confirmed")
        self._closed.set()

    async def close(self):
        if self._closed.is_set():
            return
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._finish())
        await asyncio.shield(self._close_task)

    async def wait_closed(self):
        await self._closed.wait()
