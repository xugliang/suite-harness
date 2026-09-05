"""Executable contract tests for private memory and curated Customer360 sharing."""

from __future__ import annotations

import pytest

from suiteharness.memory import (
    CURATOR,
    ENTITY_ADMIN,
    PRIVATE_READ,
    PRIVATE_WRITE,
    SHARE_PROPOSE,
    SHARED_READ,
    SHARING_ADMIN,
    CandidateStatus,
    CurationAction,
    Customer360Provider,
    InMemoryCustomer360Provider,
    InMemoryProfileDirectory,
    InMemoryProfileProvider,
    MemoryCapabilityError,
    MemoryErrorCode,
    ProfileProvider,
    RequestScope,
    SharedClaimStatus,
)
from suiteharness.runtime import ScopePath


def _scope(
    product_id: str,
    *roles: str,
    tenant_id: str = "tenant-a",
    principal_id: str | None = None,
) -> RequestScope:
    return RequestScope(
        path=ScopePath.product(tenant_id, product_id),
        principal_id=principal_id or f"{product_id}-service",
        roles=frozenset(roles),
        purpose="memory-test",
    )


def _producer(product_id: str = "alpha", *, tenant_id: str = "tenant-a") -> RequestScope:
    return _scope(
        product_id,
        PRIVATE_READ,
        PRIVATE_WRITE,
        SHARE_PROPOSE,
        SHARING_ADMIN,
        tenant_id=tenant_id,
    )


def _consumer(product_id: str = "beta", *, tenant_id: str = "tenant-a") -> RequestScope:
    return _scope(product_id, SHARED_READ, SHARING_ADMIN, tenant_id=tenant_id)


def _curator(*, tenant_id: str = "tenant-a") -> RequestScope:
    return _scope(
        "customer360",
        CURATOR,
        ENTITY_ADMIN,
        tenant_id=tenant_id,
        principal_id="curator-1",
    )


def _configure_publish(
    customer360: InMemoryCustomer360Provider,
    producer: RequestScope,
    *,
    revision: int = 0,
    predicate: str = "shared.attribute.one",
    consumer: str = "beta",
) -> None:
    customer360.configure_policy(
        producer,
        publish_enabled=True,
        consume_enabled=False,
        publish_predicates={predicate},
        consume_predicates=set(),
        publish_to_products={consumer},
        consume_from_products=set(),
        expected_revision=revision,
        idempotency_key=f"publish-{revision}",
    )


def _configure_consume(
    customer360: InMemoryCustomer360Provider,
    consumer: RequestScope,
    *,
    revision: int = 0,
    predicate: str = "shared.attribute.one",
    producer: str = "alpha",
) -> None:
    customer360.configure_policy(
        consumer,
        publish_enabled=False,
        consume_enabled=True,
        publish_predicates=set(),
        consume_predicates={predicate},
        publish_to_products=set(),
        consume_from_products={producer},
        expected_revision=revision,
        idempotency_key=f"consume-{revision}",
    )


def _claim_and_alias(
    profile: InMemoryProfileProvider,
    customer360: InMemoryCustomer360Provider,
    producer: RequestScope,
    curator: RequestScope,
    *,
    value: object = "value-one",
    claim_key: str = "claim-1",
    alias_key: str = "alias-1",
    local_subject_id: str = "local-42",
    entity_id: str = "entity-9000",
    predicate: str = "shared.attribute.one",
):
    claim = profile.assert_claim(
        producer,
        local_subject_id=local_subject_id,
        predicate=predicate,
        value=value,
        source_ref="source:event-17",
        idempotency_key=claim_key,
    )
    customer360.bind_entity_alias(
        curator,
        source_product_id=producer.product_id,
        local_subject_id=local_subject_id,
        entity_id=entity_id,
        expected_revision=0,
        idempotency_key=alias_key,
    )
    return claim


def test_reference_implementations_satisfy_capability_protocols() -> None:
    profile = InMemoryProfileProvider()
    customer360 = InMemoryCustomer360Provider(profile)

    assert isinstance(profile, ProfileProvider)
    assert isinstance(customer360, Customer360Provider)


