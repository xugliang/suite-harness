"""Nominal extension contracts and inherited optional defaults."""
import inspect
import asyncio
from abc import ABC

import pytest

from suiteharness.memory.conversation import ConversationMemory, InMemoryConversationMemory
from suiteharness.memory.in_memory import InMemoryProfileProvider, InMemoryCustomer360Provider
from suiteharness.memory.knowledge import InMemoryKnowledgeProvider
from suiteharness.memory.protocols import ProfileProvider, Customer360Provider, KnowledgeProvider
from suiteharness.models.protocols import ModelProvider, SecretResolver, AccessTokenProvider
from suiteharness.models.adapters.openai import OpenAIChatAdapter
from suiteharness.models.adapters.anthropic import AnthropicMessagesAdapter
from suiteharness.models.adapters.gemini import GeminiAdapter
from suiteharness.models.adapters.bedrock import BedrockConverseAdapter
from suiteharness.models.adapters.openai_responses import OpenAIResponsesAdapter
from suiteharness.models.adapters.vertex import VertexGeminiAdapter
from suiteharness.sandbox.protocols import SandboxBackend, SandboxQuarantineStore
from suiteharness.sandbox.docker import DockerSandboxBackend
from suiteharness.sandbox.local import LocalDevelopmentSandboxBackend
from suiteharness.sessions.protocols import SessionStore
from suiteharness.sessions.memory import InMemorySessionStore
from suiteharness.sessions.sqlite import SQLiteSessionStore
from suiteharness.memory.conversation import MemoryTurn, DefaultMemoryResolver
from suiteharness.runtime.scopes import RequestScope, ScopePath
from suiteharness.sessions.models import SessionIdentity, TranscriptEventType


@pytest.mark.parametrize("contract", [ConversationMemory, ProfileProvider, Customer360Provider,
    KnowledgeProvider, ModelProvider, SecretResolver, AccessTokenProvider, SandboxBackend,
    SandboxQuarantineStore, SessionStore])
def test_mandatory_contracts_are_abstract(contract):
    assert issubclass(contract, ABC)
    assert inspect.isabstract(contract)
    with pytest.raises(TypeError):
        contract()


@pytest.mark.parametrize("implementation, contract", [
    (InMemoryConversationMemory, ConversationMemory),
    (InMemoryProfileProvider, ProfileProvider), (InMemoryCustomer360Provider, Customer360Provider),
    (InMemoryKnowledgeProvider, KnowledgeProvider),
    (OpenAIChatAdapter, ModelProvider), (AnthropicMessagesAdapter, ModelProvider),
    (GeminiAdapter, ModelProvider), (BedrockConverseAdapter, ModelProvider),
    (OpenAIResponsesAdapter, ModelProvider), (VertexGeminiAdapter, ModelProvider),
    (DockerSandboxBackend, SandboxBackend), (LocalDevelopmentSandboxBackend, SandboxBackend),
    (InMemorySessionStore, SessionStore), (SQLiteSessionStore, SessionStore),
])
def test_builtins_explicitly_inherit_complete_contracts(implementation, contract):
    assert contract in implementation.__mro__
    assert not inspect.isabstract(implementation)


def test_legacy_profile_subclass_inherits_optional_close():
    class ProductMemory(ProfileProvider):
        name = "old-product"
        def assert_claim(self, *args, **kwargs):
            raise NotImplementedError
        def get_claim(self, *args, **kwargs):
            raise KeyError
        def list_claims(self, *args, **kwargs):
            return ()
        def retract_claim(self, *args, **kwargs):
            raise KeyError

    memory = ProductMemory()
    assert memory.list_claims(None, "customer") == ()
    assert memory.close() is None
    assert "close" not in ProductMemory.__dict__


def test_similar_unrelated_object_is_not_a_memory_provider():
    class Unrelated:
        name = "shape-only"
        def append(self, *args): pass
        def snapshot(self, *args): pass
        def clear(self, *args): pass

    assert not isinstance(Unrelated(), ConversationMemory)


def test_default_and_product_memory_preserve_account_isolation():
    generic = InMemoryConversationMemory()
    product = InMemoryConversationMemory()
    resolver = DefaultMemoryResolver(generic)
    scope_a = RequestScope(ScopePath.product("company", "sales"), "seller-a")
    scope_b = RequestScope(ScopePath.product("company", "sales"), "seller-b")
    assert resolver.resolve(scope_a) is generic
    generic.append(scope_a, "general", MemoryTurn("user", "previous question"))
    assert resolver.resolve(scope_a).snapshot(scope_a, "general").short_term
    assert not generic.snapshot(scope_b, "general").short_term
    resolver.register("company", "sales", product)
    assert resolver.resolve(scope_a) is product
    assert not resolver.resolve(scope_a).snapshot(scope_a, "general").short_term


def test_session_store_abc_preserves_transcript_and_owner_isolation():
    async def run():
        store = InMemorySessionStore()
        identity = SessionIdentity(tenant_id="company", product_id="sales", agent_id="chat",
                                   session_id="general", principal_id="seller-a")
        await store.create(identity)
        await store.append(identity, event_id="message1", event_type=TranscriptEventType.USER_MESSAGE,
                           payload={"text": "previous question"})
        transcript = await store.recent_transcript(identity, limit=10)
        assert transcript[0].payload == {"text": "previous question"}
        other = identity.model_copy(update={"principal_id": "seller-b"})
        assert await store.get(other) is None
    asyncio.run(run())
