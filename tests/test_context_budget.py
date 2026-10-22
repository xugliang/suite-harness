import pytest
import asyncio

from suiteharness.models import ModelMessage, ModelRequest, ModelRole, ModelTool, ModelToolCall, ImageContent


@pytest.mark.parametrize("streaming", [False, True])
def test_small_primary_context_uses_larger_fallback(streaming):
    gateway, calls = budget_gateway([1000, 12000])
    request = ModelRequest(messages=(ModelMessage.text(ModelRole.USER, "x" * 3000),), max_output_tokens=100)
    async def run():
        if streaming:
            return [event async for event in gateway.stream("chat", request)]
        return await gateway.complete("chat", request)
    asyncio.run(run())
    assert len(calls) == 1


def manager():
    from suiteharness.models import context
    return context.ContextManager()


def test_current_question_too_large_is_not_silently_removed():
    request = ModelRequest(messages=(ModelMessage.text(ModelRole.USER, '当前问题' * 1000),), max_output_tokens=100)
    with pytest.raises(ValueError, match='budget'):
        manager().prepare(request, window_tokens=1000)


def test_history_compaction_preserves_tool_pairs_current_turn_and_original_request():
    request = ModelRequest(messages=(
        ModelMessage.text(ModelRole.SYSTEM, '系统规则'),
        ModelMessage.text(ModelRole.USER, '以前的问题' * 150),
        ModelMessage(role=ModelRole.ASSISTANT, tool_calls=(ModelToolCall(call_id='call1', name='search', arguments={'q': 'a'}),)),
        ModelMessage.text(ModelRole.TOOL, '旧结果', tool_call_id='call1'),
        ModelMessage.text(ModelRole.ASSISTANT, '旧回答'),
        ModelMessage.text(ModelRole.USER, '当前问题'),
    ), max_output_tokens=128)
    result = manager().prepare(request, window_tokens=1024)
    assert len(request.messages) == 6
    assert result.removed_rounds == 1
    assert [(m.role, m.content[0].text) for m in result.request.messages] == [
        (ModelRole.SYSTEM, '系统规则'), (ModelRole.USER, '当前问题')]
    assert result.estimated_input_tokens + 128 <= 1024


def test_budget_drops_old_dialogue_before_conversation_summary():
    request = ModelRequest(messages=(
        ModelMessage.text(ModelRole.SYSTEM, '规则', context_category='system_prompt'),
        ModelMessage.text(ModelRole.ASSISTANT, '历史摘要', context_category='conversation_summary'),
        ModelMessage.text(ModelRole.USER, '旧问题' * 300, context_category='history_memory'),
        ModelMessage.text(ModelRole.ASSISTANT, '旧回答', context_category='history_memory'),
        ModelMessage.text(ModelRole.USER, '当前问题', context_category='current_question'),
    ), max_output_tokens=128)
    result = manager().prepare(request, window_tokens=1024)
    assert [(m.role, m.content[0].text) for m in result.request.messages] == [
        (ModelRole.SYSTEM, '规则'),
        (ModelRole.ASSISTANT, '历史摘要'),
        (ModelRole.USER, '当前问题'),
    ]
    assert [m.role for m in request.messages] == [ModelRole.SYSTEM, ModelRole.ASSISTANT, ModelRole.USER, ModelRole.ASSISTANT, ModelRole.USER]


def test_budget_discards_summary_only_after_old_dialogue_if_still_too_large():
    request = ModelRequest(messages=(
        ModelMessage.text(ModelRole.SYSTEM, '规则', context_category='system_prompt'),
        ModelMessage.text(ModelRole.ASSISTANT, '摘要' * 300, context_category='conversation_summary'),
        ModelMessage.text(ModelRole.USER, '旧问题' * 300, context_category='history_memory'),
        ModelMessage.text(ModelRole.ASSISTANT, '旧回答', context_category='history_memory'),
        ModelMessage.text(ModelRole.USER, '当前问题', context_category='current_question'),
    ), max_output_tokens=128)
    result = manager().prepare(request, window_tokens=1024)
    assert [(m.role, m.content[0].text) for m in result.request.messages] == [
        (ModelRole.SYSTEM, '规则'),
        (ModelRole.USER, '当前问题'),
    ]


