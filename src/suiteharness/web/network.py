"""SSRF-resistant target validation and DNS-pinned HTTP transport."""

from __future__ import annotations

import asyncio
import http.client
import ipaddress
import socket
import ssl
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol
from urllib.parse import SplitResult, urlsplit, urlunsplit

from .models import (
    HttpRequest,
    HttpResponse,
    ValidatedTarget,
    WebPolicyDenied,
    WebResponseTooLarge,
)


class DnsResolver(Protocol):
    async def resolve(self, hostname: str, port: int) -> frozenset[str]: ...


class HttpTransport(Protocol):
    """Direct/proxy/browser contract with target-address attestation."""

    async def send(self, request: HttpRequest) -> HttpResponse: ...


class SystemDnsResolver:
    async def resolve(self, hostname: str, port: int) -> frozenset[str]:
        def lookup() -> frozenset[str]:
            records = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
            return frozenset(record[4][0] for record in records)

        try:
            return await asyncio.to_thread(lookup)
        except OSError as exc:
            raise WebPolicyDenied("outbound hostname could not be resolved") from exc


def _public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    # ``is_global`` also excludes ranges such as carrier-grade NAT/shared
    # address space that are neither ``private`` nor publicly reachable.  A
    # deny-by-enumeration check is easy to bypass when Python learns a new
    # special-purpose range.
    return address.is_global


class PublicNetworkPolicy:
    """Allow HTTP(S) only when every current DNS answer is globally routable."""

    def __init__(self, resolver: DnsResolver | None = None) -> None:
        self._resolver = resolver or SystemDnsResolver()

    async def validate(self, url: str) -> ValidatedTarget:
        if not isinstance(url, str) or len(url) > 8_192 or any(char in url for char in "\r\n\x00"):
            raise WebPolicyDenied("invalid outbound URL")
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"}:
            raise WebPolicyDenied("only http and https outbound URLs are allowed")
        if parsed.username is not None or parsed.password is not None:
            raise WebPolicyDenied("URL user information is forbidden")
        if not parsed.hostname:
            raise WebPolicyDenied("outbound URL requires a hostname")
        try:
            hostname = parsed.hostname.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise WebPolicyDenied("invalid international hostname") from exc
        if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".localhost"):
            raise WebPolicyDenied("local hostnames are forbidden")
        try:
            port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        except ValueError as exc:
            raise WebPolicyDenied("invalid outbound URL port") from exc
        addresses = await self._resolver.resolve(hostname, port)
        if not addresses or any(not _public_address(address) for address in addresses):
            raise WebPolicyDenied("outbound target resolves to a non-public address")
        normalized = urlunsplit(
            SplitResult(
                parsed.scheme.lower(),
                parsed.netloc,
                parsed.path or "/",
                parsed.query,
                "",
            )
        )
        return ValidatedTarget(normalized, hostname, port, addresses)

    @staticmethod
    def verify_connected_peer(target: ValidatedTarget, connected_ip: str) -> None:
        try:
            connected = ipaddress.ip_address(connected_ip)
            allowed = {ipaddress.ip_address(value) for value in target.addresses}
        except ValueError as exc:
            raise WebPolicyDenied("transport returned an invalid target address") from exc
        if connected not in allowed:
            raise WebPolicyDenied("outbound target address changed after DNS validation")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, hostname: str, ip: str, port: int, timeout: float) -> None:
        super().__init__(hostname, port, timeout=timeout)
        self._pinned_ip = ip

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout, self.source_address
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, ip: str, port: int, timeout: float) -> None:
        super().__init__(hostname, port, timeout=timeout, context=ssl.create_default_context())
        self._pinned_ip = ip

    def connect(self) -> None:
        raw = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout, self.source_address
        )
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


class DirectHttpTransport:
    """Direct transport with no environment proxy and no automatic redirect."""

    async def send(self, request: HttpRequest) -> HttpResponse:
        return await asyncio.to_thread(self._send_sync, request)

    @staticmethod
    def _send_sync(request: HttpRequest) -> HttpResponse:
        parsed = urlsplit(request.url)
        hostname = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        ip = sorted(request.allowed_target_ips)[0]
        connection_type = (
            _PinnedHTTPSConnection if parsed.scheme == "https" else _PinnedHTTPConnection
        )
        connection = connection_type(hostname, ip, port, request.timeout_seconds)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        headers = dict(request.headers)
        headers.setdefault("Host", parsed.netloc)
        # Callers that implement bounded decompression opt in explicitly.
        # Structured API clients remain identity-encoded by default.
        headers.setdefault("Accept-Encoding", "identity")
        try:
            connection.request(request.method, path, body=request.body, headers=headers)
            response = connection.getresponse()
            body = response.read(request.max_response_bytes + 1)
            if len(body) > request.max_response_bytes:
                raise WebResponseTooLarge("transport response exceeded its byte limit")
            response_headers = {key: value for key, value in response.getheaders()}
            return HttpResponse(response.status, response_headers, body, ip)
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            raise WebPolicyDenied("outbound HTTP request failed") from exc
        finally:
            connection.close()


