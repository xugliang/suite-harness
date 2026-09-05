from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import JsonValue

from suiteharness.execution import (
    ExecutionPolicy,
    ExecutionRunner,
    ExecutionStrategies,
    InMemoryApprovalStore,
    InMemoryAuditJournal,
    InMemoryCapabilityAuthority,
    InMemoryToolRegistry,
    RunRequest,
    RunStatus,
    ToolEffect,
    ToolIntent,
    WorkflowDecision,
)
from suiteharness.mcp.bridge import McpToolBridge, mcp_tool_spec
from suiteharness.mcp.client import McpClient
from suiteharness.mcp.manager import McpClientManager
from suiteharness.mcp.models import (
    ClientCapabilities,
    CompleteParams,
    CompletionArgument,
    CompletionReference,
    ElicitationCapability,
    LoggingLevel,
    McpCapabilityError,
    McpFeatureFlags,
    McpProtocolError,
    McpServerConfig,
    McpTool,
    McpToolEffectOverride,
    McpToolSecurityOverride,
    McpTransportKind,
    ProductMcpConfig,
    RootsCapability,
    SamplingCapability,
    TaskClientCapability,
    ToolAnnotations,
)
from suiteharness.runtime.scopes import RequestScope, ScopePath


def _scope(tenant: str = "tenant-a", product: str = "product-a") -> RequestScope:
    return RequestScope(
        path=ScopePath.agent(tenant, product, "agent-1", "session-1"),
        principal_id="user-1",
        purpose="mcp-test",
        request_id="request-1",
        correlation_id="correlation-1",
    )


class FullTransport:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, JsonValue] | None, RequestScope | None]] = []
        self.notifications = []
        self.closed = False
        self.request_handler = None

    async def start(self, request_handler, notification_handler):  # type: ignore[no-untyped-def]
        self.request_handler = request_handler
        self.notification_handler = notification_handler

    async def request(
        self, method, params, *, context, timeout_seconds  # type: ignore[no-untyped-def]
    ):
        self.requests.append((method, params, context))
        if method == "initialize":
            return {
                "protocolVersion": "2025-11-25",
                "capabilities": {
                    "tools": {"listChanged": True},
                    "resources": {"subscribe": True, "listChanged": True},
                    "prompts": {"listChanged": True},
                    "completions": {},
                    "logging": {},
                    "tasks": {"list": True, "cancel": True, "requests": {}},
                },
                "serverInfo": {"name": "test", "version": "1"},
            }
        if method == "tools/list":
            return {
                "tools": [
                    {
                        "name": "Read Everything!",
                        "description": "remote hint says read",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"q": {"type": "string"}},
                            "additionalProperties": False,
                        },
                        "annotations": {"readOnlyHint": True},
                    }
                ]
            }
        if method == "tools/call":
            return {
                "content": [{"type": "text", "text": "called"}],
                "structuredContent": {"arguments": params["arguments"]},
                "isError": False,
            }
        if method == "resources/list":
            return {"resources": []}
        if method == "resources/templates/list":
            return {"resourceTemplates": []}
        if method == "resources/read":
            return {"contents": [{"uri": params["uri"], "text": "resource"}]}
        if method in {"resources/subscribe", "resources/unsubscribe", "logging/setLevel", "ping"}:
            return {}
        if method == "prompts/list":
            return {"prompts": []}
        if method == "prompts/get":
            return {"messages": [{"role": "user", "content": {"type": "text", "text": "x"}}]}
        if method == "completion/complete":
            return {"completion": {"values": ["alpha"], "total": 1, "hasMore": False}}
        if method == "tasks/list":
            return {"tasks": []}
        if method in {"tasks/get", "tasks/cancel"}:
            return {
                "taskId": params["taskId"],
                "status": "working",
                "createdAt": "2026-09-04T00:00:00Z",
                "lastUpdatedAt": "2026-09-04T00:00:00Z",
                "ttl": None,
            }
        if method == "tasks/result":
            return {
                "task": {"taskId": params["taskId"], "status": "completed"},
                "result": {"ok": True},
            }
        raise AssertionError(f"unexpected method {method}")

    async def notify(self, method, params=None, *, context=None):  # type: ignore[no-untyped-def]
        self.notifications.append((method, params, context))

    async def close(self):  # type: ignore[no-untyped-def]
        self.closed = True