def test_private_profiles_are_isolated_even_when_local_subject_ids_match() -> None:
    profile = InMemoryProfileProvider()
    alpha = _producer("alpha")
    beta = _producer("beta")

    alpha_claim = profile.assert_claim(
        alpha,
        local_subject_id="same-id",
        predicate="profile.note",
        value="alpha-only",
        source_ref="alpha:event-1",
        idempotency_key="alpha-1",
    )
    beta_claim = profile.assert_claim(
        beta,
        local_subject_id="same-id",
        predicate="profile.note",
        value="beta-only",
        source_ref="beta:event-1",
        idempotency_key="beta-1",
    )

    assert [claim.value for claim in profile.list_claims(alpha, "same-id")] == ["alpha-only"]
    assert [claim.value for claim in profile.list_claims(beta, "same-id")] == [
        "beta-only"
    ]
    assert alpha_claim.space.owner_product_id == "alpha"
    assert beta_claim.space.owner_product_id == "beta"

    with pytest.raises(MemoryCapabilityError) as error:
        profile.get_claim(beta, alpha_claim.claim_id)
    assert error.value.code is MemoryErrorCode.NOT_FOUND

    other_tenant = _producer("alpha", tenant_id="tenant-b")
    with pytest.raises(MemoryCapabilityError) as error:
        profile.get_claim(other_tenant, alpha_claim.claim_id)
    assert error.value.code is MemoryErrorCode.NOT_FOUND


def test_share_candidate_index_isolated_when_products_reuse_a_claim_id() -> None:
    profiles = InMemoryProfileDirectory()
    alpha_profile = InMemoryProfileProvider(id_factory=lambda: "same-claim-id")
    beta_profile = InMemoryProfileProvider(id_factory=lambda: "same-claim-id")
    profiles.register("tenant-a", "alpha", alpha_profile)
    profiles.register("tenant-a", "beta", beta_profile)
    candidate_ids = iter(("candidate-alpha", "candidate-beta"))
    customer360 = InMemoryCustomer360Provider(
        profiles,
        id_factory=lambda: next(candidate_ids),
    )
    alpha = _producer("alpha")
    beta = _producer("beta")
    curator = _curator()
    _configure_publish(customer360, alpha, consumer="gamma")
    _configure_publish(customer360, beta, consumer="gamma")

    alpha_claim = alpha_profile.assert_claim(
        alpha,
        local_subject_id="local-alpha",
        predicate="shared.attribute.one",
        value="alpha-value",
        source_ref="alpha:source",
        idempotency_key="claim",
    )
    beta_claim = beta_profile.assert_claim(
        beta,
        local_subject_id="local-beta",
        predicate="shared.attribute.one",
        value="beta-value",
        source_ref="beta:source",
        idempotency_key="claim",
    )
    assert alpha_claim.claim_id == beta_claim.claim_id == "same-claim-id"

    customer360.bind_entity_alias(
        curator,
        source_product_id="alpha",
        local_subject_id="local-alpha",
        entity_id="entity-alpha",
        expected_revision=0,
        idempotency_key="alias-alpha",
    )
    customer360.bind_entity_alias(
        curator,
        source_product_id="beta",
        local_subject_id="local-beta",
        entity_id="entity-beta",
        expected_revision=0,
        idempotency_key="alias-beta",
    )

    alpha_candidate = customer360.propose_share(
        alpha,
        source_claim_id=alpha_claim.claim_id,
        idempotency_key="propose",
    )
    beta_candidate = customer360.propose_share(
        beta,
        source_claim_id=beta_claim.claim_id,
        idempotency_key="propose",
    )

    assert alpha_candidate.candidate_id == "candidate-alpha"
    assert beta_candidate.candidate_id == "candidate-beta"
    assert alpha_candidate.source_product_id == "alpha"
    assert beta_candidate.source_product_id == "beta"
    assert alpha_candidate.entity_id == "entity-alpha"
    assert beta_candidate.entity_id == "entity-beta"


def test_entity_alias_is_explicit_and_never_inferred_from_matching_local_id() -> None:
    profile = InMemoryProfileProvider()
    customer360 = InMemoryCustomer360Provider(profile)
    alpha = _producer("alpha")
    beta = _producer("beta")
    curator = _curator()
    _configure_publish(customer360, alpha)

    claim = profile.assert_claim(
        alpha,
        local_subject_id="subject-42",
        predicate="shared.attribute.one",
        value=42,
        source_ref="alpha:event-42",
        idempotency_key="claim-42",
    )
    with pytest.raises(MemoryCapabilityError) as error:
        customer360.propose_share(alpha, source_claim_id=claim.claim_id, idempotency_key="p-1")
    assert error.value.code is MemoryErrorCode.ENTITY_ALIAS_NOT_FOUND

    alias = customer360.bind_entity_alias(
        curator,
        source_product_id="alpha",
        local_subject_id="subject-42",
        entity_id="entity-42",
        expected_revision=0,
        idempotency_key="bind-alpha",
    )
    assert alias.entity_id == "entity-42"

    # The same textual id in a second product is still a different, unresolved identity.
    with pytest.raises(MemoryCapabilityError) as error:
        customer360.resolve_entity_alias(beta, "subject-42")
    assert error.value.code is MemoryErrorCode.ENTITY_ALIAS_NOT_FOUND
    assert customer360.propose_share(
        alpha, source_claim_id=claim.claim_id, idempotency_key="p-2"
    ).entity_id == "entity-42"


