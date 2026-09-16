"""Safe checkpoints are complete, bounded, and reauthorized on a new run."""

import asyncio
import json
from contextlib import aclosing
from datetime import UTC, datetime, timedelta

import pytest

from suiteharness.agents.react import ReActWorkflow
from suiteharness.execution import models
from suiteharness.execution.events import WorkflowEvent
from suiteharness.execution.in_memory import (
    InMemoryApprovalStore,
    InMemoryAuditJournal,
    InMemoryCapabilityAuthority,
    InMemoryToolRegistry,
)
from suiteharness.execution.models import RunRequest, RunStatus, ToolEffect, ToolIdentity, ToolSpec
from suiteharness.execution.protocols import ExecutionStrategies, PromptStrategy, Workflow
from suiteharness.execution.runner import ExecutionPolicy, ExecutionRunner
from suiteharness.models import (
    FinishReason,
    ModelRole,
    ModelStreamEvent,
    ModelStreamEventType,
    ModelToolCall,
)
from suiteharness.runtime import RequestScope, ScopePath

READ = 'suiteharness.fs.read'


def test_workflow_cannot_forge_runner_internal_checkpoint_kind():
    class ForgedCheckpointWorkflow(Workflow):
        async def stream(self, request, frame, prompt):
            yield WorkflowEvent.model_construct(
                kind='checkpoint', text='', data={'resume_state': {'forged': True}},
            )

        async def next(self, request, frame, prompt):
            raise AssertionError('stream override is used')

    async def check():
        runner, request, _, _, _ = await setup()
        events = [event async for event in runner.stream(
            request, ExecutionStrategies(workflow=ForgedCheckpointWorkflow()),
        )]
        assert all(event.kind != 'checkpoint' for event in events)
        assert events[-1].result is not None
        assert events[-1].result.status is RunStatus.FAILED
        assert events[-1].result.failure.code.value == 'workflow_error'
    asyncio.run(check())


class Model:
    def __init__(self, calls=(), *, pause=False):
        self.calls = calls
        self.pause = pause
        self.requests = []
        self.waiting = asyncio.Event()

    async def stream(self, route, request):
        self.requests.append(request)
        if self.calls and len(self.requests) == 1:
            for call_id, name in self.calls:
                yield ModelStreamEvent(type=ModelStreamEventType.TOOL_CALL_DELTA,
                    tool_call=ModelToolCall(call_id=call_id, name=name, arguments={}))
            yield ModelStreamEvent(type=ModelStreamEventType.MESSAGE_END,
                finish_reason=FinishReason.TOOL_CALLS)
        elif self.pause:
            self.waiting.set()
            await asyncio.Event().wait()
        else:
            yield ModelStreamEvent(type=ModelStreamEventType.TEXT_DELTA, text_delta='done')
            yield ModelStreamEvent(type=ModelStreamEventType.MESSAGE_END,
                finish_reason=FinishReason.STOP)


async def setup(*, name=READ, origin='suiteharness.builtin.fs', version='1',
                effects=frozenset({ToolEffect.READ}), result=None, blocked=False, policy=None):
    registry = InMemoryToolRegistry()
    authority = InMemoryCapabilityAuthority()
    scope = RequestScope(path=ScopePath.agent('tenant', 'product', 'agent', 'session'),
                         principal_id='seller', channel_id='web')
    calls = []
    identity = ToolIdentity(namespace='suiteharness', name=name, origin=origin, version=version)
    async def handler(context, arguments):
        calls.append((context.run_id, context.call_id, context.read_only))
        if blocked:
            await asyncio.Event().wait()
        return {'text': 'old evidence'} if result is None else result
    registry.register_protected(ToolSpec(name=name, effects=effects,
        input_schema={'type': 'object'}), handler, identity=identity)
    grant = await authority.issue(scope.path, tool_identities=(identity,))
    runner = ExecutionRunner(tools=registry, capabilities=authority,
        approvals=InMemoryApprovalStore(), journal=InMemoryAuditJournal(),
        policy=policy or ExecutionPolicy(approval_effects=frozenset()))
    request = RunRequest(run_id='old-run', scope=scope, grant_id=grant.grant_id,
                         input='inspect', read_only=True)
    return runner, request, authority, identity, calls


