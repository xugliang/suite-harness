from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from pydantic import SecretStr

from suiteharness.mcp.auth import ServerManagedOAuthTokenProvider
from suiteharness.mcp.models import OAuthTokenSet


class FakeIssuer:
    def __init__(self) -> None:
        self.calls = 0

    async def issue(self, credential_ref, *, resource):  # type: ignore[no-untyped-def]
        self.calls += 1
        return OAuthTokenSet(
            access_token=SecretStr(f"token-{self.calls}"),
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )


def test_server_oauth_tokens_are_cached_and_explicitly_invalidated() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        issuer = FakeIssuer()
        provider = ServerManagedOAuthTokenProvider(issuer)
        first = await provider.authorization_header(
            "mcp/service", endpoint="https://mcp.example.com"
        )
        second = await provider.authorization_header(
            "mcp/service", endpoint="https://mcp.example.com"
        )
        await provider.invalidate("mcp/service", endpoint="https://mcp.example.com")
        third = await provider.authorization_header(
            "mcp/service", endpoint="https://mcp.example.com"
        )
        return issuer, first, second, third

    issuer, first, second, third = asyncio.run(exercise())
    assert issuer.calls == 2
    assert first == second == "Bearer token-1"
    assert third == "Bearer token-2"


def test_oauth_token_repr_does_not_expose_secret() -> None:
    token = OAuthTokenSet(access_token=SecretStr("top-secret-token"))
    assert "top-secret-token" not in repr(token)
    assert "top-secret-token" not in token.model_dump_json()
