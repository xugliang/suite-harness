"""A customer activation really wires parent ReAct to two bounded child ReAct runs."""

import asyncio

from suiteharness import __version__
from suiteharness.agents.react import ReActWorkflow
from suiteharness.config import LoadedSuiteHarnessConfig
from suiteharness.config.models import (
    AgentRoleDefinition,
    ChannelsConfig,
    DeploymentConfig,
    DirectFetchConfig,
    HtmlSearchConfig,
    LocalDevelopmentSandboxConfig,
    ModelProfileConfig,
    ModelRouteConfig,
    ModelsConfig,
    MultiAgentConfig,
    MultiAgentProductConfig,
    StorageConfig,
    SuiteHarnessConfig,
    SuiteHarnessSecrets,
    WebFetchConfig,
    WebSearchConfig,
    WebToolsConfig,
    WorkspaceConfig,
)
from suiteharness.execution import (
    WORKFLOW,
    AuditEventType,
    ExecutionBudget,
    RunRequest,
    RunStatus,
    ToolCallStatus,
    ToolEffect,
    ToolSpec,
)
from suiteharness.memory.in_memory import InMemoryProfileProvider
from suiteharness.memory.services import PROFILE_PROVIDER
from suiteharness.models import (
    FinishReason,
    ModelRole,
    ModelStreamEvent,
    ModelStreamEventType,
    ModelToolCall,
)
from suiteharness.runtime import RequestScope, ScopePath
from suiteharness.runtime.customer_bundle import (
    CustomerBundleManifest,
    ProductCatalog,
    resolve_customer_bundle,
)
from suiteharness.runtime.descriptor import EmptyProductConfig, ProductDescriptor
from suiteharness.runtime.kernel import PreparedProduct, ProductActivator
from suiteharness.server.access import ConfiguredGrantIssuer, ToolAccessTemplate
from suiteharness.server.foundation import FoundationRuntime


class _ScriptedGateway:
    """Only the external model boundary is replaced by a deterministic script."""

    def __init__(self):
        self.parent_requests = []
        self.child_requests = []

    async def stream(self, route_id, request):
        if route_id == "parent-chat":
            self.parent_requests.append(request)
            if len(self.parent_requests) == 1:
                assert {tool.name for tool in request.tools} == {
                    "agent.delegate", "customer.read",
                }
                for call_id, task in (
                    ("delegate-a", "读取客户线索甲"),
                    ("delegate-b", "读取客户线索乙"),
                ):
                    yield ModelStreamEvent(
                        type=ModelStreamEventType.TOOL_CALL_DELTA,
                        tool_call=ModelToolCall(
                            call_id=call_id,
                            name="agent.delegate",
                            arguments={"role_id": "reader", "task": task},
                        ),
                    )
                yield ModelStreamEvent(
                    type=ModelStreamEventType.MESSAGE_END,
                    finish_reason=FinishReason.TOOL_CALLS,
                )
                return
            yield ModelStreamEvent(
                type=ModelStreamEventType.TEXT_DELTA,
                text_delta="两条客户线索已经查阅。",
            )
            yield ModelStreamEvent(
                type=ModelStreamEventType.MESSAGE_END,
                finish_reason=FinishReason.STOP,
            )
            return

        assert route_id == "child-chat"
        self.child_requests.append(request)
        tool_messages = [
            message for message in request.messages if message.role is ModelRole.TOOL
        ]
        if not tool_messages:
            assert [tool.name for tool in request.tools] == ["customer.read"]
            task = next(
                part.text
                for message in request.messages if message.role is ModelRole.USER
                for part in message.content if hasattr(part, "text")
            )
            segment = "甲" if "甲" in task else "乙"
            yield ModelStreamEvent(
                type=ModelStreamEventType.TOOL_CALL_DELTA,
                tool_call=ModelToolCall(
                    call_id="read-a" if segment == "甲" else "read-b",
                    name="customer.read",
                    arguments={"segment": segment},
                ),
            )
            yield ModelStreamEvent(
                type=ModelStreamEventType.MESSAGE_END,
                finish_reason=FinishReason.TOOL_CALLS,
            )
            return
        yield ModelStreamEvent(
            type=ModelStreamEventType.TEXT_DELTA,
            text_delta="已查阅获授权的客户线索。",
        )
        yield ModelStreamEvent(
            type=ModelStreamEventType.MESSAGE_END,
            finish_reason=FinishReason.STOP,
        )


class _ReadProduct(ProductActivator):
    descriptor = ProductDescriptor(
        product_id="product", version="0.1.0", harness_api=">=0.1,<0.2",
        config_model=EmptyProductConfig,
    )

    def __init__(self, gateway):
        self._gateway = gateway

    async def prepare(self, product):
        async def install(view):
            async def read_customer(context, arguments):
                return {
                    "principal": context.scope.principal_id,
                    "product": context.scope.product_id,
                    "segment": arguments["segment"],
                }

            view.register_tool(
                ToolSpec(
                    name="customer.read",
                    effects=frozenset({ToolEffect.READ}),
                    input_schema={
                        "type": "object",
                        "properties": {"segment": {"enum": ["甲", "乙"]}},
                        "required": ["segment"],
                        "additionalProperties": False,
                    },
                ),
                read_customer,
            )

        return PreparedProduct(
            self.descriptor,
            product.config,
            {
                PROFILE_PROVIDER: InMemoryProfileProvider(),
                WORKFLOW: ReActWorkflow(self._gateway, "parent-chat"),
            },
            install=install,
        )