def test_publish_and_consume_are_default_deny_and_require_field_allowlists() -> None:
    profile = InMemoryProfileProvider()
    customer360 = InMemoryCustomer360Provider(profile)
    alpha = _producer()
    beta = _consumer()
    curator = _curator()
    claim = _claim_and_alias(profile, customer360, alpha, curator)

    assert customer360.get_policy(alpha).publish_enabled is False
    assert customer360.get_policy(beta).consume_enabled is False
    with pytest.raises(MemoryCapabilityError) as error:
        customer360.propose_share(alpha, source_claim_id=claim.claim_id, idempotency_key="p-1")
    assert error.value.code is MemoryErrorCode.POLICY_DENIED
    with pytest.raises(MemoryCapabilityError) as error:
        customer360.read_shared(beta, "entity-9000")
    assert error.value.code is MemoryErrorCode.POLICY_DENIED

    _configure_publish(customer360, alpha, predicate="shared.attribute.two")
    with pytest.raises(MemoryCapabilityError) as error:
        customer360.propose_share(alpha, source_claim_id=claim.claim_id, idempotency_key="p-2")
    assert error.value.code is MemoryErrorCode.POLICY_DENIED


def test_candidate_requires_curation_then_exposes_bilateral_provenance() -> None:
    profile = InMemoryProfileProvider()
    customer360 = InMemoryCustomer360Provider(profile)
    alpha = _producer()
    beta = _consumer()
    curator = _curator()
    _configure_publish(customer360, alpha)
    _configure_consume(customer360, beta)
    claim = _claim_and_alias(profile, customer360, alpha, curator)

    candidate = customer360.propose_share(
        alpha,
        source_claim_id=claim.claim_id,
        idempotency_key="propose-budget",
    )
    assert candidate.status is CandidateStatus.PENDING
    assert candidate.policy_revision == 1
    assert customer360.get_policy(alpha).revision == 1
    assert customer360.read_shared(beta, "entity-9000") == ()

    result = customer360.curate(
        curator,
        candidate_id=candidate.candidate_id,
        action=CurationAction.ACCEPT,
        reason="verified against source event",
        expected_revision=0,
        idempotency_key="curate-budget",
    )
    assert result.entity_revision == 1
    assert result.shared_claim is not None
    assert result.shared_claim.status is SharedClaimStatus.SELECTED
    assert result.shared_claim.space.owner_product_id is None
    assert result.shared_claim.provenance[0].source_claim_id == claim.claim_id
    assert result.shared_claim.provenance[0].source_product_id == "alpha"
    assert result.shared_claim.provenance[0].candidate_id == candidate.candidate_id
    assert result.shared_claim.provenance[0].policy_revision == 1

    visible = customer360.read_shared(beta, "entity-9000")
    assert visible == (result.shared_claim,)

    # Retry after the revision advanced replays the original result instead of double-writing.
    replayed = customer360.curate(
        curator,
        candidate_id=candidate.candidate_id,
        action=CurationAction.ACCEPT,
        reason="verified against source event",
        expected_revision=0,
        idempotency_key="curate-budget",
    )
    assert replayed == result
    assert len(customer360.list_decisions(curator)) == 1


