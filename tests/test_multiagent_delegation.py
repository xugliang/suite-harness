"""Controlled delegation must keep the authenticated parent's exact authority."""

import asyncio
import time
from dataclasses import replace

import pytest

from suiteharness.agents.prompts import DefaultPromptStrategy
from suiteharness.config import AgentRoleDefinition, MultiAgentConfig, MultiAgentProductConfig
from suiteharness.execution import AuditEventType, ToolEffect, ToolSpec, WorkflowDecision
from suiteharness.execution.in_memory import (
    InMemoryApprovalStore,
    InMemoryAuditJournal,
    InMemoryCapabilityAuthority,
    InMemoryToolRegistry,
)
from suiteharness.execution.protocols import ExecutionStrategies, ToolCallContext, Workflow
from suiteharness.execution.runner import ExecutionRunner
from suiteharness.multiagent.budget import AggregateRunBudget
from suiteharness.multiagent.roles import ConfiguredAgentRoleRegistry
from suiteharness.multiagent.service import (
    ControlledDelegationService,
    DelegationDenied,
    DisabledDelegationService,
    RoleRoutedWorkflow,
)
from suiteharness.multiagent.sqlite import SQLiteAgentTaskStore
from suiteharness.multiagent.tasks import AgentTaskStatus
from suiteharness.persistence import SQLiteDatabase
from suiteharness.runtime import RequestScope, RootContext, ScopePath


class _ReportChild(RoleRoutedWorkflow):
    def __init__(self, selected_route=None):
        self.selected_route = selected_route

    def with_role_route(self, role_id, model_route):
        return _ReportChild(model_route)

    async def next(self, request, frame, prompt):
        return WorkflowDecision.final({
            "selected_route": self.selected_route,
            "principal": request.scope.principal_id,
            "channel": request.scope.channel_id,
            "product": request.scope.product_id,
            "read_only": request.read_only,
            "tools": sorted(tool.name for tool in frame.available_tools),
            "role_instructions": [
                message.content for message in prompt.messages if message.role == "system"
            ],
        })


class _TestActivation:
    def __init__(self, product, runner):
        self._product = product
        self._runner = runner
        self.workflow = _ReportChild()
        self.child_request = None

    def product(self, product_id):
        if product_id != self._product.path.product_id:
            raise LookupError(product_id)
        return self._product

    def strategies(self, product_id):
        self.product(product_id)
        return ExecutionStrategies(workflow=self.workflow, prompt=DefaultPromptStrategy())

    async def run_trusted_child(self, request, strategies, aggregate_budget):
        self.child_request = request
        return await self._runner.run(request, strategies, aggregate_budget=aggregate_budget)


class _UnsupportedCustomWorkflow(Workflow):
    async def next(self, request, frame, prompt):
        return WorkflowDecision.final("custom workflow ignored the role model route")


def test_default_delegate_service_always_denies():
    async def check():
        with pytest.raises(DelegationDenied):
            await DisabledDelegationService().delegate(None, "reader", "Summarize")

    asyncio.run(check())


