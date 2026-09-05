"""Typed service keys that place memory capabilities at their safe scope."""

from __future__ import annotations

from suiteharness.runtime.scopes import ScopeKind, ServiceKey

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

__all__ = [
    "CUSTOMER360_PROVIDER",
    "EVIDENCE_PROVIDER",
    "KNOWLEDGE_PROVIDER",
    "PROFILE_PROVIDER",
]