def test_exact_peer_predicate_rules_do_not_leak_in_three_product_bundle() -> None:
    profile = InMemoryProfileProvider()
    customer360 = InMemoryCustomer360Provider(profile)
    alpha = _producer("alpha")
    beta = _consumer("beta")
    gamma = _consumer("gamma")
    curator = _curator()

    customer360.configure_policy(
        alpha,
        publish_enabled=True,
        consume_enabled=False,
        publish_predicates={"profile.attribute.one", "profile.attribute.two"},
        consume_predicates=set(),
        publish_to_products={"beta", "gamma"},
        consume_from_products=set(),
        publish_rules={
            "beta": {"profile.attribute.one"},
            "gamma": {"profile.attribute.two"},
        },
        expected_revision=0,
        idempotency_key="alpha-exact-publish",
    )
    customer360.configure_policy(
        beta,
        publish_enabled=False,
        consume_enabled=True,
        publish_predicates=set(),
        consume_predicates={"profile.attribute.one"},
        publish_to_products=set(),
        consume_from_products={"alpha"},
        consume_rules={"alpha": {"profile.attribute.one"}},
        expected_revision=0,
        idempotency_key="beta-exact-consume",
    )
    customer360.configure_policy(
        gamma,
        publish_enabled=False,
        consume_enabled=True,
        publish_predicates=set(),
        consume_predicates={"profile.attribute.two"},
        publish_to_products=set(),
        consume_from_products={"alpha"},
        consume_rules={"alpha": {"profile.attribute.two"}},
        expected_revision=0,
        idempotency_key="gamma-exact-consume",
    )
    customer360.bind_entity_alias(
        curator,
        source_product_id="alpha",
        local_subject_id="subject-1",
        entity_id="entity-1",
        expected_revision=0,
        idempotency_key="alpha-entity-alias",
    )

    first = profile.assert_claim(
        alpha,
        local_subject_id="subject-1",
        predicate="profile.attribute.one",
        value="value-one",
        source_ref="source:one",
        idempotency_key="first-claim",
    )
    second = profile.assert_claim(
        alpha,
        local_subject_id="subject-1",
        predicate="profile.attribute.two",
        value="value-two",
        source_ref="source:two",
        idempotency_key="second-claim",
    )
    first_candidate = customer360.propose_share(
        alpha,
        source_claim_id=first.claim_id,
        idempotency_key="first-candidate",
    )
    second_candidate = customer360.propose_share(
        alpha,
        source_claim_id=second.claim_id,
        idempotency_key="second-candidate",
    )
    assert first_candidate.audience_product_ids == frozenset({"beta"})
    assert second_candidate.audience_product_ids == frozenset({"gamma"})

    customer360.curate(
        curator,
        candidate_id=first_candidate.candidate_id,
        action=CurationAction.ACCEPT,
        reason="first attribute verified",
        expected_revision=0,
        idempotency_key="accept-first",
    )
    customer360.curate(
        curator,
        candidate_id=second_candidate.candidate_id,
        action=CurationAction.ACCEPT,
        reason="second attribute verified",
        expected_revision=1,
        idempotency_key="accept-second",
    )

    beta_claims = customer360.read_shared(beta, "entity-1")
    gamma_claims = customer360.read_shared(gamma, "entity-1")
    assert [(claim.predicate, claim.value) for claim in beta_claims] == [
        ("profile.attribute.one", "value-one")
    ]
    assert [(claim.predicate, claim.value) for claim in gamma_claims] == [
        ("profile.attribute.two", "value-two")
    ]


def test_exact_rules_must_match_flattened_diagnostic_allowlists() -> None:
    customer360 = InMemoryCustomer360Provider(InMemoryProfileProvider())
    alpha = _producer("alpha")

    with pytest.raises(MemoryCapabilityError) as captured:
        customer360.configure_policy(
            alpha,
            publish_enabled=True,
            consume_enabled=False,
            publish_predicates={"profile.attribute.one"},
            consume_predicates=set(),
            publish_to_products={"beta"},
            consume_from_products=set(),
            publish_rules={"gamma": {"profile.attribute.one"}},
            expected_revision=0,
            idempotency_key="mismatched-exact-rule",
        )

    assert captured.value.code is MemoryErrorCode.INVALID_ARGUMENT


def test_curation_and_policy_updates_use_cas_and_commands_are_idempotent() -> None:
    profile = InMemoryProfileProvider()
    customer360 = InMemoryCustomer360Provider(profile)
    alpha = _producer()
    curator = _curator()
    _configure_publish(customer360, alpha)
    first = _claim_and_alias(profile, customer360, alpha, curator)
    first_again = profile.assert_claim(
        alpha,
        local_subject_id="local-42",
        predicate="shared.attribute.one",
        value="value-one",
        source_ref="source:event-17",
        idempotency_key="claim-1",
    )
    assert first_again == first

    with pytest.raises(MemoryCapabilityError) as error:
        profile.assert_claim(
            alpha,
            local_subject_id="local-42",
            predicate="shared.attribute.one",
            value="value-two",
            source_ref="source:event-17",
            idempotency_key="claim-1",
        )
    assert error.value.code is MemoryErrorCode.IDEMPOTENCY_CONFLICT

    candidate = customer360.propose_share(
        alpha, source_claim_id=first.claim_id, idempotency_key="candidate-1"
    )
    with pytest.raises(MemoryCapabilityError) as error:
        customer360.curate(
            curator,
            candidate_id=candidate.candidate_id,
            action=CurationAction.ACCEPT,
            reason="reviewed",
            expected_revision=7,
            idempotency_key="decision-wrong-revision",
        )
    assert error.value.code is MemoryErrorCode.REVISION_CONFLICT
    assert customer360.get_candidate(curator, candidate.candidate_id).status is CandidateStatus.PENDING

    with pytest.raises(MemoryCapabilityError) as error:
        customer360.configure_policy(
            alpha,
            publish_enabled=False,
            consume_enabled=False,
            publish_predicates=set(),
            consume_predicates=set(),
            publish_to_products=set(),
            consume_from_products=set(),
            expected_revision=0,
            idempotency_key="stale-policy-update",
        )
    assert error.value.code is MemoryErrorCode.REVISION_CONFLICT


