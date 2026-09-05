"""Server-managed OAuth token adapter for remote MCP connections."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from .models import OAuthTokenSet
from .protocols import OAuthTokenIssuer


class ServerManagedOAuthTokenProvider:
    """Cache scoped Bearer tokens obtained from a trusted credential issuer.

    This adapter has no browser, device-code or personal-account flow. The
    ``credential_ref`` must resolve to a deployment-managed service identity.
    """

    def __init__(
        self,
        issuer: OAuthTokenIssuer,
        *,
        refresh_skew: timedelta = timedelta(seconds=30),
    ) -> None:
        if refresh_skew < timedelta(0):
            raise ValueError("OAuth refresh skew must not be negative")
        self._issuer = issuer
        self._refresh_skew = refresh_skew
        self._tokens: dict[tuple[str, str], OAuthTokenSet] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def authorization_header(self, credential_ref: str, *, endpoint: str) -> str:
        key = (credential_ref, endpoint)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            token = self._tokens.get(key)
            if token is None or self._needs_refresh(token):
                token = await self._issuer.issue(credential_ref, resource=endpoint)
                self._tokens[key] = token
            return f"Bearer {token.access_token.get_secret_value()}"

    async def invalidate(self, credential_ref: str, *, endpoint: str) -> None:
        self._tokens.pop((credential_ref, endpoint), None)

    def _needs_refresh(self, token: OAuthTokenSet) -> bool:
        if token.expires_at is None:
            return False
        return token.expires_at <= datetime.now(UTC) + self._refresh_skew


__all__ = ["ServerManagedOAuthTokenProvider"]
