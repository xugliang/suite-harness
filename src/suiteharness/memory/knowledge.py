"""Small deterministic knowledge provider used as a contract reference."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from threading import RLock
from typing import Any

from .errors import MemoryCapabilityError, MemoryErrorCode
from .models import (
    KNOWLEDGE_READ,
    KNOWLEDGE_WRITE,
    KnowledgeDocument,
    KnowledgeHit,
    RequestScope,
    SpaceKind,
    SpaceRef,
)

_TOKEN = re.compile(r"\w+", re.UNICODE)


def _require(scope: RequestScope, capability: str) -> None:
    if capability not in scope.roles:
        raise MemoryCapabilityError(
            MemoryErrorCode.PERMISSION_DENIED,
            "the authenticated principal lacks the required knowledge capability",
            capability=capability,
        )


def _private_space(scope: RequestScope, space: SpaceRef) -> None:
    if (
        space.kind is not SpaceKind.PRODUCT_PRIVATE
        or space.tenant_id != scope.tenant_id
        or space.owner_product_id != scope.product_id
    ):
        # Shared knowledge needs its own policy provider. The reference
        # implementation refuses it instead of mistaking Customer360 claim
        # permission for document permission.
        raise MemoryCapabilityError(
            MemoryErrorCode.PERMISSION_DENIED,
            "knowledge space is not owned by this product scope",
        )


def _fingerprint(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class InMemoryKnowledgeProvider:
    """Product-private lexical store; useful for tests and local prototypes.

    This deliberately is not a vector database. It defines authorization,
    idempotency and isolation semantics that production RAG providers must
    preserve before applying their own ranking algorithm.
    """

    name = "knowledge-reference"

    def __init__(self) -> None:
        self._lock = RLock()
        self._documents: dict[tuple[str, str, str], KnowledgeDocument] = {}
        self._idempotency: dict[tuple[str, str, str], tuple[str, tuple[str, ...]]] = {}

    def index(
        self,
        scope: RequestScope,
        documents: Sequence[KnowledgeDocument],
        *,
        idempotency_key: str,
    ) -> tuple[str, ...]:
        _require(scope, KNOWLEDGE_WRITE)
        key_text = idempotency_key.strip()
        if not key_text:
            raise MemoryCapabilityError(
                MemoryErrorCode.INVALID_ARGUMENT, "idempotency_key must not be empty"
            )
        material: list[dict[str, Any]] = []
        pending: list[tuple[tuple[str, str, str], KnowledgeDocument]] = []
        seen: set[str] = set()
        for document in documents:
            _private_space(scope, document.space)
            document_id = document.document_id.strip()
            if not document_id or not document.text.strip():
                raise MemoryCapabilityError(
                    MemoryErrorCode.INVALID_ARGUMENT,
                    "knowledge documents require a non-empty id and text",
                )
            if document_id in seen:
                raise MemoryCapabilityError(
                    MemoryErrorCode.INVALID_ARGUMENT,
                    f"duplicate document id in one index command: {document_id}",
                )
            seen.add(document_id)
            storage_key = (scope.tenant_id, scope.product_id, document_id)
            pending.append((storage_key, deepcopy(document)))
            material.append(
                {
                    "document_id": document_id,
                    "text": document.text,
                    "metadata": dict(document.metadata),
                }
            )

        fingerprint = _fingerprint(material)
        operation_key = (scope.tenant_id, scope.product_id, key_text)
        with self._lock:
            replay = self._idempotency.get(operation_key)
            if replay is not None:
                if replay[0] != fingerprint:
                    raise MemoryCapabilityError(
                        MemoryErrorCode.IDEMPOTENCY_CONFLICT,
                        "idempotency key was already used for a different index command",
                    )
                return replay[1]
            if any(key in self._documents for key, _ in pending):
                raise MemoryCapabilityError(
                    MemoryErrorCode.INVALID_STATE,
                    "a document id already exists in this product knowledge space",
                )
            for key, document in pending:
                self._documents[key] = document
            result = tuple(document.document_id for _, document in pending)
            self._idempotency[operation_key] = (fingerprint, result)
            return result

    def search(
        self,
        scope: RequestScope,
        *,
        spaces: Sequence[SpaceRef],
        query: str,
        filters: Mapping[str, Any],
        limit: int,
    ) -> tuple[KnowledgeHit, ...]:
        _require(scope, KNOWLEDGE_READ)
        if not query.strip() or limit < 1 or limit > 100:
            raise MemoryCapabilityError(
                MemoryErrorCode.INVALID_ARGUMENT,
                "query must not be empty and limit must be between 1 and 100",
            )
        # Authorize every requested space before looking at candidates or scores.
        for space in spaces:
            _private_space(scope, space)
        allowed = set(spaces)
        query_text = query.casefold().strip()
        tokens = set(_TOKEN.findall(query_text))
        hits: list[KnowledgeHit] = []
        with self._lock:
            documents = tuple(deepcopy(tuple(self._documents.values())))
        for document in documents:
            if document.space not in allowed:
                continue
            if any(document.metadata.get(key) != value for key, value in filters.items()):
                continue
            text = document.text.casefold()
            matches = sum(text.count(token) for token in tokens if token)
            if query_text in text:
                matches += 2
            if matches <= 0:
                continue
            hits.append(
                KnowledgeHit(
                    document_id=document.document_id,
                    space=document.space,
                    score=float(matches),
                    excerpt=document.text[:240],
                    citation=deepcopy(dict(document.metadata)),
                )
            )
        hits.sort(key=lambda hit: (-hit.score, hit.document_id))
        return tuple(hits[:limit])

    def delete(
        self,
        scope: RequestScope,
        document_ids: Sequence[str],
        *,
        reason: str,
    ) -> None:
        _require(scope, KNOWLEDGE_WRITE)
        if not reason.strip() or not document_ids:
            raise MemoryCapabilityError(
                MemoryErrorCode.INVALID_ARGUMENT,
                "delete requires document ids and a non-empty reason",
            )
        keys = [(scope.tenant_id, scope.product_id, item.strip()) for item in document_ids]
        if len(keys) != len(set(keys)):
            raise MemoryCapabilityError(
                MemoryErrorCode.INVALID_ARGUMENT,
                "delete command contains duplicate document ids",
            )
        with self._lock:
            if any(not item[2] or item not in self._documents for item in keys):
                raise MemoryCapabilityError(MemoryErrorCode.NOT_FOUND, "knowledge document not found")
            for key in keys:
                del self._documents[key]

    def close(self) -> None:
        return None


__all__ = ["InMemoryKnowledgeProvider"]