def test_child_uses_same_owner_channel_product_and_only_narrowed_read_tools():
    async def check():
        root = RootContext()
        product = root.tenant("tenant").product("product")
        parent_scope = RequestScope(
            path=ScopePath.agent("tenant", "product", "parent", "session"),
            principal_id="sales-one", channel_id="feishu", correlation_id="correlation-one",
        )
        registry = InMemoryToolRegistry()
        authority = InMemoryCapabilityAuthority()

        async def unused(context, arguments):
            return "unused"

        read = registry.register_protected(
            ToolSpec(name="read", effects=frozenset({ToolEffect.READ})), unused
        )
        write = registry.register_protected(
            ToolSpec(name="write", effects=frozenset({ToolEffect.WRITE})), unused
        )
        delegate = registry.register_protected(
            ToolSpec(name="agent.delegate", effects=frozenset({ToolEffect.READ})), unused
        )
        registry.register_protected(
            ToolSpec(name="secret", effects=frozenset({ToolEffect.READ})), unused
        )
        parent_grant = await authority.issue(
            parent_scope.path,
            tool_identities=(read.identity, write.identity, delegate.identity),
            principal_id="sales-one",
        )
        journal = InMemoryAuditJournal()
        runner = ExecutionRunner(
            tools=registry, capabilities=authority, approvals=InMemoryApprovalStore(),
            journal=journal,
        )
        activation = _TestActivation(product, runner)
        database = SQLiteDatabase(":memory:")
        store = SQLiteAgentTaskStore(database)
        role = AgentRoleDefinition(
            role_id="reader", instruction="Only summarize authorized data.",
            allowed_tools=("read",), model_route="chat",
        )
        denied_role = AgentRoleDefinition(
            role_id="secret-reader", instruction="Read the secret.",
            allowed_tools=("secret",), model_route="chat",
        )
        roles = ConfiguredAgentRoleRegistry(MultiAgentConfig(products={
            "product": MultiAgentProductConfig(
                enabled=True, roles=(role, denied_role), max_children=1, max_parallel=1,
            ),
        }))
        service = ControlledDelegationService(
            activation=activation, roles=roles, tools=registry,
            authority=authority, tasks=store, journal=journal,
        )
        pool = AggregateRunBudget(
            max_iterations=4, max_tool_calls=4, max_children=2,
            deadline_monotonic=time.monotonic() + 10,
        )
        context = ToolCallContext(
            scope=parent_scope, run_id="parent-run", call_id="delegate-one",
            tool_identity=delegate.identity,
            tool=registry.resolve(parent_scope, "agent.delegate").spec,
            remaining_seconds=10, grant_id=parent_grant.grant_id,
            read_only=True, aggregate_budget=pool,
        )
        result = await service.delegate(context, "reader", "Summarize the customer safely")
        assert result["status"] == "SUCCEEDED"
        assert result["output"]["tools"] == ["read"]
        assert result["output"]["selected_route"] == "chat"
        assert result["output"]["principal"] == "sales-one"
        assert result["output"]["channel"] == "feishu"
        assert result["output"]["product"] == "product"
        assert result["output"]["read_only"] is True
        assert result["output"]["role_instructions"][-1].startswith(
            "Only summarize authorized data."
        )
        assert activation.child_request.scope.correlation_id == "correlation-one"
        assert await authority.resolve(activation.child_request.grant_id) is None
        assert (
            await store.get_owned(parent_scope, result["task_id"], parent_run_id="parent-run")
        ).status.value == "SUCCEEDED"
        link = next(
            event for event in await journal.events(run_id="parent-run")
            if event.event_type is AuditEventType.AGENT_DELEGATED
        )
        assert link.call_id == "delegate-one"
        assert link.data["task_id"] == result["task_id"]
        assert link.data["child_run_id"] == activation.child_request.run_id
        original_child_run = activation.child_request.run_id
        replay = await service.delegate(
            context, "reader", "Summarize the customer safely"
        )
        assert replay == result
        assert activation.child_request.run_id == original_child_run
        assert len([
            event for event in await journal.events(run_id="parent-run")
            if event.event_type is AuditEventType.AGENT_DELEGATED
        ]) == 1
        cancelled = asyncio.Event()
        cancelled.set()
        with pytest.raises(DelegationDenied, match="cancelled"):
            await service.delegate(
                replace(context, cancel_signal=cancelled),
                "reader", "Summarize the customer safely",
            )
        narrower_grant = await authority.issue(
            parent_scope.path,
            tool_identities=(delegate.identity,),
            principal_id="sales-one",
        )
        with pytest.raises(DelegationDenied, match="conflict"):
            await service.delegate(
                replace(context, grant_id=narrower_grant.grant_id),
                "reader", "Summarize the customer safely",
            )
        with pytest.raises(DelegationDenied, match="conflict"):
            await service.delegate(
                replace(context, scope=replace(parent_scope, session_owner_id="another-owner")),
                "reader", "Summarize the customer safely",
            )
        with pytest.raises(DelegationDenied, match="conflict"):
            await service.delegate(
                replace(context, read_only=False),
                "reader", "Summarize the customer safely",
            )
        with pytest.raises(DelegationDenied, match="conflict"):
            await service.delegate(context, "reader", "A different task")
        with pytest.raises(DelegationDenied, match="child quota"):
            await service.delegate(
                replace(context, call_id="delegate-two"),
                "reader", "A second summary exceeds the product child ceiling",
            )
        secret_pool = AggregateRunBudget(
            max_iterations=4, max_tool_calls=4, max_children=2,
            deadline_monotonic=time.monotonic() + 10,
        )
        secret_result = await service.delegate(
            replace(
                context, run_id="secret-parent", call_id="secret-one",
                aggregate_budget=secret_pool,
            ),
            "secret-reader", "Read only the tools this parent may use",
        )
        assert secret_result["status"] == "SUCCEEDED"
        assert secret_result["output"]["tools"] == []
        activation.workflow = _UnsupportedCustomWorkflow()
        fresh_pool = AggregateRunBudget(
            max_iterations=4, max_tool_calls=4, max_children=2,
            deadline_monotonic=time.monotonic() + 10,
        )
        with pytest.raises(DelegationDenied, match="model route"):
            await service.delegate(
                replace(
                    context, run_id="custom-run", call_id="custom-one",
                    aggregate_budget=fresh_pool,
                ),
                "reader", "This custom workflow must choose its route",
            )
        await database.close()

    asyncio.run(check())


