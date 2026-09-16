"""Artificial business resources exercise the delegated run's trust boundary.

The framework supplies trusted scope and attenuated tools. Customer ownership is
deliberately decided by this test product's business authorizer, not by delegation.
"""

import asyncio
import time

from suiteharness.config import AgentRoleDefinition, MultiAgentConfig, MultiAgentProductConfig
from suiteharness.execution import (
    ExecutionBudget,
    FailureCode,
    RunRequest,
    RunStatus,
    ToolCallStatus,
    ToolEffect,
    ToolIntent,
    ToolSpec,
    WorkflowDecision,
)
from suiteharness.execution.in_memory import (
    InMemoryApprovalStore,
    InMemoryAuditJournal,
    InMemoryCapabilityAuthority,
    InMemoryToolRegistry,
)
from suiteharness.execution.protocols import ExecutionStrategies, ToolHandlerDenied, Workflow
from suiteharness.execution.runner import ExecutionRunner
from suiteharness.multiagent.budget import AggregateRunBudget
from suiteharness.multiagent.roles import ConfiguredAgentRoleRegistry
from suiteharness.multiagent.service import ControlledDelegationService, RoleRoutedWorkflow
from suiteharness.multiagent.sqlite import SQLiteAgentTaskStore
from suiteharness.multiagent.tool import install_delegate_tool
from suiteharness.persistence import SQLiteDatabase
from suiteharness.runtime import RequestScope, RootContext, ScopePath


class _BusinessResourceAuthorizer:
    """Artificial product decision over trusted scope and a resource owner."""

    def read(self, scope, resource):
        if (
            scope.product_id != resource["product_id"]
            or scope.principal_id != resource["principal_id"]
            or scope.effective_session_owner_id != resource["customer_id"]
        ):
            raise ToolHandlerDenied("resource is outside the business owner scope")
        return resource["value"]


class _DelegateOnce(Workflow):
    async def next(self, request, frame, prompt):
        if frame.observations:
            return WorkflowDecision.final(frame.observations[0].result)
        return WorkflowDecision.tools(
            ToolIntent(
                call_id="delegate-one",
                tool_name="agent.delegate",
                arguments={"role_id": "customer-reader", "task": "Probe customer boundaries"},
            )
        )


class _ChildBoundaryProbe(RoleRoutedWorkflow):
    def __init__(self, model_route=None):
        self.model_route = model_route

    def with_role_route(self, role_id, model_route):
        return _ChildBoundaryProbe(model_route)

    async def next(self, request, frame, prompt):
        if frame.iteration == 1:
            return WorkflowDecision.tools(
                ToolIntent(
                    call_id="own-read", tool_name="customer.read",
                    arguments={"resource_id": "a-own"},
                ),
                ToolIntent(
                    call_id="other-customer-read", tool_name="customer.read",
                    arguments={"resource_id": "a-other-customer"},
                ),
            )
        if frame.iteration == 2:
            return WorkflowDecision.tools(
                ToolIntent(
                    call_id="other-principal-read", tool_name="customer.read",
                    arguments={"resource_id": "a-other-principal"},
                ),
                ToolIntent(
                    call_id="foreign-product-read", tool_name="customer.read",
                    arguments={"resource_id": "b-own"},
                ),
            )
        if frame.iteration == 3:
            return WorkflowDecision.tools(
                ToolIntent(
                    call_id="attempt-write", tool_name="customer.write",
                    arguments={"resource_id": "a-own", "value": "tampered"},
                )
            )
        return WorkflowDecision.final({
            "principal_id": request.scope.principal_id,
            "session_owner_id": request.scope.effective_session_owner_id,
            "product_id": request.scope.product_id,
            "read_only": request.read_only,
            "model_route": self.model_route,
            "visible_tools": sorted(spec.name for spec in frame.available_tools),
        })


