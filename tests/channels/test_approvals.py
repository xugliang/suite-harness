from __future__ import annotations

import asyncio

from suiteharness.channels import (
    ApprovalChallenge,
    CompanyChannelAuthorizationPolicy,
    InteractiveApprovalCoordinator,
)
from suiteharness.execution import (
    ApprovalTarget,
    ExecutionStrategies,
    InMemoryApprovalStore,
    InMemoryAuditJournal,
    InMemoryCapabilityAuthority,
    InMemoryToolRegistry,
    RunRequest,
    ToolEffect,
    ToolIdentity,
    ToolIntent,
    ToolSpec,
    WorkflowDecision,
    arguments_digest,
)
from suiteharness.execution.runner import ExecutionRunner
from suiteharness.runtime import RequestScope, ScopePath


def _request() -> RunRequest:
    return RunRequest(
        run_id="run-1",
        grant_id="grant-1",
        input="test",
        scope=RequestScope(
            ScopePath.agent("acme", "sales", "default", "session-1"),
            "user-1",
            channel_id="web",
        ),
    )


def _target(request: RunRequest) -> ApprovalTarget:
    return ApprovalTarget(
        scope=request.scope.path,
        principal_id=request.scope.principal_id,
        run_id=request.run_id,
        call_id="write-1",
        tool_identity=ToolIdentity(
            namespace="suiteharness", name="suiteharness.fs.write", origin="suiteharness.builtin.fs", version="1"
        ),
        arguments_digest=arguments_digest({"path": "report.md"}),
    )


def test_exact_principal_can_approve_once() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        seen: list[ApprovalChallenge] = []

        async def publish(request, challenge):  # type: ignore[no-untyped-def]
            seen.append(challenge)
            assert not await coordinator.decide(
                challenge.challenge_id,
                principal_id="other-user",
                approved=True,
                approved_by="manager-1",
            )
            assert await coordinator.decide(
                challenge.challenge_id,
                principal_id="user-1",
                approved=True,
                approved_by="manager-1",
            )

        coordinator = InteractiveApprovalCoordinator(publish, timeout_seconds=1)
        request = _request()
        binding = await coordinator.request_approval(
            request,
            _target(request),
            ToolSpec(name="suiteharness.fs.write", effects=frozenset({ToolEffect.WRITE})),
            {"path": "report.md"},
        )
        return binding, seen

    binding, seen = asyncio.run(exercise())
    assert binding is not None
    assert binding.approved_by == "manager-1"
    assert binding.target == _target(_request())
    assert len(seen) == 1


def test_denial_resolves_to_no_binding_and_is_not_reusable() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        decisions: list[bool] = []

        async def publish(request, challenge):  # type: ignore[no-untyped-def]
            decisions.append(
                await coordinator.decide(
                    challenge.challenge_id,
                    principal_id="user-1",
                    approved=False,
                    approved_by="user-1",
                )
            )

        coordinator = InteractiveApprovalCoordinator(publish, timeout_seconds=1)
        request = _request()
        binding = await coordinator.request_approval(
            request,
            _target(request),
            ToolSpec(name="suiteharness.fs.write", effects=frozenset({ToolEffect.WRITE})),
            {"path": "report.md"},
        )
        return binding, decisions

    binding, decisions = asyncio.run(exercise())
    assert binding is None
    assert decisions == [True]


def test_runner_waits_for_web_approval_without_putting_a_token_in_model_output() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        request = _request()
        registry = InMemoryToolRegistry()
        calls: list[str] = []

        async def write(context, arguments):  # type: ignore[no-untyped-def]
            calls.append(arguments["path"])
            return {"written": True}

        handle = registry.register(
            ScopePath.product("acme", "sales"),
            ToolSpec(name="data.write", effects=frozenset({ToolEffect.WRITE})),
            write,
        )
        authority = InMemoryCapabilityAuthority()
        grant = await authority.issue(
            request.scope.path,
            tool_identities={handle.identity},
            principal_id=request.scope.principal_id,
        )

        async def publish(pending_request, challenge):  # type: ignore[no-untyped-def]
            assert pending_request.scope.channel_id == "web"
            assert await coordinator.decide(
                challenge.challenge_id,
                principal_id="user-1",
                approved=True,
                approved_by="user-1",
            )

        coordinator = InteractiveApprovalCoordinator(publish, timeout_seconds=1)
        runner = ExecutionRunner(
            tools=registry,
            capabilities=authority,
            approvals=InMemoryApprovalStore(),
            journal=InMemoryAuditJournal(),
            authorization_policy=CompanyChannelAuthorizationPolicy(),
            interactive_approvals=coordinator,
        )

        class Workflow:
            count = 0

            async def next(self, request, frame, prompt):  # type: ignore[no-untyped-def]
                self.count += 1
                if self.count == 1:
                    return WorkflowDecision.tools(
                        ToolIntent(
                            call_id="write-1",
                            tool_name="data.write",
                            arguments={"path": "report.md"},
                        )
                    )
                return WorkflowDecision.final("done")

        result = await runner.run(
            request.model_copy(update={"grant_id": grant.grant_id}),
            ExecutionStrategies(workflow=Workflow()),
        )
        return result, calls

    result, calls = asyncio.run(exercise())
    assert result.output == "done"
    assert result.observations[0].result == {"written": True}
    assert calls == ["report.md"]
