"""Injection ports for MCP transports, auth and privileged client callbacks."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import JsonValue

from suiteharness.runtime.scopes import RequestScope
from suiteharness.sandbox import SandboxBackend

from .models import (
    CreateMessageParams,
    CreateMessageResult,
    CreateTaskResult,
    ElicitationParams,
    ElicitationResult,
    HttpResumeState,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
    ListTasksResult,
    LoggingMessage,
    McpOperationContext,
    McpServerConfig,
    McpTask,
    OAuthAuthorizationServerMetadata,
    OAuthProtectedResourceMetadata,
    OAuthTokenSet,
)


@dataclass(frozen=True, slots=True)
class HttpRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | None
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True, slots=True)
class ServerSentEvent:
    data: str
    event: str | None = None
    event_id: str | None = None
    retry_ms: int | None = None


@runtime_checkable
class McpHttpExchange(Protocol):
    """Bounded HTTP implementation supplied by the platform network layer."""

    async def send(self, request: HttpRequest) -> HttpResponse: ...

    def open_events(self, request: HttpRequest) -> AsyncIterator[ServerSentEvent]: ...


@runtime_checkable
class McpEgressPolicy(Protocol):
    """Authorize and pin an MCP endpoint before any bytes leave the process."""

    async def authorize(
        self,
        endpoint: str,
        *,
        tenant_id: str,
        product_id: str,
    ) -> str:
        """Return the policy-approved endpoint, or raise to fail closed."""


@runtime_checkable
class McpTokenProvider(Protocol):
    """Resolve server-managed OAuth/service tokens without exposing secrets."""

    async def authorization_header(
        self,
        credential_ref: str,
        *,
        endpoint: str,
    ) -> str: ...


@runtime_checkable
class OAuthMetadataResolver(Protocol):
    """Discover protected-resource/authorization-server metadata via policy."""

    async def protected_resource(self, endpoint: str) -> OAuthProtectedResourceMetadata: ...

    async def authorization_server(
        self, issuer: str
    ) -> OAuthAuthorizationServerMetadata: ...


@runtime_checkable
class OAuthTokenIssuer(Protocol):
    """Exchange a configured server credential for a scoped OAuth token."""

    async def issue(
        self,
        credential_ref: str,
        *,
        resource: str,
    ) -> OAuthTokenSet: ...


@runtime_checkable
class SandboxedStdioSession(Protocol):
    """A persistent, already-isolated byte session; one JSON message per line."""

    async def send_line(self, payload: bytes) -> None: ...

    async def receive_line(self) -> bytes | None: ...

    async def close(self) -> None: ...


@runtime_checkable
class SandboxedStdioSessionFactory(Protocol):
    """Launch a persistent MCP server through the injected sandbox backend.

    The factory must mount ``product_workspace`` only at ``/workspace`` and
    read-only. It receives no host environment or unrestricted path list.
    """

    async def open(
        self,
        config: McpServerConfig,
        *,
        sandbox: SandboxBackend,
        product_workspace: Path,
    ) -> SandboxedStdioSession: ...


InboundRequestHandler = Callable[
    [JsonRpcRequest, RequestScope | None], Awaitable[JsonRpcResponse]
]
InboundNotificationHandler = Callable[
    [JsonRpcNotification, RequestScope | None], Awaitable[None]
]


@runtime_checkable
class McpTransport(Protocol):
    """Request/notification transport after JSON-RPC correlation."""

    async def start(
        self,
        request_handler: InboundRequestHandler,
        notification_handler: InboundNotificationHandler,
    ) -> None: ...

    async def request(
        self,
        method: str,
        params: dict[str, JsonValue] | None,
        *,
        context: RequestScope | None,
        timeout_seconds: float,
    ) -> JsonValue: ...

    async def notify(
        self,
        method: str,
        params: dict[str, JsonValue] | None = None,
        *,
        context: RequestScope | None = None,
    ) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class McpHttpResumeCheckpoint:
    """One durable, revisioned Streamable HTTP resume cursor."""

    state: HttpResumeState
    revision: int
    updated_at: datetime


@runtime_checkable
class McpHttpResumeStore(Protocol):
    """Persistence port owned by the server, not by the MCP package."""

    async def load(
        self,
        *,
        tenant_id: str,
        product_id: str,
        server_id: str,
        endpoint: str,
    ) -> McpHttpResumeCheckpoint | None: ...

    async def save(
        self,
        *,
        tenant_id: str,
        product_id: str,
        server_id: str,
        endpoint: str,
        state: HttpResumeState,
        expected_revision: int,
        idempotency_key: str,
    ) -> McpHttpResumeCheckpoint: ...


@runtime_checkable
class ResumableMcpHttpTransport(McpTransport, Protocol):
    """Streamable HTTP transport that can suspend without deleting a session."""

    @property
    def resume_state(self) -> HttpResumeState: ...

    async def close_for_resume(self) -> None: ...


@runtime_checkable
class LegacySseAdapter(Protocol):
    """Explicit extension seam for the pre-Streamable-HTTP SSE transport."""

    def create_transport(
        self,
        config: McpServerConfig,
        *,
        tenant_id: str,
        product_id: str,
        resume: HttpResumeState | None,
    ) -> McpTransport: ...


@runtime_checkable
class McpTransportFactory(Protocol):
    def create(
        self,
        config: McpServerConfig,
        *,
        tenant_id: str,
        product_id: str,
        product_workspace: Path,
        resume: HttpResumeState | None = None,
    ) -> McpTransport: ...


@runtime_checkable
class McpSampler(Protocol):
    """Privileged callback; production implementation must call ModelGateway."""

    async def create_message(
        self,
        context: McpOperationContext,
        request: CreateMessageParams,
    ) -> CreateMessageResult | CreateTaskResult: ...


@runtime_checkable
class McpElicitationHandler(Protocol):
    async def elicit(
        self,
        context: McpOperationContext,
        request: ElicitationParams,
    ) -> ElicitationResult | CreateTaskResult: ...


@runtime_checkable
class McpClientTaskHandler(Protocol):
    """Own client-side tasks created for sampling or elicitation callbacks."""

    async def get(self, context: McpOperationContext, task_id: str) -> McpTask: ...

    async def result(self, context: McpOperationContext, task_id: str) -> JsonValue: ...

    async def list(
        self, context: McpOperationContext, cursor: str | None
    ) -> ListTasksResult: ...

    async def cancel(self, context: McpOperationContext, task_id: str) -> McpTask: ...


@runtime_checkable
class McpLogSink(Protocol):
    async def append(
        self,
        context: McpOperationContext | None,
        server_id: str,
        message: LoggingMessage,
    ) -> None: ...


@runtime_checkable
class McpChangeSink(Protocol):
    async def changed(
        self,
        context: McpOperationContext | None,
        server_id: str,
        subject: str,
        payload: dict[str, JsonValue],
    ) -> None: ...


__all__ = [
    "HttpRequest",
    "HttpResponse",
    "InboundNotificationHandler",
    "InboundRequestHandler",
    "LegacySseAdapter",
    "McpChangeSink",
    "McpClientTaskHandler",
    "McpEgressPolicy",
    "McpElicitationHandler",
    "McpHttpExchange",
    "McpHttpResumeCheckpoint",
    "McpHttpResumeStore",
    "McpLogSink",
    "McpSampler",
    "McpTokenProvider",
    "McpTransport",
    "McpTransportFactory",
    "OAuthMetadataResolver",
    "OAuthTokenIssuer",
    "ResumableMcpHttpTransport",
    "SandboxedStdioSession",
    "SandboxedStdioSessionFactory",
    "ServerSentEvent",
]
