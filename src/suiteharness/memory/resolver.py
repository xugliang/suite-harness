"""Resolve session conversation memory providers for channel execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Mapping

from suiteharness.runtime.scopes import ProductContext, RequestScope, ScopePath

from .session_memory import SessionConversationMemory
from .services import CONVERSATION_MEMORY


@dataclass(slots=True)
class DefaultSessionMemoryResolver:
    """Product-specific memory with a generic, server-owned fallback."""

    default: SessionConversationMemory = field(default_factory=SessionConversationMemory)
    _custom: dict[tuple[str, str], SessionConversationMemory] = field(default_factory=dict)

    def register(
        self,
        tenant_id: str,
        product_id: str,
        provider: SessionConversationMemory,
    ) -> None:
        if not isinstance(provider, SessionConversationMemory):
            raise TypeError("provider must be a SessionConversationMemory")
        self._custom[(tenant_id, product_id)] = provider

    def resolve(self, scope: RequestScope) -> SessionConversationMemory:
        return self._custom.get((scope.tenant_id, scope.product_id), self.default)


@dataclass(frozen=True, slots=True)
class ProductSessionMemoryResolver:
    """Resolve actual activated product bindings, never a second registration list."""

    products: Mapping[str, ProductContext]
    default: SessionConversationMemory

    def resolve(self, scope: RequestScope) -> SessionConversationMemory:
        product = self.products.get(scope.product_id)
        if product is None or product.path != ScopePath.product(scope.tenant_id, scope.product_id):
            raise ValueError('memory scope does not belong to an activated product')
        if product.bindings.contains(CONVERSATION_MEMORY):
            return product.resolve(CONVERSATION_MEMORY)
        return self.default


__all__ = ["DefaultSessionMemoryResolver", "ProductSessionMemoryResolver"]
