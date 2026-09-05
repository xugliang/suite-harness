"""Replaceable capability contracts for profile memory, Customer360, evidence and RAG."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from .models import (
    CandidateStatus,
    CurationAction,
    CurationDecision,
    CurationResult,
    EntityAlias,
    EvidenceRecord,
    KnowledgeDocument,
    KnowledgeHit,
    PrivateClaim,
    RequestScope,
    ShareCandidate,
    SharedClaim,
    SharingPolicy,
    SpaceRef,
    WithdrawalReceipt,
)


@runtime_checkable
class ProfileProvider(Protocol):
    """Structured private profile storage. Every operation carries a trusted scope."""

    name: str

    def assert_claim(
        self,
        scope: RequestScope,
        *,
        local_subject_id: str,
        predicate: str,
        value: Any,
        source_ref: str,
        idempotency_key: str,
    ) -> PrivateClaim: ...

    def get_claim(self, scope: RequestScope, claim_id: str) -> PrivateClaim: ...

    def list_claims(
        self,
        scope: RequestScope,
        local_subject_id: str,
        *,
        include_retracted: bool = False,
    ) -> tuple[PrivateClaim, ...]: ...

    def retract_claim(
        self,
        scope: RequestScope,
        claim_id: str,
        *,
        reason: str,
        idempotency_key: str,
    ) -> PrivateClaim: ...

    def close(self) -> None: ...


@runtime_checkable
class ProfileProviderResolver(Protocol):
    """Resolve the private provider belonging to the authenticated product scope."""

    def resolve(self, scope: RequestScope) -> ProfileProvider: ...


@runtime_checkable
class Customer360Provider(Protocol):
    """Explicit promotion boundary from private claims to curated shared claims."""

    name: str

    def bind_entity_alias(
        self,
        scope: RequestScope,
        *,
        source_product_id: str,
        local_subject_id: str,
        entity_id: str,
        expected_revision: int,
        idempotency_key: str,
    ) -> EntityAlias: ...

    def resolve_entity_alias(self, scope: RequestScope, local_subject_id: str) -> EntityAlias: ...

    def get_policy(self, scope: RequestScope) -> SharingPolicy: ...

    def configure_policy(
        self,
        scope: RequestScope,
        *,
        publish_enabled: bool,
        consume_enabled: bool,
        publish_predicates: Iterable[str],
        consume_predicates: Iterable[str],
        publish_to_products: Iterable[str],
        consume_from_products: Iterable[str],
        expected_revision: int,
        idempotency_key: str,
        withdraw_existing: bool = False,
        publish_rules: Mapping[str, Iterable[str]] | None = None,
        consume_rules: Mapping[str, Iterable[str]] | None = None,
    ) -> SharingPolicy: ...

    def disable_publishing(
        self,
        scope: RequestScope,
        *,
        expected_revision: int,
        idempotency_key: str,
        withdraw_existing: bool = False,
    ) -> SharingPolicy: ...

    def disable_consumption(
        self,
        scope: RequestScope,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> SharingPolicy: ...

    def propose_share(
        self,
        scope: RequestScope,
        *,
        source_claim_id: str,
        idempotency_key: str,
    ) -> ShareCandidate: ...

    def get_candidate(self, scope: RequestScope, candidate_id: str) -> ShareCandidate: ...

    def list_candidates(
        self,
        scope: RequestScope,
        *,
        status: CandidateStatus | None = None,
    ) -> tuple[ShareCandidate, ...]: ...

    def curate(
        self,
        scope: RequestScope,
        *,
        candidate_id: str,
        action: CurationAction,
        reason: str,
        expected_revision: int,
        idempotency_key: str,
    ) -> CurationResult: ...

    def read_shared(self, scope: RequestScope, entity_id: str) -> tuple[SharedClaim, ...]: ...

    def entity_revision(self, scope: RequestScope, entity_id: str) -> int: ...

    def list_decisions(self, scope: RequestScope) -> tuple[CurationDecision, ...]: ...

    def withdraw_source(
        self,
        scope: RequestScope,
        *,
        source_claim_id: str,
        reason: str,
        idempotency_key: str,
    ) -> WithdrawalReceipt: ...

    def close(self) -> None: ...


@runtime_checkable
class EvidenceProvider(Protocol):
    """Evidence permission is independent from claim permission."""

    name: str

    def put(
        self,
        scope: RequestScope,
        *,
        space: SpaceRef,
        content: bytes,
        media_type: str,
        metadata: Mapping[str, Any],
        idempotency_key: str,
    ) -> EvidenceRecord: ...

    def open(self, scope: RequestScope, evidence_id: str) -> tuple[EvidenceRecord, bytes]: ...

    def erase(self, scope: RequestScope, evidence_id: str, *, reason: str) -> None: ...


@runtime_checkable
class KnowledgeProvider(Protocol):
    """Unstructured knowledge/RAG seam; authorization must precede ranking."""

    name: str

    def index(
        self,
        scope: RequestScope,
        documents: Sequence[KnowledgeDocument],
        *,
        idempotency_key: str,
    ) -> tuple[str, ...]: ...

    def search(
        self,
        scope: RequestScope,
        *,
        spaces: Sequence[SpaceRef],
        query: str,
        filters: Mapping[str, Any],
        limit: int,
    ) -> tuple[KnowledgeHit, ...]: ...

    def delete(self, scope: RequestScope, document_ids: Sequence[str], *, reason: str) -> None: ...
