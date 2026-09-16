"""Resume binds effective provider configuration, including inherited endpoints."""

import asyncio

import pytest

from suiteharness.agents.react import ReActWorkflow
from suiteharness.execution.models import RunStatus
from suiteharness.execution.protocols import ExecutionStrategies, PromptStrategy
from suiteharness.models import ModelProfile, ModelRoute
from suiteharness.models.gateway import ModelGateway
from suiteharness.models.protocols import ModelProvider
from suiteharness.models.registry import ProviderRegistry
from suiteharness.models.types import AdapterKind, ProviderAuthKind, ProviderDescriptor
from tests.test_run_resume_runner import READ, Model, checkpoint, resumed_request, setup


class Provider(ModelProvider):
    provider_id = 'test'

    def __init__(self, model):
        self.model = model

    async def complete(self, request):
        raise AssertionError('streaming is required')

    def stream(self, request):
        return self.model.stream('chat', request)


def gateway(model, *, endpoint='https://old-provider.example/v1',
            adapter=AdapterKind.OPENAI_CHAT, aliases=frozenset()):
    registry = ProviderRegistry()
    registry.register(ProviderDescriptor(provider_id='test', display_name='Test provider',
        adapter=adapter, auth=ProviderAuthKind.INTERNAL_NONE,
        default_base_url=endpoint, aliases=aliases),
        lambda descriptor, profile, transport, secret: Provider(model))
    return ModelGateway(registry,
        {'primary': ModelProfile(profile_id='primary', provider_id='test', model='same-model')},
        {'chat': ModelRoute(route_id='chat', primary_profile='primary')},
        transport=None, secrets=None)


@pytest.mark.parametrize('changes', [
    {'endpoint': 'https://different-provider.example/v1'},
    {'adapter': AdapterKind.OPENAI_RESPONSES},
])
def test_resume_rejects_changed_provider_before_sending_saved_evidence(changes):
    async def check():
        runner, request, authority, identity, _ = await setup()
        first = gateway(Model((('read-1', READ),), pause=True))
        data, _ = await checkpoint(runner, request, first)
        resumed = await resumed_request(request, authority, identity, data)
        model = Model()
        result = await runner.run(resumed, ExecutionStrategies(
            workflow=ReActWorkflow(gateway(model, **changes), 'chat'), prompt=PromptStrategy()))
        assert result.status is RunStatus.FAILED
        assert model.requests == []
    asyncio.run(check())


def test_unchanged_provider_recreated_after_restart_can_resume():
    async def check():
        runner, request, authority, identity, calls = await setup()
        first = gateway(Model((('read-1', READ),), pause=True), aliases=frozenset({'b', 'a'}))
        data, _ = await checkpoint(runner, request, first)
        resumed = await resumed_request(request, authority, identity, data)
        model = Model()
        second = gateway(model, aliases=frozenset({'a', 'b'}))
        result = await runner.run(resumed, ExecutionStrategies(
            workflow=ReActWorkflow(second, 'chat'), prompt=PromptStrategy()))
        assert result.status is RunStatus.SUCCEEDED
        assert len(model.requests) == 1
        assert calls == [('old-run', 'read-1', True)]
        assert set(data['resume_state']['route_key']) <= set('0123456789abcdef')
        assert len(data['resume_state']['route_key']) == 64
    asyncio.run(check())
