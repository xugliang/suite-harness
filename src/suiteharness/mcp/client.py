"""Capability-aware MCP 2025-11-25 client session."""

from __future__ import annotations

import asyncio
from typing import TypeVar, cast

from pydantic import BaseModel, JsonValue

from suiteharness.runtime.scopes import RequestScope

from .callbacks import McpClientRequestDispatcher
from .models import (
    CallToolParams,
    CallToolResult,
    ClientCapabilities,
    CompleteParams,
    CompleteResult,
    GetPromptResult,
    GetTaskResult,
    Implementation,
    InitializeParams,
    InitializeResult,
    JsonRpcId,
    ListPromptsResult,
    ListResourcesResult,
    ListResourceTemplatesResult,
    ListTasksResult,
    ListToolsResult,
    LoggingLevel,
    McpCapabilityError,
    McpConnectionState,
    McpProtocolError,
    McpServerConfig,
    McpServerHealth,
    McpTool,
    ReadResourceResult,
    TaskMetadata,
)
from .protocols import McpTransport

TModel = TypeVar("TModel", bound=BaseModel)


def _payload(model: BaseModel) -> dict[str, JsonValue]:
    return cast(
        dict[str, JsonValue],
        model.model_dump(mode="json", by_alias=True, exclude_none=True),
    )


