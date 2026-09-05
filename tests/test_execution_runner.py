"""Security invariants for the non-replaceable execution runner."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from jsonschema.exceptions import SchemaError
from pydantic import ValidationError

from suiteharness.execution import (
    WORKFLOW,
    ApprovalBinding,
    ApprovalTarget,
    ExecutionBudget,
    ExecutionPolicy,
    ExecutionRunner,
    ExecutionStrategies,
    FailureCode,
    InMemoryApprovalStore,
    InMemoryAuditJournal,
    InMemoryCapabilityAuthority,
    InMemoryToolRegistry,
    RunRequest,
    RunStatus,
    ToolCallStatus,
    ToolEffect,
    ToolIdentity,
    ToolIntent,
    ToolSpec,
    WorkflowDecision,
    arguments_digest,
)
from suiteharness.runtime.effects import EffectScope
from suiteharness.runtime.scopes import RequestScope, ScopeKind, ScopePath, ServiceBindings


class ScriptWorkflow:
    def __init__(self, decisions: Sequence[WorkflowDecision]) -> None:
        self.decisions = tuple(decisions)
        self.frames = []

    async def next(self, request, frame, prompt):  # type: ignore[no-untyped-def]
        self.frames.append(frame)
        return self.decisions[len(self.frames) - 1]


def _scope(product: str = "product-a", *, tenant: str = "tenant-1") -> RequestScope:
    path = ScopePath.agent(tenant, product, "agent-1", "session-1")
    return RequestScope(
        path=path,
        principal_id="user-1",
        purpose="execution-test",
        request_id="request-1",
        correlation_id="correlation-1",
    )


def _product_path(scope: RequestScope) -> ScopePath:
    return ScopePath.product(scope.tenant_id, scope.product_id)


def _tool_identity(scope: RequestScope, name: str) -> ToolIdentity:
    return ToolIdentity.scoped(_product_path(scope), name)


def _request(
    scope: RequestScope,
    grant_id: str,
    *,
    run_id: str = "run-1",
    read_only: bool = False,
    budget: ExecutionBudget | None = None,
) -> RunRequest:
    return RunRequest(
        run_id=run_id,
        scope=scope,
        grant_id=grant_id,
        input={"message": "go"},
        read_only=read_only,
        budget=budget or ExecutionBudget(),
    )


async def _harness(
    scope: RequestScope,
    *,
    tools: frozenset[str],
    capabilities: frozenset[str] = frozenset(),
    policy: ExecutionPolicy | None = None,
):  # type: ignore[no-untyped-def]
    registry = InMemoryToolRegistry()
    grants = InMemoryCapabilityAuthority()
    approvals = InMemoryApprovalStore()
    journal = InMemoryAuditJournal()
    grant = await grants.issue(
        scope.path,
        tool_identities=(_tool_identity(scope, name) for name in tools),
        capabilities=capabilities,
        principal_id=scope.principal_id,
    )
    runner = ExecutionRunner(
        tools=registry,
        capabilities=grants,
        approvals=approvals,
        journal=journal,
        policy=policy,
    )
    return runner, registry, grants, approvals, journal, grant


def test_product_sees_only_granted_tools_and_hidden_call_is_denied() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        scope = _scope()
        runner, registry, _, _, journal, grant = await _harness(
            scope, tools=frozenset({"product.visible"})
        )
        calls: list[str] = []

        async def visible(context, arguments):  # type: ignore[no-untyped-def]
            calls.append("visible")
            return {"ok": True}

        async def hidden(context, arguments):  # type: ignore[no-untyped-def]
            calls.append("hidden")
            return {"secret": True}

        registry.register(
            _product_path(scope),
            ToolSpec(name="product.visible", effects=frozenset({ToolEffect.READ})),
            visible,
        )
        registry.register(
            ScopePath.root(),
            ToolSpec(name="admin.hidden", effects=frozenset({ToolEffect.READ})),
            hidden,
        )
        workflow = ScriptWorkflow(
            (
                WorkflowDecision.tools(ToolIntent(call_id="call-1", tool_name="admin.hidden")),
                WorkflowDecision.final({"done": True}),
            )
        )
        result = await runner.run(
            _request(scope, grant.grant_id), ExecutionStrategies(workflow=workflow)
        )
        events = await journal.events(run_id="run-1")
        return result, workflow, calls, events

    result, workflow, calls, events = asyncio.run(exercise())
    assert result.status is RunStatus.SUCCEEDED
    assert [tool.name for tool in workflow.frames[0].available_tools] == ["product.visible"]
    assert result.observations[0].status is ToolCallStatus.DENIED
    assert result.observations[0].failure.code is FailureCode.TOOL_NOT_GRANTED
    assert calls == []
    serialized_events = "".join(event.model_dump_json() for event in events)
    assert "admin.hidden" in serialized_events
    assert '"arguments":' not in serialized_events
    assert all(event.principal_id == "user-1" for event in events)
    assert all(event.request_id == "request-1" for event in events)
    assert all(event.correlation_id == "correlation-1" for event in events)
    assert all(event.purpose == "execution-test" for event in events)


def test_same_tool_name_resolves_to_each_product() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        scope_a = _scope("product-a")
        scope_b = _scope("product-b")
        registry = InMemoryToolRegistry()
        grants = InMemoryCapabilityAuthority()
        approvals = InMemoryApprovalStore()
        journal = InMemoryAuditJournal()
        grant_a = await grants.issue(
            scope_a.path, tool_identities={_tool_identity(scope_a, "common.lookup")}
        )
        grant_b = await grants.issue(
            scope_b.path, tool_identities={_tool_identity(scope_b, "common.lookup")}
        )

        async def handler_a(context, arguments):  # type: ignore[no-untyped-def]
            return "A"

        async def handler_b(context, arguments):  # type: ignore[no-untyped-def]
            return "B"

        spec = ToolSpec(name="common.lookup", effects=frozenset({ToolEffect.READ}))
        registry.register(_product_path(scope_a), spec, handler_a)
        registry.register(_product_path(scope_b), spec, handler_b)
        runner = ExecutionRunner(
            tools=registry,
            capabilities=grants,
            approvals=approvals,
            journal=journal,
        )

        def workflow():
            return ScriptWorkflow(
                (
                    WorkflowDecision.tools(
                        ToolIntent(call_id="lookup-1", tool_name="common.lookup")
                    ),
                    WorkflowDecision.final("done"),
                )
            )

        result_a = await runner.run(
            _request(scope_a, grant_a.grant_id, run_id="run-a"),
            ExecutionStrategies(workflow=workflow()),
        )
        result_b = await runner.run(
            _request(scope_b, grant_b.grant_id, run_id="run-b"),
            ExecutionStrategies(workflow=workflow()),
        )
        return result_a, result_b

    result_a, result_b = asyncio.run(exercise())
    assert result_a.observations[0].result == "A"
    assert result_b.observations[0].result == "B"


def test_grant_cannot_cross_tenant_or_product_scope() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        scope_a = _scope("product-a", tenant="tenant-a")
        scope_b = _scope("product-b", tenant="tenant-b")
        runner, _, _, _, _, grant = await _harness(scope_a, tools=frozenset())
        workflow = ScriptWorkflow((WorkflowDecision.final("must-not-run"),))
        result = await runner.run(
            _request(scope_b, grant.grant_id),
            ExecutionStrategies(workflow=workflow),
        )
        return result, workflow

    result, workflow = asyncio.run(exercise())
    assert result.status is RunStatus.FAILED
    assert result.failure.code is FailureCode.GRANT_SCOPE_MISMATCH
    assert workflow.frames == []


def test_read_only_hides_and_blocks_every_non_read_tool() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        scope = _scope()
        runner, registry, _, _, _, grant = await _harness(
            scope, tools=frozenset({"data.read", "data.write"})
        )
        calls: list[str] = []

        async def read(context, arguments):  # type: ignore[no-untyped-def]
            calls.append("read")
            return 1

        async def write(context, arguments):  # type: ignore[no-untyped-def]
            calls.append("write")
            return 2

        registry.register(
            _product_path(scope),
            ToolSpec(name="data.read", effects=frozenset({ToolEffect.READ})),
            read,
        )
        registry.register(
            _product_path(scope),
            ToolSpec(name="data.write", effects=frozenset({ToolEffect.WRITE})),
            write,
        )
        workflow = ScriptWorkflow(
            (
                WorkflowDecision.tools(
                    ToolIntent(call_id="read-1", tool_name="data.read"),
                    ToolIntent(call_id="write-1", tool_name="data.write"),
                ),
                WorkflowDecision.final("done"),
            )
        )
        result = await runner.run(
            _request(scope, grant.grant_id, read_only=True),
            ExecutionStrategies(workflow=workflow),
        )
        return result, workflow, calls

    result, workflow, calls = asyncio.run(exercise())
    assert [spec.name for spec in workflow.frames[0].available_tools] == ["data.read"]
    assert [item.status for item in result.observations] == [
        ToolCallStatus.SUCCEEDED,
        ToolCallStatus.DENIED,
    ]
    assert result.observations[1].failure.code is FailureCode.READ_ONLY_DENIED
    assert calls == ["read"]


def test_approval_is_exact_single_use_and_never_confirms_future_calls() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        scope = _scope()
        runner, registry, _, approvals, journal, grant = await _harness(
            scope, tools=frozenset({"data.update"})
        )
        calls: list[dict] = []

        async def update(context, arguments):  # type: ignore[no-untyped-def]
            calls.append(arguments)
            return {"updated": True}

        registry.register(
            _product_path(scope),
            ToolSpec(name="data.update", effects=frozenset({ToolEffect.WRITE})),
            update,
        )
        approved_args = {"value": 1}
        issued_at = datetime.now(UTC)
        binding = ApprovalBinding(
            target=ApprovalTarget(
                scope=scope.path,
                principal_id=scope.principal_id,
                run_id="run-1",
                call_id="write-1",
                tool_identity=_tool_identity(scope, "data.update"),
                arguments_digest=arguments_digest(approved_args),
            ),
            approved_by="manager-1",
            issued_at=issued_at,
            expires_at=issued_at + timedelta(minutes=5),
        )
        credential = await approvals.issue(binding)
        token = credential.token.get_secret_value()
        workflow = ScriptWorkflow(
            (
                WorkflowDecision.tools(
                    ToolIntent(
                        call_id="write-1",
                        tool_name="data.update",
                        arguments=approved_args,
                        approval_token=token,
                    ),
                    ToolIntent(
                        call_id="write-2",
                        tool_name="data.update",
                        arguments=approved_args,
                        approval_token=token,
                    ),
                ),
                WorkflowDecision.final("done"),
            )
        )
        result = await runner.run(
            _request(
                scope,
                grant.grant_id,
                budget=ExecutionBudget(max_parallel_calls=1),
            ),
            ExecutionStrategies(workflow=workflow),
        )
        return result, calls, await journal.events(run_id="run-1"), token

    result, calls, events, token = asyncio.run(exercise())
    assert [item.status for item in result.observations] == [
        ToolCallStatus.SUCCEEDED,
        ToolCallStatus.DENIED,
    ]
    assert result.observations[1].failure.code is FailureCode.APPROVAL_INVALID
    assert calls == [{"value": 1}]
    started = [event for event in events if event.event_type.value == "tool_started"]
    assert started[0].data["approved_by"] == "manager-1"
    assert token not in "".join(event.model_dump_json() for event in events)


def test_approval_rejects_changed_arguments() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        scope = _scope()
        runner, registry, _, approvals, _, grant = await _harness(
            scope, tools=frozenset({"data.update"})
        )
        called = False

        async def update(context, arguments):  # type: ignore[no-untyped-def]
            nonlocal called
            called = True
            return True

        registry.register(
            _product_path(scope),
            ToolSpec(name="data.update", effects=frozenset({ToolEffect.WRITE})),
            update,
        )
        issued_at = datetime.now(UTC)
        credential = await approvals.issue(
            ApprovalBinding(
                target=ApprovalTarget(
                    scope=scope.path,
                    principal_id=scope.principal_id,
                    run_id="run-1",
                    call_id="write-1",
                    tool_identity=_tool_identity(scope, "data.update"),
                    arguments_digest=arguments_digest({"value": 1}),
                ),
                approved_by="manager-1",
                issued_at=issued_at,
                expires_at=issued_at + timedelta(minutes=5),
            )
        )
        workflow = ScriptWorkflow(
            (
                WorkflowDecision.tools(
                    ToolIntent(
                        call_id="write-1",
                        tool_name="data.update",
                        arguments={"value": 2},
                        approval_token=credential.token.get_secret_value(),
                    )
                ),
                WorkflowDecision.final("done"),
            )
        )
        result = await runner.run(
            _request(scope, grant.grant_id), ExecutionStrategies(workflow=workflow)
        )
        return result, called

    result, called = asyncio.run(exercise())
    assert result.observations[0].failure.code is FailureCode.APPROVAL_INVALID
    assert called is False


def test_capability_grant_is_resolved_again_before_each_call() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        scope = _scope()
        runner, registry, grants, _, _, grant = await _harness(
            scope, tools=frozenset({"data.read"})
        )
        calls = 0

        async def read(context, arguments):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            await grants.revoke(grant.grant_id)
            return calls

        registry.register(
            _product_path(scope),
            ToolSpec(name="data.read", effects=frozenset({ToolEffect.READ})),
            read,
        )
        workflow = ScriptWorkflow(
            (
                WorkflowDecision.tools(
                    ToolIntent(call_id="read-1", tool_name="data.read"),
                    ToolIntent(call_id="read-2", tool_name="data.read"),
                ),
                WorkflowDecision.final("done"),
            )
        )
        result = await runner.run(
            _request(
                scope,
                grant.grant_id,
                budget=ExecutionBudget(max_parallel_calls=1),
            ),
            ExecutionStrategies(workflow=workflow),
        )
        return result, calls

    result, calls = asyncio.run(exercise())
    assert calls == 1
    assert result.observations[0].status is ToolCallStatus.SUCCEEDED
    assert result.observations[1].status is ToolCallStatus.DENIED
    assert result.observations[1].failure.code is FailureCode.GRANT_NOT_FOUND


def test_batch_is_hard_capped_and_parallelism_is_bounded() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        scope = _scope()
        runner, registry, _, _, _, grant = await _harness(
            scope, tools=frozenset({"data.read"})
        )
        current = 0
        peak = 0
        calls = 0
        lock = asyncio.Lock()

        async def read(context, arguments):  # type: ignore[no-untyped-def]
            nonlocal current, peak, calls
            async with lock:
                current += 1
                calls += 1
                peak = max(peak, current)
            await asyncio.sleep(0.02)
            async with lock:
                current -= 1
            return arguments["n"]

        registry.register(
            _product_path(scope),
            ToolSpec(name="data.read", effects=frozenset({ToolEffect.READ})),
            read,
        )
        workflow = ScriptWorkflow(
            (
                WorkflowDecision.tools(
                    *(
                        ToolIntent(
                            call_id=f"read-{index}",
                            tool_name="data.read",
                            arguments={"n": index},
                        )
                        for index in range(4)
                    )
                ),
            )
        )
        result = await runner.run(
            _request(
                scope,
                grant.grant_id,
                budget=ExecutionBudget(
                    max_iterations=2,
                    max_tool_calls=2,
                    max_parallel_calls=2,
                ),
            ),
            ExecutionStrategies(workflow=workflow),
        )
        return result, calls, peak

    result, calls, peak = asyncio.run(exercise())
    assert result.status is RunStatus.BUDGET_EXCEEDED
    assert result.failure.code is FailureCode.TOOL_BUDGET_EXCEEDED
    assert result.tool_calls_used == 2
    assert calls == 2
    assert peak == 2
    assert [item.status for item in result.observations] == [
        ToolCallStatus.SUCCEEDED,
        ToolCallStatus.SUCCEEDED,
        ToolCallStatus.BUDGET_EXCEEDED,
        ToolCallStatus.BUDGET_EXCEEDED,
    ]


def test_timeout_cancels_inflight_handler_and_returns_structured_result() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        scope = _scope()
        runner, registry, _, _, _, grant = await _harness(
            scope, tools=frozenset({"data.slow"})
        )
        stopped = asyncio.Event()

        async def slow(context, arguments):  # type: ignore[no-untyped-def]
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        registry.register(
            _product_path(scope),
            ToolSpec(name="data.slow", effects=frozenset({ToolEffect.READ})),
            slow,
        )
        workflow = ScriptWorkflow(
            (
                WorkflowDecision.tools(ToolIntent(call_id="slow-1", tool_name="data.slow")),
            )
        )
        result = await runner.run(
            _request(
                scope,
                grant.grant_id,
                budget=ExecutionBudget(timeout_seconds=0.05),
            ),
            ExecutionStrategies(workflow=workflow),
        )
        return result, stopped.is_set()

    result, stopped = asyncio.run(exercise())
    assert result.status is RunStatus.TIMED_OUT
    assert result.failure.code is FailureCode.TIMED_OUT
    assert result.tool_calls_used == 1
    assert stopped is True


def test_explicit_cancel_stops_inflight_handler() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        scope = _scope()
        runner, registry, _, _, _, grant = await _harness(
            scope, tools=frozenset({"data.slow"})
        )
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def slow(context, arguments):  # type: ignore[no-untyped-def]
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        registry.register(
            _product_path(scope),
            ToolSpec(name="data.slow", effects=frozenset({ToolEffect.READ})),
            slow,
        )
        workflow = ScriptWorkflow(
            (
                WorkflowDecision.tools(ToolIntent(call_id="slow-1", tool_name="data.slow")),
            )
        )
        task = asyncio.create_task(
            runner.run(
                _request(scope, grant.grant_id),
                ExecutionStrategies(workflow=workflow),
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        assert await runner.cancel(scope, "run-1") is True
        result = await asyncio.wait_for(task, timeout=1)
        return result, stopped.is_set(), await runner.cancel(scope, "run-1")

    result, stopped, still_active = asyncio.run(exercise())
    assert result.status is RunStatus.CANCELLED
    assert result.failure.code is FailureCode.CANCELLED
    assert stopped is True
    assert still_active is False


def test_run_identity_and_cancel_are_isolated_by_full_authenticated_scope() -> None:
    class BlockingWorkflow:
        def __init__(self, started: asyncio.Event) -> None:
            self.started = started

        async def next(self, request, frame, prompt):  # type: ignore[no-untyped-def]
            self.started.set()
            await asyncio.Event().wait()

    async def exercise():  # type: ignore[no-untyped-def]
        scope_a = _scope(tenant="tenant-a")
        scope_b = _scope(tenant="tenant-b")
        impostor = RequestScope(
            path=scope_a.path,
            principal_id="different-user",
            purpose="execution-test",
        )
        registry = InMemoryToolRegistry()
        grants = InMemoryCapabilityAuthority()
        approvals = InMemoryApprovalStore()
        journal = InMemoryAuditJournal()
        runner = ExecutionRunner(
            tools=registry,
            capabilities=grants,
            approvals=approvals,
            journal=journal,
        )
        grant_a = await grants.issue(
            scope_a.path, tool_identities=(), principal_id=scope_a.principal_id
        )
        grant_b = await grants.issue(
            scope_b.path, tool_identities=(), principal_id=scope_b.principal_id
        )
        started_a = asyncio.Event()
        started_b = asyncio.Event()
        task_a = asyncio.create_task(
            runner.run(
                _request(scope_a, grant_a.grant_id, run_id="same-run"),
                ExecutionStrategies(workflow=BlockingWorkflow(started_a)),
            )
        )
        task_b = asyncio.create_task(
            runner.run(
                _request(scope_b, grant_b.grant_id, run_id="same-run"),
                ExecutionStrategies(workflow=BlockingWorkflow(started_b)),
            )
        )
        await asyncio.wait_for(asyncio.gather(started_a.wait(), started_b.wait()), timeout=1)
        assert await runner.cancel(impostor, "same-run") is False
        assert await runner.cancel(scope_a, "same-run") is True
        result_a = await asyncio.wait_for(task_a, timeout=1)
        assert task_b.done() is False
        assert await runner.cancel(scope_b, "same-run") is True
        result_b = await asyncio.wait_for(task_b, timeout=1)
        return result_a, result_b

    result_a, result_b = asyncio.run(exercise())
    assert result_a.status is RunStatus.CANCELLED
    assert result_b.status is RunStatus.CANCELLED


def test_reflection_cannot_bypass_post_strategy_authorization() -> None:
    class MaliciousReflection:
        async def review(self, request, frame, decision):  # type: ignore[no-untyped-def]
            if frame.iteration == 1:
                return WorkflowDecision.tools(
                    ToolIntent(call_id="hidden-1", tool_name="admin.hidden")
                )
            return decision

    async def exercise():  # type: ignore[no-untyped-def]
        scope = _scope()
        runner, registry, _, _, _, grant = await _harness(scope, tools=frozenset())
        called = False

        async def hidden(context, arguments):  # type: ignore[no-untyped-def]
            nonlocal called
            called = True
            return True

        registry.register(
            ScopePath.root(),
            ToolSpec(name="admin.hidden", effects=frozenset({ToolEffect.READ})),
            hidden,
        )
        workflow = ScriptWorkflow(
            (WorkflowDecision.final("safe"), WorkflowDecision.final("safe"))
        )
        result = await runner.run(
            _request(scope, grant.grant_id),
            ExecutionStrategies(workflow=workflow, reflection=MaliciousReflection()),
        )
        return result, called

    result, called = asyncio.run(exercise())
    assert result.status is RunStatus.SUCCEEDED
    assert result.observations[0].failure.code is FailureCode.TOOL_NOT_GRANTED
    assert called is False


def test_workflow_is_product_overridable_through_scoped_bindings() -> None:
    root_workflow = ScriptWorkflow((WorkflowDecision.final("root"),))
    product_workflow = ScriptWorkflow((WorkflowDecision.final("product"),))
    root = ServiceBindings.root({WORKFLOW: root_workflow})
    tenant = root.derive(ScopeKind.TENANT)
    product = tenant.derive(ScopeKind.PRODUCT, {WORKFLOW: product_workflow})

    assert ExecutionStrategies.from_bindings(root).workflow is root_workflow
    assert ExecutionStrategies.from_bindings(product).workflow is product_workflow
    with pytest.raises(TypeError):
        tenant.derive(ScopeKind.PRODUCT, {WORKFLOW: object()})


def test_invalid_strategy_result_and_audit_failure_are_structured() -> None:
    class InvalidWorkflow:
        async def next(self, request, frame, prompt):  # type: ignore[no-untyped-def]
            return {"kind": "final"}

    class BrokenJournal:
        async def append(self, event):  # type: ignore[no-untyped-def]
            raise OSError("journal unavailable")

    async def exercise():  # type: ignore[no-untyped-def]
        scope = _scope()
        runner, _, grants, approvals, _, grant = await _harness(scope, tools=frozenset())
        invalid = await runner.run(
            _request(scope, grant.grant_id),
            ExecutionStrategies(workflow=InvalidWorkflow()),
        )
        broken = ExecutionRunner(
            tools=InMemoryToolRegistry(),
            capabilities=grants,
            approvals=approvals,
            journal=BrokenJournal(),
        )
        audit_failure = await broken.run(
            _request(scope, grant.grant_id, run_id="run-audit-failure"),
            ExecutionStrategies(workflow=ScriptWorkflow((WorkflowDecision.final("ok"),))),
        )
        return invalid, audit_failure

    invalid, audit_failure = asyncio.run(exercise())
    assert invalid.status is RunStatus.FAILED
    assert invalid.failure.code is FailureCode.WORKFLOW_ERROR
    assert audit_failure.status is RunStatus.FAILED
    assert audit_failure.failure.code is FailureCode.INTERNAL_ERROR


def test_in_memory_journal_returns_detached_events() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        scope = _scope()
        runner, _, _, _, journal, grant = await _harness(scope, tools=frozenset())
        await runner.run(
            _request(scope, grant.grant_id),
            ExecutionStrategies(
                workflow=ScriptWorkflow((WorkflowDecision.final({"ok": True}),))
            ),
        )
        first = await journal.events(run_id="run-1")
        first[0].data["tampered"] = True
        second = await journal.events(run_id="run-1")
        return second

    events = asyncio.run(exercise())
    assert all("tampered" not in event.data for event in events)


def test_json_schema_is_checked_at_registration_and_before_handler() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        scope = _scope()
        runner, registry, _, _, _, grant = await _harness(
            scope, tools=frozenset({"math.increment"})
        )
        called = False

        async def increment(context, arguments):  # type: ignore[no-untyped-def]
            nonlocal called
            called = True
            return arguments["count"] + 1

        registry.register(
            _product_path(scope),
            ToolSpec(
                name="math.increment",
                effects=frozenset({ToolEffect.READ}),
                input_schema={
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                    "required": ["count"],
                    "additionalProperties": False,
                },
            ),
            increment,
        )
        workflow = ScriptWorkflow(
            (
                WorkflowDecision.tools(
                    ToolIntent(
                        call_id="increment-1",
                        tool_name="math.increment",
                        arguments={"count": "one"},
                    )
                ),
                WorkflowDecision.final("done"),
            )
        )
        result = await runner.run(
            _request(scope, grant.grant_id), ExecutionStrategies(workflow=workflow)
        )
        return result, called

    registry = InMemoryToolRegistry()

    async def unused(context, arguments):  # type: ignore[no-untyped-def]
        return None

    with pytest.raises(SchemaError):
        registry.register(
            ScopePath.root(),
            ToolSpec(
                name="invalid.schema",
                effects=frozenset({ToolEffect.READ}),
                input_schema={"type": "not-a-json-schema-type"},
            ),
            unused,
        )
    with pytest.raises(SchemaError):
        registry.register(
            ScopePath.root(),
            ToolSpec(
                name="remote.schema",
                effects=frozenset({ToolEffect.READ}),
                input_schema={"$ref": "https://example.invalid/schema.json"},
            ),
            unused,
        )

    result, called = asyncio.run(exercise())
    assert result.observations[0].status is ToolCallStatus.DENIED
    assert result.observations[0].failure.code is FailureCode.TOOL_INPUT_INVALID
    assert called is False


def test_approval_cannot_cross_requesting_principal() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        scope = _scope()
        runner, registry, grants, approvals, _, grant = await _harness(
            scope, tools=frozenset({"data.update"})
        )
        approved_scope = RequestScope(
            path=scope.path,
            principal_id="different-user",
            purpose="execution-test",
        )
        approved_grant = await grants.issue(
            approved_scope.path,
            tool_identities={_tool_identity(scope, "data.update")},
            principal_id=approved_scope.principal_id,
        )
        calls: list[str] = []

        async def update(context, arguments):  # type: ignore[no-untyped-def]
            calls.append(context.scope.principal_id)
            return True

        registry.register(
            _product_path(scope),
            ToolSpec(name="data.update", effects=frozenset({ToolEffect.WRITE})),
            update,
        )
        issued_at = datetime.now(UTC)
        credential = await approvals.issue(
            ApprovalBinding(
                target=ApprovalTarget(
                    scope=scope.path,
                    principal_id="different-user",
                    run_id="run-1",
                    call_id="write-1",
                    tool_identity=_tool_identity(scope, "data.update"),
                    arguments_digest=arguments_digest({"value": 1}),
                ),
                approved_by="manager-1",
                issued_at=issued_at,
                expires_at=issued_at + timedelta(minutes=5),
            )
        )
        workflow = ScriptWorkflow(
            (
                WorkflowDecision.tools(
                    ToolIntent(
                        call_id="write-1",
                        tool_name="data.update",
                        arguments={"value": 1},
                        approval_token=credential.token.get_secret_value(),
                    )
                ),
                WorkflowDecision.final("done"),
            )
        )
        result = await runner.run(
            _request(scope, grant.grant_id), ExecutionStrategies(workflow=workflow)
        )
        approved_workflow = ScriptWorkflow(
            (
                WorkflowDecision.tools(
                    ToolIntent(
                        call_id="write-1",
                        tool_name="data.update",
                        arguments={"value": 1},
                        approval_token=credential.token.get_secret_value(),
                    )
                ),
                WorkflowDecision.final("done"),
            )
        )
        approved_result = await runner.run(
            _request(approved_scope, approved_grant.grant_id),
            ExecutionStrategies(workflow=approved_workflow),
        )
        return result, approved_result, calls

    result, approved_result, calls = asyncio.run(exercise())
    assert result.observations[0].failure.code is FailureCode.APPROVAL_INVALID
    assert approved_result.observations[0].status is ToolCallStatus.SUCCEEDED
    assert calls == ["different-user"]


def test_destructive_approval_cannot_be_disabled_by_host_policy() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        scope = _scope()
        runner, registry, _, _, _, grant = await _harness(
            scope,
            tools=frozenset({"data.destroy"}),
            policy=ExecutionPolicy(approval_effects=frozenset()),
        )
        called = False

        async def destroy(context, arguments):  # type: ignore[no-untyped-def]
            nonlocal called
            called = True
            return True

        registry.register(
            _product_path(scope),
            ToolSpec(
                name="data.destroy",
                effects=frozenset({ToolEffect.WRITE, ToolEffect.DESTRUCTIVE}),
            ),
            destroy,
        )
        workflow = ScriptWorkflow(
            (
                WorkflowDecision.tools(
                    ToolIntent(call_id="destroy-1", tool_name="data.destroy")
                ),
                WorkflowDecision.final("done"),
            )
        )
        result = await runner.run(
            _request(scope, grant.grant_id), ExecutionStrategies(workflow=workflow)
        )
        return result, called

    result, called = asyncio.run(exercise())
    assert result.observations[0].failure.code is FailureCode.APPROVAL_REQUIRED
    assert called is False


def test_owned_tool_registration_unloads_only_its_exact_scope() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        scope = _scope()
        registry = InMemoryToolRegistry()

        async def root_handler(context, arguments):  # type: ignore[no-untyped-def]
            return "root"

        async def product_handler(context, arguments):  # type: ignore[no-untyped-def]
            return "product"

        spec = ToolSpec(name="common.lookup", effects=frozenset({ToolEffect.READ}))
        root_registration = registry.register(ScopePath.root(), spec, root_handler)
        effects = EffectScope("product-tools")
        product_registration = registry.register_owned(
            _product_path(scope), effects, spec, product_handler
        )

        assert registry.resolve(scope, spec.name).handler is product_handler
        assert (
            registry.unregister(ScopePath.root(), spec.name, owner=product_registration)
            is False
        )
        assert root_registration.closed is False
        await effects.close()
        assert product_registration.closed is True
        assert registry.resolve(scope, spec.name).handler is root_handler
        root_registration.close()
        return registry.resolve(scope, spec.name), root_registration.closed

    remaining, root_closed = asyncio.run(exercise())
    assert remaining is None
    assert root_closed is True


def test_protected_root_tool_alias_cannot_be_shadowed() -> None:
    registry = InMemoryToolRegistry()

    async def handler(context, arguments):  # type: ignore[no-untyped-def]
        return True

    spec = ToolSpec(name="suiteharness.fs.read", effects=frozenset({ToolEffect.READ}))
    registration = registry.register_protected(spec, handler)

    assert registration.identity.namespace == "suiteharness"
    assert registration.identity.name == spec.name
    assert registration.identity.canonical_id.startswith("tool:sha256:")
    with pytest.raises(ValueError, match="cannot be shadowed"):
        registry.register(ScopePath.tenant("tenant-1"), spec, handler)
    with pytest.raises(ValueError, match="cannot be shadowed"):
        registry.register(ScopePath.product("tenant-1", "product-a"), spec, handler)


def test_identity_grant_cannot_authorize_same_name_shadow() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        scope = _scope()
        registry = InMemoryToolRegistry()
        grants = InMemoryCapabilityAuthority()
        approvals = InMemoryApprovalStore()
        journal = InMemoryAuditJournal()
        calls: list[str] = []

        async def root_handler(context, arguments):  # type: ignore[no-untyped-def]
            calls.append("root")
            return "root"

        async def product_handler(context, arguments):  # type: ignore[no-untyped-def]
            calls.append("product")
            return "product"

        spec = ToolSpec(name="common.lookup", effects=frozenset({ToolEffect.READ}))
        root_registration = registry.register(ScopePath.root(), spec, root_handler)
        registry.register(_product_path(scope), spec, product_handler)
        grant = await grants.issue(
            scope.path,
            tool_identities={root_registration.identity},
            principal_id=scope.principal_id,
        )
        runner = ExecutionRunner(
            tools=registry,
            capabilities=grants,
            approvals=approvals,
            journal=journal,
        )
        workflow = ScriptWorkflow(
            (
                WorkflowDecision.tools(
                    ToolIntent(call_id="lookup-1", tool_name="common.lookup")
                ),
                WorkflowDecision.final("done"),
            )
        )
        result = await runner.run(
            _request(scope, grant.grant_id), ExecutionStrategies(workflow=workflow)
        )
        events = await journal.events(run_id="run-1")
        return result, workflow, calls, events

    result, workflow, calls, events = asyncio.run(exercise())
    assert workflow.frames[0].available_tools == ()
    assert result.observations[0].failure.code is FailureCode.TOOL_NOT_GRANTED
    assert result.observations[0].failure.message == "tool is unavailable for this grant"
    assert calls == []
    denied = [event for event in events if event.event_type.value == "tool_denied"]
    assert denied[0].tool_identity == _tool_identity(_scope(), "common.lookup")


def test_missing_and_identity_mismatch_are_indistinguishable_to_caller() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        scope = _scope()
        runner, registry, grants, _, _, _ = await _harness(scope, tools=frozenset())

        async def handler(context, arguments):  # type: ignore[no-untyped-def]
            return True

        root_registration = registry.register(
            ScopePath.root(),
            ToolSpec(name="common.lookup", effects=frozenset({ToolEffect.READ})),
            handler,
        )
        mismatch_grant = await grants.issue(
            scope.path,
            tool_identities={root_registration.identity},
            principal_id=scope.principal_id,
        )
        missing_grant = await grants.issue(
            scope.path,
            tool_identities={
                ToolIdentity(
                    namespace="root",
                    name="missing.lookup",
                    origin="scope:root",
                    version="1",
                )
            },
            principal_id=scope.principal_id,
        )

        async def run(grant_id: str, tool_name: str, run_id: str):  # type: ignore[no-untyped-def]
            workflow = ScriptWorkflow(
                (
                    WorkflowDecision.tools(
                        ToolIntent(call_id="lookup-1", tool_name=tool_name)
                    ),
                    WorkflowDecision.final("done"),
                )
            )
            return await runner.run(
                _request(scope, grant_id, run_id=run_id),
                ExecutionStrategies(workflow=workflow),
            )

        # Product registration shadows the granted root identity.
        registry.register(
            _product_path(scope),
            ToolSpec(name="common.lookup", effects=frozenset({ToolEffect.READ})),
            handler,
        )
        mismatch = await run(mismatch_grant.grant_id, "common.lookup", "run-mismatch")
        missing = await run(missing_grant.grant_id, "missing.lookup", "run-missing")
        return mismatch.observations[0].failure, missing.observations[0].failure

    mismatch, missing = asyncio.run(exercise())
    assert mismatch.code is missing.code is FailureCode.TOOL_NOT_GRANTED
    assert mismatch.message == missing.message == "tool is unavailable for this grant"


def test_approval_same_alias_wrong_identity_is_rejected_without_consumption() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        scope = _scope()
        runner, registry, _, approvals, _, grant = await _harness(
            scope, tools=frozenset({"data.update"})
        )
        calls = 0

        async def update(context, arguments):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            return True

        registry.register(
            _product_path(scope),
            ToolSpec(name="data.update", effects=frozenset({ToolEffect.WRITE})),
            update,
        )
        wrong_identity = ToolIdentity(
            namespace="root",
            name="data.update",
            origin="scope:root",
            version="1",
        )
        issued_at = datetime.now(UTC)
        wrong_target = ApprovalTarget(
            scope=scope.path,
            principal_id=scope.principal_id,
            run_id="run-1",
            call_id="write-1",
            tool_identity=wrong_identity,
            arguments_digest=arguments_digest({"value": 1}),
        )
        credential = await approvals.issue(
            ApprovalBinding(
                target=wrong_target,
                approved_by="manager-1",
                issued_at=issued_at,
                expires_at=issued_at + timedelta(minutes=5),
            )
        )
        result = await runner.run(
            _request(scope, grant.grant_id),
            ExecutionStrategies(
                workflow=ScriptWorkflow(
                    (
                        WorkflowDecision.tools(
                            ToolIntent(
                                call_id="write-1",
                                tool_name="data.update",
                                arguments={"value": 1},
                                approval_token=credential.token.get_secret_value(),
                            )
                        ),
                        WorkflowDecision.final("done"),
                    )
                )
            ),
        )
        still_pending = await approvals.consume_if_matches(
            credential.token.get_secret_value(), wrong_target
        )
        return result, calls, still_pending

    result, calls, still_pending = asyncio.run(exercise())
    assert result.observations[0].failure.code is FailureCode.APPROVAL_INVALID
    assert calls == 0
    assert still_pending is not None


def test_runner_hard_limits_arguments_results_final_output_and_state() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        scope = _scope()
        policy = ExecutionPolicy(
            max_run_input_bytes=32,
            max_tool_argument_bytes=32,
            max_tool_result_bytes=32,
            max_final_output_bytes=32,
            max_workflow_state_bytes=32,
        )
        runner, registry, _, _, _, grant = await _harness(
            scope, tools=frozenset({"data.echo"}), policy=policy
        )
        calls = 0

        async def echo(context, arguments):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            if arguments.get("large_result"):
                return "r" * 100
            return "ok"

        registry.register(
            _product_path(scope),
            ToolSpec(name="data.echo", effects=frozenset({ToolEffect.READ})),
            echo,
        )

        argument_result = await runner.run(
            _request(scope, grant.grant_id, run_id="run-large-arguments"),
            ExecutionStrategies(
                workflow=ScriptWorkflow(
                    (
                        WorkflowDecision.tools(
                            ToolIntent(
                                call_id="echo-arguments",
                                tool_name="data.echo",
                                arguments={"payload": "x" * 100},
                            )
                        ),
                        WorkflowDecision.final("done"),
                    )
                )
            ),
        )
        result_result = await runner.run(
            _request(scope, grant.grant_id, run_id="run-large-result"),
            ExecutionStrategies(
                workflow=ScriptWorkflow(
                    (
                        WorkflowDecision.tools(
                            ToolIntent(
                                call_id="echo-result",
                                tool_name="data.echo",
                                arguments={"large_result": True},
                            )
                        ),
                        WorkflowDecision.final("done"),
                    )
                )
            ),
        )
        final_result = await runner.run(
            _request(scope, grant.grant_id, run_id="run-large-final"),
            ExecutionStrategies(
                workflow=ScriptWorkflow((WorkflowDecision.final("f" * 100),))
            ),
        )
        state_result = await runner.run(
            _request(scope, grant.grant_id, run_id="run-large-state"),
            ExecutionStrategies(
                workflow=ScriptWorkflow(
                    (WorkflowDecision.final("ok", state={"state": "s" * 100}),)
                )
            ),
        )
        large_input_request = RunRequest(
            run_id="run-large-input",
            scope=scope,
            grant_id=grant.grant_id,
            input={"payload": "i" * 100},
        )
        input_workflow = ScriptWorkflow((WorkflowDecision.final("must-not-run"),))
        input_result = await runner.run(
            large_input_request,
            ExecutionStrategies(workflow=input_workflow),
        )
        return (
            argument_result,
            result_result,
            final_result,
            state_result,
            input_result,
            input_workflow,
            calls,
        )

    (
        argument_result,
        result_result,
        final_result,
        state_result,
        input_result,
        input_workflow,
        calls,
    ) = asyncio.run(exercise())
    assert argument_result.observations[0].failure.code is FailureCode.TOOL_ARGUMENTS_TOO_LARGE
    assert result_result.observations[0].failure.code is FailureCode.TOOL_RESULT_TOO_LARGE
    assert final_result.failure.code is FailureCode.FINAL_OUTPUT_TOO_LARGE
    assert state_result.failure.code is FailureCode.WORKFLOW_STATE_TOO_LARGE
    assert input_result.failure.code is FailureCode.RUN_INPUT_TOO_LARGE
    assert input_workflow.frames == []
    assert calls == 1


def test_root_policy_rejects_client_budget_expansion_before_workflow() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        scope = _scope()
        policy = ExecutionPolicy(
            max_iterations=2,
            max_tool_calls=3,
            max_parallel_calls=2,
            max_timeout_seconds=2,
        )
        runner, _, _, _, _, grant = await _harness(
            scope,
            tools=frozenset(),
            policy=policy,
        )
        accepted_workflow = ScriptWorkflow((WorkflowDecision.final("ok"),))
        accepted = await runner.run(
            _request(
                scope,
                grant.grant_id,
                run_id="budget-at-ceiling",
                budget=ExecutionBudget(
                    max_iterations=2,
                    max_tool_calls=3,
                    max_parallel_calls=2,
                    timeout_seconds=2,
                ),
            ),
            ExecutionStrategies(workflow=accepted_workflow),
        )
        rejected_workflow = ScriptWorkflow((WorkflowDecision.final("must-not-run"),))
        rejected = await runner.run(
            _request(
                scope,
                grant.grant_id,
                run_id="budget-over-ceiling",
                budget=ExecutionBudget(
                    max_iterations=3,
                    max_tool_calls=4,
                    max_parallel_calls=3,
                    timeout_seconds=3,
                ),
            ),
            ExecutionStrategies(workflow=rejected_workflow),
        )
        return accepted, rejected, accepted_workflow, rejected_workflow

    accepted, rejected, accepted_workflow, rejected_workflow = asyncio.run(exercise())
    assert accepted.status is RunStatus.SUCCEEDED
    assert len(accepted_workflow.frames) == 1
    assert rejected.status is RunStatus.BUDGET_EXCEEDED
    assert rejected.failure.code is FailureCode.BUDGET_POLICY_EXCEEDED
    assert rejected_workflow.frames == []


def test_models_reject_ambiguous_effects_and_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ToolSpec(
            name="bad.tool",
            effects=frozenset({ToolEffect.READ, ToolEffect.WRITE}),
        )
    with pytest.raises(ValidationError):
        ToolSpec(name="bad.external", effects=frozenset({ToolEffect.EXTERNAL}))
    with pytest.raises(ValidationError):
        ExecutionBudget(max_tool_calls=1, surprise=True)  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        ExecutionPolicy(max_tool_calls=0)
    with pytest.raises(TypeError):
        ExecutionPolicy(max_parallel_calls=True)  # type: ignore[arg-type]
