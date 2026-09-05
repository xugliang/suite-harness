"""Scope identity and immutable service-binding invariants."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError

import pytest

from suiteharness.runtime.scopes import (
    RequestScope,
    RootContext,
    ScopeKind,
    ScopePath,
    ServiceBindingError,
    ServiceBindings,
    ServiceKey,
)


class Store:
    pass


class MemoryStore(Store):
    def __init__(self, name: str) -> None:
        self.name = name


class Gate:
    pass


STORE = ServiceKey(
    "memory.store",
    Store,
    ScopeKind.ROOT,
    frozenset({ScopeKind.TENANT, ScopeKind.PRODUCT, ScopeKind.AGENT}),
)
GATE = ServiceKey("security.gate", Gate, ScopeKind.ROOT)


def test_scope_path_rejects_incomplete_or_mixed_hierarchy() -> None:
    assert ScopePath.root().parent() is None
    assert ScopePath.product("tenant-a", "alpha").parent() == ScopePath.tenant("tenant-a")
    with pytest.raises(ValueError):
        ScopePath(ScopeKind.PRODUCT, tenant_id="tenant-a")
    with pytest.raises(ValueError):
        ScopePath(ScopeKind.ROOT, tenant_id="tenant-a")
    with pytest.raises(ValueError):
        ScopePath.agent("tenant-a", "alpha", "", "session-a")


def test_request_scope_is_frozen_and_server_scoped() -> None:
    path = ScopePath.agent("tenant-a", "alpha", "agent-a", "session-a")
    request = RequestScope(
        path=path,
        principal_id="user-1",
        roles={"product-role", "reader"},  # type: ignore[arg-type]
        request_id="req-1",
    )
    assert request.tenant_id == "tenant-a"
    assert request.product_id == "alpha"
    assert request.roles == frozenset({"product-role", "reader"})
    with pytest.raises(FrozenInstanceError):
        request.purpose = "other"  # type: ignore[misc]
    with pytest.raises(ValueError):
        RequestScope(path=ScopePath.tenant("tenant-a"), principal_id="user-1")


def test_bindings_resolve_parent_and_permitted_overrides() -> None:
    root_store = MemoryStore("root")
    tenant_store = MemoryStore("tenant")
    product_store = MemoryStore("product")
    root = ServiceBindings.root({STORE: root_store, GATE: Gate()})
    tenant = root.derive(ScopeKind.TENANT, {STORE: tenant_store})
    product = tenant.derive(ScopeKind.PRODUCT, {STORE: product_store})
    agent = product.derive(ScopeKind.AGENT)

    assert root.resolve(STORE) is root_store
    assert tenant.resolve(STORE) is tenant_store
    assert agent.resolve(STORE) is product_store
    assert agent.resolve(GATE) is root.resolve(GATE)
    assert agent.snapshot()["memory.store"] is product_store
    with pytest.raises(TypeError):
        agent.snapshot()["memory.store"] = root_store  # type: ignore[index]


def test_bindings_enforce_contract_and_override_policy() -> None:
    root = ServiceBindings.root({STORE: MemoryStore("root"), GATE: Gate()})
    tenant = root.derive(ScopeKind.TENANT)
    with pytest.raises(ServiceBindingError):
        tenant.derive(ScopeKind.PRODUCT, {GATE: Gate()})
    with pytest.raises(TypeError):
        root.derive(ScopeKind.TENANT, {STORE: object()})
    incompatible = ServiceKey(
        "memory.store",
        Store,
        ScopeKind.ROOT,
        frozenset({ScopeKind.PRODUCT}),
    )
    with pytest.raises(ServiceBindingError):
        tenant.derive(ScopeKind.PRODUCT, {incompatible: MemoryStore("bad-key")})
    with pytest.raises(ServiceBindingError):
        root.derive(ScopeKind.PRODUCT)


def test_context_tree_resolves_services_and_closes_children() -> None:
    async def exercise() -> list[str]:
        events: list[str] = []
        root = RootContext(bindings=ServiceBindings.root({STORE: MemoryStore("root")}))
        root.effects.callback("root-effect", lambda: events.append("root"))
        tenant = root.tenant("tenant-a")
        tenant.effects.callback("tenant-effect", lambda: events.append("tenant"))
        product = tenant.product("alpha", {STORE: MemoryStore("alpha")})
        product.effects.callback("product-effect", lambda: events.append("product"))
        agent = product.agent(
            "agent-a",
            "session-a",
            principal_id="user-1",
            channel_id="web",
            roles=frozenset({"product-role"}),
        )
        agent.effects.callback("agent-effect", lambda: events.append("agent"))

        assert agent.path.parent() == product.path
        assert agent.resolve(STORE).name == "alpha"
        assert agent.request.tenant_id == "tenant-a"
        assert agent.request.channel_id == "web"
        await root.close()
        return events

    assert asyncio.run(exercise()) == ["agent", "product", "tenant", "root"]
