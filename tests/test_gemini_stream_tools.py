import asyncio
import json

import pytest
from pydantic import SecretStr

from suiteharness.models.adapters.gemini import GeminiAdapter
from suiteharness.models.types import (AdapterKind, ModelProfile, ProviderDescriptor, ProviderAuthKind,
    ModelMessage, ModelRequest, ModelRole, FinishReason, ModelStreamEventType)


@pytest.mark.parametrize('terminal,expected', [('STOP', FinishReason.TOOL_CALLS),
    ('MAX_TOKENS', FinishReason.LENGTH), (None, FinishReason.UNKNOWN)])
@pytest.mark.parametrize('split', [False, True])
@pytest.mark.parametrize('provider', ['gemini', 'vertex'])
def test_gemini_normalizes_only_successful_function_rounds(terminal, expected, split, provider):
    tool = {'functionCall': {'id': 'call1', 'name': 'lookup', 'args': {'q': 'test'}}}
    class Transport:
        async def stream(self, request):
            parts = [({'parts': [tool]}, None), ({'parts': []}, terminal)] if split else [({'parts': [tool]}, terminal)]
            for content, reason in parts:
                payload = {'responseId': 'response1', 'candidates': [{'content': content, 'finishReason': reason}]}
                yield ('data: ' + json.dumps(payload) + '\n\n').encode()
    descriptor = ProviderDescriptor(provider_id='gemini', display_name='Gemini', adapter=AdapterKind.GEMINI,
        auth=ProviderAuthKind.API_KEY, default_base_url='https://model.example/v1')
    profile = ModelProfile(profile_id='gemini', provider_id='gemini', model='gemini',
        provider_options={'project': 'test-project'})
    if provider == 'vertex':
        from suiteharness.models.adapters.vertex import VertexGeminiAdapter
        class Token:
            async def access_token(self, audience):
                return 'test-placeholder'
        adapter = VertexGeminiAdapter(descriptor, profile, Transport(), Token())
    else:
        adapter = GeminiAdapter(descriptor, profile, Transport(), SecretStr('test-placeholder'))
    request = ModelRequest(messages=(ModelMessage.text(ModelRole.USER, 'lookup'),))
    async def check():
        events = [event async for event in adapter.stream(request)]
        assert events[-1].finish_reason is expected
        assert len([event for event in events if event.type is ModelStreamEventType.TOOL_CALL_DELTA]) == 1
    asyncio.run(check())
