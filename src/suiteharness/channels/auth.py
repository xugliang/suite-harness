"""Company-directory authentication for provider-verified channel identities.

The channel adapter is responsible for verifying the provider event first.  This
module then maps that provider-owned external identifier to a company-owned
principal.  Message metadata is deliberately absent from the directory lookup.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import ValidationError

from .models import AuthenticatedPrincipal, ChannelKind, InboundMessage


@dataclass(frozen=True, slots=True)
class CompanyIdentity:
    """Tenant-local identity returned by a trusted company directory adapter."""

    principal_id: str
    roles: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        # Reuse the channel boundary's strict identifier validation without
        # allowing the directory to choose or override the deployment tenant.
        try:
            checked = AuthenticatedPrincipal(
                tenant_id="validation-tenant",
                principal_id=self.principal_id,
                roles=self.roles,
            )
        except ValidationError as exc:
            raise ValueError("invalid company directory identity") from exc
        object.__setattr__(self, "principal_id", checked.principal_id)
        object.__setattr__(self, "roles", checked.roles)


class CompanyIdentityDirectory(Protocol):
    """Resolve a provider-owned Feishu identity inside one tenant.

    Production implementations commonly query an HR directory, IAM service or
    a synchronized database.  Unknown and disabled users must return ``None``.
    """

    async def resolve_feishu_identity(
        self,
        *,
        tenant_id: str,
        sender_external_id: str,
    ) -> CompanyIdentity | None: ...


class FeishuCompanyAuthenticator:
    """Map an already verified Feishu sender through a company directory."""

    def __init__(self, *, tenant_id: str, directory: CompanyIdentityDirectory) -> None:
        # AuthenticatedPrincipal owns the canonical tenant identifier rules.
        try:
            checked = AuthenticatedPrincipal(
                tenant_id=tenant_id,
                principal_id="validation-principal",
            )
        except ValidationError as exc:
            raise ValueError("invalid Feishu deployment tenant_id") from exc
        self._tenant_id = checked.tenant_id
        self._directory = directory

    async def authenticate(self, message: InboundMessage) -> AuthenticatedPrincipal | None:
        if message.channel is not ChannelKind.FEISHU:
            return None
        identity = await self._directory.resolve_feishu_identity(
            tenant_id=self._tenant_id,
            sender_external_id=message.sender_external_id,
        )
        if not isinstance(identity, CompanyIdentity):
            return None
        return AuthenticatedPrincipal(
            tenant_id=self._tenant_id,
            principal_id=identity.principal_id,
            roles=identity.roles,
        )


__all__ = [
    "CompanyIdentity",
    "CompanyIdentityDirectory",
    "FeishuCompanyAuthenticator",
]