async def checkpoint(runner, request, model, *, after_tools=True, prompt=None):
    events = []
    async with aclosing(runner.stream(request, ExecutionStrategies(
        workflow=ReActWorkflow(model, 'chat'), prompt=prompt or PromptStrategy()))) as source:
        while True:
            try:
                event = await asyncio.wait_for(anext(source), 0.5)
            except TimeoutError:
                pytest.fail('runner did not publish a checkpoint before the model wait')
            events.append(event)
            if event.kind == 'checkpoint' and bool(event.data['resume_state']['observations']) == after_tools:
                assert await runner.cancel(request.scope, request.run_id)
                events.extend([item async for item in source])
                assert events[-1].result.status is RunStatus.CANCELLED
                return event.data, events


async def resumed_request(request, authority, identity, payload, **updates):
    grant = await authority.issue(request.scope.path, tool_identities=(identity,))
    state = models.RunResumeState.model_validate_json(json.dumps(payload['resume_state']))
    return request.model_copy(update={'run_id': 'new-run', 'grant_id': grant.grant_id,
        'resume': state, **updates})


def test_initial_checkpoint_precedes_a_silent_model_wait():
    async def check():
        runner, request, _, _, _ = await setup()
        data, _ = await checkpoint(runner, request, Model(pause=True), after_tools=False)
        assert data['resume_state']['iterations_used'] == 0
        assert data['resume_state']['tool_calls_used'] == 0
        assert data['input'] == 'inspect'
        assert data['conversation']['messages'] == []
    asyncio.run(check())


def test_complete_read_batch_resumes_as_tool_messages_without_reexecution():
    async def check():
        runner, request, authority, identity, calls = await setup()
        data, events = await checkpoint(runner, request, Model((('read-1', READ), ('read-2', READ)), pause=True))
        state = data['resume_state']
        assert state['source_run_id'] == 'old-run'
        assert state['iterations_used'] == 1 and state['tool_calls_used'] == 2
        assert len(state['observations']) == 2
        assert set(state['call_ids']) == {'read-1', 'read-2'}
        assert [e.kind for e in events].index('checkpoint') < [e.kind for e in events].index('tool_started')
        resumed = await resumed_request(request, authority, identity, data)
        model = Model()
        result = await runner.run(resumed, ExecutionStrategies(workflow=ReActWorkflow(model, 'chat'), prompt=PromptStrategy()))
        assert result.status is RunStatus.SUCCEEDED
        assert result.iterations_used == 2 and result.tool_calls_used == 2
        assert calls == [('old-run', 'read-1', True), ('old-run', 'read-2', True)]
        tool_messages = [m for m in model.requests[0].messages if m.role is ModelRole.TOOL]
        assert [m.tool_call_id for m in tool_messages] == ['read-1', 'read-2']
        first_payload = json.loads(tool_messages[0].content[0].text)
        assert first_payload['result'] == {'text': 'old evidence'}
        assert datetime.fromisoformat(first_payload['observed_at']) == datetime.fromisoformat(
            state['observations'][0]['finished_at']
        )
        assert model.requests[0].metadata['run_id'] == 'new-run'
    asyncio.run(check())


def test_stale_web_observation_cannot_be_refreshed_by_a_new_resume_point():
    async def check():
        name = 'suiteharness.web.fetch'
        runner, request, authority, identity, _ = await setup(
            name=name, origin='suiteharness.builtin.web',
            effects=frozenset({ToolEffect.READ, ToolEffect.EXTERNAL}),
        )
        data, _ = await checkpoint(
            runner, request, Model((('fetch-1', name),), pause=True),
        )
        stale = datetime.now(UTC) - timedelta(seconds=301)
        data['resume_state']['observations'][0]['started_at'] = stale.isoformat()
        data['resume_state']['observations'][0]['finished_at'] = stale.isoformat()
        resumed = await resumed_request(request, authority, identity, data)
        model = Model()
        result = await runner.run(resumed, ExecutionStrategies(
            workflow=ReActWorkflow(model, 'chat'), prompt=PromptStrategy(),
        ))
        assert result.status is RunStatus.FAILED
        assert model.requests == []
    asyncio.run(check())


