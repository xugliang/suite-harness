"""Immutable vocabulary for isolated product memory and curated Customer360 data."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from suiteharness.runtime.scopes import RequestScope


def utcnow() -> datetime:
    return datetime.now(UTC)


# Role strings are deliberately capability-specific. A product id is never a role.
PRIVATE_READ = "memory.private.read"
PRIVATE_WRITE = "memory.private.write"
SHARE_PROPOSE = "memory.share.propose"
SHARED_READ = "memory.shared.read"
SHARING_ADMIN = "memory.sharing.admin"
ENTITY_ADMIN = "memory.entity.admin"
CURATOR = "memory.customer360.curate"
KNOWLEDGE_READ = "knowledge.read"
KNOWLEDGE_WRITE = "knowledge.write"


class SpaceKind(str, Enum):
    PRODUCT_PRIVATE = "product_private"
    CUSTOMER360_SHARED = "customer360_shared"


class PrivateClaimStatus(str, Enum):
    ASSERTED = "asserted"
    RETRACTED = "retracted"


class CandidateStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    STALE = "stale"
    WITHDRAWN = "withdrawn"


class SharedClaimStatus(str, Enum):
    SELECTED = "selected"
    RETRACTED = "retracted"


class CurationAction(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"


@dataclass(frozen=True)
class SpaceRef:
    tenant_id: str
    kind: SpaceKind
    owner_product_id: str | None = None

    def __post_init__(self) -> None:
        if not self.tenant_id.strip():
            raise ValueError("tenant_id must not be empty")
        if self.kind is SpaceKind.PRODUCT_PRIVATE and not self.owner_product_id:
            raise ValueError("a private space must have an owner product")
        if self.kind is SpaceKind.CUSTOMER360_SHARED and self.owner_product_id is not None:
            raise ValueError("a Customer360 space cannot have an owner product")

    @classmethod
    def private(cls, scope: RequestScope) -> SpaceRef:
        return cls(
            tenant_id=scope.tenant_id,
            kind=SpaceKind.PRODUCT_PRIVATE,
            owner_product_id=scope.product_id,
        )

    @classmethod
    def customer360(cls, tenant_id: str) -> SpaceRef:
        return cls(tenant_id=tenant_id, kind=SpaceKind.CUSTOMER360_SHARED)


@dataclass(frozen=True)
class PrivateClaim:
    claim_id: str
    space: SpaceRef
    local_subject_id: str
    predicate: str
    value: Any
    source_ref: str
    created_by: str
    recorded_at: datetime
    status: PrivateClaimStatus = PrivateClaimStatus.ASSERTED
    version: int = 1
    retraction_reason: str = ""
    retracted_at: datetime | None = None


@dataclass(frozen=True)
class EntityAlias:
    tenant_id: str
    source_product_id: str
    local_subject_id: str
    entity_id: str
    revision: int
    bound_by: str
    bound_at: datetime


@dataclass(frozen=True)
class PeerPredicateRule:
    """Exact predicates allowed for one peer product in one direction."""

    peer_product_id: str
    predicates: frozenset[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "predicates", frozenset(self.predicates))


@dataclass(frozen=True)
class SharingPolicy:
    """Bilateral policy. Exact peer rules prevent Cartesian-product grants.

    The flattened predicate/product sets remain useful for diagnostics and
    backwards-compatible adapters. Enforcement uses ``publish_rules`` and
    ``consume_rules`` whenever they are present.
    """

    tenant_id: str
    product_id: str
    publish_enabled: bool = False
    consume_enabled: bool = False
    publish_predicates: frozenset[str] = field(default_factory=frozenset)
    consume_predicates: frozenset[str] = field(default_factory=frozenset)
    publish_to_products: frozenset[str] = field(default_factory=frozenset)
    consume_from_products: frozenset[str] = field(default_factory=frozenset)
    publish_rules: tuple[PeerPredicateRule, ...] = ()
    consume_rules: tuple[PeerPredicateRule, ...] = ()
    revision: int = 0
    updated_by: str = ""
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in (
            "publish_predicates",
            "consume_predicates",
            "publish_to_products",
            "consume_from_products",
        ):
            object.__setattr__(self, name, frozenset(getattr(self, name)))
        object.__setattr__(self, "publish_rules", tuple(self.publish_rules))
        object.__setattr__(self, "consume_rules", tuple(self.consume_rules))

    def publish_audience_for(self, predicate: str) -> frozenset[str]:
        """Return only peers explicitly allowed to receive this predicate."""

        if not self.publish_enabled:
            return frozenset()
        if self.publish_rules:
            return frozenset(
                rule.peer_product_id
                for rule in self.publish_rules
                if predicate in rule.predicates
            )
        if predicate not in self.publish_predicates:
            return frozenset()
        return self.publish_to_products

    def can_consume_from(self, source_product_id: str, predicate: str) -> bool:
        """Check the exact source-product and predicate pair."""

        if not self.consume_enabled:
            return False
        if self.consume_rules:
            return any(
                rule.peer_product_id == source_product_id
                and predicate in rule.predicates
                for rule in self.consume_rules
            )
        return (
            source_product_id in self.consume_from_products
            and predicate in self.consume_predicates
        )


@dataclass(frozen=True)
class ShareCandidate:
    candidate_id: str
    tenant_id: str
    entity_id: str
    source_space: SpaceRef
    source_claim_id: str
    source_claim_version: int
    source_product_id: str
    source_ref: str
    predicate: str
    value: Any
    audience_product_ids: frozenset[str]
    policy_revision: int
    status: CandidateStatus
    proposed_by: str
    proposed_at: datetime
    decided_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "audience_product_ids", frozenset(self.audience_product_ids))


@dataclass(frozen=True)
class ProvenanceRef:
    source_product_id: str
    source_claim_id: str
    source_claim_version: int
    source_ref: str
    candidate_id: str
    policy_revision: int


@dataclass(frozen=True)
class SharedClaim:
    claim_id: str
    space: SpaceRef
    entity_id: str
    predicate: str
    value: Any
    audience_product_ids: frozenset[str]
    provenance: tuple[ProvenanceRef, ...]
    status: SharedClaimStatus
    created_at: datetime
    retracted_at: datetime | None = None
    retraction_reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "audience_product_ids", frozenset(self.audience_product_ids))
        object.__setattr__(self, "provenance", tuple(self.provenance))


@dataclass(frozen=True)
class CurationDecision:
    decision_id: str
    candidate_id: str
    action: CurationAction
    reason: str
    curator: str
    previous_revision: int
    new_revision: int
    decided_at: datetime


@dataclass(frozen=True)
class CurationResult:
    candidate: ShareCandidate
    decision: CurationDecision
    shared_claim: SharedClaim | None
    entity_revision: int


@dataclass(frozen=True)
class EntityRevision:
    entity_id: str
    revision: int


@dataclass(frozen=True)
class WithdrawalReceipt:
    source_claim_id: str
    candidates_withdrawn: int
    shared_claims_retracted: int
    entity_revisions: tuple[EntityRevision, ...]


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    space: SpaceRef
    media_type: str
    metadata: Mapping[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class KnowledgeDocument:
    document_id: str
    space: SpaceRef
    text: str
    metadata: Mapping[str, Any]


@dataclass(frozen=True)
class KnowledgeHit:
    document_id: str
    space: SpaceRef
    score: float
    excerpt: str
    citation: Mapping[str, Any]