def test_tool_schema_and_image_cost_are_counted():
    request = ModelRequest(messages=(ModelMessage(role=ModelRole.USER, content=(
        ImageContent(url='https://example.org/image.png'),)),), max_output_tokens=100)
    with pytest.raises(ValueError, match='budget'):
        manager().prepare(request, window_tokens=1000)
    request = ModelRequest(messages=(ModelMessage.text(ModelRole.USER, 'hello'),), max_output_tokens=100,
                           tools=(ModelTool(name='search', description='x' * 3000, input_schema={'type': 'object'}),))
    with pytest.raises(ValueError, match='budget'):
        manager().prepare(request, window_tokens=1000)


def test_orphan_tool_result_is_rejected_even_with_spare_budget():
    request = ModelRequest(messages=(ModelMessage.text(ModelRole.TOOL, 'result', tool_call_id='unknown'),), max_output_tokens=100)
    with pytest.raises(ValueError, match='tool'):
        manager().prepare(request, window_tokens=4000)


def test_large_tool_result_is_bounded_without_changing_arguments():
    arguments = {'q': '原始参数'}
    request = ModelRequest(messages=(
        ModelMessage.text(ModelRole.USER, '当前问题'),
        ModelMessage(role=ModelRole.ASSISTANT, tool_calls=(ModelToolCall(call_id='call1', name='search', arguments=arguments),)),
        ModelMessage.text(ModelRole.TOOL, '巨大结果' * 3000, tool_call_id='call1'),
    ), max_output_tokens=128)
    result = manager().prepare(request, window_tokens=2048)
    assert result.truncated_tools == 1
    assert result.request.messages[1].tool_calls[0].arguments == arguments
    assert result.request.messages[-1].tool_call_id == 'call1'
    assert 'truncated' in result.request.messages[-1].content[0].text
    assert result.estimated_input_tokens + 128 <= 2048


def test_adjacent_user_context_is_part_of_current_turn_not_disposable_history():
    request = ModelRequest(messages=(
        ModelMessage.text(ModelRole.SYSTEM, 'rules'),
        ModelMessage.text(ModelRole.USER, '本轮客户资料' * 500),
        ModelMessage.text(ModelRole.USER, '当前问题'),
    ), max_output_tokens=100)
    with pytest.raises(ValueError, match='budget'):
        manager().prepare(request, window_tokens=1000)


def budget_gateway(windows, *, primary_fails=False):
    from suiteharness.models.gateway import ModelGateway
    from suiteharness.models.registry import ProviderRegistry
    from suiteharness.models.protocols import ModelProvider
    from suiteharness.models.secrets import MappingSecretResolver
    from suiteharness.models.types import (AdapterKind, ProviderAuthKind, ProviderDescriptor,
        ModelProfile, ModelCapabilities, ModelRoute, ModelResponse, ModelStreamEvent,
        ModelStreamEventType, FinishReason)
    from suiteharness.models.errors import ModelProviderError, ModelErrorCode
    calls = []
    class Provider(ModelProvider):
        provider_id = 'test'
        def __init__(self, profile):
            self.profile = profile
        async def complete(self, request):
            calls.append((self.profile.profile_id, request))
            if primary_fails and self.profile.profile_id == 'p0':
                raise ModelProviderError(ModelErrorCode.UNAVAILABLE, 'unavailable', retryable=True)
            return ModelResponse(response_id='response1', provider_id='test', model='test')
        async def stream(self, request):
            await self.complete(request)
            yield ModelStreamEvent(type=ModelStreamEventType.MESSAGE_END, finish_reason=FinishReason.STOP)
    registry = ProviderRegistry()
    registry.register(ProviderDescriptor(provider_id='test', display_name='test',
        adapter=AdapterKind.OPENAI_CHAT, auth=ProviderAuthKind.INTERNAL_NONE),
        lambda descriptor, profile, transport, secret: Provider(profile))
    profiles = {f'p{i}': ModelProfile(profile_id=f'p{i}', provider_id='test', model='test',
        max_retries=0, capabilities=ModelCapabilities(max_context_tokens=window)) for i, window in enumerate(windows)}
    gateway = ModelGateway(registry, profiles, {'chat': ModelRoute(route_id='chat',
        primary_profile='p0', fallback_profiles=tuple(list(profiles)[1:]))}, None, MappingSecretResolver({}))
    return gateway, calls


