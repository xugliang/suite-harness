"""Bounded, provider-neutral models for outbound web access."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any


class WebAccessError(RuntimeError):
    """Base error for a rejected or failed web operation."""


class WebPolicyDenied(WebAccessError):
    """The outbound target violates the server-owned network policy."""


class WebResponseTooLarge(WebAccessError):
    """The remote response exceeded a configured hard limit."""


class UnsupportedWebContent(WebAccessError):
    """No safe extractor is configured for the returned content type."""


class FetchRoute(str, Enum):
    """Administrator-selected egress implementation."""

    DIRECT = "direct"
    MANAGED_PROXY = "managed_proxy"
    BROWSER_WORKER = "browser_worker"


@dataclass(frozen=True, slots=True)
class HttpRequest:
    """One no-redirect request bound to already-validated target addresses.

    A transport MUST connect only to ``allowed_target_ips`` and MUST report the
    selected upstream address. This closes the validation-to-connect DNS
    rebinding window.
    """

    method: str
    url: str
    allowed_target_ips: frozenset[str]
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    body: bytes | None = field(default=None, repr=False)
    timeout_seconds: float = 15.0
    max_response_bytes: int = 2_097_152

    def __post_init__(self) -> None:
        method = self.method.upper()
        if method not in {"GET", "POST"}:
            raise ValueError("only GET and POST web requests are supported")
        if not self.allowed_target_ips:
            raise ValueError("HTTP requests require validated target addresses")
        if not 0 < self.timeout_seconds <= 120:
            raise ValueError("HTTP timeout must be in (0, 120] seconds")
        if not 1_024 <= self.max_response_bytes <= 67_108_864:
            raise ValueError("HTTP response limit must be in [1024, 67108864] bytes")
        clean_headers: dict[str, str] = {}
        for key, value in self.headers.items():
            if not key or any(char in key for char in "\r\n:"):
                raise ValueError("invalid HTTP header name")
            if "\r" in value or "\n" in value:
                raise ValueError("invalid HTTP header value")
            clean_headers[key] = value
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "headers", MappingProxyType(clean_headers))


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes = field(repr=False)
    connected_target_ip: str

    def __post_init__(self) -> None:
        if not 100 <= self.status <= 599:
            raise ValueError("invalid HTTP status")
        object.__setattr__(
            self,
            "headers",
            MappingProxyType({key.lower(): value for key, value in self.headers.items()}),
        )


@dataclass(frozen=True, slots=True)
class ValidatedTarget:
    url: str
    hostname: str
    port: int
    addresses: frozenset[str]


@dataclass(frozen=True, slots=True)
class FetchLimits:
    timeout_seconds: float = 20.0
    max_redirects: int = 5
    max_compressed_bytes: int = 4_194_304
    max_content_bytes: int = 8_388_608
    max_text_characters: int = 1_000_000

    def __post_init__(self) -> None:
        if not 0 < self.timeout_seconds <= 120:
            raise ValueError("fetch timeout must be in (0, 120] seconds")
        if not 0 <= self.max_redirects <= 10:
            raise ValueError("max_redirects must be in [0, 10]")
        if not 1_024 <= self.max_compressed_bytes <= 67_108_864:
            raise ValueError("invalid compressed response limit")
        if not self.max_compressed_bytes <= self.max_content_bytes <= 134_217_728:
            raise ValueError("invalid decompressed content limit")
        if not 1_024 <= self.max_text_characters <= 10_000_000:
            raise ValueError("invalid extracted text limit")


@dataclass(frozen=True, slots=True)
class FetchDocument:
    requested_url: str
    final_url: str
    status: int
    content_type: str
    title: str | None
    text: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class SearchRequest:
    query: str
    count: int = 10
    freshness: str | None = None

    def __post_init__(self) -> None:
        query = self.query.strip()
        if not query or len(query) > 2_000:
            raise ValueError("search query must contain 1 to 2000 characters")
        if not 1 <= self.count <= 50:
            raise ValueError("search count must be in [1, 50]")
        object.__setattr__(self, "query", query)


@dataclass(frozen=True, slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    published_at: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class SearchResponse:
    provider_id: str
    query: str
    results: tuple[SearchResult, ...]


__all__ = [
    "FetchDocument",
    "FetchLimits",
    "FetchRoute",
    "HttpRequest",
    "HttpResponse",
    "SearchRequest",
    "SearchResponse",
    "SearchResult",
    "UnsupportedWebContent",
    "ValidatedTarget",
    "WebAccessError",
    "WebPolicyDenied",
    "WebResponseTooLarge",
]
