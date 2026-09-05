"""Fail-closed handlers for MCP server-to-client requests and notifications."""

from __future__ import annotations

from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import JsonValue, ValidationError

from suiteharness.models import ModelGateway
from suiteharness.models.types import (
    FinishReason,
    ModelMessage,
    ModelRequest,
    ModelRole,
    ModelTool,
)
from suiteharness.models.types import (
    TextContent as ModelTextContent,
)
from suiteharness.runtime.scopes import RequestScope

from .models import (
    ClientCapabilities,
    CreateMessageParams,
    CreateMessageResult,
    CreateTaskResult,
    ElicitationMode,
    ElicitationParams,
    ElicitationResult,
    JsonRpcErrorObject,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
    ListRootsResult,
    LoggingMessage,
    McpErrorCode,
    McpFeatureFlags,
    McpOperationContext,
    McpProtocolError,
    McpRoot,
    SamplingMessage,
    TextContent,
    ToolResultContent,
    ToolUseContent,
)
from .protocols import (
    McpChangeSink,
    McpClientTaskHandler,
    McpElicitationHandler,
    McpLogSink,
    McpSampler,
)


def operation_context(scope: RequestScope) -> McpOperationContext:
    return McpOperationContext(
        tenant_id=scope.tenant_id,
        product_id=scope.product_id,
        principal_id=scope.principal_id,
        roles=scope.roles,
        purpose=scope.purpose,
        request_id=scope.request_id,
        correlation_id=scope.correlation_id,
    )


class ProductRootProvider:
    """Expose exactly one canonical product workspace root to an MCP server."""

    def __init__(self, product_workspace: Path, *, display_name: str | None = None) -> None:
        root = Path(product_workspace)
        if not root.is_absolute() or not root.exists() or not root.is_dir():
            raise ValueError("product workspace must be an existing absolute directory")
        if root.is_symlink():
            raise ValueError("product workspace must not be a symbolic link")
        self._root = root.resolve(strict=True)
        self._display_name = display_name

    @property
    def root(self) -> Path:
        return self._root

    def list_roots(self) -> ListRootsResult:
        return ListRootsResult(
            roots=(McpRoot(uri=self._root.as_uri(), name=self._display_name),)
        )


class GatewayMcpSampler:
    """Sampling callback hard-wired to a root-owned ModelGateway route.

    MCP model preferences are hints only. The server cannot select a provider,
    credential, endpoint or unapproved model; all routing stays inside the
    administrator-declared ``route_id``.
    """

    def __init__(self, gateway: ModelGateway, *, route_id: str, max_tokens: int = 32_768) -> None:
        if not route_id.strip():
            raise ValueError("sampling route_id must not be blank")
        if max_tokens < 1:
            raise ValueError("sampling max_tokens must be positive")
        self._gateway = gateway
        self._route_id = route_id
        self._max_tokens = max_tokens

    async def create_message(
        self,
        context: McpOperationContext,
        request: CreateMessageParams,
    ) -> CreateMessageResult:
        if request.task is not None:
            raise McpProtocolError(
                "GatewayMcpSampler does not create experimental tasks; install a task-aware sampler",
                code=McpErrorCode.INVALID_PARAMS,
            )
        if request.max_tokens > self._max_tokens:
            raise McpProtocolError(
                "sampling token budget exceeds deployment policy",
                code=McpErrorCode.INVALID_PARAMS,
            )
        messages: list[ModelMessage] = []
        if request.system_prompt:
            messages.append(ModelMessage.text(ModelRole.SYSTEM, request.system_prompt))
        messages.extend(_sampling_message(item) for item in request.messages)
        tools = tuple(
            ModelTool(
                name=item.name,
                description=item.description,
                input_schema=item.input_schema,
            )
            for item in request.tools
        )
        tool_choice = "auto"
        if request.tool_choice is not None:
            mode = request.tool_choice.get("mode")
            if mode in {"auto", "none", "required"}:
                tool_choice = mode
        response = await self._gateway.complete(
            self._route_id,
            ModelRequest(
                messages=tuple(messages),
                tools=tools,
                tool_choice=tool_choice,
                temperature=request.temperature,
                max_output_tokens=request.max_tokens,
                stop=request.stop_sequences,
                metadata={
                    "mcp_tenant_id": context.tenant_id,
                    "mcp_product_id": context.product_id,
                    "mcp_principal_id": context.principal_id,
                    "mcp_purpose": context.purpose,
                },
            ),
        )
        content: list[TextContent | ToolUseContent] = []
        for part in response.content:
            if isinstance(part, ModelTextContent):
                content.append(TextContent(text=part.text))
        for call in response.tool_calls:
            content.append(
                ToolUseContent(id=call.call_id, name=call.name, input=call.arguments)
            )
        if not content:
            content.append(TextContent(text=""))
        return CreateMessageResult(
            content=tuple(content),
            model=response.model,
            stopReason=_finish_reason(response.finish_reason),
        )


