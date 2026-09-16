"""A cancelled parent run must fence and stop its active delegated child."""

import asyncio

from suiteharness.agents.prompts import DefaultPromptStrategy
from suiteharness.config import AgentRoleDefinition, MultiAgentConfig, MultiAgentProductConfig
from suiteharness.execution import RunStatus, ToolEffect, ToolSpec, WorkflowDecision
from suiteharness.execution.in_memory import (
    InMemoryApprovalStore,
    InMemoryAuditJournal,
    InMemoryCapabilityAuthority,
    InMemoryToolRegistry,
)
from suiteharness.execution.models import ExecutionBudget, RunRequest, ToolIntent
from suiteharness.execution.protocols import ExecutionStrategies, Workflow
from suiteharness.execution.runner import ExecutionRunner
from suiteharness.multiagent.roles import ConfiguredAgentRoleRegistry
from suiteharness.multiagent.service import ControlledDelegationService, RoleRoutedWorkflow
from suiteharness.multiagent.sqlite import SQLiteAgentTaskStore
from suiteharness.multiagent.tasks import AgentTaskStatus
from suiteharness.persistence import SQLiteDatabase
from suiteharness.runtime import RequestScope, RootContext, ScopePath


class _ParentDelegatesOnce(Workflow):
    async def next(self, request, frame, prompt):
        if not frame.observations:
            return WorkflowDecision.tools(ToolIntent(
                call_id="delegation-one", tool_name="agent.delegate",
                arguments={"role_id": "reader", "task": "Read the source"},
            ))
        return WorkflowDecision.final(frame.observations[-1].result)


class _ChildFinishes(RoleRoutedWorkflow):
    def with_role_route(self, role_id, model_route):
        return self

    async def next(self, request, frame, prompt):
        return WorkflowDecision.final("child completed")


def test_cancelling_parent_fences_active_child_and_prevents_late_success():
    async def check():
        root = RootContext()
        product = root.tenant("tenant").product("product")
        scope = RequestScope(
            path=ScopePath.agent("tenant", "product", "parent", "session"),
            principal_id="sales-one", channel_id="web",
        )
        registry = InMemoryToolRegistry()
        authority = InMemoryCapabilityAuthority()
        database = SQLiteDatabase(":memory:")
        store = SQLiteAgentTaskStore(database)
        runner = ExecutionRunner(
            tools=registry, capabilities=authority, approvals=InMemoryApprovalStore(),
            journal=InMemoryAuditJournal(),
        )
        started = asyncio.Event()
        release = asyncio.Event()

        class BlockingActivation:
            child_request = None

            def product(self, product_id):
                assert product_id == "product"
                return product

            def strategies(self, product_id):
                assert product_id == "product"
                return ExecutionStrategies(
                    workflow=_ChildFinishes(), prompt=DefaultPromptStrategy()
                )

            async def run_trusted_child(self, request, strategies, aggregate_budget):
                self.child_request = request
                started.set()
                await release.wait()
                return await runner.run(request, strategies, aggregate_budget=aggregate_budget)

        activation = BlockingActivation()
        roles = ConfiguredAgentRoleRegistry(MultiAgentConfig(products={
            "product": MultiAgentProductConfig(enabled=True, roles=(AgentRoleDefinition(
                role_id="reader", instruction="Read only.", model_route="chat",
            ),)),
        }))
        service = ControlledDelegationService(
            activation=activation, roles=roles, tools=registry,
            authority=authority, tasks=store,
            journal=InMemoryAuditJournal(),
        )

        async def delegated(context, arguments):
            return await service.delegate(context, arguments["role_id"], arguments["task"])

        handle = registry.register_protected(
            ToolSpec(name="agent.delegate", effects=frozenset({ToolEffect.READ})), delegated
        )
        grant = await authority.issue(
            scope.path, tool_identities=(handle.identity,), principal_id="sales-one"
        )
        request = RunRequest(
            run_id="parent-run", scope=scope, grant_id=grant.grant_id,
            input="Please delegate", budget=ExecutionBudget(timeout_seconds=10),
        )
        parent = asyncio.create_task(runner.run(request, ExecutionStrategies(
            workflow=_ParentDelegatesOnce(), prompt=DefaultPromptStrategy(),
        )))
        await asyncio.wait_for(started.wait(), 2)
        assert await runner.cancel(scope, "parent-run")
        result = await asyncio.wait_for(parent, 2)
        assert result.status is RunStatus.CANCELLED
        record = await store.get_by_call(scope, "parent-run", "delegation-one")
        assert record is not None
        assert record.status is AgentTaskStatus.CANCELLED
        assert await authority.resolve(activation.child_request.grant_id) is None
        release.set()
        await database.close()

    asyncio.run(check())
