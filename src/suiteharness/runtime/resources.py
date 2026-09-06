"""Object authorization seams; products own the resource and ownership rules."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import Protocol

from .scopes import RequestScope


@dataclass(frozen=True, slots=True)
class ResourceRef:
    tenant_id: str
    product_id: str
    kind: str
    resource_id: str

    def __post_init__(self) -> None:
        for value in (self.tenant_id, self.product_id, self.kind, self.resource_id):
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > 512
                or "\x00" in value
            ):
                raise ValueError("resource identifiers must be bounded non-blank strings")


class ResourceAuthorizer(Protocol):
    async def authorize(self, scope: RequestScope, resource: ResourceRef, *, action: str) -> bool:
        """Check current object ownership from trusted server storage, not request fields."""
        ...


class ResourceAccessDenied(PermissionError):
    """Use the same public response for unknown and inaccessible resources."""


@dataclass(frozen=True, slots=True)
class AuthorizedResource:
    """Request-local result, never a client credential or reusable authorization cache."""

    scope: RequestScope
    resource: ResourceRef
    action: str


async def require_resource_access(
    authorizer: ResourceAuthorizer,
    scope: RequestScope,
    resource: ResourceRef,
    *,
    action: str,
    timeout_seconds: float = 10.0,
) -> AuthorizedResource:
    if (
        not isinstance(action, str)
        or not action.strip()
        or len(action) > 128
        or isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int | float)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("invalid resource authorization parameters")
    if resource.tenant_id != scope.tenant_id or resource.product_id != scope.product_id:
        raise ResourceAccessDenied("resource not found")
    try:
        async with asyncio.timeout(timeout_seconds):
            permitted = await authorizer.authorize(scope, resource, action=action)
    except Exception:
        raise ResourceAccessDenied("resource not found") from None
    if permitted is not True:
        raise ResourceAccessDenied("resource not found")
    return AuthorizedResource(scope=scope, resource=resource, action=action)
