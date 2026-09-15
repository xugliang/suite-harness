import asyncio
import pytest

from suiteharness.models.gateway import ModelGateway
from suiteharness.models.protocols import ModelProvider
from suiteharness.models.registry import ProviderRegistry
from suiteharness.models.secrets import MappingSecretResolver
from suiteharness.models.types import (
    AdapterKind, FinishReason, ModelMessage, ModelProfile, ModelRequest,
    ModelResponse, ModelRole, ModelRoute, ModelStreamEvent,
    ModelStreamEventType, ModelUsage, ProviderAuthKind, ProviderDescriptor,
)


class Provider(ModelProvider):
    provider_id = 'test'
    fail = False
    closed = False

    async def complete(self, request):
        if self.fail:
            raise RuntimeError('供应商异常：客户秘密')
        return ModelResponse(response_id='test-response', provider_id='test', model='test',
                             usage=ModelUsage(input_tokens=12, output_tokens=3))

    async def stream(self, request):
        try:
            yield ModelStreamEvent(type=ModelStreamEventType.TEXT_DELTA, text_delta='你好')
            for _ in range(2):
                yield ModelStreamEvent(type=ModelStreamEventType.USAGE,
                                       usage=ModelUsage(input_tokens=12, output_tokens=3))
            yield ModelStreamEvent(type=ModelStreamEventType.MESSAGE_END, finish_reason=FinishReason.STOP)
        finally:
            self.closed = True


def build():
    provider = Provider()
    registry = ProviderRegistry()
    registry.register(ProviderDescriptor(provider_id='test', display_name='test',
        adapter=AdapterKind.OPENAI_CHAT, auth=ProviderAuthKind.INTERNAL_NONE),
        lambda *args: provider)
    gateway = ModelGateway(registry,
        {'test': ModelProfile(profile_id='test', provider_id='test', model='test', max_retries=0)},
        {'test': ModelRoute(route_id='test', primary_profile='test')},
        None, MappingSecretResolver({}))
    request = ModelRequest(messages=(ModelMessage.text(ModelRole.USER, '客户秘密'),))
    return gateway, provider, request


def test_complete_counts_once_without_content():
    gateway, _, request = build()
    asyncio.run(gateway.complete('test', request))
    snapshot = gateway.diagnostics()
    route = snapshot['routes']['test']
    assert route['calls'] == route['succeeded'] == 1
    assert route['input_tokens'] == 12
    assert route['output_tokens'] == 3
    assert '客户秘密' not in str(snapshot)
    route['calls'] = 99
    assert gateway.diagnostics()['routes']['test']['calls'] == 1


def test_stream_usage_is_not_counted_twice():
    gateway, provider, request = build()
    async def run():
        return [event async for event in gateway.stream('test', request)]
    assert len(asyncio.run(run())) == 4
    assert provider.closed
    route = gateway.diagnostics()['routes']['test']
    assert route['calls'] == 1
    assert route['input_tokens'] == 12
    assert route['output_tokens'] == 3


def test_failure_propagates_and_is_counted_without_exception_text():
    gateway, provider, request = build()
    provider.fail = True
    with pytest.raises(RuntimeError):
        asyncio.run(gateway.complete('test', request))
    snapshot = gateway.diagnostics()
    assert snapshot['routes']['test']['failed'] == 1
    assert snapshot['routes']['test']['usage_unknown'] == 1
    assert '客户秘密' not in str(snapshot)


def test_cancelled_stream_closes_provider_and_is_not_success():
    gateway, provider, request = build()
    async def run():
        stream = gateway.stream('test', request)
        await anext(stream)
        await stream.aclose()
        assert provider.closed
    asyncio.run(run())
    assert provider.closed
    assert gateway.diagnostics()['routes']['test']['cancelled'] == 1
