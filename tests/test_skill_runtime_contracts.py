"""Runtime/activation edge contracts using real host boundaries and fake IO."""

from __future__ import annotations

import asyncio
import hashlib

import pytest
from pydantic import BaseModel

from suiteharness.execution import (
    CommandSpec,
    ExecutionPolicy,
    ExecutionRunner,
    ExecutionStrategies,
    InMemoryApprovalStore,
    InMemoryAuditJournal,
    InMemoryCapabilityAuthority,
    InMemoryCommandRegistry,
    InMemoryToolRegistry,
    RunRequest,
    ToolEffect,
    ToolIdentity,
    ToolIntent,
    ToolSpec,
    Workflow,
    WorkflowDecision,
)
from suiteharness.runtime.kernel import ProductActivationContext
from suiteharness.runtime.scopes import RequestScope, RootContext, ScopePath
from suiteharness.skills import (
    SKILL_PROVIDER,
    EmptySkillProvider,
    SkillDescriptor,
    SkillDocument,
    SkillError,
    SkillProvider,
)
from suiteharness.skills.runtime import (
    SkillRuntime,
    bind_skill_runtime,
    isolated_skill_stage,
    preload_skill,
    read_skill_tool,
    skill_read_spec,
)

SKILL = "demo.work"
BODY = "Use only this authenticated request and its actual operation result."
DESCRIPTOR = SkillDescriptor(
    skill_id=SKILL, name="work", description="Handle the requested work",
    version="1.0.0", source_sha256="a" * 64,
)


def request(*, text="/work requested", run_id="runtime-contract"):
    return RunRequest(
        run_id=run_id, grant_id="contract-grant", input=text,
        scope=RequestScope(ScopePath.product("company", "demo"), "employee"),
    )


class CountingProvider(SkillProvider):
    """Custom provider intentionally inherits the default empty static manifest."""

    def __init__(self):
        self.reads = []

    async def list_skills(self, access):
        return (DESCRIPTOR,)

    async def read_skill(self, access, skill_id, *, resource_id="instructions"):
        self.reads.append((access, skill_id, resource_id))
        return SkillDocument(
            descriptor=DESCRIPTOR, resource_id=resource_id, instructions=BODY,
            instructions_sha256=hashlib.sha256(BODY.encode()).hexdigest(),
        )


class ManifestProvider(CountingProvider):
    def static_manifest(self):
        return (DESCRIPTOR,)


def trusted_command(req):
    registry = InMemoryCommandRegistry()
    registry.register(req.scope.path, (CommandSpec(
        command_id="work", aliases=("/work",), preload_skills=(SKILL,),
    ),))
    return registry.resolve(req)


def test_command_then_workflow_preload_reads_same_stage_only_once():
    async def exercise():
        req = request()
        provider = ManifestProvider()
        runtime = SkillRuntime(req, provider)
        runtime.command_context = trusted_command(req)
        with bind_skill_runtime(runtime):
            first = await preload_skill(req, SKILL, source="command")
            second = await preload_skill(req, SKILL, source="workflow")
            assert second is first
        assert len(provider.reads) == 1
        assert provider.reads[0][0].source == "command"
        assert provider.reads[0][0].command_name == "work"

    asyncio.run(exercise())


def test_isolated_stages_recheck_and_read_their_own_instruction_body():
    async def exercise():
        req = request()
        provider = ManifestProvider()
        runtime = SkillRuntime(req, provider)
        with bind_skill_runtime(runtime):
            await preload_skill(req, SKILL)
            with isolated_skill_stage(req, "independent-reply"):
                await preload_skill(req, SKILL)
                await preload_skill(req, SKILL)
            # Parent stage remains intact; the reply's loading state is not
            # promoted into a permanent/global cache shared by future stages.
            await preload_skill(req, SKILL)
            with isolated_skill_stage(req, "another-stage"):
                await preload_skill(req, SKILL)
        assert len(provider.reads) == 3

    asyncio.run(exercise())


@pytest.mark.parametrize("already_prepared", [False, True])
def test_untrusted_command_source_is_rejected_even_with_a_cached_body(already_prepared):
    async def exercise():
        req = request(text={"message": "ordinary request", "command_id": "work"})
        provider = ManifestProvider()
        runtime = SkillRuntime(req, provider)
        runtime.command_context = trusted_command(req)
        assert runtime.command_context.command_id is None
        with bind_skill_runtime(runtime):
            if already_prepared:
                await preload_skill(req, SKILL, source="workflow")
            with pytest.raises(SkillError) as caught:
                await preload_skill(req, SKILL, source="command")
            assert caught.value.code == "skill_context_unavailable"
        assert len(provider.reads) == int(already_prepared)

    asyncio.run(exercise())