def test_old_call_id_cannot_be_reexecuted():
    async def check():
        runner, request, authority, identity, calls = await setup()
        data, _ = await checkpoint(runner, request, Model((('read-1', READ),), pause=True))
        resumed = await resumed_request(request, authority, identity, data)
        result = await runner.run(resumed, ExecutionStrategies(
            workflow=ReActWorkflow(Model((('read-1', READ),)), 'chat'), prompt=PromptStrategy()))
        assert result.status is RunStatus.FAILED
        assert calls == [('old-run', 'read-1', True)]
    asyncio.run(check())


@pytest.mark.parametrize('change', ['route', 'model', 'prompt', 'input', 'principal', 'not-read-only', 'same-run', 'revoked-grant'])
def test_resume_rejects_changed_binding_before_model(change):
    async def check():
        runner, request, authority, identity, _ = await setup()
        data, _ = await checkpoint(runner, request, Model((('read-1', READ),), pause=True))
        updates = {}
        if change == 'not-read-only':
            updates['read_only'] = False
        if change == 'same-run':
            updates['run_id'] = 'old-run'
        if change == 'input':
            updates['input'] = 'different'
        if change == 'revoked-grant':
            updates['grant_id'] = 'unknown-grant'
        if change == 'principal':
            updates['scope'] = RequestScope(path=request.scope.path, principal_id='other', channel_id='web')
        resumed = await resumed_request(request, authority, identity, data, **updates)
        model = Model()
        prompt = PromptStrategy()
        if change == 'prompt':
            prompt.system_prompt = 'Changed policy'
        result = await runner.run(resumed, ExecutionStrategies(workflow=ReActWorkflow(model,
            'other-route' if change == 'route' else 'chat', model='other' if change == 'model' else None), prompt=prompt))
        assert result.status is RunStatus.FAILED
        assert model.requests == []
    asyncio.run(check())


@pytest.mark.parametrize('options', [
    {'name': 'agent.delegate', 'origin': 'suiteharness.builtin.agent'},
    {'name': 'suiteharness.result.read', 'origin': 'suiteharness.builtin.result'},
    {'origin': 'plugin.fs'}, {'version': '2'},
    {'effects': frozenset({ToolEffect.WRITE})},
    {'result': {'nested': {'result_id': 'old-result'}}},
    {'result': {'result_reference': {'id': 'old-result'}}},
])
def test_unsafe_tools_and_results_never_publish_completed_checkpoint(options):
    async def check():
        runner, request, _, _, _ = await setup(**options)
        request = request.model_copy(update={'read_only': False})
        model = Model((('unsafe-1', options.get('name', READ)),))
        events = [e async for e in runner.stream(request, ExecutionStrategies(
            workflow=ReActWorkflow(model, 'chat'), prompt=PromptStrategy()))]
        checkpoints = [e for e in events if e.kind == 'checkpoint']
        assert len(checkpoints) == 1
        assert checkpoints[0].data['resume_state']['observations'] == []
    asyncio.run(check())


@pytest.mark.parametrize('mutation', ['result_id', 'delegate', 'write', 'identity', 'count', 'approval'])
def test_resume_rejects_unsafe_observations_and_state(mutation):
    async def check():
        runner, request, authority, identity, _ = await setup()
        data, _ = await checkpoint(runner, request, Model((('read-1', READ),), pause=True))
        state = data['resume_state']
        if mutation == 'result_id':
            state['observations'][0]['result'] = {'result_id': 'old-result'}
        if mutation == 'delegate':
            state['observations'][0]['tool_name'] = 'agent.delegate'
        if mutation == 'write':
            state['observations'][0]['tool_name'] = 'suiteharness.fs.write'
        if mutation == 'identity':
            state['observations'][0]['tool_identity']['version'] = '2'
        if mutation == 'count':
            state['tool_calls_used'] = 0
        if mutation == 'approval':
            state['workflow_state']['approval_token'] = 'old-approval'
        resumed = await resumed_request(request, authority, identity, data)
        model = Model()
        result = await runner.run(resumed, ExecutionStrategies(workflow=ReActWorkflow(model, 'chat'), prompt=PromptStrategy()))
        assert result.status is RunStatus.FAILED
        assert model.requests == []
    asyncio.run(check())