class McpClient:
    """One negotiated MCP connection owned by exactly one tenant/product."""

    def __init__(
        self,
        config: McpServerConfig,
        *,
        tenant_id: str,
        product_id: str,
        transport: McpTransport,
        dispatcher: McpClientRequestDispatcher,
        capabilities: ClientCapabilities,
        client_info: Implementation | None = None,
    ) -> None:
        self.config = config
        self.tenant_id = tenant_id
        self.product_id = product_id
        self._transport = transport
        self._dispatcher = dispatcher
        self._client_capabilities = capabilities
        self._client_info = client_info or Implementation(
            name="suite-harness", version="0.1.0"
        )
        self._state = McpConnectionState.DISCONNECTED
        self._initialize: InitializeResult | None = None
        self._detail = ""

    @property
    def state(self) -> McpConnectionState:
        return self._state

    @property
    def negotiated(self) -> InitializeResult | None:
        return self._initialize

    @property
    def health(self) -> McpServerHealth:
        transport_operational = getattr(self._transport, "operational", True)
        state = self._state
        detail = self._detail
        if state is McpConnectionState.READY and transport_operational is False:
            state = McpConnectionState.DEGRADED
            detail = "MCP transport is unavailable"
        return McpServerHealth(
            server_id=self.config.server_id,
            state=state,
            negotiated_version=(
                None if self._initialize is None else self._initialize.protocol_version
            ),
            server_info=(None if self._initialize is None else self._initialize.server_info),
            detail=detail,
        )

    async def connect(self) -> InitializeResult:
        if self._state is McpConnectionState.CLOSED:
            raise McpProtocolError("closed MCP client cannot reconnect")
        if self._state is McpConnectionState.READY and self._initialize is not None:
            return self._initialize
        self._state = McpConnectionState.INITIALIZING
        self._detail = ""
        try:
            await self._transport.start(
                self._dispatcher.request,
                self._dispatcher.notification,
            )
            params = InitializeParams(
                protocolVersion=self.config.protocol_versions[0],
                capabilities=self._client_capabilities,
                clientInfo=self._client_info,
            )
            raw = await self._transport.request(
                "initialize",
                _payload(params),
                context=None,
                timeout_seconds=self.config.timeout_seconds,
            )
            initialized = InitializeResult.model_validate(raw)
            if initialized.protocol_version not in self.config.protocol_versions:
                raise McpProtocolError(
                    "server selected an unsupported MCP protocol version: "
                    f"{initialized.protocol_version}"
                )
            self._initialize = initialized
            set_version = getattr(self._transport, "set_protocol_version", None)
            if callable(set_version):
                set_version(initialized.protocol_version)
            await self._transport.notify("notifications/initialized")
            self._state = McpConnectionState.READY
            return initialized
        except BaseException as exc:
            self._state = McpConnectionState.DEGRADED
            self._detail = str(exc)
            raise

    async def ping(self, scope: RequestScope) -> None:
        await self._request("ping", {}, scope)

    async def list_tools(
        self, scope: RequestScope, *, cursor: str | None = None
    ) -> ListToolsResult:
        self._require_server_capability("tools")
        return await self._typed(
            "tools/list", {"cursor": cursor} if cursor is not None else {}, scope, ListToolsResult
        )

    async def all_tools(self, scope: RequestScope) -> tuple[McpTool, ...]:
        try:
            async with asyncio.timeout(self.config.discovery_timeout_seconds):
                return await self._all_tools_bounded(scope)
        except TimeoutError as exc:
            raise McpProtocolError(
                "tools/list discovery exceeded discovery_timeout_seconds"
            ) from exc

    async def _all_tools_bounded(self, scope: RequestScope) -> tuple[McpTool, ...]:
        items = []
        cursor: str | None = None
        seen: set[str] = set()
        pages = 0
        while True:
            if pages >= self.config.max_list_pages:
                raise McpProtocolError("tools/list discovery exceeded max_list_pages")
            page = await self.list_tools(scope, cursor=cursor)
            pages += 1
            if len(items) + len(page.tools) > self.config.max_list_items:
                raise McpProtocolError("tools/list discovery exceeded max_list_items")
            items.extend(page.tools)
            cursor = page.next_cursor
            if cursor is None:
                return tuple(items)
            if cursor in seen:
                raise McpProtocolError("tools/list returned a repeated pagination cursor")
            seen.add(cursor)

    async def call_tool(
        self,
        scope: RequestScope,
        name: str,
        arguments: dict[str, JsonValue] | None = None,
        *,
        task: TaskMetadata | None = None,
    ) -> CallToolResult:
        self._require_server_capability("tools")
        if task is not None:
            tasks = self._require_tasks()
            if (
                tasks.requests is None
                or tasks.requests.tools is None
                or tasks.requests.tools.call is None
            ):
                raise McpCapabilityError("server did not advertise task-augmented tools/call")
        params = CallToolParams(name=name, arguments=arguments or {}, task=task)
        return await self._typed("tools/call", _payload(params), scope, CallToolResult)

    async def list_resources(
        self, scope: RequestScope, *, cursor: str | None = None
    ) -> ListResourcesResult:
        self._require_server_capability("resources")
        return await self._typed(
            "resources/list",
            {"cursor": cursor} if cursor is not None else {},
            scope,
            ListResourcesResult,
        )

    async def list_resource_templates(
        self, scope: RequestScope, *, cursor: str | None = None
    ) -> ListResourceTemplatesResult:
        self._require_server_capability("resources")
        return await self._typed(
            "resources/templates/list",
            {"cursor": cursor} if cursor is not None else {},
            scope,
            ListResourceTemplatesResult,
        )

    async def read_resource(self, scope: RequestScope, uri: str) -> ReadResourceResult:
        self._require_server_capability("resources")
        return await self._typed("resources/read", {"uri": uri}, scope, ReadResourceResult)

    async def subscribe_resource(self, scope: RequestScope, uri: str) -> None:
        resources = self._require_server_capability("resources")
        if not resources.subscribe:
            raise McpCapabilityError("server did not advertise resource subscriptions")
        await self._request("resources/subscribe", {"uri": uri}, scope)

    async def unsubscribe_resource(self, scope: RequestScope, uri: str) -> None:
        resources = self._require_server_capability("resources")
        if not resources.subscribe:
            raise McpCapabilityError("server did not advertise resource subscriptions")
        await self._request("resources/unsubscribe", {"uri": uri}, scope)

    async def list_prompts(
        self, scope: RequestScope, *, cursor: str | None = None
    ) -> ListPromptsResult:
        self._require_server_capability("prompts")
        return await self._typed(
            "prompts/list",
            {"cursor": cursor} if cursor is not None else {},
            scope,
            ListPromptsResult,
        )

    async def get_prompt(
        self,
        scope: RequestScope,
        name: str,
        *,
        arguments: dict[str, str] | None = None,
    ) -> GetPromptResult:
        self._require_server_capability("prompts")
        params: dict[str, JsonValue] = {"name": name}
        if arguments is not None:
            params["arguments"] = cast(JsonValue, arguments)
        return await self._typed("prompts/get", params, scope, GetPromptResult)

    async def complete(self, scope: RequestScope, params: CompleteParams) -> CompleteResult:
        self._require_server_capability("completions")
        return await self._typed("completion/complete", _payload(params), scope, CompleteResult)

    async def set_logging_level(self, scope: RequestScope, level: LoggingLevel) -> None:
        self._require_server_capability("logging")
        await self._request("logging/setLevel", {"level": level.value}, scope)

    async def list_tasks(
        self, scope: RequestScope, *, cursor: str | None = None
    ) -> ListTasksResult:
        tasks = self._require_tasks()
        if tasks.list is None:
            raise McpCapabilityError("server did not advertise tasks/list")
        return await self._typed(
            "tasks/list", {"cursor": cursor} if cursor is not None else {}, scope, ListTasksResult
        )

    async def get_task(self, scope: RequestScope, task_id: str) -> GetTaskResult:
        self._require_tasks()
        return await self._typed("tasks/get", {"taskId": task_id}, scope, GetTaskResult)

    async def get_task_result(self, scope: RequestScope, task_id: str) -> JsonValue:
        self._require_tasks()
        return await self._request("tasks/result", {"taskId": task_id}, scope)

    async def cancel_task(self, scope: RequestScope, task_id: str) -> GetTaskResult:
        tasks = self._require_tasks()
        if tasks.cancel is None:
            raise McpCapabilityError("server did not advertise tasks/cancel")
        return await self._typed("tasks/cancel", {"taskId": task_id}, scope, GetTaskResult)

    async def notify_progress(
        self,
        scope: RequestScope,
        *,
        progress_token: str | int,
        progress: float,
        total: float | None = None,
        message: str | None = None,
    ) -> None:
        params: dict[str, JsonValue] = {
            "progressToken": progress_token,
            "progress": progress,
        }
        if total is not None:
            params["total"] = total
        if message is not None:
            params["message"] = message
        await self._transport.notify("notifications/progress", params, context=scope)

    async def cancel_request(
        self, scope: RequestScope, request_id: JsonRpcId, *, reason: str | None = None
    ) -> None:
        params: dict[str, JsonValue] = {"requestId": request_id}
        if reason is not None:
            params["reason"] = reason
        await self._transport.notify("notifications/cancelled", params, context=scope)

    async def notify_roots_changed(self, scope: RequestScope) -> None:
        if self._client_capabilities.roots is None:
            raise McpCapabilityError("client did not advertise roots")
        if not self._client_capabilities.roots.list_changed:
            raise McpCapabilityError("client did not advertise roots listChanged")
        await self._transport.notify("notifications/roots/list_changed", {}, context=scope)

    async def notify_elicitation_complete(
        self, scope: RequestScope, elicitation_id: str
    ) -> None:
        capability = self._client_capabilities.elicitation
        if (
            capability is None
            or capability.url is None
            or not self.config.features.url_elicitation
        ):
            raise McpCapabilityError("URL elicitation completion is disabled")
        await self._transport.notify(
            "notifications/elicitation/complete",
            {"elicitationId": elicitation_id},
            context=scope,
        )

    async def _typed(
        self,
        method: str,
        params: dict[str, JsonValue],
        scope: RequestScope,
        model: type[TModel],
    ) -> TModel:
        return model.model_validate(await self._request(method, params, scope))

    async def _request(
        self,
        method: str,
        params: dict[str, JsonValue],
        scope: RequestScope,
    ) -> JsonValue:
        self._require_context(scope)
        if self._state is not McpConnectionState.READY:
            raise McpProtocolError("MCP client is not ready")
        return await self._transport.request(
            method,
            params,
            context=scope,
            timeout_seconds=self.config.timeout_seconds,
        )

    def _require_context(self, scope: RequestScope) -> None:
        if scope.tenant_id != self.tenant_id or scope.product_id != self.product_id:
            raise McpProtocolError("MCP operation scope does not own this connection")

    def _require_server_capability(self, name: str):
        if self._initialize is None or self._state is not McpConnectionState.READY:
            raise McpProtocolError("MCP client is not ready")
        capability = getattr(self._initialize.capabilities, name)
        if capability is None:
            raise McpCapabilityError(f"server did not advertise {name}")
        return capability

    def _require_tasks(self):
        if not self.config.features.experimental_tasks:
            raise McpCapabilityError("experimental MCP tasks are disabled by deployment policy")
        return self._require_server_capability("tasks")

    async def close(self) -> None:
        if self._state is McpConnectionState.CLOSED:
            return
        await self._transport.close()
        self._state = McpConnectionState.CLOSED


__all__ = ["McpClient"]
