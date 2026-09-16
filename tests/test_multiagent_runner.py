"""The trusted runner must charge real child work against its parent pool."""

import asyncio
import time

from suiteharness.execution import (
    AuditEventType,
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
from suiteharness.execution.protocols import ExecutionStrategies, Workflow
from suiteharness.execution.runner import ExecutionRunner
from suiteharness.multiagent.budget import AggregateRunBudget
from suiteharness.multiagent.service import DelegationDenied
from suiteharness.runtime import RequestScope, ScopePath


class _Answer(Workflow):
    async def next(self, request, frame, prompt):
        return WorkflowDecision.final("ok")


def test_child_run_does_not_get_a_fresh_iteration_budget():
    async def check():
        authority = InMemoryCapabilityAuthority()
        parent_scope = RequestScope(
            path=ScopePath.agent("tenant", "product", "parent", "session"),
            principal_id="sales-one",
        )
        child_scope = RequestScope(
            path=ScopePath.agent("tenant", "product", "child", "child-session"),
            principal_id="sales-one",
        )
        parent_grant = await authority.issue(parent_scope.path, tool_identities=(), principal_id="sales-one")
        child_grant = await authority.issue(child_scope.path, tool_identities=(), principal_id="sales-one")
        runner = ExecutionRunner(
            tools=InMemoryToolRegistry(),
            capabilities=authority,
            approvals=InMemoryApprovalStore(),
            journal=InMemoryAuditJournal(),
        )
        budget = ExecutionBudget(max_iterations=1, max_tool_calls=0, timeout_seconds=10)
        parent = RunRequest(
            run_id="parent-run", scope=parent_scope, grant_id=parent_grant.grant_id,
            budget=budget, input="parent task",
        )
        child = RunRequest(
            run_id="child-run", scope=child_scope, grant_id=child_grant.grant_id,
            budget=budget, input="child task",
        )
        pool = AggregateRunBudget(
            max_iterations=1,
            max_tool_calls=0,
            max_children=2,
            deadline_monotonic=time.monotonic() + 10,
        )
        strategies = ExecutionStrategies(workflow=_Answer())
        parent_result = await runner.run(parent, strategies, aggregate_budget=pool)
        child_result = await runner.run(child, strategies, aggregate_budget=pool)
        assert parent_result.status is RunStatus.SUCCEEDED
        assert child_result.status is RunStatus.BUDGET_EXCEEDED
        assert pool.iterations_used == 1

    asyncio.run(check())


def test_trusted_tool_context_exposes_parent_grant_read_only_and_shared_budget():
    class _CallTool(Workflow):
        async def next(self, request, frame, prompt):
            if frame.observations:
                return WorkflowDecision.final(frame.observations[0].result)
            return WorkflowDecision.tools(ToolIntent(call_id="delegate-call", tool_name="inspect", arguments={}))

    async def check():
        registry = InMemoryToolRegistry()
        authority = InMemoryCapabilityAuthority()
        scope = RequestScope(
            path=ScopePath.agent("tenant", "product", "parent", "session"),
            principal_id="sales-one",
        )
        pool = AggregateRunBudget(
            max_iterations=2, max_tool_calls=1, max_children=2,
            deadline_monotonic=time.monotonic() + 10,
        )

        async def inspect(context, arguments):
            return {
                "grant_id": context.grant_id,
                "read_only": context.read_only,
                "same_pool": context.aggregate_budget is pool,
                "cancel_signal": isinstance(context.cancel_signal, asyncio.Event),
            }

        handle = registry.register(
            ScopePath.root(),
            ToolSpec(name="inspect", effects=frozenset({ToolEffect.READ}), input_schema={"type": "object"}),
            inspect,
        )
        grant = await authority.issue(
            scope.path, tool_identities=(handle.identity,), principal_id="sales-one"
        )
        runner = ExecutionRunner(
            tools=registry, capabilities=authority, approvals=InMemoryApprovalStore(),
            journal=InMemoryAuditJournal(),
        )
        request = RunRequest(
            run_id="parent-run", scope=scope, grant_id=grant.grant_id,
            budget=ExecutionBudget(max_iterations=2, max_tool_calls=1, timeout_seconds=10),
            read_only=True, input="inspect delegated rights",
        )
        result = await runner.run(request, ExecutionStrategies(workflow=_CallTool()), aggregate_budget=pool)
        assert result.status is RunStatus.SUCCEEDED
        assert result.output == {
            "grant_id": grant.grant_id, "read_only": True, "same_pool": True,
            "cancel_signal": True,
        }

    asyncio.run(check())


def test_role_authorization_failure_is_a_denied_tool_observation_and_audit_event():
    class _TryDelegate(Workflow):
        async def next(self, request, frame, prompt):
            if frame.observations:
                return WorkflowDecision.final("delegation was denied")
            return WorkflowDecision.tools(ToolIntent(
                call_id="delegate-one", tool_name="agent.delegate", arguments={},
            ))

    async def check():
        registry = InMemoryToolRegistry()
        authority = InMemoryCapabilityAuthority()
        journal = InMemoryAuditJournal()
        scope = RequestScope(
            path=ScopePath.agent("tenant", "product", "parent", "session"),
            principal_id="sales-one",
        )

        async def denied(context, arguments):
            raise DelegationDenied("unknown role")

        handle = registry.register_protected(
            ToolSpec(name="agent.delegate", effects=frozenset({ToolEffect.READ})), denied
        )
        grant = await authority.issue(
            scope.path, tool_identities=(handle.identity,), principal_id="sales-one"
        )
        runner = ExecutionRunner(
            tools=registry, capabilities=authority, approvals=InMemoryApprovalStore(),
            journal=journal,
        )
        result = await runner.run(RunRequest(
            run_id="parent-run", scope=scope, grant_id=grant.grant_id,
            input="delegate", budget=ExecutionBudget(max_tool_calls=1, max_iterations=2),
        ), ExecutionStrategies(workflow=_TryDelegate()))
        observation = result.observations[0]
        assert observation.status is ToolCallStatus.DENIED
        assert observation.failure.code is FailureCode.POLICY_DENIED
        assert any(
            event.event_type is AuditEventType.TOOL_DENIED
            for event in await journal.events(run_id="parent-run")
        )

    asyncio.run(check())