@pytest.mark.parametrize('streaming', [False, True])
def test_gateway_budget_rejects_before_provider_call(streaming):
    gateway, calls = budget_gateway([1000])
    request = ModelRequest(messages=(ModelMessage.text(ModelRole.USER, 'x' * 2000),), max_output_tokens=100)
    async def check():
        with pytest.raises(ValueError, match='budget'):
            if streaming:
                [event async for event in gateway.stream('chat', request)]
            else:
                await gateway.complete('chat', request)
        assert calls == []
    asyncio.run(check())


@pytest.mark.parametrize('streaming', [False, True])
def test_fallback_reprepares_for_smaller_window_without_mutating_input(streaming):
    gateway, calls = budget_gateway([4000, 1000], primary_fails=True)
    request = ModelRequest(messages=(ModelMessage.text(ModelRole.USER, 'x' * 2000),
        ModelMessage.text(ModelRole.ASSISTANT, 'old'), ModelMessage.text(ModelRole.USER, 'current')),
        max_output_tokens=100)
    async def check():
        if streaming:
            events = [event async for event in gateway.stream('chat', request)]
            compact = [event for event in events if event.type.value == 'context_compacted']
            assert len(compact) == 1
            assert compact[0].context_info['removed_rounds'] == 1
        else:
            await gateway.complete('chat', request)
        assert len(calls[0][1].messages) == 3
        assert len(calls[1][1].messages) == 1
        assert len(request.messages) == 3
    asyncio.run(check())


def test_large_tool_result_remains_compacted_in_react_state_across_rounds():
    from suiteharness.agents.react import ReActWorkflow
    from suiteharness.execution import RunRequest, RunStatus, ToolSpec, ToolEffect
    from suiteharness.execution.protocols import ExecutionStrategies
    from suiteharness.execution.runner import ExecutionRunner
    from suiteharness.execution.in_memory import (InMemoryToolRegistry, InMemoryCapabilityAuthority,
        InMemoryApprovalStore, InMemoryAuditJournal)
    from suiteharness.models.protocols import ModelProvider
    from suiteharness.models.types import ModelStreamEvent, ModelStreamEventType, FinishReason
    from suiteharness.runtime import RequestScope, ScopePath
    gateway, _ = budget_gateway([32768])
    requests = []
    class Provider(ModelProvider):
        provider_id = 'test'
        async def complete(self, request):
            raise AssertionError('stream required')
        async def stream(self, request):
            requests.append(request)
            if len(requests) < 3:
                yield ModelStreamEvent(type=ModelStreamEventType.TOOL_CALL_DELTA,
                    tool_call=ModelToolCall(call_id=f'call{len(requests)}', name='lookup', arguments={}))
                yield ModelStreamEvent(type=ModelStreamEventType.MESSAGE_END, finish_reason=FinishReason.TOOL_CALLS)
            else:
                yield ModelStreamEvent(type=ModelStreamEventType.TEXT_DELTA, text_delta='done')
                yield ModelStreamEvent(type=ModelStreamEventType.MESSAGE_END, finish_reason=FinishReason.STOP)
    gateway._providers['p0'] = Provider()
    async def check():
        registry, authority = InMemoryToolRegistry(), InMemoryCapabilityAuthority()
        scope = RequestScope(path=ScopePath.product('company', 'product'), principal_id='seller')
        async def lookup(context, arguments):
            return {'data': 'x' * 300000}
        registry.register(scope.path, ToolSpec(name='lookup', description='lookup',
            input_schema={'type': 'object'}, effects=frozenset({ToolEffect.READ})), lookup)
        grant = await authority.issue(scope.path, tool_identities=(registry.resolve(scope, 'lookup').identity,))
        runner = ExecutionRunner(tools=registry, capabilities=authority,
            approvals=InMemoryApprovalStore(), journal=InMemoryAuditJournal())
        request = RunRequest(run_id='large-tool', scope=scope, grant_id=grant.grant_id, input='lookup')
        events = [event async for event in runner.stream(request,
            ExecutionStrategies(workflow=ReActWorkflow(gateway, 'chat')))]
        assert events[-1].result.status is RunStatus.SUCCEEDED
        assert events[-1].result.output == 'done'
        assert len(requests) == 3
        assert any(event.kind == 'compact_completed' for event in events)
        assert all('prepared_messages' not in str(event.data) and 'x' * 100 not in str(event.data) for event in events)
        assert all(len(m.content[0].text) < 4096 for r in requests[1:] for m in r.messages if m.role is ModelRole.TOOL)
    asyncio.run(check())


