from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from suiteharness.channels import (
    AuthenticatedPrincipal,
    ChannelAdmissionError,
    ChannelKind,
    ChannelRoute,
    EnterpriseChannelGateway,
    InboundMessage,
    OutboundEvent,
    OutboundEventKind,
    ProductRouter,
)


def _message(*, product_id: str | None = None, sender: str = "ou-external") -> InboundMessage:
    return InboundMessage(
        channel=ChannelKind.FEISHU,
        event_id="event/ext/1",
        message_id="message/ext/1",
        conversation_id="chat/ext/1",
        sender_external_id=sender,
        text="hello",
        product_id=product_id,
        received_at=datetime.now(UTC),
    )


class Authenticator:
    async def authenticate(self, message):  # type: ignore[no-untyped-def]
        if message.sender_external_id == "denied":
            return None
        return AuthenticatedPrincipal(
            tenant_id="acme", principal_id="user-1", roles=frozenset({"employee"})
        )


class Application:
    def __init__(self) -> None:
        self.scopes = []

    async def handle(self, message, scope):  # type: ignore[no-untyped-def]
        self.scopes.append(scope)
        yield OutboundEvent(
            kind=OutboundEventKind.COMPLETED,
            request_id=scope.request_id,
            correlation_id=scope.correlation_id,
            payload={"product_id": scope.product_id},
        )


def test_multiple_products_require_explicit_or_configured_routing() -> None:
    router = ProductRouter(("sales", "support"))
    with pytest.raises(ChannelAdmissionError, match="multiple products"):
        router.resolve(_message())
    assert router.resolve(_message(product_id="support")) == "support"

    configured = ProductRouter(
        ("sales", "support"),
        (ChannelRoute(ChannelKind.FEISHU, "chat/ext/1", "sales"),),
    )
    assert configured.resolve(_message()) == "sales"
    with pytest.raises(ChannelAdmissionError, match="conflicts"):
        configured.resolve(_message(product_id="support"))


def test_gateway_authenticates_and_builds_product_isolated_scope() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        app = Application()
        gateway = EnterpriseChannelGateway(
            tenant_id="acme",
            authenticator=Authenticator(),
            router=ProductRouter(("sales",)),
            application=app,
        )
        events = [event async for event in gateway.dispatch(_message())]
        return app.scopes[0], events

    scope, events = asyncio.run(exercise())
    assert scope.tenant_id == "acme"
    assert scope.product_id == "sales"
    assert scope.channel_id == "feishu"
    assert scope.path.session_id.startswith("session-")
    assert events[0].payload == {"product_id": "sales"}


def test_gateway_does_not_trust_an_unknown_external_sender() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        gateway = EnterpriseChannelGateway(
            tenant_id="acme",
            authenticator=Authenticator(),
            router=ProductRouter(("sales",)),
            application=Application(),
        )
        return [event async for event in gateway.dispatch(_message(sender="denied"))]

    with pytest.raises(ChannelAdmissionError, match="not authorized"):
        asyncio.run(exercise())


def test_gateway_authenticator_timeout_and_errors_fail_closed() -> None:
    class BrokenAuthenticator:
        def __init__(self, *, slow: bool) -> None:
            self.slow = slow

        async def authenticate(self, message):  # type: ignore[no-untyped-def]
            if self.slow:
                await asyncio.sleep(1)
            raise RuntimeError("directory unavailable")

    async def dispatch(*, slow: bool):  # type: ignore[no-untyped-def]
        gateway = EnterpriseChannelGateway(
            tenant_id="acme",
            authenticator=BrokenAuthenticator(slow=slow),
            router=ProductRouter(("sales",)),
            application=Application(),
            authentication_timeout_seconds=0.001,
        )
        return [event async for event in gateway.dispatch(_message())]

    for slow in (False, True):
        with pytest.raises(ChannelAdmissionError, match="authentication failed closed"):
            asyncio.run(dispatch(slow=slow))


