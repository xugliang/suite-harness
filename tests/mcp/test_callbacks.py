from __future__ import annotations

import asyncio
from pathlib import Path

from suiteharness.mcp.callbacks import (
    GatewayMcpSampler,
    McpClientRequestDispatcher,
    ProductRootProvider,
)
from suiteharness.mcp.models import (
    ClientCapabilities,
    ClientTaskRequests,
    CreateMessageParams,
    ElicitationCapability,
    ElicitationTaskRequests,
    EmptyCapability,
    JsonRpcNotification,
    JsonRpcRequest,
    ListTasksResult,
    McpFeatureFlags,
    McpTask,
    RootsCapability,
    SamplingCapability,
    SamplingMessage,
    SamplingTaskRequests,
    SamplingTool,
    TaskClientCapability,
    TaskStatus,
    TextContent,
)
from suiteharness.models.types import (
    FinishReason,
    ModelResponse,
    ModelToolCall,
    ModelUsage,
)
from suiteharness.models.types import (
    TextContent as ModelTextContent,
)
from suiteharness.runtime.scopes import RequestScope, ScopePath


def _scope(product: str = "product-a") -> RequestScope:
    return RequestScope(
        path=ScopePath.agent("tenant-a", product, "agent-1", "session-1"),
        principal_id="user-1",
        purpose="mcp-callback",
    )


class FakeSampler:
    def __init__(self) -> None:
        self.calls = []

    async def create_message(self, context, request):  # type: ignore[no-untyped-def]
        self.calls.append((context, request))
        from suiteharness.mcp.models import CreateMessageResult

        return CreateMessageResult(content=TextContent(text="sample"), model="approved")


class FakeElicitation:
    def __init__(self) -> None:
        self.calls = []

    async def elicit(self, context, request):  # type: ignore[no-untyped-def]
        self.calls.append((context, request))
        from suiteharness.mcp.models import ElicitationResult

        return ElicitationResult(
            action="accept",
            content=None if request.mode.value == "url" else {"name": "Ada"},
        )


class FakeLogs:
    def __init__(self) -> None:
        self.calls = []

    async def append(self, context, server_id, message):  # type: ignore[no-untyped-def]
        self.calls.append((context, server_id, message))


class FakeChanges:
    def __init__(self) -> None:
        self.calls = []

    async def changed(self, context, server_id, subject, payload):  # type: ignore[no-untyped-def]
        self.calls.append((context, server_id, subject, payload))


class FakeTasks:
    def __init__(self) -> None:
        self.calls = []

    def _task(self, task_id):  # type: ignore[no-untyped-def]
        return McpTask(
            taskId=task_id,
            status="working",
            createdAt="2026-09-04T00:00:00Z",
            lastUpdatedAt="2026-09-04T00:00:00Z",
            ttl=None,
        )

    async def get(self, context, task_id):  # type: ignore[no-untyped-def]
        self.calls.append(("get", context, task_id))
        return self._task(task_id)

    async def result(self, context, task_id):  # type: ignore[no-untyped-def]
        self.calls.append(("result", context, task_id))
        return {"content": [{"type": "text", "text": "done"}]}

    async def list(self, context, cursor):  # type: ignore[no-untyped-def]
        self.calls.append(("list", context, cursor))
        return ListTasksResult(tasks=(self._task("task-1"),))

    async def cancel(self, context, task_id):  # type: ignore[no-untyped-def]
        self.calls.append(("cancel", context, task_id))
        return self._task(task_id).model_copy(update={"status": TaskStatus.CANCELLED})


def test_dispatcher_fails_privileged_callbacks_without_matching_context(tmp_path: Path) -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        sampler = FakeSampler()
        dispatcher = McpClientRequestDispatcher(
            server_id="remote",
            tenant_id="tenant-a",
            product_id="product-a",
            capabilities=ClientCapabilities(
                roots=RootsCapability(), sampling=SamplingCapability(tools=True)
            ),
            features=McpFeatureFlags(),
            roots=ProductRootProvider(tmp_path),
            sampler=sampler,
        )
        missing = await dispatcher.request(JsonRpcRequest(id=1, method="roots/list"), None)
        crossed = await dispatcher.request(
            JsonRpcRequest(id=2, method="roots/list"), _scope("product-b")
        )
        ping = await dispatcher.request(JsonRpcRequest(id=3, method="ping"), None)
        return missing, crossed, ping, sampler

    missing, crossed, ping, sampler = asyncio.run(exercise())
    assert missing.error is not None and missing.error.code == -32600
    assert crossed.error is not None and crossed.error.code == -32600
    assert ping.result == {}
    assert sampler.calls == []


