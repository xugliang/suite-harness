"""Small HTTP seam used by every REST model adapter.

Keeping transport outside adapters makes proxying, mTLS, observability and
tests deployment concerns.  Request representations intentionally redact
headers from ``repr`` because they may contain credentials.
"""

from __future__ import annotations

import codecs
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import JsonValue

from suiteharness.models.errors import ModelErrorCode, ModelProviderError


@dataclass(frozen=True, slots=True)
class HttpRequest:
    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    json_body: JsonValue | None = field(default=None, repr=False)
    timeout_seconds: float = 120.0


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes = field(repr=False)

    def json(self) -> JsonValue:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ModelProviderError(
                ModelErrorCode.RESPONSE_INVALID,
                "model provider returned invalid JSON",
            ) from exc


class HttpTransport(Protocol):
    async def send(self, request: HttpRequest) -> HttpResponse:
        """Send one buffered request."""

    def stream(self, request: HttpRequest) -> AsyncIterator[bytes]:
        """Yield raw response chunks after validating the HTTP status."""


class HttpxTransport:
    """Production async transport backed by ``httpx``.

    The dependency is imported lazily so model conversion and registry tests do
    not need a network stack.  ``trust_env`` is disabled: an administrator must
    configure an explicit transport/proxy instead of inheriting a process-wide
    proxy unexpectedly.
    """

    def __init__(
        self,
        *,
        proxy: str | None = None,
        verify: bool | str = True,
        max_connections: int = 100,
        max_response_bytes: int = 32 * 1024 * 1024,
        max_stream_bytes: int = 256 * 1024 * 1024,
    ) -> None:
        if max_response_bytes < 1 or max_response_bytes > 1024 * 1024 * 1024:
            raise ValueError("max_response_bytes must be between 1 and 1073741824")
        if max_stream_bytes < 1 or max_stream_bytes > 4 * 1024 * 1024 * 1024:
            raise ValueError("max_stream_bytes must be between 1 and 4294967296")
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - deployment diagnostic
            raise ModelProviderError(
                ModelErrorCode.CONFIGURATION,
                "HttpxTransport requires the 'httpx' dependency",
            ) from exc
        self._httpx = httpx
        self._max_response_bytes = max_response_bytes
        self._max_stream_bytes = max_stream_bytes
        self._client = httpx.AsyncClient(
            proxy=proxy,
            verify=verify,
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=max_connections),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def send(self, request: HttpRequest) -> HttpResponse:
        try:
            async with self._client.stream(
                request.method,
                request.url,
                headers=dict(request.headers),
                json=request.json_body,
                timeout=request.timeout_seconds,
            ) as response:
                body = await self._read_bounded(response, self._max_response_bytes)
                status_code = response.status_code
                headers = dict(response.headers)
        except ModelProviderError:
            raise
        except self._httpx.TimeoutException as exc:
            raise ModelProviderError(
                ModelErrorCode.TIMEOUT,
                "model provider request timed out",
                retryable=True,
            ) from exc
        except self._httpx.HTTPError as exc:
            raise ModelProviderError(
                ModelErrorCode.UNAVAILABLE,
                f"model provider transport failed: {type(exc).__name__}",
                retryable=True,
            ) from exc
        return HttpResponse(
            status_code=status_code,
            headers=headers,
            body=body,
        )

    async def _read_bounded(self, response: Any, limit: int) -> bytes:
        declared = response.headers.get("content-length")
        if declared is not None:
            try:
                declared_size = int(declared)
                if declared_size < 0:
                    raise ValueError("negative Content-Length")
                if declared_size > limit:
                    raise ModelProviderError(
                        ModelErrorCode.RESPONSE_INVALID,
                        "model provider response exceeded the configured byte limit",
                    )
            except ValueError as exc:
                raise ModelProviderError(
                    ModelErrorCode.RESPONSE_INVALID,
                    "model provider returned an invalid Content-Length",
                ) from exc
        content = bytearray()
        async for chunk in response.aiter_bytes():
            if len(content) + len(chunk) > limit:
                raise ModelProviderError(
                    ModelErrorCode.RESPONSE_INVALID,
                    "model provider response exceeded the configured byte limit",
                )
            content.extend(chunk)
        return bytes(content)

    async def _stream(self, request: HttpRequest) -> AsyncIterator[bytes]:
        try:
            async with self._client.stream(
                request.method,
                request.url,
                headers=dict(request.headers),
                json=request.json_body,
                timeout=request.timeout_seconds,
            ) as response:
                if response.status_code >= 400:
                    body = await self._read_bounded(response, self._max_response_bytes)
                    raise http_status_error(response.status_code, body=body)
                total = 0
                async for chunk in response.aiter_bytes():
                    if chunk:
                        total += len(chunk)
                        if total > self._max_stream_bytes:
                            raise ModelProviderError(
                                ModelErrorCode.RESPONSE_INVALID,
                                "model provider stream exceeded the configured byte limit",
                            )
                        yield chunk
        except ModelProviderError:
            raise
        except self._httpx.TimeoutException as exc:
            raise ModelProviderError(
                ModelErrorCode.TIMEOUT,
                "model provider stream timed out",
                retryable=True,
            ) from exc
        except self._httpx.HTTPError as exc:
            raise ModelProviderError(
                ModelErrorCode.UNAVAILABLE,
                f"model provider stream failed: {type(exc).__name__}",
                retryable=True,
            ) from exc

    def stream(self, request: HttpRequest) -> AsyncIterator[bytes]:
        return self._stream(request)


