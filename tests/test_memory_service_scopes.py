"""Memory service placement prevents accidental cross-product inheritance."""

from __future__ import annotations

import pytest

from suiteharness.memory import (
    CUSTOMER360_PROVIDER,
    PRIVATE_READ,
    PRIVATE_WRITE,
    PROFILE_PROVIDER,
    SHARE_PROPOSE,
    InMemoryCustomer360Provider,
    InMemoryProfileDirectory,
    InMemoryProfileProvider,
)
from suiteharness.runtime import RequestScope, RootContext, ScopePath, ServiceBindingError


def test_private_profiles_are_distinct_product_bindings() -> None:
    root = RootContext()
    tenant = root.tenant("tenant-a")
    alpha_profile = InMemoryProfileProvider()
    beta_profile = InMemoryProfileProvider()

    alpha = tenant.product("alpha", {PROFILE_PROVIDER: alpha_profile})
    beta = tenant.product("beta", {PROFILE_PROVIDER: beta_profile})

    assert alpha.resolve(PROFILE_PROVIDER) is alpha_profile
    assert beta.resolve(PROFILE_PROVIDER) is beta_profile
    assert alpha.resolve(PROFILE_PROVIDER) is not beta.resolve(PROFILE_PROVIDER)


def test_customer360_is_explicit_tenant_capability_shared_by_selected_products() -> None:
    root = RootContext()
    profiles = InMemoryProfileDirectory()
    alpha_profile = InMemoryProfileProvider()
    beta_profile = InMemoryProfileProvider()
    profiles.register("tenant-a", "alpha", alpha_profile)
    profiles.register("tenant-a", "beta", beta_profile)
    customer360 = InMemoryCustomer360Provider(profiles)
    tenant = root.tenant("tenant-a", {CUSTOMER360_PROVIDER: customer360})

    alpha = tenant.product("alpha", {PROFILE_PROVIDER: alpha_profile})
    beta = tenant.product("beta", {PROFILE_PROVIDER: beta_profile})

    assert alpha.resolve(CUSTOMER360_PROVIDER) is customer360
    assert beta.resolve(CUSTOMER360_PROVIDER) is customer360
    assert profiles.resolve(alpha.agent("agent", "s1", principal_id="alpha-service").request) is (
        alpha_profile
    )
    assert profiles.resolve(
        beta.agent("agent", "s2", principal_id="beta-service").request
    ) is beta_profile


def test_customer360_is_absent_by_default_and_product_cannot_replace_it() -> None:
    root = RootContext()
    tenant = root.tenant("tenant-a")
    product = tenant.product("alpha")

    with pytest.raises(LookupError):
        product.resolve(CUSTOMER360_PROVIDER)

    customer360 = InMemoryCustomer360Provider(InMemoryProfileProvider())
    with pytest.raises(ServiceBindingError, match="cannot be bound at product"):
        tenant.product("beta", {CUSTOMER360_PROVIDER: customer360})


def test_customer360_source_withdrawal_resolves_the_product_private_provider() -> None:
    profiles = InMemoryProfileDirectory()
    profile = InMemoryProfileProvider()
    profiles.register("tenant-a", "alpha", profile)
    customer360 = InMemoryCustomer360Provider(profiles)
    scope = RequestScope(
        path=ScopePath.product("tenant-a", "alpha"),
        principal_id="alpha-service",
        roles=frozenset({PRIVATE_READ, PRIVATE_WRITE, SHARE_PROPOSE}),
    )
    claim = profile.assert_claim(
        scope,
        local_subject_id="subject-1",
        predicate="profile.attribute.one",
        value="value-one",
        source_ref="evidence-1",
        idempotency_key="claim-1",
    )

    receipt = customer360.withdraw_source(
        scope,
        source_claim_id=claim.claim_id,
        reason="source was removed",
        idempotency_key="withdraw-1",
    )

    assert receipt.source_claim_id == claim.claim_id
    assert receipt.candidates_withdrawn == 0
    assert receipt.shared_claims_retracted == 0