class FakeFactory:
    def __init__(self) -> None:
        self.created: list[tuple[str, str, str, FullTransport]] = []

    def create(
        self,
        config,
        *,
        tenant_id,
        product_id,
        product_workspace,
        resume=None,
    ):  # type: ignore[no-untyped-def]
        transport = FullTransport()
        self.created.append((tenant_id, product_id, config.server_id, transport))
        return transport


class PaginatingTransport(FullTransport):
    def __init__(self, *, tool_counts: tuple[int, ...], hang: bool = False) -> None:
        super().__init__()
        self._tool_counts = tool_counts
        self._hang = hang

    async def request(
        self, method, params, *, context, timeout_seconds  # type: ignore[no-untyped-def]
    ):
        if method != "tools/list":
            return await super().request(
                method, params, context=context, timeout_seconds=timeout_seconds
            )
        self.requests.append((method, params, context))
        if self._hang:
            await asyncio.Event().wait()
        page_index = 0 if "cursor" not in params else int(params["cursor"])
        count = self._tool_counts[page_index]
        result: dict[str, JsonValue] = {
            "tools": [
                {
                    "name": f"tool-{page_index}-{item_index}",
                    "inputSchema": {"type": "object"},
                }
                for item_index in range(count)
            ]
        }
        if page_index + 1 < len(self._tool_counts):
            result["nextCursor"] = str(page_index + 1)
        return result


def _server(*, tasks: bool = True) -> McpServerConfig:
    return McpServerConfig(
        server_id="remote",
        transport=McpTransportKind.STREAMABLE_HTTP,
        endpoint="https://mcp.example.com/rpc",
        resume_sessions=False,
        features=McpFeatureFlags(experimental_tasks=tasks),
    )


def _capabilities() -> ClientCapabilities:
    return ClientCapabilities(
        roots=RootsCapability(list_changed=True),
        sampling=SamplingCapability(tools=True),
        elicitation=ElicitationCapability(form=True),
        tasks=TaskClientCapability(list=True, cancel=True, requests=True),
    )


def _client(config: McpServerConfig, transport: FullTransport) -> McpClient:
    from suiteharness.mcp.callbacks import McpClientRequestDispatcher

    dispatcher = McpClientRequestDispatcher(
        server_id="remote",
        tenant_id="tenant-a",
        product_id="product-a",
        capabilities=_capabilities(),
        features=config.features,
    )
    return McpClient(
        config,
        tenant_id="tenant-a",
        product_id="product-a",
        transport=transport,
        dispatcher=dispatcher,
        capabilities=_capabilities(),
    )


@pytest.mark.parametrize(
    ("config_overrides", "tool_counts", "message", "expected_list_calls"),
    (
        ({"max_list_pages": 2}, (1, 1, 1), "max_list_pages", 2),
        ({"max_list_items": 2}, (3,), "max_list_items", 1),
    ),
)
def test_all_tools_fails_closed_when_discovery_budget_is_exceeded(
    config_overrides: dict[str, int],
    tool_counts: tuple[int, ...],
    message: str,
    expected_list_calls: int,
) -> None:
    async def exercise() -> tuple[PaginatingTransport, McpClient]:
        config = McpServerConfig(
            server_id="remote",
            transport=McpTransportKind.STREAMABLE_HTTP,
            endpoint="https://mcp.example.com/rpc",
            resume_sessions=False,
            **config_overrides,
        )
        transport = PaginatingTransport(tool_counts=tool_counts)
        client = _client(config, transport)
        await client.connect()
        with pytest.raises(McpProtocolError, match=message):
            await client.all_tools(_scope())
        return transport, client

    transport, client = asyncio.run(exercise())
    assert client.state.value == "ready"
    assert sum(method == "tools/list" for method, _, _ in transport.requests) == expected_list_calls


def test_all_tools_fails_closed_when_total_discovery_time_expires() -> None:
    async def exercise() -> None:
        config = McpServerConfig(
            server_id="remote",
            transport=McpTransportKind.STREAMABLE_HTTP,
            endpoint="https://mcp.example.com/rpc",
            resume_sessions=False,
            discovery_timeout_seconds=0.01,
        )
        client = _client(config, PaginatingTransport(tool_counts=(1,), hang=True))
        await client.connect()
        with pytest.raises(McpProtocolError, match="discovery_timeout_seconds"):
            await client.all_tools(_scope())

    asyncio.run(exercise())