class _ChildActivation:
    def __init__(self, product, runner, authority):
        self.product_context = product
        self.runner = runner
        self.authority = authority
        self.child_request = None
        self.child_grant = None
        self.child_result = None

    def product(self, product_id):
        if product_id != self.product_context.path.product_id:
            raise LookupError(product_id)
        return self.product_context

    def strategies(self, product_id):
        self.product(product_id)
        return ExecutionStrategies(workflow=_ChildBoundaryProbe())

    async def run_trusted_child(self, request, strategies, aggregate_budget):
        self.child_request = request
        self.child_grant = await self.authority.resolve(request.grant_id)
        self.child_result = await self.runner.run(
            request, strategies, aggregate_budget=aggregate_budget
        )
        return self.child_result


def test_delegated_child_keeps_principal_customer_product_and_read_only_boundaries():
    """Removing scope, grant, read-only, or task-owner checks must fail this test."""

    async def check():
        root = RootContext()
        product_a = root.tenant("tenant").product("product-a")
        parent_scope = RequestScope(
            path=ScopePath.agent("tenant", "product-a", "parent", "session"),
            principal_id="seller-one", session_owner_id="customer-one",
            channel_id="company-channel", correlation_id="correlation-one",
        )
        registry = InMemoryToolRegistry()
        authority = InMemoryCapabilityAuthority()
        journal = InMemoryAuditJournal()
        runner = ExecutionRunner(
            tools=registry, capabilities=authority,
            approvals=InMemoryApprovalStore(), journal=journal,
        )
        database = SQLiteDatabase(":memory:")
        tasks = SQLiteAgentTaskStore(database)
        authorizer = _BusinessResourceAuthorizer()
        resources = {
            "a-own": {
                "product_id": "product-a", "principal_id": "seller-one",
                "customer_id": "customer-one", "value": "owned data",
            },
            "a-other-customer": {
                "product_id": "product-a", "principal_id": "seller-one",
                "customer_id": "customer-two", "value": "other customer data",
            },
            "a-other-principal": {
                "product_id": "product-a", "principal_id": "seller-two",
                "customer_id": "customer-one", "value": "other principal data",
            },
            "b-own": {
                "product_id": "product-b", "principal_id": "seller-one",
                "customer_id": "customer-one", "value": "other product data",
            },
        }
        writes = []
        product_b_reads = []

        async def read_a(context, arguments):
            return authorizer.read(context.scope, resources[arguments["resource_id"]])

        async def read_b(context, arguments):
            product_b_reads.append(arguments["resource_id"])
            return authorizer.read(context.scope, resources[arguments["resource_id"]])

        async def write_a(context, arguments):
            writes.append(arguments["resource_id"])
            resources[arguments["resource_id"]]["value"] = arguments["value"]
            return "written"

        read_spec = ToolSpec(
            name="customer.read", effects=frozenset({ToolEffect.READ}),
            input_schema={
                "type": "object", "properties": {"resource_id": {"type": "string"}},
                "required": ["resource_id"], "additionalProperties": False,
            },
        )
        write_spec = ToolSpec(
            name="customer.write", effects=frozenset({ToolEffect.WRITE}),
            input_schema={
                "type": "object",
                "properties": {
                    "resource_id": {"type": "string"}, "value": {"type": "string"},
                },
                "required": ["resource_id", "value"], "additionalProperties": False,
            },
        )
        read_a_handle = registry.register(product_a.path, read_spec, read_a)
        write_a_handle = registry.register(product_a.path, write_spec, write_a)
        read_b_handle = registry.register(
            ScopePath.product("tenant", "product-b"), read_spec, read_b
        )
        role = AgentRoleDefinition(
            role_id="customer-reader", instruction="Inspect only authorized customers.",
            allowed_tools=("customer.read", "customer.write"), model_route="chat",
        )
        roles = ConfiguredAgentRoleRegistry(MultiAgentConfig(products={
            "product-a": MultiAgentProductConfig(
                enabled=True, roles=(role,), max_children=2, max_parallel=2,
            )
        }))
        activation = _ChildActivation(product_a, runner, authority)
        service = ControlledDelegationService(
            activation=activation, roles=roles, tools=registry,
            authority=authority, tasks=tasks, journal=journal,
        )
        delegate_handle = install_delegate_tool(registry, service)
        parent_grant = await authority.issue(
            parent_scope.path,
            tool_identities=(
                delegate_handle.identity, read_a_handle.identity, write_a_handle.identity
            ),
            principal_id="seller-one",
        )
        pool = AggregateRunBudget(
            max_iterations=8, max_tool_calls=8, max_children=2,
            deadline_monotonic=time.monotonic() + 10,
        )
        parent_request = RunRequest(
            run_id="parent-run", scope=parent_scope, grant_id=parent_grant.grant_id,
            input="Delegate customer probe", read_only=True,
            budget=ExecutionBudget(
                max_iterations=8, max_tool_calls=8, max_parallel_calls=2,
                timeout_seconds=10,
            ),
        )
        try:
            parent_result = await runner.run(
                parent_request, ExecutionStrategies(workflow=_DelegateOnce()),
                aggregate_budget=pool,
            )
            assert parent_result.status is RunStatus.SUCCEEDED
            assert parent_result.output["status"] == "SUCCEEDED"
            assert activation.child_result.status is RunStatus.SUCCEEDED
            assert activation.child_request.scope.principal_id == "seller-one"
            assert activation.child_request.scope.effective_session_owner_id == "customer-one"
            assert activation.child_request.scope.channel_id == "company-channel"
            assert activation.child_request.scope.correlation_id == "correlation-one"
            assert activation.child_request.scope.product_id == "product-a"
            assert activation.child_request.read_only is True
            assert activation.child_grant.principal_id == "seller-one"
            assert activation.child_grant.product_id == "product-a"
            assert activation.child_grant.tool_identities == frozenset({
                read_a_handle.identity, write_a_handle.identity,
            })
            assert read_b_handle.identity not in activation.child_grant.tool_identities
            assert read_b_handle.identity != read_a_handle.identity
            assert registry.resolve(activation.child_request.scope, "customer.read").identity == read_a_handle.identity
            assert parent_result.output["output"] == {
                "principal_id": "seller-one", "session_owner_id": "customer-one",
                "product_id": "product-a", "read_only": True,
                "model_route": "chat", "visible_tools": ["customer.read"],
            }

            observations = {
                observation.call_id: observation
                for observation in activation.child_result.observations
            }
            assert observations["own-read"].status is ToolCallStatus.SUCCEEDED
            assert observations["own-read"].result == "owned data"
            for call_id in (
                "other-customer-read", "other-principal-read", "foreign-product-read"
            ):
                assert observations[call_id].status is ToolCallStatus.DENIED
                assert observations[call_id].failure.code is FailureCode.POLICY_DENIED
            assert observations["attempt-write"].status is ToolCallStatus.DENIED
            assert observations["attempt-write"].failure.code is FailureCode.READ_ONLY_DENIED
            assert writes == []
            assert product_b_reads == []
            assert resources["a-own"]["value"] == "owned data"

            task_id = parent_result.output["task_id"]
            assert await tasks.get_owned(parent_scope, task_id, parent_run_id="parent-run")
            foreign_principal = RequestScope(
                path=parent_scope.path, principal_id="seller-two",
                session_owner_id="customer-one",
            )
            foreign_product = RequestScope(
                path=ScopePath.agent("tenant", "product-b", "parent", "session"),
                principal_id="seller-one", session_owner_id="customer-one",
            )
            assert await tasks.get_owned(
                foreign_principal, task_id, parent_run_id="parent-run"
            ) is None
            assert await tasks.get_owned(
                foreign_product, task_id, parent_run_id="parent-run"
            ) is None
            assert await tasks.get_owned(
                parent_scope, task_id, parent_run_id="foreign-run"
            ) is None
        finally:
            await database.close()

    asyncio.run(check())
