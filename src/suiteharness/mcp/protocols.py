"""Injection ports for MCP transports, auth and privileged client callbacks."""

from __future__ import annotations

from abc import ABC, abstractmethod
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


class McpEgressPolicy(ABC):
    """Authorize and pin an MCP endpoint before any bytes leave the process."""

    @abstractmethod
    async def authorize(
        self,
        endpoint: str,
        *,
        tenant_id: str,
        product_id: str,
    ) -> str:
        """Return the policy-approved endpoint, or raise to fail closed."""


class McpTokenProvider(ABC):
    """Resolve server-managed OAuth/service tokens without exposing secrets."""

    @abstractmethod
    async def authorization_header(
        self,
        credential_ref: str,
        *,
        endpoint: str,
    ) -> str: ...


class OAuthMetadataResolver(ABC):
    """Discover protected-resource/authorization-server metadata via policy."""

    @abstractmethod
    async def protected_resource(
        self, endpoint: str
    ) -> OAuthProtectedResourceMetadata: ...

    @abstractmethod
    async def authorization_server(
        self, issuer: str
    ) -> OAuthAuthorizationServerMetadata: ...


class OAuthTokenIssuer(ABC):
    """Exchange a configured server credential for a scoped OAuth token."""

    @abstractmethod
    async def issue(
        self,
        credential_ref: str,
        *,
        resource: str,
    ) -> OAuthTokenSet: ...


class SandboxedStdioSession(ABC):
    """A persistent, already-isolated byte session; one JSON message per line."""

    @abstractmethod
    async def send_line(self, payload: bytes) -> None: ...

    @abstractmethod
    async def receive_line(self) -> bytes | None: ...

    @abstractmethod
    async def close(self) -> None: ...


class SandboxedStdioSessionFactory(ABC):
    """Launch a persistent MCP server through the injected sandbox backend.

    The factory must mount ``product_workspace`` only at ``/workspace`` and
    read-only. It receives no host environment or unrestricted path list.
    """

    @abstractmethod
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


class McpTransport(ABC):
    """Request/notification transport after JSON-RPC correlation."""

    @abstractmethod
    async def start(
        self,
        request_handler: InboundRequestHandler,
        notification_handler: InboundNotificationHandler,
    ) -> None: ...

    @abstractmethod
    async def request(
        self,
        method: str,
        params: dict[str, JsonValue] | None,
        *,
        context: RequestScope | None,
        timeout_seconds: float,
    ) -> JsonValue: ...

    @abstractmethod
    async def notify(
        self,
        method: str,
        params: dict[str, JsonValue] | None = None,
        *,
        context: RequestScope | None = None,
    ) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class McpHttpResumeCheckpoint:
    """One durable, revisioned Streamable HTTP resume cursor."""

    state: HttpResumeState
    revision: int
    updated_at: datetime


class McpHttpResumeStore(ABC):
    """Persistence port owned by the server, not by the MCP package."""

    @abstractmethod
    async def load(
        self,
        *,
        tenant_id: str,
        product_id: str,
        server_id: str,
        endpoint: str,
    ) -> McpHttpResumeCheckpoint | None: ...

    @abstractmethod
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


class ResumableMcpHttpTransport(McpTransport, ABC):
    """Streamable HTTP transport that can suspend without deleting a session."""

    @property
    @abstractmethod
    def resume_state(self) -> HttpResumeState: ...

    @abstractmethod
    async def close_for_resume(self) -> None: ...


class LegacySseAdapter(ABC):
    """Explicit extension seam for the pre-Streamable-HTTP SSE transport."""

    @abstractmethod
    def create_transport(
        self,
        config: McpServerConfig,
        *,
        tenant_id: str,
        product_id: str,
        resume: HttpResumeState | None,
    ) -> McpTransport: ...


class McpTransportFactory(ABC):
    @abstractmethod
    def create(
        self,
        config: McpServerConfig,
        *,
        tenant_id: str,
        product_id: str,
        product_workspace: Path,
        resume: HttpResumeState | None = None,
    ) -> McpTransport: ...


class McpSampler(ABC):
    """Privileged callback; production implementation must call ModelGateway."""

    @abstractmethod
    async def create_message(
        self,
        context: McpOperationContext,
        request: CreateMessageParams,
    ) -> CreateMessageResult | CreateTaskResult: ...


class McpElicitationHandler(ABC):
    @abstractmethod
    async def elicit(
        self,
        context: McpOperationContext,
        request: ElicitationParams,
    ) -> ElicitationResult | CreateTaskResult: ...


class McpClientTaskHandler(ABC):
    """Own client-side tasks created for sampling or elicitation callbacks."""

    @abstractmethod
    async def get(self, context: McpOperationContext, task_id: str) -> McpTask: ...

    @abstractmethod
    async def result(self, context: McpOperationContext, task_id: str) -> JsonValue: ...

    @abstractmethod
    async def list(
        self, context: McpOperationContext, cursor: str | None
    ) -> ListTasksResult: ...

    @abstractmethod
    async def cancel(self, context: McpOperationContext, task_id: str) -> McpTask: ...


class McpLogSink(ABC):
    @abstractmethod
    async def append(
        self,
        context: McpOperationContext | None,
        server_id: str,
        message: LoggingMessage,
    ) -> None: ...


class McpChangeSink(ABC):
    @abstractmethod
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