def test_dispatcher_handles_roots_sampling_elicitation_and_notifications(tmp_path: Path) -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        sampler = FakeSampler()
        elicitation = FakeElicitation()
        logs = FakeLogs()
        changes = FakeChanges()
        dispatcher = McpClientRequestDispatcher(
            server_id="remote",
            tenant_id="tenant-a",
            product_id="product-a",
            capabilities=ClientCapabilities(
                roots=RootsCapability(),
                sampling=SamplingCapability(tools=True),
                elicitation=ElicitationCapability(form=True, url=True),
            ),
            features=McpFeatureFlags(url_elicitation=True),
            roots=ProductRootProvider(tmp_path),
            sampler=sampler,
            elicitation=elicitation,
            logs=logs,
            changes=changes,
        )
        scope = _scope()
        roots = await dispatcher.request(JsonRpcRequest(id=1, method="roots/list"), scope)
        sampled = await dispatcher.request(
            JsonRpcRequest(
                id=2,
                method="sampling/createMessage",
                params={
                    "messages": [
                        {"role": "user", "content": {"type": "text", "text": "hi"}}
                    ],
                    "maxTokens": 10,
                    "tools": [
                        {
                            "name": "lookup",
                            "description": "Lookup",
                            "inputSchema": {"type": "object"},
                        }
                    ],
                },
            ),
            scope,
        )
        elicited = await dispatcher.request(
            JsonRpcRequest(
                id=3,
                method="elicitation/create",
                params={
                    "mode": "url",
                    "message": "login",
                    "url": "https://id.example.com",
                    "elicitationId": "login-1",
                },
            ),
            scope,
        )
        await dispatcher.notification(
            JsonRpcNotification(
                method="notifications/message",
                params={"level": "info", "data": "hello"},
            ),
            scope,
        )
        await dispatcher.notification(
            JsonRpcNotification(method="notifications/tools/list_changed", params={}),
            scope,
        )
        return roots, sampled, elicited, sampler, elicitation, logs, changes

    roots, sampled, elicited, sampler, elicitation, logs, changes = asyncio.run(exercise())
    root_uri = roots.result["roots"][0]["uri"]
    assert root_uri == tmp_path.resolve().as_uri()
    assert sampled.result["model"] == "approved"
    assert elicited.result == {"action": "accept"}
    assert sampler.calls[0][0].principal_id == "user-1"
    assert elicitation.calls[0][0].product_id == "product-a"
    assert logs.calls[0][1] == "remote"
    assert changes.calls[0][2] == "tools"


class FakeGateway:
    def __init__(self) -> None:
        self.calls = []

    async def complete(self, route_id, request):  # type: ignore[no-untyped-def]
        self.calls.append((route_id, request))
        return ModelResponse(
            response_id="response-1",
            provider_id="provider-1",
            model="model-1",
            content=(ModelTextContent(text="answer"),),
            tool_calls=(ModelToolCall(call_id="call-1", name="lookup", arguments={}),),
            finish_reason=FinishReason.TOOL_CALLS,
            usage=ModelUsage(),
        )


def test_gateway_sampler_uses_fixed_root_route_and_supports_sampling_tools() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        gateway = FakeGateway()
        sampler = GatewayMcpSampler(gateway, route_id="mcp-approved")
        from suiteharness.mcp.callbacks import operation_context

        result = await sampler.create_message(
            operation_context(_scope()),
            CreateMessageParams(
                messages=(
                    SamplingMessage(role="user", content=TextContent(text="hello")),
                ),
                maxTokens=64,
                tools=(
                    SamplingTool(
                        name="lookup",
                        description="Lookup",
                        inputSchema={"type": "object"},
                    ),
                ),
            ),
        )
        return gateway, result

    gateway, result = asyncio.run(exercise())
    assert gateway.calls[0][0] == "mcp-approved"
    assert gateway.calls[0][1].tools[0].name == "lookup"
    assert [item.type for item in result.content] == ["text", "tool_use"]
    assert result.stop_reason == "toolUse"


def test_experimental_client_task_methods_are_context_bound() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        tasks = FakeTasks()
        capabilities = ClientCapabilities(
            tasks=TaskClientCapability(
                list=EmptyCapability(),
                cancel=EmptyCapability(),
                requests=ClientTaskRequests(
                    sampling=SamplingTaskRequests(create_message=EmptyCapability()),
                    elicitation=ElicitationTaskRequests(create=EmptyCapability()),
                ),
            )
        )
        dispatcher = McpClientRequestDispatcher(
            server_id="remote",
            tenant_id="tenant-a",
            product_id="product-a",
            capabilities=capabilities,
            features=McpFeatureFlags(experimental_tasks=True),
            tasks=tasks,
        )
        scope = _scope()
        listed = await dispatcher.request(
            JsonRpcRequest(id=1, method="tasks/list", params={}), scope
        )
        got = await dispatcher.request(
            JsonRpcRequest(id=2, method="tasks/get", params={"taskId": "task-1"}), scope
        )
        payload = await dispatcher.request(
            JsonRpcRequest(id=3, method="tasks/result", params={"taskId": "task-1"}), scope
        )
        cancelled = await dispatcher.request(
            JsonRpcRequest(id=4, method="tasks/cancel", params={"taskId": "task-1"}),
            scope,
        )
        return listed, got, payload, cancelled, tasks

    listed, got, payload, cancelled, tasks = asyncio.run(exercise())
    assert listed.result["tasks"][0]["taskId"] == "task-1"
    assert got.result["status"] == "working"
    assert payload.result["content"][0]["text"] == "done"
    assert cancelled.result["status"] == "cancelled"
    assert all(call[1].principal_id == "user-1" for call in tasks.calls)
