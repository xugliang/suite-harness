"""Product-scoped argument policy for shared root network tools."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from suiteharness.execution import ToolCallContext


def _freeze(
    value: Mapping[str, frozenset[str]],
) -> Mapping[str, frozenset[str]]:
    return MappingProxyType(
        {product_id: frozenset(items) for product_id, items in value.items()}
    )


@dataclass(frozen=True, slots=True)
class ProductEgressArgumentPolicy:
    """Authorize caller-selected routes after the authenticated product is known.

    The Web defaults are common company routes. Any non-default Web route and
    every Bash network are denied unless the current product is explicitly
    listed. An explicit empty product set denies even the common Web default.
    """

    default_search_provider: str
    default_fetch_route: str
    bash_profiles_by_product: Mapping[str, frozenset[str]]
    search_providers_by_product: Mapping[str, frozenset[str]]
    fetch_routes_by_product: Mapping[str, frozenset[str]]

    def __post_init__(self) -> None:
        if not self.default_search_provider or not self.default_fetch_route:
            raise ValueError("Web egress defaults must not be blank")
        object.__setattr__(
            self,
            "bash_profiles_by_product",
            _freeze(self.bash_profiles_by_product),
        )
        object.__setattr__(
            self,
            "search_providers_by_product",
            _freeze(self.search_providers_by_product),
        )
        object.__setattr__(
            self,
            "fetch_routes_by_product",
            _freeze(self.fetch_routes_by_product),
        )

    def authorize_bash(
        self,
        context: ToolCallContext,
        requested: str | None,
    ) -> str | None:
        if requested is None:
            return None
        allowed = self.bash_profiles_by_product.get(context.scope.product_id, frozenset())
        if requested not in allowed:
            raise PermissionError("requested Bash egress profile is not allowed for this product")
        return requested

    def authorize_search(
        self,
        context: ToolCallContext,
        requested: str | None,
    ) -> str:
        selected = requested or self.default_search_provider
        allowed = self.search_providers_by_product.get(
            context.scope.product_id,
            frozenset({self.default_search_provider}),
        )
        if selected not in allowed:
            raise PermissionError("requested search provider is not allowed for this product")
        return selected

    def authorize_fetch(
        self,
        context: ToolCallContext,
        requested: str | None,
    ) -> str:
        selected = requested or self.default_fetch_route
        allowed = self.fetch_routes_by_product.get(
            context.scope.product_id,
            frozenset({self.default_fetch_route}),
        )
        if selected not in allowed:
            raise PermissionError("requested Web fetch route is not allowed for this product")
        return selected


__all__ = ["ProductEgressArgumentPolicy"]