def test_gateway_checks_product_access_after_routing_and_fails_closed() -> None:
    class ProductAcl:
        def __init__(self, outcome):  # type: ignore[no-untyped-def]
            self.outcome = outcome
            self.calls = []

        async def authorize(self, principal, **context):  # type: ignore[no-untyped-def]
            self.calls.append((principal, context))
            if isinstance(self.outcome, BaseException):
                raise self.outcome
            return self.outcome

    async def dispatch(authorizer):  # type: ignore[no-untyped-def]
        gateway = EnterpriseChannelGateway(
            tenant_id="acme",
            authenticator=Authenticator(),
            router=ProductRouter(("sales", "support")),
            application=Application(),
            product_access_authorizer=authorizer,
        )
        return [
            event
            async for event in gateway.dispatch(_message(product_id="support"))
        ]

    allowed = ProductAcl(True)
    assert asyncio.run(dispatch(allowed))[0].payload == {"product_id": "support"}
    assert allowed.calls[0][1] == {
        "channel": ChannelKind.FEISHU,
        "conversation_id": "chat/ext/1",
        "product_id": "support",
    }

    with pytest.raises(ChannelAdmissionError, match="may not access"):
        asyncio.run(dispatch(ProductAcl(False)))
    with pytest.raises(ChannelAdmissionError, match="failed closed"):
        asyncio.run(dispatch(ProductAcl(RuntimeError("directory unavailable"))))


def test_gateway_authorizers_timeout_and_conversation_errors_fail_closed() -> None:
    class SlowProductAcl:
        async def authorize(self, principal, **context):  # type: ignore[no-untyped-def]
            await asyncio.sleep(1)
            return True

    class BrokenMembership:
        async def authorize(self, principal, **context):  # type: ignore[no-untyped-def]
            raise RuntimeError("directory unavailable")

    web_message = InboundMessage(
        channel=ChannelKind.WEB,
        event_id="event-web-timeout",
        message_id="message-web-timeout",
        conversation_id="team-room",
        sender_external_id="alice",
        text="hello",
        received_at=datetime.now(UTC),
    )
    principal = AuthenticatedPrincipal(tenant_id="acme", principal_id="alice")

    async def slow_product():  # type: ignore[no-untyped-def]
        gateway = EnterpriseChannelGateway(
            tenant_id="acme",
            authenticator=Authenticator(),
            router=ProductRouter(("sales",)),
            application=Application(),
            product_access_authorizer=SlowProductAcl(),
            authorization_timeout_seconds=0.001,
        )
        return [
            event
            async for event in gateway.dispatch_authenticated(web_message, principal)
        ]

    async def broken_conversation():  # type: ignore[no-untyped-def]
        gateway = EnterpriseChannelGateway(
            tenant_id="acme",
            authenticator=Authenticator(),
            router=ProductRouter(("sales",)),
            application=Application(),
            share_conversation_sessions=True,
            conversation_authorizer=BrokenMembership(),
        )
        return [
            event
            async for event in gateway.dispatch_authenticated(web_message, principal)
        ]

    with pytest.raises(ChannelAdmissionError, match="product access authorization failed"):
        asyncio.run(slow_product())
    with pytest.raises(
        ChannelAdmissionError, match="conversation membership authorization failed"
    ):
        asyncio.run(broken_conversation())


@pytest.mark.parametrize(
    "invalid", (True, "10", 0, -1, 60.1, float("inf"), float("nan"))
)
@pytest.mark.parametrize(
    ("keyword", "message"),
    (
        ("authentication_timeout_seconds", "authentication_timeout_seconds"),
        ("authorization_timeout_seconds", "authorization_timeout_seconds"),
    ),
)
def test_gateway_rejects_invalid_security_timeout(
    invalid: object,
    keyword: str,
    message: str,
) -> None:
    arguments = {keyword: invalid}
    with pytest.raises(ValueError, match=message):
        EnterpriseChannelGateway(
            tenant_id="acme",
            authenticator=Authenticator(),
            router=ProductRouter(("sales",)),
            application=Application(),
            **arguments,  # type: ignore[arg-type]
        )


def test_gateway_preserves_pre_authenticated_web_roles_without_second_authentication() -> None:
    class MustNotAuthenticate:
        async def authenticate(self, message):  # type: ignore[no-untyped-def]
            raise AssertionError("WebSocket identity must not be authenticated twice")

    async def exercise():  # type: ignore[no-untyped-def]
        app = Application()
        gateway = EnterpriseChannelGateway(
            tenant_id="acme",
            authenticator=MustNotAuthenticate(),
            router=ProductRouter(("sales",)),
            application=app,
        )
        message = InboundMessage(
            channel=ChannelKind.WEB,
            event_id="event-web-1",
            message_id="message-web-1",
            conversation_id="conversation-web-1",
            sender_external_id="employee-1",
            text="hello",
            received_at=datetime.now(UTC),
            metadata={"principal_id": "administrator", "roles": "admin"},
        )
        principal = AuthenticatedPrincipal(
            tenant_id="acme",
            principal_id="employee-1",
            roles=frozenset({"employee", "sales"}),
        )
        events = [
            event async for event in gateway.dispatch_authenticated(message, principal)
        ]
        return app.scopes[0], events

    scope, events = asyncio.run(exercise())
    assert scope.principal_id == "employee-1"
    assert scope.roles == frozenset({"employee", "sales"})
    assert events[0].payload == {"product_id": "sales"}