def test_product_parallel_limit_serializes_two_child_agents():
    async def check():
        root = RootContext()
        product = root.tenant("tenant").product("product")
        scope = RequestScope(
            path=ScopePath.agent("tenant", "product", "parent", "session"),
            principal_id="sales-one",
        )
        registry = InMemoryToolRegistry()
        authority = InMemoryCapabilityAuthority()

        async def unused(context, arguments):
            return "unused"

        delegate = registry.register_protected(
            ToolSpec(name="agent.delegate", effects=frozenset({ToolEffect.READ})), unused
        )
        grant = await authority.issue(
            scope.path, tool_identities=(delegate.identity,), principal_id="sales-one"
        )
        runner = ExecutionRunner(
            tools=registry, capabilities=authority, approvals=InMemoryApprovalStore(),
            journal=InMemoryAuditJournal(),
        )

        class BlockingActivation(_TestActivation):
            def __init__(self):
                super().__init__(product, runner)
                self.active = 0
                self.first_started = asyncio.Event()
                self.second_started = asyncio.Event()
                self.release = asyncio.Event()

            async def run_trusted_child(self, request, strategies, aggregate_budget):
                self.active += 1
                self.first_started.set()
                if self.active > 1:
                    self.second_started.set()
                try:
                    await self.release.wait()
                    return await super().run_trusted_child(request, strategies, aggregate_budget)
                finally:
                    self.active -= 1

        activation = BlockingActivation()
        database = SQLiteDatabase(":memory:")
        roles = ConfiguredAgentRoleRegistry(MultiAgentConfig(products={
            "product": MultiAgentProductConfig(
                enabled=True,
                roles=(AgentRoleDefinition(
                    role_id="reader", instruction="Read only.", model_route="chat",
                ),),
                max_children=2, max_parallel=1,
            ),
        }))
        service = ControlledDelegationService(
            activation=activation, roles=roles, tools=registry,
            authority=authority, tasks=SQLiteAgentTaskStore(database),
            journal=InMemoryAuditJournal(),
        )
        pool = AggregateRunBudget(
            max_iterations=4, max_tool_calls=4, max_children=2,
            deadline_monotonic=time.monotonic() + 10,
        )
        context = ToolCallContext(
            scope=scope, run_id="parent-run", call_id="one",
            tool_identity=delegate.identity,
            tool=registry.resolve(scope, "agent.delegate").spec,
            remaining_seconds=10, grant_id=grant.grant_id, aggregate_budget=pool,
        )
        first = asyncio.create_task(service.delegate(context, "reader", "First task"))
        await asyncio.wait_for(activation.first_started.wait(), 1)
        second = asyncio.create_task(service.delegate(
            replace(context, call_id="two"), "reader", "Second task"
        ))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(activation.second_started.wait(), 0.05)
        activation.release.set()
        results = await asyncio.gather(first, second)
        assert [item["status"] for item in results] == ["SUCCEEDED", "SUCCEEDED"]
        await database.close()

    asyncio.run(check())


def test_failed_child_scope_construction_terminalizes_queued_task():
    async def check():
        root = RootContext()
        real_product = root.tenant("tenant").product("product")
        scope = RequestScope(
            path=ScopePath.agent("tenant", "product", "parent", "session"),
            principal_id="sales-one",
        )
        registry = InMemoryToolRegistry()
        authority = InMemoryCapabilityAuthority()

        async def unused(context, arguments):
            return "unused"

        delegate = registry.register_protected(
            ToolSpec(name="agent.delegate", effects=frozenset({ToolEffect.READ})), unused
        )
        grant = await authority.issue(
            scope.path, tool_identities=(delegate.identity,), principal_id="sales-one"
        )

        class FailingProduct:
            path = real_product.path

            def agent(self, *args, **kwargs):
                raise RuntimeError("child effect unavailable")

        class FailingActivation:
            def product(self, product_id):
                return FailingProduct()

        database = SQLiteDatabase(":memory:")
        store = SQLiteAgentTaskStore(database)
        roles = ConfiguredAgentRoleRegistry(MultiAgentConfig(products={
            "product": MultiAgentProductConfig(
                enabled=True,
                roles=(AgentRoleDefinition(
                    role_id="reader", instruction="Read only.", model_route="chat",
                ),),
            ),
        }))
        service = ControlledDelegationService(
            activation=FailingActivation(), roles=roles, tools=registry,
            authority=authority, tasks=store,
            journal=InMemoryAuditJournal(),
        )
        pool = AggregateRunBudget(
            max_iterations=4, max_tool_calls=4, max_children=2,
            deadline_monotonic=time.monotonic() + 10,
        )
        context = ToolCallContext(
            scope=scope, run_id="parent-run", call_id="one",
            tool_identity=delegate.identity,
            tool=registry.resolve(scope, "agent.delegate").spec,
            remaining_seconds=10, grant_id=grant.grant_id, aggregate_budget=pool,
        )
        task = "Read the source"
        with pytest.raises(RuntimeError, match="child effect unavailable"):
            await service.delegate(context, "reader", task)
        record = await store.get_by_call(scope, "parent-run", "one")
        assert record is not None
        assert record.status is AgentTaskStatus.FAILED
        await database.close()

    asyncio.run(check())


