from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from suiteharness.channels import (
    ChannelKind,
    CompanyIdentity,
    FeishuCompanyAuthenticator,
    InboundMessage,
)


class Directory:
    def __init__(self, identities: dict[str, CompanyIdentity]) -> None:
        self.identities = identities
        self.calls: list[tuple[str, str]] = []

    async def resolve_feishu_identity(
        self,
        *,
        tenant_id: str,
        sender_external_id: str,
    ) -> CompanyIdentity | None:
        self.calls.append((tenant_id, sender_external_id))
        return self.identities.get(sender_external_id)


def _message(
    sender: str,
    *,
    channel: ChannelKind = ChannelKind.FEISHU,
    metadata: dict[str, str] | None = None,
) -> InboundMessage:
    return InboundMessage(
        channel=channel,
        event_id="event-1",
        message_id="message-1",
        conversation_id="conversation-1",
        sender_external_id=sender,
        text="hello",
        received_at=datetime.now(UTC),
        metadata=metadata or {},
    )


def test_feishu_sender_is_mapped_only_through_tenant_directory() -> None:
    directory = Directory(
        {
            "ou_verified": CompanyIdentity(
                principal_id="employee-42",
                roles=frozenset({"employee", "sales"}),
            )
        }
    )
    authenticator = FeishuCompanyAuthenticator(tenant_id="acme", directory=directory)

    principal = asyncio.run(
        authenticator.authenticate(
            _message(
                "ou_verified",
                metadata={
                    "tenant_id": "attacker",
                    "principal_id": "administrator",
                    "roles": "admin",
                },
            )
        )
    )

    assert directory.calls == [("acme", "ou_verified")]
    assert principal is not None
    assert principal.tenant_id == "acme"
    assert principal.principal_id == "employee-42"
    assert principal.roles == frozenset({"employee", "sales"})


def test_unknown_feishu_user_is_denied_by_default() -> None:
    directory = Directory({})
    authenticator = FeishuCompanyAuthenticator(tenant_id="acme", directory=directory)

    principal = asyncio.run(authenticator.authenticate(_message("ou_unknown")))

    assert principal is None
    assert directory.calls == [("acme", "ou_unknown")]


def test_non_feishu_message_never_reaches_feishu_directory() -> None:
    directory = Directory(
        {"web-user": CompanyIdentity(principal_id="employee-42")}
    )
    authenticator = FeishuCompanyAuthenticator(tenant_id="acme", directory=directory)

    principal = asyncio.run(
        authenticator.authenticate(_message("web-user", channel=ChannelKind.WEB))
    )

    assert principal is None
    assert directory.calls == []


def test_directory_identity_validation_is_strict() -> None:
    with pytest.raises(ValueError, match="directory identity"):
        CompanyIdentity(principal_id="../../administrator")
