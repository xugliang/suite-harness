"""Child runs re-enter the active product lease and the fixed runner."""

import asyncio
import time

import pytest

from suiteharness.execution import RunRequest, RunStatus, WorkflowDecision
from suiteharness.execution.in_memory import (
    InMemoryApprovalStore,
    InMemoryAuditJournal,
    InMemoryCapabilityAuthority,
    InMemoryToolRegistry,
)
from suiteharness.execution.protocols import WORKFLOW, ExecutionStrategies, Workflow
from suiteharness.execution.runner import ExecutionRunner
from suiteharness.multiagent.budget import AggregateRunBudget
from suiteharness.runtime import RequestScope, RootContext, ScopePath, ServiceBindings
from suiteharness.runtime.kernel import (
    ActivatedCustomerBundle,
    CustomerBundleActivationError,
    _RunLease,
)


class _Answer(Workflow):
    async def next(self, request, frame, prompt):
        return WorkflowDecision.final("child answer")


def test_trusted_child_entry_rechecks_active_product_and_uses_shared_pool():
    async def check():
        root = RootContext(bindings=ServiceBindings.root({WORKFLOW: _Answer()}))
        tenant = root.tenant("tenant")
        product = tenant.product("product")
        authority = InMemoryCapabilityAuthority()
        runner = ExecutionRunner(
            tools=InMemoryToolRegistry(), capabilities=authority,
            approvals=InMemoryApprovalStore(), journal=InMemoryAuditJournal(),
        )
        activation = ActivatedCustomerBundle(
            plan=None, tenant=tenant, products={"product": product}, runner=runner,
            _kernel=None, _activation_id="test", _run_leases={"product": _RunLease()},
        )
        child_scope = RequestScope(
            path=ScopePath.agent("tenant", "product", "child", "sub-session"),
            principal_id="sales-one",
        )
        grant = await authority.issue(
            child_scope.path, tool_identities=(), principal_id="sales-one"
        )
        request = RunRequest(
            run_id="child-run", scope=child_scope, grant_id=grant.grant_id,
            input="task",
        )
        pool = AggregateRunBudget(
            max_iterations=1, max_tool_calls=0, max_children=2,
            deadline_monotonic=time.monotonic() + 10,
        )
        result = await activation.run_trusted_child(
            request, ExecutionStrategies(workflow=_Answer()), pool
        )
        assert result.status is RunStatus.SUCCEEDED
        assert pool.iterations_used == 1
        foreign_scope = RequestScope(
            path=ScopePath.agent("tenant", "other", "child", "sub-session"),
            principal_id="sales-one",
        )
        foreign_grant = await authority.issue(
            foreign_scope.path, tool_identities=(), principal_id="sales-one"
        )
        with pytest.raises(CustomerBundleActivationError):
            await activation.run_trusted_child(
                request.model_copy(update={
                    "scope": foreign_scope, "grant_id": foreign_grant.grant_id,
                }),
                ExecutionStrategies(workflow=_Answer()), pool,
            )

    asyncio.run(check())
