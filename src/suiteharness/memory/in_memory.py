"""Thread-safe reference providers for private profile memory and Customer360.

These providers are intentionally small and deterministic. They demonstrate the security and
lifecycle semantics expected from durable adapters; they are not a substitute for database RLS,
an outbox, encryption or a persistent audit log.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import asdict, is_dataclass, replace
from datetime import datetime
from enum import Enum
from threading import RLock
from typing import Any
from uuid import uuid4

from .errors import MemoryCapabilityError, MemoryErrorCode
from .models import (
    CURATOR,
    ENTITY_ADMIN,
    PRIVATE_READ,
    PRIVATE_WRITE,
    SHARE_PROPOSE,
    SHARED_READ,
    SHARING_ADMIN,
    CandidateStatus,
    CurationAction,
    CurationDecision,
    CurationResult,
    EntityAlias,
    EntityRevision,
    PeerPredicateRule,
    PrivateClaim,
    PrivateClaimStatus,
    ProvenanceRef,
    RequestScope,
    ShareCandidate,
    SharedClaim,
    SharedClaimStatus,
    SharingPolicy,
    SpaceRef,
    WithdrawalReceipt,
    utcnow,
)
from .protocols import ProfileProvider, ProfileProviderResolver

_MISSING = object()


def _new_id() -> str:
    return uuid4().hex


def _canonical(value: Any) -> Any:
    if is_dataclass(value):
        return _canonical(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        return {key: _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, set | frozenset):
        return sorted((_canonical(item) for item in value), key=repr)
    if isinstance(value, tuple | list):
        return [_canonical(item) for item in value]
    return value


def _fingerprint(payload: Any) -> str:
    try:
        encoded = json.dumps(
            _canonical(payload),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MemoryCapabilityError(
            MemoryErrorCode.INVALID_ARGUMENT,
            "memory values must be JSON serializable",
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _text(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise MemoryCapabilityError(
            MemoryErrorCode.INVALID_ARGUMENT,
            f"{name} must be a string",
            argument=name,
        )
    clean = value.strip()
    if not clean:
        raise MemoryCapabilityError(
            MemoryErrorCode.INVALID_ARGUMENT,
            f"{name} must not be empty",
            argument=name,
        )
    return clean


def _string_set(values: Any, name: str) -> frozenset[str]:
    if isinstance(values, str | bytes):
        raise MemoryCapabilityError(
            MemoryErrorCode.INVALID_ARGUMENT,
            f"{name} must be an iterable of strings, not one string",
            argument=name,
        )
    try:
        result = frozenset(_text(value, name) for value in values)
    except TypeError as exc:
        raise MemoryCapabilityError(
            MemoryErrorCode.INVALID_ARGUMENT,
            f"{name} must be an iterable of strings",
            argument=name,
        ) from exc
    return result


def _peer_rules(values: Any, name: str) -> tuple[PeerPredicateRule, ...]:
    if not isinstance(values, Mapping):
        raise MemoryCapabilityError(
            MemoryErrorCode.INVALID_ARGUMENT,
            f"{name} must map peer product ids to predicate collections",
            argument=name,
        )
    rules: list[PeerPredicateRule] = []
    for peer, predicates in values.items():
        peer_id = _text(peer, f"{name}.peer_product_id")
        predicate_set = _string_set(predicates, f"{name}.{peer_id}")
        if not predicate_set:
            raise MemoryCapabilityError(
                MemoryErrorCode.INVALID_ARGUMENT,
                f"{name} entries must contain at least one predicate",
                argument=name,
                peer_product_id=peer_id,
            )
        rules.append(PeerPredicateRule(peer_id, predicate_set))
    return tuple(sorted(rules, key=lambda rule: rule.peer_product_id))


def _flatten_rules(
    rules: tuple[PeerPredicateRule, ...],
) -> tuple[frozenset[str], frozenset[str]]:
    peers = frozenset(rule.peer_product_id for rule in rules)
    predicates = frozenset(
        predicate for rule in rules for predicate in rule.predicates
    )
    return peers, predicates


def _revision(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MemoryCapabilityError(
            MemoryErrorCode.INVALID_ARGUMENT,
            f"{name} must be a non-negative integer",
            argument=name,
        )
    return value


def _boolean(value: bool, name: str) -> bool:
    if not isinstance(value, bool):
        raise MemoryCapabilityError(
            MemoryErrorCode.INVALID_ARGUMENT,
            f"{name} must be a boolean",
            argument=name,
        )
    return value


def _require(scope: RequestScope, role: str) -> None:
    if role not in scope.roles:
        raise MemoryCapabilityError(
            MemoryErrorCode.PERMISSION_DENIED,
            "the authenticated principal lacks the required memory capability",
            required_role=role,
        )


def _require_any(scope: RequestScope, roles: tuple[str, ...]) -> None:
    if not any(role in scope.roles for role in roles):
        raise MemoryCapabilityError(
            MemoryErrorCode.PERMISSION_DENIED,
            "the authenticated principal lacks the required memory capability",
            required_any_role=roles,
        )


def _idem_key(scope: RequestScope, operation: str, idempotency_key: str) -> tuple[str, str, str, str]:
    return (
        scope.tenant_id,
        scope.product_id,
        operation,
        _text(idempotency_key, "idempotency_key"),
    )


def _replay(
    records: dict[tuple[str, str, str, str], tuple[str, Any]],
    key: tuple[str, str, str, str],
    fingerprint: str,
) -> Any:
    stored = records.get(key)
    if stored is None:
        return _MISSING
    stored_fingerprint, result = stored
    if stored_fingerprint != fingerprint:
        raise MemoryCapabilityError(
            MemoryErrorCode.IDEMPOTENCY_CONFLICT,
            "an idempotency key was reused with a different command",
            operation=key[2],
        )
    return deepcopy(result)


def _remember(
    records: dict[tuple[str, str, str, str], tuple[str, Any]],
    key: tuple[str, str, str, str],
    fingerprint: str,
    result: Any,
) -> Any:
    records[key] = (fingerprint, deepcopy(result))
    return deepcopy(result)


class InMemoryProfileProvider:
    """Private product profile store with hard tenant/product ownership checks."""

    name = "memory-profile-reference"

    def __init__(
        self,
        *,
        id_factory: Callable[[], str] = _new_id,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._id_factory = id_factory
        self._clock = clock
        self._lock = RLock()
        self._claims: dict[str, PrivateClaim] = {}
        self._subject_claims: dict[tuple[str, str, str], list[str]] = {}
        self._idempotency: dict[tuple[str, str, str, str], tuple[str, Any]] = {}

    def assert_claim(
        self,
        scope: RequestScope,
        *,
        local_subject_id: str,
        predicate: str,
        value: Any,
        source_ref: str,
        idempotency_key: str,
    ) -> PrivateClaim:
        _require(scope, PRIVATE_WRITE)
        local_subject_id = _text(local_subject_id, "local_subject_id")
        predicate = _text(predicate, "predicate")
        source_ref = _text(source_ref, "source_ref")
        fingerprint = _fingerprint(
            {
                "local_subject_id": local_subject_id,
                "predicate": predicate,
                "value": value,
                "source_ref": source_ref,
            }
        )
        operation_key = _idem_key(scope, "profile.assert_claim", idempotency_key)

        with self._lock:
            replayed = _replay(self._idempotency, operation_key, fingerprint)
            if replayed is not _MISSING:
                return replayed

            claim = PrivateClaim(
                claim_id=self._id_factory(),
                space=SpaceRef.private(scope),
                local_subject_id=local_subject_id,
                predicate=predicate,
                value=deepcopy(value),
                source_ref=source_ref,
                created_by=scope.principal_id,
                recorded_at=self._clock(),
            )
            self._claims[claim.claim_id] = claim
            subject_key = (scope.tenant_id, scope.product_id, local_subject_id)
            self._subject_claims.setdefault(subject_key, []).append(claim.claim_id)
            return _remember(self._idempotency, operation_key, fingerprint, claim)

    def get_claim(self, scope: RequestScope, claim_id: str) -> PrivateClaim:
        _require(scope, PRIVATE_READ)
        claim_id = _text(claim_id, "claim_id")
        with self._lock:
            claim = self._claims.get(claim_id)
            if claim is None or not self._owns(scope, claim):
                # Deliberately indistinguishable: foreign ids must not be enumerable.
                raise MemoryCapabilityError(MemoryErrorCode.NOT_FOUND, "private claim not found")
            return deepcopy(claim)

    def list_claims(
        self,
        scope: RequestScope,
        local_subject_id: str,
        *,
        include_retracted: bool = False,
    ) -> tuple[PrivateClaim, ...]:
        _require(scope, PRIVATE_READ)
        local_subject_id = _text(local_subject_id, "local_subject_id")
        subject_key = (scope.tenant_id, scope.product_id, local_subject_id)
        with self._lock:
            claims = (self._claims[claim_id] for claim_id in self._subject_claims.get(subject_key, ()))
            visible = (
                claim
                for claim in claims
                if include_retracted or claim.status is PrivateClaimStatus.ASSERTED
            )
            return deepcopy(tuple(visible))

    def retract_claim(
        self,
        scope: RequestScope,
        claim_id: str,
        *,
        reason: str,
        idempotency_key: str,
    ) -> PrivateClaim:
        _require(scope, PRIVATE_WRITE)
        claim_id = _text(claim_id, "claim_id")
        reason = _text(reason, "reason")
        fingerprint = _fingerprint({"claim_id": claim_id, "reason": reason})
        operation_key = _idem_key(scope, "profile.retract_claim", idempotency_key)
        with self._lock:
            replayed = _replay(self._idempotency, operation_key, fingerprint)
            if replayed is not _MISSING:
                return replayed
            claim = self._claims.get(claim_id)
            if claim is None or not self._owns(scope, claim):
                raise MemoryCapabilityError(MemoryErrorCode.NOT_FOUND, "private claim not found")
            if claim.status is not PrivateClaimStatus.ASSERTED:
                raise MemoryCapabilityError(
                    MemoryErrorCode.INVALID_STATE,
                    "only an asserted private claim can be retracted",
                )
            updated = replace(
                claim,
                status=PrivateClaimStatus.RETRACTED,
                version=claim.version + 1,
                retraction_reason=reason,
                retracted_at=self._clock(),
            )
            self._claims[claim_id] = updated
            return _remember(self._idempotency, operation_key, fingerprint, updated)

    @staticmethod
    def _owns(scope: RequestScope, claim: PrivateClaim) -> bool:
        return (
            claim.space.tenant_id == scope.tenant_id
            and claim.space.owner_product_id == scope.product_id
        )

    def close(self) -> None:
        return None


class InMemoryProfileDirectory:
    """Reference resolver for separately instantiated product profile providers.

    The directory routes only from a trusted ``RequestScope``. It does not own or
    close the providers; their ProductContext effect scopes do.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._providers: dict[tuple[str, str], ProfileProvider] = {}

    def register(
        self,
        tenant_id: str,
        product_id: str,
        provider: ProfileProvider,
    ) -> None:
        tenant_id = _text(tenant_id, "tenant_id")
        product_id = _text(product_id, "product_id")
        if not isinstance(provider, ProfileProvider):
            raise TypeError("provider must satisfy ProfileProvider")
        key = (tenant_id, product_id)
        with self._lock:
            current = self._providers.get(key)
            if current is not None and current is not provider:
                raise ValueError(f"profile provider already registered: {tenant_id}/{product_id}")
            self._providers[key] = provider

    def unregister(self, tenant_id: str, product_id: str) -> bool:
        key = (_text(tenant_id, "tenant_id"), _text(product_id, "product_id"))
        with self._lock:
            return self._providers.pop(key, None) is not None

    def resolve(self, scope: RequestScope) -> ProfileProvider:
        with self._lock:
            provider = self._providers.get((scope.tenant_id, scope.product_id))
        if provider is None:
            raise MemoryCapabilityError(
                MemoryErrorCode.NOT_FOUND,
                "private profile provider is not configured for this product scope",
            )
        return provider


