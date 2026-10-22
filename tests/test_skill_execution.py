"""Skill prerequisites exercise the real runner and model/tool message path."""

import asyncio
import hashlib

from suiteharness.agents.react import ReActWorkflow
from suiteharness.execution import (
    ExecutionPolicy,
    ExecutionRunner,
    ExecutionStrategies,
    InMemoryApprovalStore,
    InMemoryAuditJournal,
    InMemoryCapabilityAuthority,
    InMemoryToolRegistry,
    RunRequest,
    ToolEffect,
    ToolIdentity,
    ToolSpec,
)
from suiteharness.models import (
    FinishReason,
    ModelStreamEvent,
    ModelStreamEventType,
    ModelToolCall,
)
from suiteharness.runtime.scopes import RequestScope, ScopePath
from suiteharness.skills import SkillDescriptor, SkillDocument, SkillProvider
from suiteharness.skills.runtime import (
    prepare_skill_model_request,
    read_skill_tool,
    record_skill_model_response,
    skill_read_spec,
    verify_skill_model_request,
)

SKILL = "product-a.operation"
BODY = "Read the actual target and preserve its ownership before changing it."


class Provider(SkillProvider):
    descriptor = SkillDescriptor(
        skill_id=SKILL, name="operation", description="Perform the requested operation",
        version="1", source_sha256="a" * 64,
    )

    def static_manifest(self):
        return (self.descriptor,)

    async def list_skills(self, access):
        return self.static_manifest()

    async def read_skill(self, access, skill_id, *, resource_id="instructions"):
        assert access.scope.product_id == "product-a"
        return SkillDocument(
            descriptor=self.descriptor, resource_id=resource_id, instructions=BODY,
            instructions_sha256=hashlib.sha256(BODY.encode()).hexdigest(),
        )


class ScriptedModel:
    def __init__(self, batches, *, skill_protocol=True):
        self.batches = list(batches)
        self.requests = []
        self.skill_protocol = skill_protocol

    async def stream(self, route_id, request):
        if self.skill_protocol:
            request = prepare_skill_model_request(request)
            verify_skill_model_request(request)
        self.requests.append(request)
        batch = self.batches.pop(0)
        for call in batch:
            yield ModelStreamEvent(type=ModelStreamEventType.TOOL_CALL_DELTA, tool_call=call)
        if not batch:
            yield ModelStreamEvent(type=ModelStreamEventType.TEXT_DELTA, text_delta="done")
        yield ModelStreamEvent(type=ModelStreamEventType.MESSAGE_END,
                               finish_reason=FinishReason.TOOL_CALLS if batch else FinishReason.STOP)
        if self.skill_protocol:
            record_skill_model_response(request, batch)


async def execute(batches, *, skill_protocol=True):
    registry = InMemoryToolRegistry()
    scope = RequestScope(ScopePath.product("company", "product-a"), "employee")
    registry.register_protected(skill_read_spec(), read_skill_tool, identity=ToolIdentity(
        namespace="suiteharness", name="suiteharness.skill.read",
        origin="suiteharness.builtin.skill", version="1",
    ))
    writes = []

    async def change(context, arguments):
        writes.append(context.call_id)
        return {"changed": True}

    registry.register(scope.path, ToolSpec(
        name="operation.change", effects=frozenset({ToolEffect.WRITE}),
        required_skills=(SKILL,), input_schema={"type": "object", "additionalProperties": False},
    ), change)
    authority = InMemoryCapabilityAuthority()
    grant = await authority.issue(scope.path, tool_identities=tuple(
        registry.resolve(scope, name).identity
        for name in ("suiteharness.skill.read", "operation.change")
    ))
    model = ScriptedModel(batches, skill_protocol=skill_protocol)
    runner = ExecutionRunner(
        tools=registry, capabilities=authority, approvals=InMemoryApprovalStore(),
        journal=InMemoryAuditJournal(), policy=ExecutionPolicy(approval_effects=frozenset()),
    )
    result = await runner.run(RunRequest(run_id="skill-run", scope=scope,
        grant_id=grant.grant_id, input="perform the operation"), ExecutionStrategies(
            workflow=ReActWorkflow(gateway=model, route_id="route"), skills=Provider(),
        ))
    return result, writes, model


def test_missing_skill_blocks_the_actual_side_effect():
    result, writes, _ = asyncio.run(execute([
        [ModelToolCall(call_id="write-first", name="operation.change", arguments={})], [],
    ]))
    assert writes == []
    assert result.observations[0].failure.code.value == "skill_required"


def test_same_batch_read_does_not_authorize_write_but_next_model_request_does():
    result, writes, model = asyncio.run(execute([
        [ModelToolCall(call_id="read", name="suiteharness.skill.read", arguments={"skill_id": SKILL}),
         ModelToolCall(call_id="too-early", name="operation.change", arguments={})],
        [ModelToolCall(call_id="after-reading", name="operation.change", arguments={})], [],
    ]))
    assert writes == ["after-reading"]
    assert result.observations[1].failure.code.value == "skill_required"
    assert any(BODY in part.text for message in model.requests[1].messages
               for part in message.content if hasattr(part, "text"))


def test_unintegrated_custom_gateway_cannot_borrow_outer_prompt_proof():
    result, writes, _ = asyncio.run(execute([
        [ModelToolCall(call_id="read", name="suiteharness.skill.read", arguments={"skill_id": SKILL})],
        [ModelToolCall(call_id="unproven", name="operation.change", arguments={})], [],
    ], skill_protocol=False))
    assert writes == []
    assert result.observations[1].failure.code.value == "skill_context_unavailable"
