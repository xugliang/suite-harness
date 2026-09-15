import asyncio
from contextlib import aclosing

import pytest

from suiteharness.agents.react import ReActWorkflow
from suiteharness.execution import RunRequest, WorkflowFrame, WorkflowDecision, RunStatus
from suiteharness.execution import ToolSpec, ToolEffect, ToolIntent
from suiteharness.execution.in_memory import (
    InMemoryApprovalStore, InMemoryAuditJournal, InMemoryCapabilityAuthority,
    InMemoryToolRegistry,
)
from suiteharness.execution.protocols import ExecutionStrategies, Workflow
from suiteharness.execution.runner import ExecutionRunner
from suiteharness.models import ModelStreamEvent, ModelStreamEventType, FinishReason, ModelToolCall
from suiteharness.runtime import RequestScope, ScopePath


class PausedGateway:
    def __init__(self, *, truncate=False, tool=False):
        self.release = asyncio.Event()
        self.finished = False
        self.closed = False
        self.truncate = truncate
        self.tool = tool

    async def stream(self, route_id, request):
        assert route_id == 'chat'
        try:
            yield ModelStreamEvent(type=ModelStreamEventType.REASONING_DELTA,
                                   reasoning_delta='hidden reasoning')
            yield ModelStreamEvent(type=ModelStreamEventType.TEXT_DELTA, text_delta='你')
            await self.release.wait()
            if not self.truncate:
                if self.tool:
                    yield ModelStreamEvent(type=ModelStreamEventType.TOOL_CALL_DELTA,
                                           tool_call=ModelToolCall(call_id='call1', name='missing', arguments={}))
                yield ModelStreamEvent(type=ModelStreamEventType.TEXT_DELTA, text_delta='好')
                yield ModelStreamEvent(type=ModelStreamEventType.MESSAGE_END,
                                       finish_reason=FinishReason.TOOL_CALLS if self.tool else FinishReason.STOP)
            self.finished = True
        finally:
            self.closed = True


async def components():
    authority = InMemoryCapabilityAuthority()
    scope = RequestScope(path=ScopePath.product('company', 'product'), principal_id='seller')
    grant = await authority.issue(scope.path, tool_identities=())
    runner = ExecutionRunner(tools=InMemoryToolRegistry(), capabilities=authority,
                             approvals=InMemoryApprovalStore(), journal=InMemoryAuditJournal())
    return runner, RunRequest(run_id='stream-test', scope=scope, grant_id=grant.grant_id, input='hello')


def test_react_text_arrives_before_model_finishes_without_reasoning():
    async def check():
        _, request = await components()
        gateway = PausedGateway()
        workflow = ReActWorkflow(gateway=gateway, route_id='chat')
        frame = WorkflowFrame(iteration=1, tool_calls_used=0, elapsed_seconds=0.0)
        async with aclosing(workflow.stream(request, frame, None)) as source:
            first = await asyncio.wait_for(anext(source), 1)
            assert first.text == '你'
            assert not gateway.finished
            gateway.release.set()
            rest = [item async for item in source]
        assert rest[-1].output == '你好'
        assert all('hidden reasoning' not in str(item) for item in [first, *rest])
        assert gateway.closed
    asyncio.run(check())


def test_runner_emits_live_delta_and_authoritative_result_once():
    async def check():
        runner, request = await components()
        gateway = PausedGateway()
        strategies = ExecutionStrategies(workflow=ReActWorkflow(gateway, 'chat'))
        async with aclosing(runner.stream(request, strategies)) as source:
            events = [await asyncio.wait_for(anext(source), 1)]
            assert events[0].kind == 'started'
            events.append(await asyncio.wait_for(anext(source), 1))
            assert events[-1].text == '你'
            assert not gateway.finished
            gateway.release.set()
            events.extend([item async for item in source])
        assert [e.sequence for e in events] == list(range(1, len(events) + 1))
        assert all(e.run_id == request.run_id for e in events)
        results = [e.result for e in events if e.result is not None]
        assert len(results) == 1
        assert results[0].status is RunStatus.SUCCEEDED
        assert results[0].output == '你好'
    asyncio.run(check())


def test_truncated_stream_is_not_saved_as_success():
    async def check():
        runner, request = await components()
        gateway = PausedGateway(truncate=True)
        gateway.release.set()
        result = await runner.run(request, ExecutionStrategies(workflow=ReActWorkflow(gateway, 'chat')))
        assert result.status is RunStatus.FAILED
        assert result.output is None
        assert gateway.closed
    asyncio.run(check())


def test_closing_stream_cancels_model_and_releases_active_run():
    async def check():
        runner, request = await components()
        gateway = PausedGateway()
        strategies = ExecutionStrategies(workflow=ReActWorkflow(gateway, 'chat'))
        source = runner.stream(request, strategies)
        await anext(source)
        await anext(source)
        await asyncio.wait_for(source.aclose(), 1)
        assert gateway.closed
        assert not await runner.cancel(request.scope, request.run_id)
    asyncio.run(check())


def test_single_decision_workflow_uses_default_stream():
    class Answer(Workflow):
        async def next(self, request, frame, prompt):
            return WorkflowDecision.final('done')
    async def check():
        runner, request = await components()
        events = [e async for e in runner.stream(request, ExecutionStrategies(workflow=Answer()))]
        assert events[-1].result.output == 'done'
    asyncio.run(check())


