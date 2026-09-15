"""Typed service keys that place memory capabilities at their safe scope."""

from __future__ import annotations

from suiteharness.runtime.scopes import ScopeKind, ServiceKey

from .session_memory import SessionConversationMemory
from .protocols import Customer360Provider, EvidenceProvider, KnowledgeProvider, ProfileProvider

# Private product data capabilities must be bound separately for every ProductContext.
# Reusing an implementation class is fine; accidentally inheriting one live product
# instance from the root or tenant is not.
PROFILE_PROVIDER = ServiceKey(
    name="memory.profile",
    contract=ProfileProvider,
    declared_at=ScopeKind.PRODUCT,
)
EVIDENCE_PROVIDER = ServiceKey(
    name="memory.evidence",
    contract=EvidenceProvider,
    declared_at=ScopeKind.PRODUCT,
)
KNOWLEDGE_PROVIDER = ServiceKey(
    name="knowledge.provider",
    contract=KnowledgeProvider,
    declared_at=ScopeKind.PRODUCT,
)

# Customer360 is a distinct tenant-owned capability. It is absent by default and
# cannot be silently replaced by one product. Its own bilateral policy still gates
# every publish and consume operation.
CUSTOMER360_PROVIDER = ServiceKey(
    name="memory.customer360",
    contract=Customer360Provider,
    declared_at=ScopeKind.TENANT,
)

# Conversation memory is product-owned.  Hosts should bind a product-specific
# provider when one exists; otherwise the runtime resolver supplies the generic
# provider configured by the deployment.
CONVERSATION_MEMORY = ServiceKey(
    name="memory.conversation",
    contract=SessionConversationMemory,
    declared_at=ScopeKind.PRODUCT,
)

__all__ = [
    "CUSTOMER360_PROVIDER",
    "CONVERSATION_MEMORY",
    "EVIDENCE_PROVIDER",
    "KNOWLEDGE_PROVIDER",
    "PROFILE_PROVIDER",
]
