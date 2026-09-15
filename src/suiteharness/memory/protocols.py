"""Replaceable capability contracts for profile memory, Customer360, evidence and RAG."""

from __future__ import annotations

from abc import ABC, abstractmethod

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

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


class ProfileProvider(ABC):
    """Structured private profile storage. Every operation carries a trusted scope."""

    name: str

    @abstractmethod
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

    @abstractmethod
    def get_claim(self, scope: RequestScope, claim_id: str) -> PrivateClaim: ...

    @abstractmethod
    def list_claims(
        self,
        scope: RequestScope,
        local_subject_id: str,
        *,
        include_retracted: bool = False,
    ) -> tuple[PrivateClaim, ...]: ...

    @abstractmethod
    def retract_claim(
        self,
        scope: RequestScope,
        claim_id: str,
        *,
        reason: str,
        idempotency_key: str,
    ) -> PrivateClaim: ...

    def close(self) -> None:
        """No resources by default; resource-owning providers override this hook."""
        return None


class ProfileProviderResolver(ABC):
    """Resolve the private provider belonging to the authenticated product scope."""

    @abstractmethod
    def resolve(self, scope: RequestScope) -> ProfileProvider: ...


class Customer360Provider(ABC):
    """Explicit promotion boundary from private claims to curated shared claims."""

    name: str

    @abstractmethod
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

    @abstractmethod
    def resolve_entity_alias(self, scope: RequestScope, local_subject_id: str) -> EntityAlias: ...

    @abstractmethod
    def get_policy(self, scope: RequestScope) -> SharingPolicy: ...

    @abstractmethod
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

    @abstractmethod
    def disable_publishing(
        self,
        scope: RequestScope,
        *,
        expected_revision: int,
        idempotency_key: str,
        withdraw_existing: bool = False,
    ) -> SharingPolicy: ...

    @abstractmethod
    def disable_consumption(
        self,
        scope: RequestScope,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> SharingPolicy: ...

    @abstractmethod
    def propose_share(
        self,
        scope: RequestScope,
        *,
        source_claim_id: str,
        idempotency_key: str,
    ) -> ShareCandidate: ...

    @abstractmethod
    def get_candidate(self, scope: RequestScope, candidate_id: str) -> ShareCandidate: ...

    @abstractmethod
    def list_candidates(
        self,
        scope: RequestScope,
        *,
        status: CandidateStatus | None = None,
    ) -> tuple[ShareCandidate, ...]: ...

    @abstractmethod
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

    @abstractmethod
    def read_shared(self, scope: RequestScope, entity_id: str) -> tuple[SharedClaim, ...]: ...

    @abstractmethod
    def entity_revision(self, scope: RequestScope, entity_id: str) -> int: ...

    @abstractmethod
    def list_decisions(self, scope: RequestScope) -> tuple[CurationDecision, ...]: ...

    @abstractmethod
    def withdraw_source(
        self,
        scope: RequestScope,
        *,
        source_claim_id: str,
        reason: str,
        idempotency_key: str,
    ) -> WithdrawalReceipt: ...

    def close(self) -> None:
        """No resources by default; resource-owning providers override this hook."""
        return None


class EvidenceProvider(ABC):
    """Evidence permission is independent from claim permission."""

    name: str

    @abstractmethod
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

    @abstractmethod
    def open(self, scope: RequestScope, evidence_id: str) -> tuple[EvidenceRecord, bytes]: ...

    @abstractmethod
    def erase(self, scope: RequestScope, evidence_id: str, *, reason: str) -> None: ...


class KnowledgeProvider(ABC):
    """Unstructured knowledge/RAG seam; authorization must precede ranking."""

    name: str

    @abstractmethod
    def index(
        self,
        scope: RequestScope,
        documents: Sequence[KnowledgeDocument],
        *,
        idempotency_key: str,
    ) -> tuple[str, ...]: ...

    @abstractmethod
    def search(
        self,
        scope: RequestScope,
        *,
        spaces: Sequence[SpaceRef],
        query: str,
        filters: Mapping[str, Any],
        limit: int,
    ) -> tuple[KnowledgeHit, ...]: ...

    @abstractmethod
    def delete(self, scope: RequestScope, document_ids: Sequence[str], *, reason: str) -> None: ...

    def close(self) -> None:
        """No resources by default; resource-owning providers override this hook."""
        return None