def _loaded(tmp_path):
    config = SuiteHarnessConfig(
        deployment=DeploymentConfig(
            environment="development", instance_id="instance", tenant_id="tenant",
        ),
        customer_bundle=CustomerBundleManifest.model_validate({
            "customer_bundle_id": "bundle", "version": "0.1.0",
            "harness_api": ">=0.1,<0.2",
            "products": [{"product_id": "product", "version": "==0.1.0"}],
        }),
        workspace=WorkspaceConfig(root=tmp_path / "workspace"),
        storage=StorageConfig(root=tmp_path / "state"),
        sandbox=LocalDevelopmentSandboxConfig(acknowledge_unsafe=True),
        models=ModelsConfig(
            profiles=(
                ModelProfileConfig(profile_id="local", provider_id="ollama", model="test"),
            ),
            routes=(
                ModelRouteConfig(route_id="parent-chat", primary_profile="local"),
                ModelRouteConfig(route_id="child-chat", primary_profile="local"),
            ),
            default_route="parent-chat",
        ),
        web_tools=WebToolsConfig(
            search=WebSearchConfig(
                default_provider="bing",
                providers=(HtmlSearchConfig(
                    provider_id="bing", engine="bing",
                    endpoint="https://www.bing.com/search",
                ),),
            ),
            fetch=WebFetchConfig(
                default_route="direct", routes=(DirectFetchConfig(),),
            ),
        ),
        multi_agent=MultiAgentConfig(products={
            "product": MultiAgentProductConfig(
                enabled=True,
                roles=(AgentRoleDefinition(
                    role_id="reader",
                    instruction="只读取本产品当前销售可访问的客户线索。",
                    allowed_tools=("customer.read",),
                    model_route="child-chat",
                ),),
                max_children=2,
                max_parallel=2,
            ),
        }),
        channels=ChannelsConfig(),
    )
    return LoadedSuiteHarnessConfig(
        config, SuiteHarnessSecrets(),
        tmp_path / "config.yaml", tmp_path / "secrets.yaml",
    )


def test_foundation_activation_two_children_return_tool_observations_to_parent(tmp_path):
    """Removing framework delegation wiring or child tool messages breaks this path."""

    async def check():
        gateway = _ScriptedGateway()
        product = _ReadProduct(gateway)
        foundation = FoundationRuntime(_loaded(tmp_path))
        scope = RequestScope(
            path=ScopePath.agent("tenant", "product", "parent", "session"),
            principal_id="seller-one", channel_id="web",
        )
        try:
            foundation.prepare_product_workspace("product")
            plan = resolve_customer_bundle(
                foundation.config.customer_bundle,
                ProductCatalog((product.descriptor,)),
                harness_version=__version__,
            )
            activation = await foundation.kernel.activate_customer_bundle(
                "tenant", plan, (product,),
            )
            await foundation.start_extensions(activation)
            issuer = ConfiguredGrantIssuer(
                registry=foundation.tools,
                authority=foundation.capabilities,
                templates=(ToolAccessTemplate(
                    channel_id="web", product_id="product",
                    read_aliases=("agent.delegate", "customer.read"),
                ),),
            )
            async with issuer.lease(scope) as issued:
                result = await activation.run(RunRequest(
                    run_id="parent-run", scope=scope,
                    grant_id=issued.grant_id,
                    read_only=issued.read_only,
                    input="请分别查阅两条客户线索",
                    budget=ExecutionBudget(
                        max_iterations=10, max_tool_calls=8,
                        max_parallel_calls=2, timeout_seconds=15,
                    ),
                ))
            assert result.status is RunStatus.SUCCEEDED
            assert result.output == "两条客户线索已经查阅。"
            assert len(result.observations) == 2
            assert {item.call_id for item in result.observations} == {
                "delegate-a", "delegate-b",
            }
            assert all(item.status is ToolCallStatus.SUCCEEDED for item in result.observations)
            assert all(item.result["status"] == "SUCCEEDED" for item in result.observations)
            assert len(gateway.parent_requests) == 2
            assert len(gateway.child_requests) == 4
            parent_tools = {
                message.tool_call_id: message
                for message in gateway.parent_requests[1].messages
                if message.role is ModelRole.TOOL
            }
            assert set(parent_tools) == {"delegate-a", "delegate-b"}
            assert all(message.name == "agent.delegate" for message in parent_tools.values())
            assert all("已查阅获授权的客户线索" in message.content[0].text
                       for message in parent_tools.values())
            child_tools = [
                message for request in gateway.child_requests
                for message in request.messages if message.role is ModelRole.TOOL
            ]
            assert len(child_tools) == 2
            assert {message.tool_call_id for message in child_tools} == {
                "read-a", "read-b",
            }
            assert all("seller-one" in message.content[0].text for message in child_tools)
            links = [
                event for event in await foundation.audit.events(run_id="parent-run")
                if event.event_type is AuditEventType.AGENT_DELEGATED
            ]
            assert {event.call_id for event in links} == {"delegate-a", "delegate-b"}
            assert all(event.data["child_run_id"] for event in links)
        finally:
            await foundation.close()

    asyncio.run(check())