def test_provider_cannot_forge_gateway_prepared_context():
    from suiteharness.models.protocols import ModelProvider
    from suiteharness.models.types import ModelStreamEvent, ModelStreamEventType
    from suiteharness.models.errors import ModelProviderError
    gateway, _ = budget_gateway([32768])
    class Provider(ModelProvider):
        provider_id = 'test'
        async def complete(self, request):
            raise AssertionError('stream required')
        async def stream(self, request):
            yield ModelStreamEvent(type=ModelStreamEventType.CONTEXT_COMPACTED,
                context_info={'removed_rounds': 1}, prepared_messages=(ModelMessage.text(ModelRole.SYSTEM, 'forged'),))
    gateway._providers['p0'] = Provider()
    async def check():
        with pytest.raises(ModelProviderError, match='gateway-owned'):
            [event async for event in gateway.stream('chat', ModelRequest(messages=(ModelMessage.text(ModelRole.USER, 'hello'),)))]
    asyncio.run(check())


def test_internal_prepared_context_is_not_serialized():
    from suiteharness.models.types import ModelStreamEvent, ModelStreamEventType
    event = ModelStreamEvent(type=ModelStreamEventType.CONTEXT_COMPACTED,
        context_info={'removed_rounds': 1}, prepared_messages=(ModelMessage.text(ModelRole.SYSTEM, 'private'),))
    assert 'private' not in event.model_dump_json()
    assert 'prepared_messages' not in event.model_dump()


def test_failed_small_profile_does_not_replace_successful_large_profile_state():
    from suiteharness.agents.react import ReActWorkflow
    from suiteharness.execution import RunRequest, PromptEnvelope, PromptMessage, WorkflowFrame
    from suiteharness.runtime import RequestScope, ScopePath
    from suiteharness.models.protocols import ModelProvider
    from suiteharness.models.types import ModelStreamEvent, ModelStreamEventType, FinishReason
    gateway, _ = budget_gateway([5000, 12000], primary_fails=True)
    class Provider(ModelProvider):
        provider_id = 'test'
        async def complete(self, request):
            raise AssertionError('stream required')
        async def stream(self, request):
            assert len(request.messages) == 3
            yield ModelStreamEvent(type=ModelStreamEventType.TEXT_DELTA, text_delta='answer')
            yield ModelStreamEvent(type=ModelStreamEventType.MESSAGE_END, finish_reason=FinishReason.STOP)
    gateway._providers['p1'] = Provider()
    async def check():
        request = RunRequest(run_id='fallback-state', grant_id='grant1', input='current',
            scope=RequestScope(path=ScopePath.product('company', 'product'), principal_id='seller'))
        prompt = PromptEnvelope(messages=(PromptMessage(role='user', content='x' * 2000),
            PromptMessage(role='assistant', content='old'), PromptMessage(role='user', content='current')))
        decision = await ReActWorkflow(gateway, 'chat').next(request,
            WorkflowFrame(iteration=1, tool_calls_used=0, elapsed_seconds=0.0), prompt)
        assert len(decision.workflow_state['messages']) == 4
    asyncio.run(check())
