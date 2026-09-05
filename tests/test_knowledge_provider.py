"""Knowledge providers authorize spaces before retrieval and ranking."""

from __future__ import annotations

import pytest

from suiteharness.memory import (
    KNOWLEDGE_READ,
    KNOWLEDGE_WRITE,
    InMemoryKnowledgeProvider,
    KnowledgeDocument,
    KnowledgeProvider,
    MemoryCapabilityError,
    MemoryErrorCode,
    RequestScope,
    SpaceRef,
)
from suiteharness.runtime import ScopePath


def _scope(product_id: str, *roles: str, tenant_id: str = "tenant-a") -> RequestScope:
    return RequestScope(
        path=ScopePath.product(tenant_id, product_id),
        principal_id=f"{product_id}-service",
        roles=frozenset(roles),
        purpose="knowledge-test",
    )


def test_reference_provider_satisfies_protocol_and_isolates_products() -> None:
    provider = InMemoryKnowledgeProvider()
    alpha = _scope("alpha", KNOWLEDGE_READ, KNOWLEDGE_WRITE)
    beta = _scope("beta", KNOWLEDGE_READ, KNOWLEDGE_WRITE)
    alpha_space = SpaceRef.private(alpha)

    assert isinstance(provider, KnowledgeProvider)
    assert provider.index(
        alpha,
        [
            KnowledgeDocument(
                document_id="guide-one",
                space=alpha_space,
                text="Alpha reference token",
                metadata={"category": "core"},
            )
        ],
        idempotency_key="index-guide-one",
    ) == ("guide-one",)
    assert provider.search(
        alpha,
        spaces=[alpha_space],
        query="alpha",
        filters={"category": "core"},
        limit=5,
    )[0].document_id == "guide-one"

    with pytest.raises(MemoryCapabilityError) as captured:
        provider.search(
            beta,
            spaces=[alpha_space],
            query="alpha",
            filters={},
            limit=5,
        )
    assert captured.value.code is MemoryErrorCode.PERMISSION_DENIED


def test_index_is_idempotent_but_rejects_key_reuse_and_duplicate_ids() -> None:
    provider = InMemoryKnowledgeProvider()
    scope = _scope("alpha", KNOWLEDGE_READ, KNOWLEDGE_WRITE)
    space = SpaceRef.private(scope)
    document = KnowledgeDocument("doc-1", space, "alpha beta", {})

    first = provider.index(scope, [document], idempotency_key="command-1")
    assert provider.index(scope, [document], idempotency_key="command-1") == first

    with pytest.raises(MemoryCapabilityError) as captured:
        provider.index(
            scope,
            [KnowledgeDocument("doc-2", space, "different", {})],
            idempotency_key="command-1",
        )
    assert captured.value.code is MemoryErrorCode.IDEMPOTENCY_CONFLICT

    with pytest.raises(MemoryCapabilityError) as captured:
        provider.index(scope, [document], idempotency_key="command-2")
    assert captured.value.code is MemoryErrorCode.INVALID_STATE


def test_permissions_are_required_for_read_and_write() -> None:
    provider = InMemoryKnowledgeProvider()
    no_roles = _scope("alpha")
    space = SpaceRef.private(no_roles)

    with pytest.raises(MemoryCapabilityError) as captured:
        provider.index(
            no_roles,
            [KnowledgeDocument("doc", space, "text", {})],
            idempotency_key="index",
        )
    assert captured.value.code is MemoryErrorCode.PERMISSION_DENIED

    with pytest.raises(MemoryCapabilityError) as captured:
        provider.search(no_roles, spaces=[space], query="text", filters={}, limit=1)
    assert captured.value.code is MemoryErrorCode.PERMISSION_DENIED