def test_revoked_parent_grant_stops_waiting_child_before_it_runs():
    async def check():
        root = RootContext()
        product = root.tenant("tenant").product("product")
        scope = RequestScope(
            path=ScopePath.agent("tenant", "product", "parent", "session"),
            principal_id="sales-one",
        )
        registry = InMemoryToolRegistry()
        authority = InMemoryCapabilityAuthority()

        async def unused(context, arguments):
            return "unused"

        read = registry.register_protected(
            ToolSpec(name="read", effects=frozenset({ToolEffect.READ})), unused
        )
        delegate = registry.register_protected(
            ToolSpec(name="agent.delegate", effects=frozenset({ToolEffect.READ})), unused
        )
        grant = await authority.issue(
            scope.path, tool_identities=(read.identity, delegate.identity),
            principal_id="sales-one",
        )
        runner = ExecutionRunner(
            tools=registry, capabilities=authority, approvals=InMemoryApprovalStore(),
            journal=InMemoryAuditJournal(),
        )

        class WaitingActivation(_TestActivation):
            def __init__(self):
                super().__init__(product, runner)
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def run_trusted_child(self, request, strategies, aggregate_budget):
                self.started.set()
                await self.release.wait()
                return await super().run_trusted_child(request, strategies, aggregate_budget)

        activation = WaitingActivation()
        database = SQLiteDatabase(":memory:")
        roles = ConfiguredAgentRoleRegistry(MultiAgentConfig(products={
            "product": MultiAgentProductConfig(enabled=True, roles=(AgentRoleDefinition(
                role_id="reader", instruction="Only read.",
                allowed_tools=("read",), model_route="chat",
            ),)),
        }))
        service = ControlledDelegationService(
            activation=activation, roles=roles, tools=registry,
            authority=authority, tasks=SQLiteAgentTaskStore(database),
            journal=InMemoryAuditJournal(),
        )
        pool = AggregateRunBudget(
            max_iterations=4, max_tool_calls=4, max_children=2,
            deadline_monotonic=time.monotonic() + 10,
        )
        context = ToolCallContext(
            scope=scope, run_id="parent-run", call_id="delegate-one",
            tool_identity=delegate.identity,
            tool=registry.resolve(scope, "agent.delegate").spec,
            remaining_seconds=10, grant_id=grant.grant_id, aggregate_budget=pool,
        )
        pending = asyncio.create_task(service.delegate(context, "reader", "Read the source"))
        await asyncio.wait_for(activation.started.wait(), 1)
        assert await authority.revoke(grant.grant_id)
        activation.release.set()
        result = await asyncio.wait_for(pending, 1)
        assert result["status"] != "SUCCEEDED"

        workflow_started = asyncio.Event()
        workflow_release = asyncio.Event()

        class BlockingFinal(RoleRoutedWorkflow):
            def with_role_route(self, role_id, model_route):
                return self

            async def next(self, request, frame, prompt):
                workflow_started.set()
                await workflow_release.wait()
                return WorkflowDecision.final("model finished after parent revocation")

        activation.workflow = BlockingFinal()
        fresh_grant = await authority.issue(
            scope.path, tool_identities=(read.identity, delegate.identity),
            principal_id="sales-one",
        )
        fresh_pool = AggregateRunBudget(
            max_iterations=4, max_tool_calls=4, max_children=2,
            deadline_monotonic=time.monotonic() + 10,
        )
        late = asyncio.create_task(service.delegate(
            replace(
                context, run_id="second-parent", call_id="delegate-two",
                grant_id=fresh_grant.grant_id, aggregate_budget=fresh_pool,
            ),
            "reader", "Wait for a model answer",
        ))
        await asyncio.wait_for(workflow_started.wait(), 1)
        assert await authority.revoke(fresh_grant.grant_id)
        workflow_release.set()
        late_result = await asyncio.wait_for(late, 1)
        assert late_result["status"] != "SUCCEEDED"
        await database.close()

    asyncio.run(check())