def http_status_error(
    status_code: int,
    *,
    body: bytes = b"",
    provider_id: str | None = None,
) -> ModelProviderError:
    """Map a provider status without returning its possibly sensitive body."""

    if status_code in {401}:
        code = ModelErrorCode.AUTHENTICATION
        retryable = False
    elif status_code in {403}:
        code = ModelErrorCode.PERMISSION_DENIED
        retryable = False
    elif status_code in {408, 504}:
        code = ModelErrorCode.TIMEOUT
        retryable = True
    elif status_code == 429:
        code = ModelErrorCode.RATE_LIMIT
        retryable = True
    elif 400 <= status_code < 500:
        code = ModelErrorCode.INVALID_REQUEST
        retryable = False
    else:
        code = ModelErrorCode.UNAVAILABLE
        retryable = True
    suffix = ""
    if body:
        # Length is useful operationally; response content is deliberately not exposed.
        suffix = f" ({len(body)} response bytes redacted)"
    return ModelProviderError(
        code,
        f"model provider HTTP {status_code}{suffix}",
        provider_id=provider_id,
        status_code=status_code,
        retryable=retryable,
    )


async def iter_sse_json(
    chunks: AsyncIterator[bytes],
    *,
    max_bytes: int = 256 * 1024 * 1024,
    max_buffer_characters: int = 4 * 1024 * 1024,
) -> AsyncIterator[dict[str, Any]]:
    """Decode arbitrarily chunked Server-Sent Events containing JSON data."""

    if max_bytes < 1 or max_buffer_characters < 1:
        raise ValueError("SSE limits must be positive")
    buffer = ""
    event_lines: list[str] = []
    received = 0
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")

    def decode_event(lines: list[str]) -> dict[str, Any] | None:
        data = "\n".join(line[5:].lstrip() for line in lines if line.startswith("data:"))
        if not data or data == "[DONE]":
            return None
        try:
            value = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ModelProviderError(
                ModelErrorCode.RESPONSE_INVALID,
                "model provider returned an invalid SSE JSON event",
            ) from exc
        if not isinstance(value, dict):
            raise ModelProviderError(
                ModelErrorCode.RESPONSE_INVALID,
                "model provider SSE event must be a JSON object",
            )
        return value

    async for chunk in chunks:
        received += len(chunk)
        if received > max_bytes:
            raise ModelProviderError(
                ModelErrorCode.RESPONSE_INVALID,
                "model provider stream exceeded the configured byte limit",
            )
        try:
            buffer += decoder.decode(chunk, final=False)
        except UnicodeDecodeError as exc:
            raise ModelProviderError(
                ModelErrorCode.RESPONSE_INVALID,
                "model provider stream is not UTF-8",
            ) from exc
        if len(buffer) > max_buffer_characters:
            raise ModelProviderError(
                ModelErrorCode.RESPONSE_INVALID,
                "model provider SSE event exceeded the configured buffer limit",
            )
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.rstrip("\r")
            if line:
                if not line.startswith(":"):
                    event_lines.append(line)
                continue
            value = decode_event(event_lines)
            event_lines = []
            if value is not None:
                yield value
    try:
        buffer += decoder.decode(b"", final=True)
    except UnicodeDecodeError as exc:
        raise ModelProviderError(
            ModelErrorCode.RESPONSE_INVALID,
            "model provider stream is not UTF-8",
        ) from exc
    if len(buffer) > max_buffer_characters:
        raise ModelProviderError(
            ModelErrorCode.RESPONSE_INVALID,
            "model provider SSE event exceeded the configured buffer limit",
        )
    while "\n" in buffer:
        line, buffer = buffer.split("\n", 1)
        line = line.rstrip("\r")
        if line:
            if not line.startswith(":"):
                event_lines.append(line)
            continue
        value = decode_event(event_lines)
        event_lines = []
        if value is not None:
            yield value
    if buffer:
        event_lines.append(buffer.rstrip("\r"))
    value = decode_event(event_lines)
    if value is not None:
        yield value
