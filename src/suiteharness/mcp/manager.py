"""Tenant/product-isolated MCP connection ownership and lifecycle."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import TypeVar, cast

from pydantic import JsonValue

from suiteharness.runtime.scopes import RequestScope

from .callbacks import McpClientRequestDispatcher, ProductRootProvider
from .client import McpClient
from .models import (
    CallToolResult,
    ClientCapabilities,
    ElicitationCapability,
    HttpResumeState,
    LegacySseCompatibility,
    McpConnectionState,
    McpProtocolError,
    McpServerHealth,
    McpTransportError,
    McpTransportKind,
    ProductMcpConfig,
)
from .protocols import (
    InboundNotificationHandler,
    InboundRequestHandler,
    McpChangeSink,
    McpClientTaskHandler,
    McpElicitationHandler,
    McpHttpResumeCheckpoint,
    McpHttpResumeStore,
    McpLogSink,
    McpSampler,
    McpTransport,
    McpTransportFactory,
    ResumableMcpHttpTransport,
)

T = TypeVar("T")


class _DurableResumeTransport:
    """Serialize remote operations with revisioned resume-state commits."""

    def __init__(
        self,
        transport: ResumableMcpHttpTransport,
        store: McpHttpResumeStore,
        *,
        tenant_id: str,
        product_id: str,
        server_id: str,
        endpoint: str,
        checkpoint: McpHttpResumeCheckpoint | None,
    ) -> None:
        if checkpoint is not None and checkpoint.revision < 1:
            raise McpTransportError("durable MCP checkpoint revision must be positive")
        expected = HttpResumeState() if checkpoint is None else checkpoint.state
        if transport.resume_state != expected:
            raise McpTransportError("MCP transport did not accept its durable resume state")
        self._transport = transport
        self._store = store
        self._tenant_id = tenant_id
        self._product_id = product_id
        self._server_id = server_id
        self._endpoint = endpoint
        self._revision = 0 if checkpoint is None else checkpoint.revision
        self._writer_id = uuid.uuid4().hex
        self._operation_sequence = 0
        self._persistence_failure: BaseException | None = None
        self._operation_lock = asyncio.Lock()
        self._closed = False

    @property
    def resume_state(self) -> HttpResumeState:
        return self._transport.resume_state

    @property
    def operational(self) -> bool:
        return not self._closed and self._persistence_failure is None

    async def start(
        self,
        request_handler: InboundRequestHandler,
        notification_handler: InboundNotificationHandler,
    ) -> None:
        self._require_available()
        await self._transport.start(request_handler, notification_handler)

    def set_protocol_version(self, version: str) -> None:
        setter = getattr(self._transport, "set_protocol_version", None)
        if not callable(setter):
            raise McpTransportError("resumable MCP transport cannot set protocol version")
        setter(version)

    async def request(
        self,
        method: str,
        params: dict[str, JsonValue] | None,
        *,
        context: RequestScope | None,
        timeout_seconds: float,
    ) -> JsonValue:
        return await self._run_operation(
            lambda: self._transport.request(
                method,
                params,
                context=context,
                timeout_seconds=timeout_seconds,
            )
        )

    async def notify(
        self,
        method: str,
        params: dict[str, JsonValue] | None = None,
        *,
        context: RequestScope | None = None,
    ) -> None:
        await self._run_operation(
            lambda: self._transport.notify(method, params, context=context)
        )

    async def _run_operation(self, operation: Callable[[], Awaitable[T]]) -> T:
        async with self._operation_lock:
            self._require_available()
            result: object = None
            operation_failure: BaseException | None = None
            try:
                result = await operation()
            except BaseException as exc:
                operation_failure = exc

            persistence_cancellation: asyncio.CancelledError | None = None
            try:
                persistence_cancellation = await self._persist_after_operation()
            except BaseException as exc:
                self._persistence_failure = exc
                failure = McpTransportError(
                    "durable MCP resume state could not be persisted; connection disabled"
                )
                if operation_failure is not None:
                    failure.add_note(
                        "The remote MCP operation also failed before persistence failed "
                        f"({type(operation_failure).__name__})."
                    )
                raise failure from exc

            if persistence_cancellation is not None:
                raise persistence_cancellation
            if operation_failure is not None:
                raise operation_failure.with_traceback(operation_failure.__traceback__)
            return cast(T, result)

    async def _persist_after_operation(self) -> asyncio.CancelledError | None:
        self._operation_sequence += 1
        state = self._transport.resume_state
        save = asyncio.create_task(
            self._store.save(
                tenant_id=self._tenant_id,
                product_id=self._product_id,
                server_id=self._server_id,
                endpoint=self._endpoint,
                state=state,
                expected_revision=self._revision,
                idempotency_key=(
                    f"mcp-resume-{self._writer_id}-{self._operation_sequence}"
                ),
            )
        )
        cancellation: asyncio.CancelledError | None = None
        while not save.done():
            try:
                await asyncio.shield(save)
            except asyncio.CancelledError as exc:
                # A response may already have advanced the remote cursor. Finish
                # the bounded SQLite commit before honoring caller cancellation.
                cancellation = exc
        checkpoint = save.result()
        if checkpoint.revision != self._revision + 1 or checkpoint.state != state:
            raise RuntimeError("resume store returned an invalid CAS checkpoint")
        self._revision = checkpoint.revision
        return cancellation

    def _require_available(self) -> None:
        if self._closed:
            raise McpTransportError("durable MCP transport is closed")
        if self._persistence_failure is not None:
            raise McpTransportError(
                "durable MCP resume persistence previously failed; connection disabled"
            ) from self._persistence_failure

    async def close(self) -> None:
        async with self._operation_lock:
            if self._closed:
                return
            self._closed = True
            await self._transport.close_for_resume()


class McpClientManager:
    """Own connections by ``(tenant_id, product_id, server_id)``.

    There is deliberately no global server lookup. Every runtime operation must
    provide its authenticated ``RequestScope`` before a client can be obtained.
    """

    def __init__(
        self,
        transport_factory: McpTransportFactory,
        *,
        client_capabilities: ClientCapabilities,
        sampler: McpSampler | None = None,
        elicitation: McpElicitationHandler | None = None,
        logs: McpLogSink | None = None,
        changes: McpChangeSink | None = None,
        tasks: McpClientTaskHandler | None = None,
        resume_store: McpHttpResumeStore | None = None,
    ) -> None:
        if resume_store is not None and not isinstance(resume_store, McpHttpResumeStore):
            raise TypeError("resume_store must implement McpHttpResumeStore")
        self._factory = transport_factory
        self._base_capabilities = client_capabilities
        self._sampler = sampler
        self._elicitation = elicitation
        self._logs = logs
        self._changes = changes
        self._tasks = tasks
        self._resume_store = resume_store
        self._configs: dict[tuple[str, str], ProductMcpConfig] = {}
        self._clients: dict[tuple[str, str, str], McpClient] = {}
        self._failed_health: dict[tuple[str, str, str], McpServerHealth] = {}
        self._lock = asyncio.Lock()

    def configure(self, config: ProductMcpConfig) -> None:
        key = (config.tenant_id, config.product_id)
        if key in self._configs:
            raise ValueError(f"MCP product is already configured: {key!r}")
        for server in config.servers:
            if not server.enabled or not server.resume_sessions:
                continue
            if server.transport is not McpTransportKind.STREAMABLE_HTTP:
                continue
            if server.legacy_sse is not LegacySseCompatibility.DISABLED:
                raise ValueError("durable resume is unsupported for legacy SSE adapters")
            if self._resume_store is None:
                raise ValueError(
                    "resume_sessions=true requires an McpHttpResumeStore"
                )
        self._configs[key] = config

    async def connect_product(self, tenant_id: str, product_id: str) -> tuple[McpClient, ...]:
        product_key = (tenant_id, product_id)
        try:
            product = self._configs[product_key]
        except KeyError as exc:
            raise LookupError(f"MCP product is not configured: {product_key!r}") from exc
        async with self._lock:
            existing = tuple(
                client
                for (tenant, product_name, _), client in self._clients.items()
                if (tenant, product_name) == product_key
            )
            if existing:
                return existing
            created: list[tuple[tuple[str, str, str], McpClient]] = []
            try:
                for server in product.servers:
                    if not server.enabled:
                        continue
                    key = (tenant_id, product_id, server.server_id)
                    capabilities = self._capabilities_for(server.features)
                    checkpoint = await self._load_resume_checkpoint(
                        tenant_id, product_id, server
                    )
                    transport = self._factory.create(
                        server,
                        tenant_id=tenant_id,
                        product_id=product_id,
                        product_workspace=product.product_workspace,
                        resume=(None if checkpoint is None else checkpoint.state),
                    )
                    transport = self._durable_transport(
                        transport,
                        server=server,
                        tenant_id=tenant_id,
                        product_id=product_id,
                        checkpoint=checkpoint,
                    )
                    dispatcher = McpClientRequestDispatcher(
                        server_id=server.server_id,
                        tenant_id=tenant_id,
                        product_id=product_id,
                        capabilities=capabilities,
                        features=server.features,
                        roots=(
                            ProductRootProvider(
                                product.product_workspace,
                                display_name=f"{product_id} workspace",
                            )
                            if capabilities.roots is not None
                            else None
                        ),
                        sampler=self._sampler,
                        elicitation=self._elicitation,
                        logs=self._logs,
                        changes=self._changes,
                        tasks=self._tasks,
                    )
                    client = McpClient(
                        server,
                        tenant_id=tenant_id,
                        product_id=product_id,
                        transport=transport,
                        dispatcher=dispatcher,
                        capabilities=capabilities,
                    )
                    try:
                        await client.connect()
                    except BaseException:
                        self._failed_health[key] = client.health
                        await client.close()
                        raise
                    self._failed_health.pop(key, None)
                    created.append((key, client))
            except BaseException:
                await asyncio.gather(
                    *(client.close() for _, client in created), return_exceptions=True
                )
                raise
            self._clients.update(created)
            return tuple(client for _, client in created)

    async def _load_resume_checkpoint(
        self,
        tenant_id: str,
        product_id: str,
        server,
    ) -> McpHttpResumeCheckpoint | None:
        if not self._durable_resume_enabled(server):
            return None
        assert self._resume_store is not None
        assert server.endpoint is not None
        try:
            return await self._resume_store.load(
                tenant_id=tenant_id,
                product_id=product_id,
                server_id=server.server_id,
                endpoint=server.endpoint,
            )
        except BaseException as exc:
            raise McpTransportError(
                f"failed to load durable MCP resume state for {server.server_id!r}"
            ) from exc

    def _durable_transport(
        self,
        transport: McpTransport,
        *,
        server,
        tenant_id: str,
        product_id: str,
        checkpoint: McpHttpResumeCheckpoint | None,
    ) -> McpTransport:
        if not self._durable_resume_enabled(server):
            return transport
        if not isinstance(transport, ResumableMcpHttpTransport):
            raise McpTransportError(
                "resume_sessions=true requires a resumable Streamable HTTP transport"
            )
        assert self._resume_store is not None
        assert server.endpoint is not None
        return _DurableResumeTransport(
            transport,
            self._resume_store,
            tenant_id=tenant_id,
            product_id=product_id,
            server_id=server.server_id,
            endpoint=server.endpoint,
            checkpoint=checkpoint,
        )

    @staticmethod
    def _durable_resume_enabled(server) -> bool:
        return (
            server.enabled
            and server.resume_sessions
            and server.transport is McpTransportKind.STREAMABLE_HTTP
            and server.legacy_sse is LegacySseCompatibility.DISABLED
        )

    def client(self, scope: RequestScope, server_id: str) -> McpClient:
        key = (scope.tenant_id, scope.product_id, server_id)
        try:
            client = self._clients[key]
        except KeyError as exc:
            raise LookupError("MCP server is not connected for this product") from exc
        if client.state is not McpConnectionState.READY:
            raise McpProtocolError("MCP server connection is not ready")
        return client

    async def call_tool(
        self,
        scope: RequestScope,
        server_id: str,
        tool_name: str,
        arguments: dict[str, JsonValue],
    ) -> CallToolResult:
        return await self.client(scope, server_id).call_tool(scope, tool_name, arguments)

    def health(self, tenant_id: str, product_id: str) -> Mapping[str, McpServerHealth]:
        product = self._configs.get((tenant_id, product_id))
        if product is None:
            return {}
        result: dict[str, McpServerHealth] = {}
        for server in product.servers:
            client = self._clients.get((tenant_id, product_id, server.server_id))
            if client is not None:
                result[server.server_id] = client.health
            else:
                result[server.server_id] = self._failed_health.get(
                    (tenant_id, product_id, server.server_id),
                    McpServerHealth(
                        server_id=server.server_id,
                        state=McpConnectionState.DISCONNECTED,
                        detail="disabled" if not server.enabled else "not connected",
                    ),
                )
        return result

    async def close_product(self, tenant_id: str, product_id: str) -> None:
        async with self._lock:
            selected = [
                (key, client)
                for key, client in self._clients.items()
                if key[:2] == (tenant_id, product_id)
            ]
            for key, _client in selected:
                self._clients.pop(key, None)
            await asyncio.gather(*(client.close() for _, client in selected), return_exceptions=False)

    async def close(self) -> None:
        async with self._lock:
            selected = tuple(self._clients.items())
            self._clients.clear()
            await asyncio.gather(*(client.close() for _, client in selected), return_exceptions=False)

    def _capabilities_for(self, features) -> ClientCapabilities:
        elicitation = self._base_capabilities.elicitation
        if elicitation is not None:
            elicitation = ElicitationCapability(
                form=elicitation.form,
                url=elicitation.url if features.url_elicitation else None,
            )
        return self._base_capabilities.model_copy(
            update={
                "elicitation": elicitation,
                "tasks": (
                    self._base_capabilities.tasks
                    if features.experimental_tasks and self._tasks is not None
                    else None
                ),
            }
        )


__all__ = ["McpClientManager"]