class InMemoryCustomer360Provider:
    """Reference implementation of an explicit candidate/curation sharing boundary."""

    name = "customer360-reference"

    def __init__(
        self,
        profile: ProfileProvider | ProfileProviderResolver,
        *,
        id_factory: Callable[[], str] = _new_id,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        if not isinstance(profile, ProfileProvider | ProfileProviderResolver):
            raise TypeError("profile must satisfy ProfileProvider or ProfileProviderResolver")
        self._profile = profile
        self._id_factory = id_factory
        self._clock = clock
        self._lock = RLock()
        self._aliases: dict[tuple[str, str, str], EntityAlias] = {}
        self._policies: dict[tuple[str, str], SharingPolicy] = {}
        self._candidates: dict[str, ShareCandidate] = {}
        self._candidate_by_source_revision: dict[
            tuple[str, str, str, int, int], str
        ] = {}
        self._shared_claims: dict[str, SharedClaim] = {}
        self._shared_by_candidate: dict[str, str] = {}
        self._shared_by_entity: dict[tuple[str, str], list[str]] = {}
        self._entity_revisions: dict[tuple[str, str], int] = {}
        self._decisions: list[CurationDecision] = []
        self._idempotency: dict[tuple[str, str, str, str], tuple[str, Any]] = {}

    def bind_entity_alias(
        self,
        scope: RequestScope,
        *,
        source_product_id: str,
        local_subject_id: str,
        entity_id: str,
        expected_revision: int,
        idempotency_key: str,
    ) -> EntityAlias:
        _require_any(scope, (ENTITY_ADMIN, CURATOR))
        source_product_id = _text(source_product_id, "source_product_id")
        local_subject_id = _text(local_subject_id, "local_subject_id")
        entity_id = _text(entity_id, "entity_id")
        expected_revision = _revision(expected_revision, "expected_revision")
        fingerprint = _fingerprint(
            {
                "source_product_id": source_product_id,
                "local_subject_id": local_subject_id,
                "entity_id": entity_id,
                "expected_revision": expected_revision,
            }
        )
        operation_key = _idem_key(scope, "customer360.bind_entity_alias", idempotency_key)
        alias_key = (scope.tenant_id, source_product_id, local_subject_id)
        with self._lock:
            replayed = _replay(self._idempotency, operation_key, fingerprint)
            if replayed is not _MISSING:
                return replayed
            current = self._aliases.get(alias_key)
            current_revision = current.revision if current is not None else 0
            if expected_revision != current_revision:
                raise MemoryCapabilityError(
                    MemoryErrorCode.REVISION_CONFLICT,
                    "entity alias revision changed",
                    expected=expected_revision,
                    actual=current_revision,
                )
            alias = EntityAlias(
                tenant_id=scope.tenant_id,
                source_product_id=source_product_id,
                local_subject_id=local_subject_id,
                entity_id=entity_id,
                revision=current_revision + 1,
                bound_by=scope.principal_id,
                bound_at=self._clock(),
            )
            self._aliases[alias_key] = alias
            return _remember(self._idempotency, operation_key, fingerprint, alias)

    def resolve_entity_alias(self, scope: RequestScope, local_subject_id: str) -> EntityAlias:
        _require(scope, PRIVATE_READ)
        local_subject_id = _text(local_subject_id, "local_subject_id")
        key = (scope.tenant_id, scope.product_id, local_subject_id)
        with self._lock:
            alias = self._aliases.get(key)
            if alias is None:
                raise MemoryCapabilityError(
                    MemoryErrorCode.ENTITY_ALIAS_NOT_FOUND,
                    "no verified Customer360 entity alias exists",
                )
            return deepcopy(alias)

    def get_policy(self, scope: RequestScope) -> SharingPolicy:
        with self._lock:
            return deepcopy(self._policy(scope.tenant_id, scope.product_id))

    def configure_policy(
        self,
        scope: RequestScope,
        *,
        publish_enabled: bool,
        consume_enabled: bool,
        publish_predicates: Any,
        consume_predicates: Any,
        publish_to_products: Any,
        consume_from_products: Any,
        expected_revision: int,
        idempotency_key: str,
        withdraw_existing: bool = False,
        publish_rules: Any | None = None,
        consume_rules: Any | None = None,
    ) -> SharingPolicy:
        _require(scope, SHARING_ADMIN)
        publish_enabled = _boolean(publish_enabled, "publish_enabled")
        consume_enabled = _boolean(consume_enabled, "consume_enabled")
        withdraw_existing = _boolean(withdraw_existing, "withdraw_existing")
        expected_revision = _revision(expected_revision, "expected_revision")
        publish_predicates = _string_set(publish_predicates, "publish_predicates")
        consume_predicates = _string_set(consume_predicates, "consume_predicates")
        publish_to_products = _string_set(publish_to_products, "publish_to_products")
        consume_from_products = _string_set(consume_from_products, "consume_from_products")
        exact_publish = (
            tuple(
                PeerPredicateRule(peer, publish_predicates)
                for peer in sorted(publish_to_products)
                if publish_predicates
            )
            if publish_rules is None
            else _peer_rules(publish_rules, "publish_rules")
        )
        exact_consume = (
            tuple(
                PeerPredicateRule(peer, consume_predicates)
                for peer in sorted(consume_from_products)
                if consume_predicates
            )
            if consume_rules is None
            else _peer_rules(consume_rules, "consume_rules")
        )
        exact_publish_peers, exact_publish_predicates = _flatten_rules(exact_publish)
        exact_consume_peers, exact_consume_predicates = _flatten_rules(exact_consume)
        if publish_rules is not None and (
            exact_publish_peers != publish_to_products
            or exact_publish_predicates != publish_predicates
        ):
            raise MemoryCapabilityError(
                MemoryErrorCode.INVALID_ARGUMENT,
                "publish_rules must match the flattened publish allowlists",
                argument="publish_rules",
            )
        if consume_rules is not None and (
            exact_consume_peers != consume_from_products
            or exact_consume_predicates != consume_predicates
        ):
            raise MemoryCapabilityError(
                MemoryErrorCode.INVALID_ARGUMENT,
                "consume_rules must match the flattened consume allowlists",
                argument="consume_rules",
            )
        fingerprint = _fingerprint(
            {
                "publish_enabled": publish_enabled,
                "consume_enabled": consume_enabled,
                "publish_predicates": publish_predicates,
                "consume_predicates": consume_predicates,
                "publish_to_products": publish_to_products,
                "consume_from_products": consume_from_products,
                "publish_rules": exact_publish,
                "consume_rules": exact_consume,
                "expected_revision": expected_revision,
                "withdraw_existing": withdraw_existing,
            }
        )
        operation_key = _idem_key(scope, "customer360.configure_policy", idempotency_key)
        with self._lock:
            replayed = _replay(self._idempotency, operation_key, fingerprint)
            if replayed is not _MISSING:
                return replayed
            current = self._policy(scope.tenant_id, scope.product_id)
            if current.revision != expected_revision:
                raise MemoryCapabilityError(
                    MemoryErrorCode.REVISION_CONFLICT,
                    "sharing policy revision changed",
                    expected=expected_revision,
                    actual=current.revision,
                )
            policy = SharingPolicy(
                tenant_id=scope.tenant_id,
                product_id=scope.product_id,
                publish_enabled=publish_enabled,
                consume_enabled=consume_enabled,
                publish_predicates=publish_predicates,
                consume_predicates=consume_predicates,
                publish_to_products=publish_to_products,
                consume_from_products=consume_from_products,
                publish_rules=exact_publish,
                consume_rules=exact_consume,
                revision=current.revision + 1,
                updated_by=scope.principal_id,
                updated_at=self._clock(),
            )
            self._policies[(scope.tenant_id, scope.product_id)] = policy
            self._invalidate_pending_locked(policy)
            if withdraw_existing:
                self._withdraw_product_locked(
                    tenant_id=scope.tenant_id,
                    source_product_id=scope.product_id,
                    reason="sharing policy withdrew existing contributions",
                )
            return _remember(self._idempotency, operation_key, fingerprint, policy)

    def disable_publishing(
        self,
        scope: RequestScope,
        *,
        expected_revision: int,
        idempotency_key: str,
        withdraw_existing: bool = False,
    ) -> SharingPolicy:
        current = self.get_policy(scope)
        return self.configure_policy(
            scope,
            publish_enabled=False,
            consume_enabled=current.consume_enabled,
            publish_predicates=current.publish_predicates,
            consume_predicates=current.consume_predicates,
            publish_to_products=current.publish_to_products,
            consume_from_products=current.consume_from_products,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            withdraw_existing=withdraw_existing,
            publish_rules={
                rule.peer_product_id: rule.predicates
                for rule in current.publish_rules
            }
            if current.publish_rules
            else None,
            consume_rules={
                rule.peer_product_id: rule.predicates
                for rule in current.consume_rules
            }
            if current.consume_rules
            else None,
        )

    def disable_consumption(
        self,
        scope: RequestScope,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> SharingPolicy:
        current = self.get_policy(scope)
        return self.configure_policy(
            scope,
            publish_enabled=current.publish_enabled,
            consume_enabled=False,
            publish_predicates=current.publish_predicates,
            consume_predicates=current.consume_predicates,
            publish_to_products=current.publish_to_products,
            consume_from_products=current.consume_from_products,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            publish_rules={
                rule.peer_product_id: rule.predicates
                for rule in current.publish_rules
            }
            if current.publish_rules
            else None,
            consume_rules={
                rule.peer_product_id: rule.predicates
                for rule in current.consume_rules
            }
            if current.consume_rules
            else None,
        )

    def propose_share(
        self,
        scope: RequestScope,
        *,
        source_claim_id: str,
        idempotency_key: str,
    ) -> ShareCandidate:
        _require(scope, SHARE_PROPOSE)
        source_claim_id = _text(source_claim_id, "source_claim_id")
        fingerprint = _fingerprint({"source_claim_id": source_claim_id})
        operation_key = _idem_key(scope, "customer360.propose_share", idempotency_key)
        with self._lock:
            replayed = _replay(self._idempotency, operation_key, fingerprint)
            if replayed is not _MISSING:
                return replayed
        # The profile provider independently enforces both read permission and exact ownership.
        profile = (
            self._profile.resolve(scope)
            if isinstance(self._profile, ProfileProviderResolver)
            else self._profile
        )
        source_claim = profile.get_claim(scope, source_claim_id)
        if source_claim.status is not PrivateClaimStatus.ASSERTED:
            raise MemoryCapabilityError(
                MemoryErrorCode.INVALID_STATE,
                "a retracted private claim cannot be proposed for sharing",
            )
        with self._lock:
            # A concurrent caller may have committed after the first replay check.
            replayed = _replay(self._idempotency, operation_key, fingerprint)
            if replayed is not _MISSING:
                return replayed
            alias = self._aliases.get(
                (scope.tenant_id, scope.product_id, source_claim.local_subject_id)
            )
            if alias is None:
                raise MemoryCapabilityError(
                    MemoryErrorCode.ENTITY_ALIAS_NOT_FOUND,
                    "no verified Customer360 entity alias exists",
                )
            policy = self._policy(scope.tenant_id, scope.product_id)
            audience = policy.publish_audience_for(source_claim.predicate)
            if not audience:
                raise MemoryCapabilityError(
                    MemoryErrorCode.POLICY_DENIED,
                    "publishing is disabled or the predicate is not allowlisted",
                )
            natural_key = (
                scope.tenant_id,
                scope.product_id,
                source_claim.claim_id,
                source_claim.version,
                policy.revision,
            )
            existing_id = self._candidate_by_source_revision.get(natural_key)
            if existing_id is not None:
                candidate = self._candidates[existing_id]
                return _remember(self._idempotency, operation_key, fingerprint, candidate)
            candidate = ShareCandidate(
                candidate_id=self._id_factory(),
                tenant_id=scope.tenant_id,
                entity_id=alias.entity_id,
                source_space=source_claim.space,
                source_claim_id=source_claim.claim_id,
                source_claim_version=source_claim.version,
                source_product_id=scope.product_id,
                source_ref=source_claim.source_ref,
                predicate=source_claim.predicate,
                value=deepcopy(source_claim.value),
                audience_product_ids=audience,
                policy_revision=policy.revision,
                status=CandidateStatus.PENDING,
                proposed_by=scope.principal_id,
                proposed_at=self._clock(),
            )
            self._candidates[candidate.candidate_id] = candidate
            self._candidate_by_source_revision[natural_key] = candidate.candidate_id
            return _remember(self._idempotency, operation_key, fingerprint, candidate)

    def get_candidate(self, scope: RequestScope, candidate_id: str) -> ShareCandidate:
        candidate_id = _text(candidate_id, "candidate_id")
        with self._lock:
            candidate = self._candidates.get(candidate_id)
            if candidate is None or candidate.tenant_id != scope.tenant_id:
                raise MemoryCapabilityError(MemoryErrorCode.NOT_FOUND, "share candidate not found")
            if CURATOR not in scope.roles and not (
                candidate.source_product_id == scope.product_id and SHARE_PROPOSE in scope.roles
            ):
                raise MemoryCapabilityError(MemoryErrorCode.NOT_FOUND, "share candidate not found")
            return deepcopy(candidate)

    def list_candidates(
        self,
        scope: RequestScope,
        *,
        status: CandidateStatus | None = None,
    ) -> tuple[ShareCandidate, ...]:
        _require_any(scope, (CURATOR, SHARE_PROPOSE))
        with self._lock:
            visible = (
                candidate
                for candidate in self._candidates.values()
                if candidate.tenant_id == scope.tenant_id
                and (CURATOR in scope.roles or candidate.source_product_id == scope.product_id)
                and (status is None or candidate.status is status)
            )
            return deepcopy(tuple(sorted(visible, key=lambda item: item.proposed_at)))

    def curate(
        self,
        scope: RequestScope,
        *,
        candidate_id: str,
        action: CurationAction,
        reason: str,
        expected_revision: int,
        idempotency_key: str,
    ) -> CurationResult:
        _require(scope, CURATOR)
        candidate_id = _text(candidate_id, "candidate_id")
        reason = _text(reason, "reason")
        expected_revision = _revision(expected_revision, "expected_revision")
        try:
            action = CurationAction(action)
        except (TypeError, ValueError) as exc:
            raise MemoryCapabilityError(
                MemoryErrorCode.INVALID_ARGUMENT,
                "unsupported curation action",
                action=str(action),
            ) from exc
        fingerprint = _fingerprint(
            {
                "candidate_id": candidate_id,
                "action": action,
                "reason": reason,
                "expected_revision": expected_revision,
            }
        )
        operation_key = _idem_key(scope, "customer360.curate", idempotency_key)
        with self._lock:
            replayed = _replay(self._idempotency, operation_key, fingerprint)
            if replayed is not _MISSING:
                return replayed
            candidate = self._candidates.get(candidate_id)
            if candidate is None or candidate.tenant_id != scope.tenant_id:
                raise MemoryCapabilityError(MemoryErrorCode.NOT_FOUND, "share candidate not found")
            if candidate.status is not CandidateStatus.PENDING:
                raise MemoryCapabilityError(
                    MemoryErrorCode.INVALID_STATE,
                    "only a pending candidate can be curated",
                    status=candidate.status.value,
                )
            if action is CurationAction.ACCEPT:
                current_policy = self._policy(
                    candidate.tenant_id, candidate.source_product_id
                )
                if not self._candidate_allowed(current_policy, candidate):
                    raise MemoryCapabilityError(
                        MemoryErrorCode.POLICY_DENIED,
                        "the current producer policy no longer permits this candidate",
                    )
            revision_key = (candidate.tenant_id, candidate.entity_id)
            current_revision = self._entity_revisions.get(revision_key, 0)
            if current_revision != expected_revision:
                raise MemoryCapabilityError(
                    MemoryErrorCode.REVISION_CONFLICT,
                    "Customer360 entity revision changed",
                    expected=expected_revision,
                    actual=current_revision,
                )
            now = self._clock()
            new_revision = current_revision + 1
            updated_candidate = replace(
                candidate,
                status=(
                    CandidateStatus.ACCEPTED
                    if action is CurationAction.ACCEPT
                    else CandidateStatus.REJECTED
                ),
                decided_at=now,
            )
            self._candidates[candidate_id] = updated_candidate
            shared_claim: SharedClaim | None = None
            if action is CurationAction.ACCEPT:
                shared_claim = SharedClaim(
                    claim_id=self._id_factory(),
                    space=SpaceRef.customer360(candidate.tenant_id),
                    entity_id=candidate.entity_id,
                    predicate=candidate.predicate,
                    value=deepcopy(candidate.value),
                    audience_product_ids=candidate.audience_product_ids,
                    provenance=(
                        ProvenanceRef(
                            source_product_id=candidate.source_product_id,
                            source_claim_id=candidate.source_claim_id,
                            source_claim_version=candidate.source_claim_version,
                            source_ref=candidate.source_ref,
                            candidate_id=candidate.candidate_id,
                            policy_revision=candidate.policy_revision,
                        ),
                    ),
                    status=SharedClaimStatus.SELECTED,
                    created_at=now,
                )
                self._shared_claims[shared_claim.claim_id] = shared_claim
                self._shared_by_candidate[candidate_id] = shared_claim.claim_id
                self._shared_by_entity.setdefault(revision_key, []).append(shared_claim.claim_id)
            decision = CurationDecision(
                decision_id=self._id_factory(),
                candidate_id=candidate_id,
                action=action,
                reason=reason,
                curator=scope.principal_id,
                previous_revision=current_revision,
                new_revision=new_revision,
                decided_at=now,
            )
            self._decisions.append(decision)
            self._entity_revisions[revision_key] = new_revision
            result = CurationResult(
                candidate=updated_candidate,
                decision=decision,
                shared_claim=shared_claim,
                entity_revision=new_revision,
            )
            return _remember(self._idempotency, operation_key, fingerprint, result)

    def read_shared(self, scope: RequestScope, entity_id: str) -> tuple[SharedClaim, ...]:
        _require(scope, SHARED_READ)
        entity_id = _text(entity_id, "entity_id")
        with self._lock:
            consumer_policy = self._policy(scope.tenant_id, scope.product_id)
            if not consumer_policy.consume_enabled:
                raise MemoryCapabilityError(
                    MemoryErrorCode.POLICY_DENIED,
                    "Customer360 consumption is disabled",
                )
            ids = self._shared_by_entity.get((scope.tenant_id, entity_id), ())
            visible: list[SharedClaim] = []
            for claim_id in ids:
                claim = self._shared_claims[claim_id]
                if claim.status is not SharedClaimStatus.SELECTED:
                    continue
                if scope.product_id not in claim.audience_product_ids:
                    continue
                if not any(
                    consumer_policy.can_consume_from(
                        provenance.source_product_id,
                        claim.predicate,
                    )
                    for provenance in claim.provenance
                ):
                    continue
                visible.append(claim)
            return deepcopy(tuple(sorted(visible, key=lambda item: (item.created_at, item.claim_id))))

    def entity_revision(self, scope: RequestScope, entity_id: str) -> int:
        _require_any(scope, (CURATOR, SHARED_READ))
        entity_id = _text(entity_id, "entity_id")
        with self._lock:
            return self._entity_revisions.get((scope.tenant_id, entity_id), 0)

    def list_decisions(self, scope: RequestScope) -> tuple[CurationDecision, ...]:
        _require(scope, CURATOR)
        with self._lock:
            candidate_ids = {
                candidate.candidate_id
                for candidate in self._candidates.values()
                if candidate.tenant_id == scope.tenant_id
            }
            return deepcopy(
                tuple(decision for decision in self._decisions if decision.candidate_id in candidate_ids)
            )

    def withdraw_source(
        self,
        scope: RequestScope,
        *,
        source_claim_id: str,
        reason: str,
        idempotency_key: str,
    ) -> WithdrawalReceipt:
        _require(scope, SHARE_PROPOSE)
        source_claim_id = _text(source_claim_id, "source_claim_id")
        reason = _text(reason, "reason")
        fingerprint = _fingerprint({"source_claim_id": source_claim_id, "reason": reason})
        operation_key = _idem_key(scope, "customer360.withdraw_source", idempotency_key)
        with self._lock:
            replayed = _replay(self._idempotency, operation_key, fingerprint)
            if replayed is not _MISSING:
                return replayed
        # A caller may only withdraw a source claim its private provider lets it read.
        # Customer360 is tenant-owned, so the production shape is a resolver over
        # distinct product-private providers rather than one shared profile store.
        profile = (
            self._profile.resolve(scope)
            if isinstance(self._profile, ProfileProviderResolver)
            else self._profile
        )
        profile.get_claim(scope, source_claim_id)
        with self._lock:
            replayed = _replay(self._idempotency, operation_key, fingerprint)
            if replayed is not _MISSING:
                return replayed
            candidates_withdrawn = 0
            shared_retracted = 0
            touched_entities: set[str] = set()
            now = self._clock()
            for candidate_id, candidate in tuple(self._candidates.items()):
                if (
                    candidate.tenant_id != scope.tenant_id
                    or candidate.source_product_id != scope.product_id
                    or candidate.source_claim_id != source_claim_id
                    or candidate.status in {CandidateStatus.REJECTED, CandidateStatus.WITHDRAWN}
                ):
                    continue
                self._candidates[candidate_id] = replace(
                    candidate,
                    status=CandidateStatus.WITHDRAWN,
                    decided_at=now,
                )
                candidates_withdrawn += 1
                shared_claim_id = self._shared_by_candidate.get(candidate_id)
                if shared_claim_id is None:
                    continue
                shared_claim = self._shared_claims[shared_claim_id]
                if shared_claim.status is SharedClaimStatus.SELECTED:
                    self._shared_claims[shared_claim_id] = replace(
                        shared_claim,
                        status=SharedClaimStatus.RETRACTED,
                        retracted_at=now,
                        retraction_reason=reason,
                    )
                    shared_retracted += 1
                    touched_entities.add(candidate.entity_id)
            revisions = self._bump_entities_locked(scope.tenant_id, touched_entities)
            receipt = WithdrawalReceipt(
                source_claim_id=source_claim_id,
                candidates_withdrawn=candidates_withdrawn,
                shared_claims_retracted=shared_retracted,
                entity_revisions=revisions,
            )
            return _remember(self._idempotency, operation_key, fingerprint, receipt)

    def _policy(self, tenant_id: str, product_id: str) -> SharingPolicy:
        return self._policies.get(
            (tenant_id, product_id),
            SharingPolicy(tenant_id=tenant_id, product_id=product_id),
        )

    @staticmethod
    def _candidate_allowed(policy: SharingPolicy, candidate: ShareCandidate) -> bool:
        return (
            bool(candidate.audience_product_ids)
            and candidate.audience_product_ids
            <= policy.publish_audience_for(candidate.predicate)
        )

    def _invalidate_pending_locked(self, policy: SharingPolicy) -> None:
        now = self._clock()
        for candidate_id, candidate in tuple(self._candidates.items()):
            if (
                candidate.tenant_id == policy.tenant_id
                and candidate.source_product_id == policy.product_id
                and candidate.status is CandidateStatus.PENDING
                and not self._candidate_allowed(policy, candidate)
            ):
                self._candidates[candidate_id] = replace(
                    candidate,
                    status=CandidateStatus.STALE,
                    decided_at=now,
                )

    def _withdraw_product_locked(
        self,
        *,
        tenant_id: str,
        source_product_id: str,
        reason: str,
    ) -> None:
        now = self._clock()
        touched_entities: set[str] = set()
        for candidate_id, candidate in tuple(self._candidates.items()):
            if (
                candidate.tenant_id != tenant_id
                or candidate.source_product_id != source_product_id
                or candidate.status in {CandidateStatus.REJECTED, CandidateStatus.WITHDRAWN}
            ):
                continue
            self._candidates[candidate_id] = replace(
                candidate,
                status=CandidateStatus.WITHDRAWN,
                decided_at=now,
            )
            shared_claim_id = self._shared_by_candidate.get(candidate_id)
            if shared_claim_id is None:
                continue
            shared_claim = self._shared_claims[shared_claim_id]
            if shared_claim.status is SharedClaimStatus.SELECTED:
                self._shared_claims[shared_claim_id] = replace(
                    shared_claim,
                    status=SharedClaimStatus.RETRACTED,
                    retracted_at=now,
                    retraction_reason=reason,
                )
                touched_entities.add(candidate.entity_id)
        self._bump_entities_locked(tenant_id, touched_entities)

    def _bump_entities_locked(
        self, tenant_id: str, entity_ids: set[str]
    ) -> tuple[EntityRevision, ...]:
        revisions: list[EntityRevision] = []
        for entity_id in sorted(entity_ids):
            key = (tenant_id, entity_id)
            revision = self._entity_revisions.get(key, 0) + 1
            self._entity_revisions[key] = revision
            revisions.append(EntityRevision(entity_id=entity_id, revision=revision))
        return tuple(revisions)

    def close(self) -> None:
        return None