def _sampling_message(message: SamplingMessage) -> ModelMessage:
    role = ModelRole.USER if message.role == "user" else ModelRole.ASSISTANT
    parts = message.content if isinstance(message.content, tuple) else (message.content,)
    text_parts: list[ModelTextContent] = []
    tool_calls = []
    tool_call_id: str | None = None
    for part in parts:
        if isinstance(part, TextContent):
            text_parts.append(ModelTextContent(text=part.text))
        elif isinstance(part, ToolUseContent):
            from suiteharness.models.types import ModelToolCall

            tool_calls.append(
                ModelToolCall(call_id=part.id, name=part.name, arguments=part.input)
            )
        elif isinstance(part, ToolResultContent):
            tool_call_id = part.tool_use_id
            text_parts.extend(
                ModelTextContent(text=value.text)
                for value in part.content
                if isinstance(value, TextContent)
            )
        else:
            raise McpProtocolError(
                "this model route cannot safely translate non-text sampling content",
                code=McpErrorCode.INVALID_PARAMS,
            )
    if tool_call_id is not None:
        return ModelMessage(
            role=ModelRole.TOOL,
            content=tuple(text_parts) or (ModelTextContent(text=""),),
            tool_call_id=tool_call_id,
        )
    return ModelMessage(
        role=role,
        content=tuple(text_parts),
        tool_calls=tuple(tool_calls),
    )


def _finish_reason(value: FinishReason) -> str:
    return {
        FinishReason.STOP: "endTurn",
        FinishReason.TOOL_CALLS: "toolUse",
        FinishReason.LENGTH: "maxTokens",
    }.get(value, value.value)