def test_shared_web_session_requires_membership_and_preserves_actual_principal() -> None:
    class Membership:
        async def authorize(
            self,
            principal,
            *,
            channel,
            conversation_id,
            product_id,
        ):  # type: ignore[no-untyped-def]
            return (
                principal.principal_id in {"alice", "bob"}
                and channel is ChannelKind.WEB
                and conversation_id == "team-room"
                and product_id == "sales"
            )

    def message(principal: str) -> InboundMessage:
        return InboundMessage(
            channel=ChannelKind.WEB,
            event_id=f"event-{principal}",
            message_id=f"message-{principal}",
            conversation_id="team-room",
            sender_external_id=principal,
            text="hello",
            received_at=datetime.now(UTC),
        )

    async def exercise():  # type: ignore[no-untyped-def]
        missing = EnterpriseChannelGateway(
            tenant_id="acme",
            authenticator=Authenticator(),
            router=ProductRouter(("sales",)),
            application=Application(),
            share_conversation_sessions=True,
        )
        alice = AuthenticatedPrincipal(tenant_id="acme", principal_id="alice")
        with pytest.raises(ChannelAdmissionError, match="not a member"):
            _ = [
                event
                async for event in missing.dispatch_authenticated(message("alice"), alice)
            ]

        app = Application()
        gateway = EnterpriseChannelGateway(
            tenant_id="acme",
            authenticator=Authenticator(),
            router=ProductRouter(("sales",)),
            application=app,
            share_conversation_sessions=True,
            conversation_authorizer=Membership(),
        )
        for principal_id in ("alice", "bob"):
            principal = AuthenticatedPrincipal(
                tenant_id="acme", principal_id=principal_id
            )
            _ = [
                event
                async for event in gateway.dispatch_authenticated(
                    message(principal_id), principal
                )
            ]
        return app.scopes

    scopes = asyncio.run(exercise())
    assert scopes[0].path.session_id == scopes[1].path.session_id
    assert scopes[0].session_owner_id == scopes[1].session_owner_id
    assert scopes[0].principal_id == "alice"
    assert scopes[1].principal_id == "bob"


@pytest.mark.parametrize(
    ("message", "principal"),
    [
        (
            _message(sender="employee-1"),
            AuthenticatedPrincipal(tenant_id="acme", principal_id="employee-1"),
        ),
        (
            InboundMessage(
                channel=ChannelKind.WEB,
                event_id="event-web-1",
                message_id="message-web-1",
                conversation_id="conversation-web-1",
                sender_external_id="employee-1",
                text="hello",
                received_at=datetime.now(UTC),
            ),
            AuthenticatedPrincipal(tenant_id="other", principal_id="employee-1"),
        ),
        (
            InboundMessage(
                channel=ChannelKind.WEB,
                event_id="event-web-1",
                message_id="message-web-1",
                conversation_id="conversation-web-1",
                sender_external_id="spoofed-user",
                text="hello",
                received_at=datetime.now(UTC),
            ),
            AuthenticatedPrincipal(tenant_id="acme", principal_id="employee-1"),
        ),
    ],
    ids=("non-web-channel", "cross-tenant", "sender-mismatch"),
)
def test_pre_authenticated_dispatch_rejects_identity_boundary_mismatch(
    message: InboundMessage,
    principal: AuthenticatedPrincipal,
) -> None:
    async def exercise():
        gateway = EnterpriseChannelGateway(
            tenant_id="acme",
            authenticator=Authenticator(),
            router=ProductRouter(("sales",)),
            application=Application(),
        )
        return [
            event async for event in gateway.dispatch_authenticated(message, principal)
        ]

    with pytest.raises(ChannelAdmissionError):
        asyncio.run(exercise())