def test_cancel_during_tool_batch_does_not_publish_partial_checkpoint():
    async def check():
        runner, request, _, _, _ = await setup(blocked=True)
        events = []
        async with aclosing(runner.stream(request, ExecutionStrategies(
            workflow=ReActWorkflow(Model((('blocked', READ),)), 'chat'), prompt=PromptStrategy()))) as source:
            async for event in source:
                events.append(event)
                if event.kind == 'tool_started':
                    await runner.cancel(request.scope, request.run_id)
        assert events[-1].result.status is RunStatus.CANCELLED
        assert len([e for e in events if e.kind == 'checkpoint']) == 1
    asyncio.run(check())


def test_same_route_name_with_changed_gateway_model_profile_cannot_resume():
    from suiteharness.models import ModelProfile, ModelRoute

    class ConfiguredModel(Model):
        def __init__(self, model_name, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.model_name = model_name

        def route(self, route_id):
            return ModelRoute(route_id=route_id, primary_profile='primary')

        def profile(self, profile_id):
            return ModelProfile(profile_id=profile_id, provider_id='test', model=self.model_name)

    async def check():
        runner, request, authority, identity, _ = await setup()
        data, _ = await checkpoint(runner, request,
            ConfiguredModel('old-model', (('read-1', READ),), pause=True))
        resumed = await resumed_request(request, authority, identity, data)
        model = ConfiguredModel('new-model')
        result = await runner.run(resumed, ExecutionStrategies(workflow=ReActWorkflow(model, 'chat'), prompt=PromptStrategy()))
        assert result.status is RunStatus.FAILED
        assert model.requests == []
    asyncio.run(check())


def test_resumed_tool_calls_cannot_replenish_the_original_budget():
    async def check():
        runner, request, authority, identity, calls = await setup()
        request = request.model_copy(update={'budget': models.ExecutionBudget(max_tool_calls=1)})
        data, _ = await checkpoint(runner, request, Model((('read-1', READ),), pause=True))
        resumed = await resumed_request(request, authority, identity, data)
        result = await runner.run(resumed, ExecutionStrategies(
            workflow=ReActWorkflow(Model((('read-2', READ),)), 'chat'), prompt=PromptStrategy()))
        assert result.status is RunStatus.BUDGET_EXCEEDED
        assert result.tool_calls_used == 1
        assert calls == [('old-run', 'read-1', True)]
    asyncio.run(check())


def test_checkpoint_too_large_is_omitted_without_failing_execution():
    async def check():
        runner, request, _, _, _ = await setup(
            result={'text': 'x' * 300000}, policy=ExecutionPolicy(max_workflow_state_bytes=512 * 1024))
        events = [e async for e in runner.stream(request, ExecutionStrategies(
            workflow=ReActWorkflow(Model((('read-1', READ),)), 'chat'), prompt=PromptStrategy()))]
        checkpoints = [e for e in events if e.kind == 'checkpoint']
        assert len(checkpoints) == 1
        assert checkpoints[0].data['resume_state']['observations'] == []
        assert all(len(json.dumps(e.data).encode()) < 256 * 1024 for e in checkpoints)
        assert events[-1].result.status is RunStatus.SUCCEEDED
    asyncio.run(check())


def test_current_grant_must_still_authorize_saved_tool_identity():
    async def check():
        runner, request, authority, identity, _ = await setup()
        data, _ = await checkpoint(runner, request, Model((('read-1', READ),), pause=True))
        restricted = await authority.issue(request.scope.path, tool_identities=())
        resumed = await resumed_request(request, authority, identity, data, grant_id=restricted.grant_id)
        model = Model()
        result = await runner.run(resumed, ExecutionStrategies(workflow=ReActWorkflow(model, 'chat'), prompt=PromptStrategy()))
        assert result.status is RunStatus.FAILED
        assert model.requests == []
    asyncio.run(check())


@pytest.mark.parametrize('name,origin', [
    ('agent.delegate', 'suiteharness.builtin.agent'),
    ('suiteharness.result.read', 'suiteharness.builtin.result'),
    (READ, 'plugin.fs'),
])
def test_resumed_new_calls_cannot_invoke_composite_or_untrusted_read_tools(name, origin):
    async def check():
        runner, request, authority, identity, calls = await setup(name=name, origin=origin)
        data, _ = await checkpoint(runner, request, Model(pause=True), after_tools=False)
        resumed = await resumed_request(request, authority, identity, data)
        model = Model((('new-call', name),))
        result = await runner.run(resumed, ExecutionStrategies(workflow=ReActWorkflow(model, 'chat'), prompt=PromptStrategy()))
        assert result.status is RunStatus.FAILED
        assert model.requests[0].tools == ()
        assert calls == []
    asyncio.run(check())


def test_activation_cancel_cannot_reach_another_active_tenant():
    from suiteharness.runtime import RootContext
    from suiteharness.runtime.kernel import ActivatedCustomerBundle, _RunLease

    async def check():
        runner, request, authority, _, _ = await setup()
        root = RootContext()
        tenant = root.tenant('tenant')
        product = tenant.product('product')
        activation = ActivatedCustomerBundle(plan=None, tenant=tenant,
            products={'product': product}, runner=runner, _kernel=None,
            _activation_id='test', _run_leases={'product': _RunLease()})
        foreign_scope = RequestScope(path=ScopePath.agent('foreign', 'product', 'agent', 'session'),
                                     principal_id='seller', channel_id='web')
        foreign_grant = await authority.issue(foreign_scope.path, tool_identities=())
        foreign_request = request.model_copy(update={'scope': foreign_scope, 'grant_id': foreign_grant.grant_id})
        models_waiting = [Model(pause=True), Model(pause=True)]
        tasks = [asyncio.create_task(runner.run(req, ExecutionStrategies(workflow=ReActWorkflow(model, 'chat'))))
                 for req, model in zip((request, foreign_request), models_waiting, strict=True)]
        try:
            await asyncio.wait_for(asyncio.gather(*(m.waiting.wait() for m in models_waiting)), 1)
            assert not await activation.cancel(foreign_scope, 'old-run')
            assert await activation.cancel(request.scope, 'old-run')
            assert (await asyncio.wait_for(tasks[0], 1)).status is RunStatus.CANCELLED
            assert not tasks[1].done()
            assert await runner.cancel(foreign_scope, 'old-run')
            assert (await asyncio.wait_for(tasks[1], 1)).status is RunStatus.CANCELLED
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.run(check())


@pytest.mark.parametrize('name', [
    'suiteharness.fs.list', 'suiteharness.fs.glob', 'suiteharness.fs.grep',
    'suiteharness.web.search', 'suiteharness.web.fetch',
])
def test_other_builtin_read_identities_are_reusable(name):
    async def check():
        family = name.split('.')[1]
        effects = frozenset({ToolEffect.READ, ToolEffect.EXTERNAL}) if family == 'web' else frozenset({ToolEffect.READ})
        runner, request, authority, identity, calls = await setup(
            name=name, origin=f'suiteharness.builtin.{family}', effects=effects)
        data, _ = await checkpoint(runner, request, Model((('read-1', name),), pause=True))
        resumed = await resumed_request(request, authority, identity, data)
        result = await runner.run(resumed, ExecutionStrategies(
            workflow=ReActWorkflow(Model(), 'chat'), prompt=PromptStrategy()))
        assert result.status is RunStatus.SUCCEEDED
        assert calls == [('old-run', 'read-1', True)]
    asyncio.run(check())


def test_completed_read_after_resume_uses_new_run_and_a_fresh_grant():
    async def check():
        runner, request, authority, identity, calls = await setup()
        data, _ = await checkpoint(runner, request, Model((('read-1', READ),), pause=True))
        assert await authority.revoke(request.grant_id)
        resumed = await resumed_request(request, authority, identity, data)
        data2, _ = await checkpoint(runner, resumed, Model((('read-2', READ),), pause=True))
        assert data2['resume_state']['source_run_id'] == 'new-run'
        assert data2['resume_state']['workflow_state']['request_identity']['run_id'] == 'new-run'
        assert data2['resume_state']['iterations_used'] == 2
        assert data2['resume_state']['tool_calls_used'] == 2
        assert calls == [('old-run', 'read-1', True), ('new-run', 'read-2', True)]
    asyncio.run(check())


def test_dynamic_prompt_checkpoint_binds_the_next_model_frame():
    class DynamicPrompt(PromptStrategy):
        async def render(self, request, frame):
            return models.PromptEnvelope(messages=(models.PromptMessage(
                role='user', content=f'iteration={frame.iteration}; observations={len(frame.observations)}',
            ),), metadata={'run_id': request.run_id})

    async def check():
        runner, request, authority, identity, calls = await setup()
        data, _ = await checkpoint(runner, request, Model((('read-1', READ),), pause=True),
                                   prompt=DynamicPrompt())
        assert data['resume_state']['iterations_used'] == 1
        resumed = await resumed_request(request, authority, identity, data)
        model = Model()
        result = await runner.run(resumed, ExecutionStrategies(
            workflow=ReActWorkflow(model, 'chat'), prompt=DynamicPrompt()))
        assert result.status is RunStatus.SUCCEEDED
        assert result.iterations_used == 2
        assert calls == [('old-run', 'read-1', True)]
        messages = [m for m in model.requests[0].messages if m.role is ModelRole.TOOL]
        assert len(messages) == 1 and messages[0].tool_call_id == 'read-1'
    asyncio.run(check())


def test_checkpoint_item_limit_does_not_fail_a_larger_valid_run():
    class ManyReads:
        def __init__(self):
            self.rounds = 0

        async def stream(self, route, request):
            self.rounds += 1
            if self.rounds <= 33:
                for index in range(32):
                    yield ModelStreamEvent(type=ModelStreamEventType.TOOL_CALL_DELTA,
                        tool_call=ModelToolCall(call_id=f'read-{self.rounds}-{index}',
                                               name=READ, arguments={}))
                yield ModelStreamEvent(type=ModelStreamEventType.MESSAGE_END,
                                       finish_reason=FinishReason.TOOL_CALLS)
            else:
                yield ModelStreamEvent(type=ModelStreamEventType.TEXT_DELTA, text_delta='done')
                yield ModelStreamEvent(type=ModelStreamEventType.MESSAGE_END,
                                       finish_reason=FinishReason.STOP)

    async def check():
        runner, request, _, _, calls = await setup(policy=ExecutionPolicy(
            max_iterations=34, max_tool_calls=1056, max_workflow_state_bytes=8 * 1024 * 1024))
        request = request.model_copy(update={'budget': models.ExecutionBudget(
            max_iterations=34, max_tool_calls=1056)})
        events = [event async for event in runner.stream(request, ExecutionStrategies(
            workflow=ReActWorkflow(ManyReads(), 'chat'), prompt=PromptStrategy()))]
        result = events[-1].result
        assert result.status is RunStatus.SUCCEEDED
        assert result.tool_calls_used == len(calls) == 1056
        assert result.iterations_used == 34
        saved = [e.data['resume_state'] for e in events if e.kind == 'checkpoint']
        assert saved and saved[0]['iterations_used'] == 0
        assert all(len(item['observations']) <= 1024 and len(item['call_ids']) <= 1024 for item in saved)
    asyncio.run(check())
