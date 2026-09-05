from __future__ import annotations

import pytest

from suiteharness.execution import ToolCallContext, ToolEffect, ToolIdentity, ToolSpec
from suiteharness.runtime import RequestScope, ScopePath
from suiteharness.tools import ProductEgressArgumentPolicy


def _context(product_id: str) -> ToolCallContext:
    scope = RequestScope(
        ScopePath.agent("acme", product_id, "agent", "session"),
        "user",
    )
    identity = ToolIdentity(
        namespace="suiteharness",
        name="suiteharness.web.fetch",
        origin="suiteharness.builtin.web",
        version="1",
    )
    return ToolCallContext(
        scope=scope,
        run_id="run",
        call_id="call",
        tool_identity=identity,
        tool=ToolSpec(
            name="suiteharness.web.fetch",
            effects=frozenset({ToolEffect.READ, ToolEffect.EXTERNAL}),
        ),
        remaining_seconds=10,
    )


def test_egress_arguments_cannot_cross_product_allowlists() -> None:
    policy = ProductEgressArgumentPolicy(
        default_search_provider="company-search",
        default_fetch_route="company-direct",
        bash_profiles_by_product={
            "product-a": frozenset({"network-a"}),
            "product-b": frozenset({"network-b"}),
        },
        search_providers_by_product={
            "product-a": frozenset({"company-search", "search-a"}),
            "product-b": frozenset({"company-search", "search-b"}),
        },
        fetch_routes_by_product={
            "product-a": frozenset({"company-direct", "proxy-a"}),
            "product-b": frozenset({"company-direct", "proxy-b"}),
        },
    )
    product_a = _context("product-a")

    assert policy.authorize_bash(product_a, "network-a") == "network-a"
    assert policy.authorize_search(product_a, None) == "company-search"
    assert policy.authorize_fetch(product_a, "proxy-a") == "proxy-a"
    with pytest.raises(PermissionError, match="this product"):
        policy.authorize_bash(product_a, "network-b")
    with pytest.raises(PermissionError, match="this product"):
        policy.authorize_search(product_a, "search-b")
    with pytest.raises(PermissionError, match="this product"):
        policy.authorize_fetch(product_a, "proxy-b")


def test_explicit_empty_web_allowlist_denies_even_company_default() -> None:
    policy = ProductEgressArgumentPolicy(
        default_search_provider="company-search",
        default_fetch_route="company-direct",
        bash_profiles_by_product={},
        search_providers_by_product={"restricted": frozenset()},
        fetch_routes_by_product={"restricted": frozenset()},
    )
    context = _context("restricted")
    with pytest.raises(PermissionError):
        policy.authorize_search(context, None)
    with pytest.raises(PermissionError):
        policy.authorize_fetch(context, None)