def test_client_exposes_all_server_surfaces_and_context() -> None:
    async def exercise() -> FullTransport:
        transport = FullTransport()
        config = _server(tasks=True)
        from suiteharness.mcp.callbacks import McpClientRequestDispatcher

        dispatcher = McpClientRequestDispatcher(
            server_id="remote",
            tenant_id="tenant-a",
            product_id="product-a",
            capabilities=_capabilities(),
            features=config.features,
        )
        client = McpClient(
            config,
            tenant_id="tenant-a",
            product_id="product-a",
            transport=transport,
            dispatcher=dispatcher,
            capabilities=_capabilities(),
        )
        scope = _scope()
        await client.connect()
        await client.ping(scope)
        await client.list_tools(scope)
        await client.call_tool(scope, "Read Everything!", {"q": "x"})
        await client.list_resources(scope)
        await client.list_resource_templates(scope)
        await client.read_resource(scope, "data:test")
        await client.subscribe_resource(scope, "data:test")
        await client.unsubscribe_resource(scope, "data:test")
        await client.list_prompts(scope)
        await client.get_prompt(scope, "hello", arguments={"name": "X"})
        await client.complete(
            scope,
            CompleteParams(
                ref=CompletionReference(type="ref/prompt", name="hello"),
                argument=CompletionArgument(name="name", value="a"),
            ),
        )
        await client.set_logging_level(scope, LoggingLevel.INFO)
        await client.list_tasks(scope)
        await client.get_task(scope, "t1")
        await client.get_task_result(scope, "t1")
        await client.cancel_task(scope, "t1")
        await client.notify_progress(scope, progress_token="p1", progress=1)
        await client.cancel_request(scope, "r1")
        await client.notify_roots_changed(scope)
        return transport

    transport = asyncio.run(exercise())
    methods = [item[0] for item in transport.requests]
    assert methods == [
        "initialize",
        "ping",
        "tools/list",
        "tools/call",
        "resources/list",
        "resources/templates/list",
        "resources/read",
        "resources/subscribe",
        "resources/unsubscribe",
        "prompts/list",
        "prompts/get",
        "completion/complete",
        "logging/setLevel",
        "tasks/list",
        "tasks/get",
        "tasks/result",
        "tasks/cancel",
    ]
    assert all(context is not None for _, _, context in transport.requests[1:])


def test_experimental_tasks_are_policy_gated() -> None:
    async def exercise() -> None:
        transport = FullTransport()
        config = _server(tasks=False)
        from suiteharness.mcp.callbacks import McpClientRequestDispatcher

        dispatcher = McpClientRequestDispatcher(
            server_id="remote",
            tenant_id="tenant-a",
            product_id="product-a",
            capabilities=_capabilities(),
            features=config.features,
        )
        client = McpClient(
            config,
            tenant_id="tenant-a",
            product_id="product-a",
            transport=transport,
            dispatcher=dispatcher,
            capabilities=_capabilities(),
        )
        await client.connect()
        with pytest.raises(McpCapabilityError, match="disabled"):
            await client.list_tasks(_scope())

    asyncio.run(exercise())


def test_manager_never_crosses_product_connection(tmp_path: Path) -> None:
    async def exercise() -> tuple[McpClientManager, FakeFactory]:
        factory = FakeFactory()
        manager = McpClientManager(factory, client_capabilities=_capabilities())
        for product in ("product-a", "product-b"):
            root = tmp_path / product
            root.mkdir()
            manager.configure(
                ProductMcpConfig(
                    tenant_id="tenant-a",
                    product_id=product,
                    product_workspace=root,
                    servers=(_server(),),
                )
            )
            await manager.connect_product("tenant-a", product)
        assert manager.client(_scope(product="product-a"), "remote") is not manager.client(
            _scope(product="product-b"), "remote"
        )
        with pytest.raises(LookupError):
            manager.client(_scope(tenant="tenant-b"), "remote")
        return manager, factory

    manager, factory = asyncio.run(exercise())
    assert [(tenant, product) for tenant, product, _, _ in factory.created] == [
        ("tenant-a", "product-a"),
        ("tenant-a", "product-b"),
    ]
    assert set(manager.health("tenant-a", "product-a")) == {"remote"}