@dataclass(frozen=True, slots=True)
class ManagedProxyConfig:
    """One explicit administrator-managed HTTP CONNECT proxy.

    Environment proxy variables are intentionally unsupported. Authentication
    headers come from server configuration and stay out of object repr.
    """

    endpoint: str
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        parsed = urlsplit(self.endpoint)
        if parsed.scheme != "http" or not parsed.hostname:
            raise ValueError("managed proxy endpoint must be an absolute http URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("managed proxy credentials must use secret headers, not URL userinfo")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("managed proxy endpoint cannot contain a path, query, or fragment")
        clean: dict[str, str] = {}
        for key, value in self.headers.items():
            if not key or any(char in key for char in "\r\n:"):
                raise ValueError("invalid managed proxy header")
            if "\r" in value or "\n" in value:
                raise ValueError("invalid managed proxy header")
            clean[key] = value
        object.__setattr__(self, "headers", MappingProxyType(clean))


class ManagedProxyHttpTransport:
    """Pinned-target HTTP/CONNECT transport through one explicit proxy."""

    def __init__(self, config: ManagedProxyConfig) -> None:
        self._config = config

    async def send(self, request: HttpRequest) -> HttpResponse:
        return await asyncio.to_thread(self._send_sync, request)

    def _send_sync(self, request: HttpRequest) -> HttpResponse:
        proxy = urlsplit(self._config.endpoint)
        target = urlsplit(request.url)
        target_host = target.hostname or ""
        target_port = target.port or (443 if target.scheme == "https" else 80)
        pinned_ip = sorted(request.allowed_target_ips)[0]
        connection = http.client.HTTPConnection(
            proxy.hostname or "",
            proxy.port or 80,
            timeout=request.timeout_seconds,
        )
        path = target.path or "/"
        if target.query:
            path = f"{path}?{target.query}"
        target_headers = dict(request.headers)
        target_headers.setdefault("Host", target.netloc)
        target_headers.setdefault("Accept-Encoding", "identity")
        try:
            if target.scheme == "https":
                connection.set_tunnel(pinned_ip, target_port, headers=dict(self._config.headers))
                connection.connect()
                assert connection.sock is not None
                connection.sock = ssl.create_default_context().wrap_socket(
                    connection.sock,
                    server_hostname=target_host,
                )
                request_target = path
                outgoing_headers = target_headers
            else:
                ip_netloc = f"[{pinned_ip}]" if ":" in pinned_ip else pinned_ip
                if target_port != 80:
                    ip_netloc = f"{ip_netloc}:{target_port}"
                request_target = urlunsplit(("http", ip_netloc, target.path or "/", target.query, ""))
                outgoing_headers = {**self._config.headers, **target_headers}
            connection.request(
                request.method,
                request_target,
                body=request.body,
                headers=outgoing_headers,
            )
            response = connection.getresponse()
            body = response.read(request.max_response_bytes + 1)
            if len(body) > request.max_response_bytes:
                raise WebResponseTooLarge("proxy response exceeded its byte limit")
            return HttpResponse(
                response.status,
                {key: value for key, value in response.getheaders()},
                body,
                pinned_ip,
            )
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            raise WebPolicyDenied("managed proxy request failed") from exc
        finally:
            connection.close()


class BrowserWorkerClient(Protocol):
    """Sandboxed browser RPC that must attest its actual upstream target IP."""

    async def fetch(self, request: HttpRequest) -> HttpResponse: ...


class BrowserWorkerHttpTransport:
    """Optional JavaScript-rendering route; the browser remains out of process."""

    def __init__(self, client: BrowserWorkerClient) -> None:
        self._client = client

    async def send(self, request: HttpRequest) -> HttpResponse:
        if request.method != "GET":
            raise WebPolicyDenied("browser workers support GET fetches only")
        return await self._client.fetch(request)


@dataclass(frozen=True, slots=True)
class RoutedTransport:
    """Explicit administrator registration for proxy/browser egress."""

    name: str
    transport: HttpTransport


__all__ = [
    "BrowserWorkerClient",
    "BrowserWorkerHttpTransport",
    "DirectHttpTransport",
    "DnsResolver",
    "HttpTransport",
    "ManagedProxyConfig",
    "ManagedProxyHttpTransport",
    "PublicNetworkPolicy",
    "RoutedTransport",
    "SystemDnsResolver",
]