def test_disable_and_withdraw_have_distinct_reversible_boundaries() -> None:
    profile = InMemoryProfileProvider()
    customer360 = InMemoryCustomer360Provider(profile)
    alpha = _producer()
    beta = _consumer()
    curator = _curator()
    _configure_publish(customer360, alpha)
    _configure_consume(customer360, beta)
    accepted_source = _claim_and_alias(profile, customer360, alpha, curator)
    accepted_candidate = customer360.propose_share(
        alpha,
        source_claim_id=accepted_source.claim_id,
        idempotency_key="accepted-candidate",
    )
    customer360.curate(
        curator,
        candidate_id=accepted_candidate.candidate_id,
        action=CurationAction.ACCEPT,
        reason="reviewed",
        expected_revision=0,
        idempotency_key="accept-1",
    )
    pending_source = profile.assert_claim(
        alpha,
        local_subject_id="local-42",
        predicate="shared.attribute.one",
        value="value-two",
        source_ref="source:event-18",
        idempotency_key="claim-2",
    )
    pending_candidate = customer360.propose_share(
        alpha,
        source_claim_id=pending_source.claim_id,
        idempotency_key="pending-candidate",
    )
    assert len(customer360.read_shared(beta, "entity-9000")) == 1

    # Stop future publishing: pending work becomes stale; already curated data remains.
    policy = customer360.disable_publishing(
        alpha,
        expected_revision=1,
        idempotency_key="stop-future",
        withdraw_existing=False,
    )
    assert policy.publish_enabled is False
    assert customer360.get_candidate(curator, pending_candidate.candidate_id).status is (
        CandidateStatus.STALE
    )
    assert len(customer360.read_shared(beta, "entity-9000")) == 1
    with pytest.raises(MemoryCapabilityError) as error:
        customer360.propose_share(
            alpha,
            source_claim_id=pending_source.claim_id,
            idempotency_key="new-proposal-after-disable",
        )
    assert error.value.code is MemoryErrorCode.POLICY_DENIED

    # A second explicit operation withdraws prior contributions and keeps provenance auditable.
    customer360.disable_publishing(
        alpha,
        expected_revision=2,
        idempotency_key="withdraw-existing",
        withdraw_existing=True,
    )
    assert customer360.read_shared(beta, "entity-9000") == ()
    assert customer360.get_candidate(curator, accepted_candidate.candidate_id).status is (
        CandidateStatus.WITHDRAWN
    )
    assert customer360.entity_revision(curator, "entity-9000") == 2


def test_consume_disable_is_immediate_and_source_withdrawal_is_idempotent() -> None:
    profile = InMemoryProfileProvider()
    customer360 = InMemoryCustomer360Provider(profile)
    alpha = _producer()
    beta = _consumer()
    curator = _curator()
    _configure_publish(customer360, alpha)
    _configure_consume(customer360, beta)
    claim = _claim_and_alias(profile, customer360, alpha, curator)
    candidate = customer360.propose_share(
        alpha, source_claim_id=claim.claim_id, idempotency_key="candidate"
    )
    customer360.curate(
        curator,
        candidate_id=candidate.candidate_id,
        action=CurationAction.ACCEPT,
        reason="reviewed",
        expected_revision=0,
        idempotency_key="accept",
    )

    customer360.disable_consumption(
        beta,
        expected_revision=1,
        idempotency_key="disable-consume",
    )
    with pytest.raises(MemoryCapabilityError) as error:
        customer360.read_shared(beta, "entity-9000")
    assert error.value.code is MemoryErrorCode.POLICY_DENIED

    receipt = customer360.withdraw_source(
        alpha,
        source_claim_id=claim.claim_id,
        reason="source record was erased",
        idempotency_key="withdraw-source",
    )
    replayed = customer360.withdraw_source(
        alpha,
        source_claim_id=claim.claim_id,
        reason="source record was erased",
        idempotency_key="withdraw-source",
    )
    assert replayed == receipt
    assert receipt.candidates_withdrawn == 1
    assert receipt.shared_claims_retracted == 1
    assert receipt.entity_revisions[0].revision == 2