def test_remote_annotation_cannot_downgrade_effect() -> None:
    remote = McpTool(
        name="read",
        inputSchema={"type": "object"},
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    default = mcp_tool_spec("remote", remote)
    assert default.effects == frozenset({ToolEffect.WRITE, ToolEffect.EXTERNAL})
    overridden = mcp_tool_spec(
        "remote",
        remote,
        McpToolSecurityOverride(
            effect=McpToolEffectOverride.READ,
            rationale="administrator verified the implementation",
        ),
    )
    assert overridden.effects == frozenset({ToolEffect.READ, ToolEffect.EXTERNAL})


@pytest.mark.parametrize(
    "input_schema",
    (
        {"type": "string", "pattern": "(a+)+$"},
        {
            "type": "object",
            "patternProperties": {"(a+)+$": {"type": "string"}},
        },
        {"$ref": "#"},
        {
            "type": "object",
            "properties": {"value": {"$dynamicRef": "#node"}},
        },
        {
            "type": "array",
            "items": {"type": "object"},
            "uniqueItems": True,
        },
        {
            "type": "array",
            "contains": {"type": "string"},
        },
    ),
)
def test_remote_schema_rejects_unbounded_local_validation(
    input_schema: dict[str, JsonValue],
) -> None:
    remote = McpTool(name="unsafe", inputSchema=input_schema)
    with pytest.raises(McpProtocolError, match="inputSchema keyword"):
        mcp_tool_spec("remote", remote)


def test_remote_schema_allows_property_named_pattern_and_annotation_keywords() -> None:
    remote = McpTool(
        name="safe",
        inputSchema={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "format": "email",
                    "contentEncoding": "base64",
                }
            },
        },
    )
    assert mcp_tool_spec("remote", remote).input_schema == remote.input_schema


@pytest.mark.parametrize(
    "input_schema",
    (
        {"enum": list(range(257))},
        {"oneOf": [{"const": value} for value in range(17)]},
        {
            "type": "object",
            "properties": {f"field-{value}": {} for value in range(257)},
        },
    ),
)
def test_remote_schema_rejects_large_validation_collections(
    input_schema: dict[str, JsonValue],
) -> None:
    with pytest.raises(McpProtocolError, match="safe (collection|branch) limit"):
        mcp_tool_spec("remote", McpTool(name="too-complex", inputSchema=input_schema))


def test_remote_array_schema_is_capped_before_expensive_item_validation() -> None:
    original: dict[str, JsonValue] = {
        "type": "array",
        "items": {"type": "integer"},
        "maxItems": 100_000,
    }
    spec = mcp_tool_spec("remote", McpTool(name="bounded-array", inputSchema=original))
    assert spec.input_schema["maxItems"] == 4_096
    assert next(iter(spec.input_schema)) == "maxItems"
    assert original["maxItems"] == 100_000


class ScriptWorkflow:
    def __init__(self, alias: str) -> None:
        self._alias = alias
        self.calls = 0

    async def next(self, request, frame, prompt):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls == 1:
            return WorkflowDecision.tools(
                ToolIntent(call_id="mcp-call-1", tool_name=self._alias, arguments={"q": "x"})
            )
        return WorkflowDecision.final({"done": True})


def test_bridge_invocation_passes_through_execution_runner(tmp_path: Path) -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        factory = FakeFactory()
        manager = McpClientManager(factory, client_capabilities=_capabilities())
        manager.configure(
            ProductMcpConfig(
                tenant_id="tenant-a",
                product_id="product-a",
                product_workspace=tmp_path,
                servers=(_server(),),
            )
        )
        await manager.connect_product("tenant-a", "product-a")
        scope = _scope()
        registry = InMemoryToolRegistry()
        handle = await McpToolBridge(manager, registry).install(
            scope,
            "remote",
            registration_scope=ScopePath.product("tenant-a", "product-a"),
        )
        bridged = handle.tools[0]
        authority = InMemoryCapabilityAuthority()
        grant = await authority.issue(
            scope.path,
            tool_identities={bridged.identity},
            capabilities={"mcp.remote.call"},
            principal_id=scope.principal_id,
        )
        runner = ExecutionRunner(
            tools=registry,
            capabilities=authority,
            approvals=InMemoryApprovalStore(),
            journal=InMemoryAuditJournal(),
            policy=ExecutionPolicy(approval_effects=frozenset()),
        )
        result = await runner.run(
            RunRequest(
                run_id="run-1",
                scope=scope,
                grant_id=grant.grant_id,
                input={"message": "run remote"},
            ),
            ExecutionStrategies(workflow=ScriptWorkflow(bridged.alias)),
        )
        return result, factory.created[0][3], bridged

    result, transport, bridged = asyncio.run(exercise())
    assert result.status is RunStatus.SUCCEEDED
    assert result.observations[0].result["structuredContent"] == {"arguments": {"q": "x"}}
    remote_call = next(item for item in transport.requests if item[0] == "tools/call")
    assert remote_call[2].principal_id == "user-1"
    assert bridged.identity.name == bridged.alias