def test_prepared_without_actual_model_request_proof_is_a_context_error():
    async def exercise():
        req = request()
        runtime = SkillRuntime(req, ManifestProvider())
        with bind_skill_runtime(runtime):
            assert runtime.missing("unknown-call", (SKILL,)) == (SKILL,)
            await preload_skill(req, SKILL)
            with pytest.raises(SkillError) as caught:
                runtime.missing("unknown-call", (SKILL,))
            assert caught.value.code == "skill_context_unavailable"

    asyncio.run(exercise())


class CaptureWorkflow(Workflow):
    def __init__(self, *, call_read=False):
        self.call_read = call_read
        self.frames = []

    async def next(self, request, frame, prompt):
        self.frames.append(frame)
        if self.call_read and frame.iteration == 1:
            return WorkflowDecision.tools(ToolIntent(
                call_id="read-contract", tool_name="suiteharness.skill.read",
                arguments={"skill_id": SKILL},
            ))
        return WorkflowDecision.final("done")


async def run_provider(provider, *, call_read=False):
    registry = InMemoryToolRegistry()
    registration = registry.register_protected(
        skill_read_spec(), read_skill_tool,
        identity=ToolIdentity(
            namespace="suiteharness", name="suiteharness.skill.read",
            origin="suiteharness.builtin.skill", version="1",
        ),
    )
    authority = InMemoryCapabilityAuthority()
    req = request()
    grant = await authority.issue(req.scope.path, tool_identities=(registration.identity,))
    req = req.model_copy(update={"grant_id": grant.grant_id})
    workflow = CaptureWorkflow(call_read=call_read)
    runner = ExecutionRunner(
        tools=registry, capabilities=authority, approvals=InMemoryApprovalStore(),
        journal=InMemoryAuditJournal(), policy=ExecutionPolicy(approval_effects=frozenset()),
    )
    result = await runner.run(req, ExecutionStrategies(workflow=workflow, skills=provider))
    return result, workflow


def test_empty_provider_does_not_publish_read_tool_even_if_granted():
    result, workflow = asyncio.run(run_provider(EmptySkillProvider()))
    assert result.output == "done"
    assert all(not frame.available_tools for frame in workflow.frames)
    runtime = SkillRuntime(request(), EmptySkillProvider())
    assert not runtime.enabled


@pytest.mark.parametrize("extension_code", ["vendor_source_revoked", "policy_denied"])
def test_extension_provider_errors_are_safely_mapped_by_the_actual_runner(extension_code):
    class FailingProvider(ManifestProvider):
        async def read_skill(self, access, skill_id, *, resource_id="instructions"):
            raise SkillError(extension_code, "The configured source is not available")

    result, workflow = asyncio.run(run_provider(FailingProvider(), call_read=True))
    assert len(result.observations) == 1
    assert result.observations[0].failure.code.value == "skill_read_failed"
    assert len(workflow.frames) == 2  # Error observation reaches the next workflow/model step.
    assert workflow.frames[-1].observations[0].failure.code.value == "skill_read_failed"


async def activation_check(provider, *, should_fail):
    class Config(BaseModel):
        pass

    root = RootContext()
    registry = InMemoryToolRegistry()
    product = root.tenant("company").product("demo", {SKILL_PROVIDER: provider})
    context = ProductActivationContext(
        path=product.path, config=Config(), _bindings=product.bindings,
        _effects=product.effects, _tools=registry, _declared_capabilities=frozenset(),
    )
    calls = []

    async def handler(context, arguments):
        calls.append(context.call_id)
        return {"ok": True}

    spec = ToolSpec(
        name="demo.perform", effects=frozenset({ToolEffect.READ}), required_skills=(SKILL,),
        input_schema={"type": "object", "additionalProperties": False},
    )
    try:
        if should_fail:
            with pytest.raises(ValueError, match="unregistered skill"):
                context.register_tool(spec, handler)
            assert registry.resolve(request().scope, spec.name) is None
        else:
            handle = context.register_tool(spec, handler)
            assert registry.resolve(request().scope, spec.name).identity == handle.identity
        assert calls == []
    finally:
        await root.close()
    assert registry.resolve(request().scope, spec.name) is None


def test_custom_provider_default_manifest_is_rejected_at_activation():
    provider = CountingProvider()
    assert provider.static_manifest() == ()
    asyncio.run(activation_check(provider, should_fail=True))
    assert provider.reads == []  # No fake administrator scope or first-request discovery.


def test_explicit_static_manifest_allows_activation_without_reading_personal_content():
    provider = ManifestProvider()
    asyncio.run(activation_check(provider, should_fail=False))
    assert provider.reads == []