@pytest.mark.parametrize('read_only,expected_calls', [(False, 1), (True, 0)])
def test_tool_live_events_do_not_bypass_authorization(read_only, expected_calls):
    class ToolWorkflow(Workflow):
        async def next(self, request, frame, prompt):
            if frame.observations:
                return WorkflowDecision.final('done')
            return WorkflowDecision.tools(ToolIntent(call_id='write1', tool_name='write', arguments={}))
    async def check():
        from suiteharness.execution.runner import ExecutionPolicy
        authority = InMemoryCapabilityAuthority()
        registry = InMemoryToolRegistry()
        scope = RequestScope(path=ScopePath.product('company', 'product'), principal_id='seller')
        calls = []
        async def handler(context, arguments):
            calls.append(context.call_id)
            return {'private_value': 'not-a-public-event'}
        registry.register(scope.path, ToolSpec(name='write', description='write',
                          input_schema={'type': 'object'}, effects=frozenset({ToolEffect.WRITE})), handler)
        grant = await authority.issue(scope.path, tool_identities=(registry.resolve(scope, 'write').identity,))
        runner = ExecutionRunner(tools=registry, capabilities=authority,
                                 approvals=InMemoryApprovalStore(), journal=InMemoryAuditJournal(),
                                 policy=ExecutionPolicy(approval_effects=frozenset()))
        request = RunRequest(run_id='write-test', scope=scope, grant_id=grant.grant_id,
                             input='write', read_only=read_only)
        events = [e async for e in runner.stream(request, ExecutionStrategies(workflow=ToolWorkflow()))]
        assert len(calls) == expected_calls
        assert len([e for e in events if e.kind == 'tool_started']) == expected_calls
        completed = [e for e in events if e.kind == 'tool_completed']
        assert len(completed) == 1
        assert completed[0].data['call_id'] == 'write1'
        assert 'private_value' not in str(completed[0].data)
    asyncio.run(check())


@pytest.mark.parametrize('reason,text', [(FinishReason.LENGTH, 'partial'),
    (FinishReason.CONTENT_FILTER, 'partial'), (FinishReason.UNKNOWN, 'partial'),
    (FinishReason.ERROR, 'partial'), (FinishReason.CANCELLED, 'partial'), (FinishReason.STOP, '')])
def test_non_success_finish_reason_and_empty_answer_never_succeed(reason, text):
    class Gateway:
        async def stream(self, route_id, request):
            yield ModelStreamEvent(type=ModelStreamEventType.TEXT_DELTA, text_delta=text)
            yield ModelStreamEvent(type=ModelStreamEventType.MESSAGE_END, finish_reason=reason)
    async def check():
        runner, request = await components()
        result = await runner.run(request, ExecutionStrategies(workflow=ReActWorkflow(Gateway(), 'chat')))
        assert result.status is RunStatus.FAILED
    asyncio.run(check())


def test_close_does_not_hang_if_workflow_swallows_one_cancel_and_keeps_emitting():
    class SwallowOnce(Workflow):
        def __init__(self):
            self.waiting = asyncio.Event()
            self.closed = False

        async def next(self, request, frame, prompt):
            raise AssertionError('must use stream')

        async def stream(self, request, frame, prompt):
            from suiteharness.execution.events import WorkflowEvent
            try:
                yield WorkflowEvent(kind='delta', text='first')
                self.waiting.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    for _ in range(40):
                        yield WorkflowEvent(kind='delta', text='late')
                yield WorkflowDecision.final('done')
            finally:
                self.closed = True
    async def check():
        runner, request = await components()
        workflow = SwallowOnce()
        source = runner.stream(request, ExecutionStrategies(workflow=workflow))
        await anext(source)
        await anext(source)
        await workflow.waiting.wait()
        await asyncio.wait_for(source.aclose(), 0.3)
        assert workflow.closed
        assert not await runner.cancel(request.scope, request.run_id)
    asyncio.run(check())


@pytest.mark.parametrize('terminal,expected', [(None, RunStatus.FAILED), ('stop', RunStatus.SUCCEEDED)])
def test_real_chat_adapter_requires_native_finish_evidence_before_eof(terminal, expected):
    import json
    from suiteharness.models.adapters.openai import OpenAIChatAdapter
    from suiteharness.models.types import AdapterKind, ModelProfile, ProviderAuthKind, ProviderDescriptor
    class Transport:
        async def stream(self, request):
            yield ('data: ' + json.dumps({'id': 'response1', 'choices': [
                {'delta': {'content': 'answer'}, 'finish_reason': terminal}]}) + '\n\n').encode()
    descriptor = ProviderDescriptor(provider_id='test', display_name='test',
        adapter=AdapterKind.OPENAI_CHAT, auth=ProviderAuthKind.INTERNAL_NONE,
        default_base_url='https://model.example/v1')
    adapter = OpenAIChatAdapter(descriptor, ModelProfile(profile_id='test', provider_id='test', model='test'),
        Transport(), None)
    class Gateway:
        def stream(self, route, request):
            return adapter.stream(request)
    async def check():
        runner, request = await components()
        result = await runner.run(request, ExecutionStrategies(workflow=ReActWorkflow(Gateway(), 'chat')))
        assert result.status is expected
    asyncio.run(check())


def test_truncated_tool_round_does_not_produce_tool_intents():
    from suiteharness.models import ModelToolCall
    from suiteharness.agents.react import ReActStateError
    class Gateway:
        async def stream(self, route, request):
            yield ModelStreamEvent(type=ModelStreamEventType.TOOL_CALL_DELTA,
                tool_call=ModelToolCall(call_id='call1', name='write', arguments={}))
            yield ModelStreamEvent(type=ModelStreamEventType.MESSAGE_END, finish_reason=FinishReason.LENGTH)
    async def check():
        _, request = await components()
        with pytest.raises(ReActStateError, match='not finish successfully'):
            await ReActWorkflow(Gateway(), 'chat').next(request,
                WorkflowFrame(iteration=1, tool_calls_used=0, elapsed_seconds=0.0), None)
    asyncio.run(check())