class McpClientRequestDispatcher:
    """Dispatch privileged callbacks with the current authenticated call scope."""

    def __init__(
        self,
        *,
        server_id: str,
        tenant_id: str,
        product_id: str,
        capabilities: ClientCapabilities,
        features: McpFeatureFlags,
        roots: ProductRootProvider | None = None,
        sampler: McpSampler | None = None,
        elicitation: McpElicitationHandler | None = None,
        logs: McpLogSink | None = None,
        changes: McpChangeSink | None = None,
        tasks: McpClientTaskHandler | None = None,
    ) -> None:
        self._server_id = server_id
        self._tenant_id = tenant_id
        self._product_id = product_id
        self._capabilities = capabilities
        self._features = features
        self._roots = roots
        self._sampler = sampler
        self._elicitation = elicitation
        self._logs = logs
        self._changes = changes
        self._tasks = tasks

    async def request(
        self, request: JsonRpcRequest, scope: RequestScope | None
    ) -> JsonRpcResponse:
        try:
            result = await self._dispatch_request(request.method, request.params, scope)
            return JsonRpcResponse(id=request.id, result=result)
        except McpProtocolError as exc:
            return JsonRpcResponse(
                id=request.id,
                error=JsonRpcErrorObject(code=exc.code, message=str(exc), data=exc.data),
            )
        except ValidationError:
            return JsonRpcResponse(
                id=request.id,
                error=JsonRpcErrorObject(code=-32602, message="invalid callback parameters"),
            )

    async def _dispatch_request(
        self,
        method: str,
        params: dict[str, JsonValue] | list[JsonValue] | None,
        scope: RequestScope | None,
    ) -> JsonValue:
        if method == "ping":
            return {}
        if not isinstance(params, dict | type(None)):
            raise McpProtocolError("named parameters are required", code=-32602)
        context = self._authorized_context(scope)
        if method == "roots/list":
            if self._capabilities.roots is None or self._roots is None:
                raise McpProtocolError("roots capability is unavailable", code=-32601)
            return self._roots.list_roots().model_dump(mode="json", by_alias=True)
        if method == "sampling/createMessage":
            if self._capabilities.sampling is None or self._sampler is None:
                raise McpProtocolError("sampling capability is unavailable", code=-32601)
            parsed = CreateMessageParams.model_validate(params or {})
            if parsed.tools and self._capabilities.sampling.tools is None:
                raise McpProtocolError("sampling tools were not negotiated", code=-32602)
            if parsed.task is not None and not self._supports_task_request("sampling"):
                raise McpProtocolError("task-augmented sampling is disabled", code=-32602)
            result = await self._sampler.create_message(context, parsed)
            if (parsed.task is not None) != isinstance(result, CreateTaskResult):
                raise McpProtocolError(
                    "sampling task response does not match the request mode",
                    code=McpErrorCode.INTERNAL_ERROR,
                )
            return result.model_dump(mode="json", by_alias=True, exclude_none=True)
        if method == "elicitation/create":
            if self._capabilities.elicitation is None or self._elicitation is None:
                raise McpProtocolError("elicitation capability is unavailable", code=-32601)
            parsed = ElicitationParams.model_validate(params or {})
            if parsed.mode is ElicitationMode.URL and (
                not self._features.url_elicitation
                or self._capabilities.elicitation.url is None
            ):
                raise McpProtocolError("URL elicitation is disabled", code=-32602)
            if parsed.task is not None and not self._supports_task_request("elicitation"):
                raise McpProtocolError("task-augmented elicitation is disabled", code=-32602)
            result = await self._elicitation.elicit(context, parsed)
            if (parsed.task is not None) != isinstance(result, CreateTaskResult):
                raise McpProtocolError(
                    "elicitation task response does not match the request mode",
                    code=McpErrorCode.INTERNAL_ERROR,
                )
            if (
                parsed.mode is ElicitationMode.URL
                and isinstance(result, ElicitationResult)
                and result.content is not None
            ):
                raise McpProtocolError(
                    "URL elicitation must not return form content",
                    code=McpErrorCode.INVALID_PARAMS,
                )
            if parsed.mode is ElicitationMode.FORM and not isinstance(result, CreateTaskResult):
                if result.action == "accept":
                    if result.content is None:
                        raise McpProtocolError(
                            "accepted form elicitation requires content",
                            code=McpErrorCode.INVALID_PARAMS,
                        )
                    schema = parsed.requested_schema
                    assert schema is not None
                    validator = Draft202012Validator(
                        schema.model_dump(mode="json", by_alias=True, exclude_none=True)
                    )
                    try:
                        validator.validate(result.content)
                    except JsonSchemaValidationError as exc:
                        raise McpProtocolError(
                            "elicitation response does not match requestedSchema",
                            code=McpErrorCode.INVALID_PARAMS,
                        ) from exc
            return result.model_dump(mode="json", by_alias=True, exclude_none=True)
        if method in {"tasks/get", "tasks/result", "tasks/list", "tasks/cancel"}:
            if not self._features.experimental_tasks or self._tasks is None:
                raise McpProtocolError("client task service is unavailable", code=-32601)
            task_capability = self._capabilities.tasks
            if task_capability is None:
                raise McpProtocolError("client tasks were not negotiated", code=-32601)
            if method == "tasks/list":
                if task_capability.list is None:
                    raise McpProtocolError("tasks/list was not negotiated", code=-32601)
                cursor = None if params is None else params.get("cursor")
                if cursor is not None and not isinstance(cursor, str):
                    raise McpProtocolError("task cursor must be a string", code=-32602)
                listed = await self._tasks.list(context, cursor)
                return listed.model_dump(mode="json", by_alias=True, exclude_none=True)
            task_id = None if params is None else params.get("taskId")
            if not isinstance(task_id, str) or not task_id:
                raise McpProtocolError("taskId is required", code=-32602)
            if method == "tasks/get":
                result = await self._tasks.get(context, task_id)
                return result.model_dump(mode="json", by_alias=True, exclude_none=True)
            if method == "tasks/result":
                return await self._tasks.result(context, task_id)
            if task_capability.cancel is None:
                raise McpProtocolError("tasks/cancel was not negotiated", code=-32601)
            result = await self._tasks.cancel(context, task_id)
            return result.model_dump(mode="json", by_alias=True, exclude_none=True)
        raise McpProtocolError(f"unsupported client method: {method}", code=-32601)

    async def notification(
        self, notification: JsonRpcNotification, scope: RequestScope | None
    ) -> None:
        context = None if scope is None else self._authorized_context(scope)
        params = notification.params if isinstance(notification.params, dict) else {}
        if notification.method == "notifications/message":
            if self._logs is not None:
                await self._logs.append(
                    context,
                    self._server_id,
                    LoggingMessage.model_validate(params),
                )
            return
        subjects = {
            "notifications/tools/list_changed": "tools",
            "notifications/resources/list_changed": "resources",
            "notifications/resources/updated": "resource",
            "notifications/prompts/list_changed": "prompts",
            "notifications/progress": "progress",
            "notifications/cancelled": "cancelled",
            "notifications/elicitation/complete": "elicitation",
            "notifications/tasks/status": "task",
        }
        subject = subjects.get(notification.method)
        if subject is not None and self._changes is not None:
            await self._changes.changed(context, self._server_id, subject, params)

    def _authorized_context(self, scope: RequestScope | None) -> McpOperationContext:
        if scope is None:
            raise McpProtocolError(
                "privileged MCP callback has no authenticated operation context",
                code=-32600,
            )
        if scope.tenant_id != self._tenant_id or scope.product_id != self._product_id:
            raise McpProtocolError("MCP callback scope mismatch", code=-32600)
        return operation_context(scope)

    def _supports_task_request(self, kind: str) -> bool:
        if not self._features.experimental_tasks or self._capabilities.tasks is None:
            return False
        requests = self._capabilities.tasks.requests
        if requests is None:
            return False
        if kind == "sampling":
            return (
                requests.sampling is not None
                and requests.sampling.create_message is not None
            )
        return requests.elicitation is not None and requests.elicitation.create is not None


__all__ = [
    "GatewayMcpSampler",
    "McpClientRequestDispatcher",
    "ProductRootProvider",
    "operation_context",
]
