"""Guarded default MCP host adapters. Connections carry explicit product identity."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import Protocol
from urllib.parse import urlparse, urlunparse

import httpx

from .models import McpProtocolError, McpServerConfig, McpTransportError
from .protocols import (
    HttpRequest,
    HttpResponse,
    LegacySseAdapter,
    McpEgressPolicy,
    McpTokenProvider,
    SandboxedStdioSessionFactory,
)
from .streaming import iter_sse, read_bounded


class DnsResolver(Protocol):
    async def resolve(self, hostname: str, port: int) -> frozenset[str]: ...


class SystemDnsResolver:
    async def resolve(self, hostname: str, port: int) -> frozenset[str]:
        infos = await asyncio.get_running_loop().getaddrinfo(
            hostname, port, type=socket.SOCK_STREAM
        )
        return frozenset(item[4][0] for item in infos)


@dataclass(frozen=True, slots=True)
class VerifiedEndpoint:
    url: str
    addresses: tuple[str, ...]


@dataclass(slots=True)
class ConfiguredMcpEgressPolicy(McpEgressPolicy):
    tenant_id: str
    servers_by_product: Mapping[str, tuple[McpServerConfig, ...]]
    resolver: DnsResolver | None = None

    def __post_init__(self) -> None:
        self.resolver = self.resolver or SystemDnsResolver()

    def server(self, endpoint, *, tenant_id, product_id, server_id=None):
        if tenant_id != self.tenant_id:
            raise PermissionError("MCP egress tenant is not configured")
        matches = [
            server
            for server in self.servers_by_product.get(product_id, ())
            if server.enabled
            and server.endpoint == endpoint
            and (server_id is None or server.server_id == server_id)
        ]
        if len(matches) != 1:
            raise PermissionError(
                "MCP endpoint must identify exactly one configured product server"
            )
        return matches[0]

    async def resolve(self, endpoint, *, tenant_id, product_id, server_id=None):
        server = self.server(
            endpoint, tenant_id=tenant_id, product_id=product_id, server_id=server_id
        )
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise PermissionError("MCP endpoint must be HTTPS without userinfo")
        addresses = tuple(sorted(await self.resolver.resolve(parsed.hostname, parsed.port or 443)))
        if not addresses:
            raise PermissionError("MCP endpoint DNS returned no addresses")
        networks = tuple(
            ipaddress.ip_network(item, strict=False) for item in server.allowed_private_networks
        )
        for raw in addresses:
            address = ipaddress.ip_address(raw)
            if not address.is_global and not any(address in network for network in networks):
                raise PermissionError("MCP endpoint resolved to a private or unsafe address")
        return VerifiedEndpoint(endpoint, addresses)

    async def authorize(self, endpoint, *, tenant_id, product_id):
        return (await self.resolve(endpoint, tenant_id=tenant_id, product_id=product_id)).url

    async def authorize_server(self, config, *, tenant_id, product_id):
        known = self.server(
            config.endpoint,
            tenant_id=tenant_id,
            product_id=product_id,
            server_id=config.server_id,
        )
        if known != config:
            raise PermissionError("MCP server differs from administrator configuration")
        return (
            await self.resolve(
                config.endpoint,
                tenant_id=tenant_id,
                product_id=product_id,
                server_id=config.server_id,
            )
        ).url


@dataclass(frozen=True, slots=True)
class StreamingResponse:
    status: int
    headers: Mapping[str, str]
    chunks: AsyncIterator[bytes]


class GuardedMcpHttpExchange:
    """Pin one checked DNS answer; never guess a product from a shared URL."""

    def __init__(
        self,
        policy: ConfiguredMcpEgressPolicy,
        *,
        client_factory: Callable[[], object] | None = None,
    ):
        self._policy = policy
        self._client_factory = client_factory or (lambda: httpx.AsyncClient(trust_env=False))
        self._binding = None
        self._legacy_endpoint = None

    def bind(self, *, tenant_id, product_id, config):
        known = self._policy.server(
            config.endpoint, tenant_id=tenant_id, product_id=product_id, server_id=config.server_id
        )
        if known != config:
            raise PermissionError("MCP binding differs from administrator configuration")
        bound = GuardedMcpHttpExchange(self._policy, client_factory=self._client_factory)
        bound._binding = (tenant_id, product_id, config)
        return bound

    def allow_legacy_endpoint(self, endpoint):
        if self._binding is None:
            raise PermissionError("MCP HTTP exchange has no product binding")
        source = urlparse(self._binding[2].endpoint)
        target = urlparse(endpoint)
        if (
            (target.scheme, target.hostname, target.port or 443)
            != (source.scheme, source.hostname, source.port or 443)
            or target.username
            or target.password
            or target.fragment
        ):
            raise PermissionError(
                "legacy SSE POST endpoint must retain the configured HTTPS origin"
            )
        self._legacy_endpoint = endpoint

    async def _approved_url(self, url):
        if self._binding is None:
            raise PermissionError(
                "MCP HTTP exchange requires an explicit tenant/product/server binding"
            )
        tenant_id, product_id, config = self._binding
        if url != config.endpoint and url != self._legacy_endpoint:
            raise PermissionError("MCP HTTP URL is outside this connection")
        verified = await self._policy.resolve(
            config.endpoint, tenant_id=tenant_id, product_id=product_id, server_id=config.server_id
        )
        parsed = urlparse(url)
        address = verified.addresses[0]
        authority = f"[{address}]" if ":" in address else address
        if parsed.port not in (None, 443):
            authority += f":{parsed.port}"
        return urlunparse((parsed.scheme, authority, parsed.path, parsed.params, parsed.query, ""))

    @asynccontextmanager
    async def open_response(self, request):
        # The total deadline includes DNS, TLS, headers and slow trickled bodies.
        async with asyncio.timeout(request.timeout_seconds):
            approved = await self._approved_url(request.url)
            parsed = urlparse(request.url)
            headers = dict(request.headers)
            headers["Host"] = parsed.netloc
            async with self._client_factory() as client:
                async with client.stream(
                    request.method,
                    approved,
                    headers=headers,
                    content=request.body,
                    timeout=request.timeout_seconds,
                    follow_redirects=False,
                    extensions={"sni_hostname": parsed.hostname},
                ) as response:
                    if sum(len(k) + len(v) for k, v in response.headers.multi_items()) > 65536:
                        raise McpProtocolError("MCP HTTP headers exceed limit")
                    yield StreamingResponse(
                        response.status_code, dict(response.headers), response.aiter_bytes()
                    )

    async def send(self, request: HttpRequest) -> HttpResponse:
        async with self.open_response(request) as response:
            body = await read_bounded(response.chunks, self._binding[2].max_message_bytes)
            return HttpResponse(response.status, response.headers, body)

    async def open_events(self, request: HttpRequest):
        async with self.open_response(request) as response:
            if (
                response.status != 200
                or response.headers.get("content-type", "").split(";")[0].lower()
                != "text/event-stream"
            ):
                raise McpTransportError("MCP event stream requires HTTP 200 text/event-stream")
            config = self._binding[2]
            async for event in iter_sse(
                response.chunks,
                max_message_bytes=config.max_message_bytes,
                max_events=config.max_stream_events,
            ):
                yield event


class DefaultStdioSessionFactory(SandboxedStdioSessionFactory):
    def __init__(self, limits):
        self._limits = limits

    async def open(self, config, *, sandbox, product_workspace):
        import uuid

        from suiteharness.sandbox import SandboxMount, SandboxRequest

        return await sandbox.open_session(
            SandboxRequest(
                request_id=f"mcp-{config.server_id}-{uuid.uuid4().hex}",
                argv=(config.command, *config.arguments),
                mounts=(SandboxMount(product_workspace, "/workspace", read_only=True),),
                limits=replace(self._limits, timeout_seconds=config.session_lifetime_seconds),
            )
        )


class DefaultLegacySseAdapter(LegacySseAdapter):
    def __init__(self, policy, exchange, token_provider=None):
        self._policy = policy
        self._exchange = exchange
        self._tokens = token_provider

    def create_transport(self, config, *, tenant_id, product_id, resume):
        from .legacy import LegacySseTransport

        if resume is not None:
            raise McpTransportError("legacy SSE does not support durable resume")
        bound = self._exchange.bind(tenant_id=tenant_id, product_id=product_id, config=config)
        return LegacySseTransport(config, bound, self._tokens)


class ConfiguredMcpTokenProvider(McpTokenProvider):
    def __init__(self, loaded):
        self._headers = {}
        for servers in loaded.config.mcp.servers_by_product.values():
            for server in servers:
                if not server.enabled or server.credential_ref is None:
                    continue
                credentials = loaded.secrets.services.get(server.credential_ref)
                if credentials is None:
                    raise ValueError("configured MCP credential reference is missing")
                authorization = next(
                    (
                        value
                        for name, value in credentials.headers.items()
                        if name.lower() == "authorization"
                    ),
                    None,
                )
                token = credentials.token or credentials.api_key
                if authorization is None and token is None:
                    raise ValueError(
                        "default MCP authentication requires a service token, API key, or Authorization header; OAuth requires an explicit token provider"
                    )
                self._headers[(server.credential_ref, server.endpoint)] = (authorization, token)

    async def authorization_header(self, credential_ref, *, endpoint):
        try:
            authorization, token = self._headers[(credential_ref, endpoint)]
        except KeyError as exc:
            raise PermissionError("MCP credential is not authorized for this endpoint") from exc
        value = (
            authorization.get_secret_value()
            if authorization is not None
            else "Bearer " + token.get_secret_value()
        )
        if not value.strip() or "\r" in value or "\n" in value:
            raise PermissionError("invalid configured MCP authorization header")
        return value


def build_default_mcp_adapters(loaded):
    """Build guarded adapters; stdio receives FoundationRuntime's actual backend at open."""
    from suiteharness.sandbox import SandboxLimits
    from suiteharness.server.extensions import McpHostAdapters

    policy = ConfiguredMcpEgressPolicy(
        loaded.config.deployment.tenant_id, loaded.config.mcp.servers_by_product
    )
    exchange = GuardedMcpHttpExchange(policy)
    tokens = ConfiguredMcpTokenProvider(loaded)
    return McpHostAdapters(
        stdio_sessions=DefaultStdioSessionFactory(
            SandboxLimits(**loaded.config.sandbox.limits.model_dump())
        ),
        egress_policy=policy,
        http_exchange=exchange,
        token_provider=tokens,
        legacy_sse_adapter=DefaultLegacySseAdapter(policy, exchange, tokens),
    )


__all__ = [
    "ConfiguredMcpEgressPolicy",
    "GuardedMcpHttpExchange",
    "SystemDnsResolver",
    "DefaultStdioSessionFactory",
    "DefaultLegacySseAdapter",
    "build_default_mcp_adapters",
]
