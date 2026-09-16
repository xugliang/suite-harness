"""Saved read results must still be authorized before reaching a new model run."""

import asyncio
import json

import pytest

from suiteharness.agents.react import ReActWorkflow
from suiteharness.execution.in_memory import (
    InMemoryApprovalStore,
    InMemoryAuditJournal,
    InMemoryCapabilityAuthority,
    InMemoryToolRegistry,
)
from suiteharness.execution.models import (
    RunRequest,
    RunStatus,
    ToolAuthorization,
    ToolEffect,
    ToolIdentity,
    ToolSpec,
)
from suiteharness.execution.protocols import (
    ExecutionStrategies,
    PromptStrategy,
    ToolAuthorizationPolicy,
)
from suiteharness.execution.runner import ExecutionPolicy, ExecutionRunner
from suiteharness.models import ModelRole, ModelStreamEventType
from suiteharness.runtime import RequestScope, ScopePath
from tests.test_run_resume_runner import READ, Model, checkpoint, resumed_request


class ReadModel(Model):
    def __init__(self, arguments):
        super().__init__((('read-1', READ),), pause=True)
        self.arguments = arguments

    async def stream(self, route, request):
        async for event in super().stream(route, request):
            if event.type is ModelStreamEventType.TOOL_CALL_DELTA:
                event = event.model_copy(update={'tool_call': event.tool_call.model_copy(
                    update={'arguments': self.arguments})})
            yield event


class ReadPolicy(ToolAuthorizationPolicy):
    def __init__(self):
        self.current = ToolAuthorization.ALLOW
        self.calls = []

    def evaluate(self, request, identity, spec, arguments):
        self.calls.append((request.run_id, json.loads(json.dumps(arguments))))
        if isinstance(self.current, Exception):
            raise self.current
        return self.current


async def components(*, mutate_handler=False):
    registry = InMemoryToolRegistry()
    authority = InMemoryCapabilityAuthority()
    scope = RequestScope(path=ScopePath.agent('tenant', 'product', 'agent', 'session'),
                         principal_id='seller', channel_id='web')
    identity = ToolIdentity(namespace='suiteharness', name=READ,
                            origin='suiteharness.builtin.fs', version='1')
    calls = []

    async def handler(context, arguments):
        calls.append(context.run_id)
        if mutate_handler:
            arguments['path'] = 'handler-changed.txt'
        return {'text': 'restricted evidence'}

    registry.register_protected(ToolSpec(name=READ, effects=frozenset({ToolEffect.READ}),
        input_schema={'type': 'object', 'required': ['path'],
                      'properties': {'path': {'type': 'string'}}}), handler, identity=identity)
    policy = ReadPolicy()
    runner = ExecutionRunner(tools=registry, capabilities=authority,
        approvals=InMemoryApprovalStore(), journal=InMemoryAuditJournal(),
        policy=ExecutionPolicy(approval_effects=frozenset()), authorization_policy=policy)
    grant = await authority.issue(scope.path, tool_identities=(identity,))
    request = RunRequest(run_id='old-run', scope=scope, grant_id=grant.grant_id,
                         input='inspect', read_only=True)
    return runner, request, authority, identity, policy, calls


@pytest.mark.parametrize('current', [
    ToolAuthorization.DENY, ToolAuthorization.REQUIRE_APPROVAL,
    RuntimeError('policy unavailable'), 'allow',
])
def test_resume_rejects_results_no_longer_authorized_before_calling_model(current):
    async def check():
        runner, request, authority, identity, policy, calls = await components()
        data, _ = await checkpoint(runner, request, ReadModel({'path': 'private.txt'}))
        policy.current = current
        resumed = await resumed_request(request, authority, identity, data)
        model = Model()
        result = await runner.run(resumed, ExecutionStrategies(
            workflow=ReActWorkflow(model, 'chat'), prompt=PromptStrategy()))
        assert result.status is RunStatus.FAILED
        assert model.requests == []
        assert calls == ['old-run']
    asyncio.run(check())


def test_resume_reauthorizes_original_arguments_after_handler_mutation():
    async def check():
        runner, request, authority, identity, policy, calls = await components(mutate_handler=True)
        data, _ = await checkpoint(runner, request, ReadModel({'path': 'private.txt'}))
        assert data['resume_state']['observations'][0].get('arguments') == {'path': 'private.txt'}
        resumed = await resumed_request(request, authority, identity, data)
        model = Model()
        result = await runner.run(resumed, ExecutionStrategies(
            workflow=ReActWorkflow(model, 'chat'), prompt=PromptStrategy()))
        assert result.status is RunStatus.SUCCEEDED
        assert policy.calls == [('old-run', {'path': 'private.txt'}),
                                ('new-run', {'path': 'private.txt'})]
        messages = [m for m in model.requests[0].messages if m.role is ModelRole.TOOL]
        assert json.loads(messages[0].content[0].text)['result'] == {'text': 'restricted evidence'}
        assert calls == ['old-run']
    asyncio.run(check())


def test_legacy_checkpoint_without_arguments_fails_closed_when_policy_is_configured():
    async def check():
        runner, request, authority, identity, _, _ = await components()
        data, _ = await checkpoint(runner, request, ReadModel({'path': 'private.txt'}))
        data['resume_state']['observations'][0].pop('arguments', None)
        resumed = await resumed_request(request, authority, identity, data)
        model = Model()
        result = await runner.run(resumed, ExecutionStrategies(
            workflow=ReActWorkflow(model, 'chat'), prompt=PromptStrategy()))
        assert result.status is RunStatus.FAILED
        assert model.requests == []
    asyncio.run(check())


def test_run_scoped_credentials_in_arguments_prevent_a_completed_checkpoint():
    async def check():
        runner, request, _, _, _, _ = await components()
        model = ReadModel({'path': 'private.txt', 'options': {'approval_token': 'old-token'}})
        model.pause = False
        events = [e async for e in runner.stream(request, ExecutionStrategies(
            workflow=ReActWorkflow(model, 'chat'), prompt=PromptStrategy()))]
        assert events[-1].result.status is RunStatus.SUCCEEDED
        assert all(not e.data['resume_state']['observations']
                   for e in events if e.kind == 'checkpoint')
    asyncio.run(check())
