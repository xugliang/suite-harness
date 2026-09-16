"""Control entry points preserve channel admission and complete session isolation."""

import asyncio
from datetime import UTC, datetime

import pytest

from suiteharness.channels.gateway import (
    ChannelAdmissionError,
    ChannelApplication,
    ChannelRoute,
    ConversationAuthorizer,
    EnterpriseChannelGateway,
    PrincipalAuthenticator,
    ProductAccessAuthorizer,
    ProductRouter,
)
from suiteharness.channels.models import (
    AuthenticatedPrincipal,
    ChannelKind,
    InboundMessage,
    OutboundEvent,
    OutboundEventKind,
)
from suiteharness.server.host import _HostedEnterpriseChannelGateway
from suiteharness.sessions import RunStopOutcome

PRINCIPAL = AuthenticatedPrincipal(
    tenant_id="company", principal_id="seller", roles=frozenset({"sales"})
)


def _message(**changes):
    values = dict(
        channel=ChannelKind.WEB, event_id="event1", message_id="control1",
        conversation_id="customer1", sender_external_id="seller", text="",
        product_id="product", received_at=datetime.now(UTC),
    )
    values.update(changes)
    return InboundMessage(**values)


class _Authenticator(PrincipalAuthenticator):
    def __init__(self, principal=PRINCIPAL):
        self.principal = principal
        self.calls = 0

    async def authenticate(self, message):
        self.calls += 1
        return self.principal


class _Access(ProductAccessAuthorizer, ConversationAuthorizer):
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.calls = []

    async def authorize(self, principal, **selection):
        self.calls.append((principal, selection))
        return self.allowed


class _Application(ChannelApplication):
    def __init__(self):
        self.calls = []

    async def handle(self, message, scope, *, before_commit=None):
        self.calls.append(("dispatch", message, scope, None))
        yield self.event(scope)

    @staticmethod
    def event(scope):
        return OutboundEvent(
            kind=OutboundEventKind.COMPLETED, request_id=scope.request_id,
            correlation_id=scope.correlation_id, payload={"output": "done"},
        )

    async def stop_message(self, scope, *, target_message_id):
        self.calls.append(("stop", None, scope, target_message_id))
        return RunStopOutcome.REQUESTED

    async def history(self, scope, *, limit=100, include_invalidated=False,
                      include_cancelled=False):
        self.calls.append(("history", None, scope, (limit, include_cancelled)))
        return ()

    async def continue_task(self, message, scope, *, target_message_id):
        self.calls.append(("continue", message, scope, target_message_id))
        yield self.event(scope)


def _gateway(application, **kwargs):
    return EnterpriseChannelGateway(
        tenant_id="company", authenticator=kwargs.pop("authenticator", _Authenticator()),
        router=kwargs.pop("router", ProductRouter(("product", "other"))),
        application=application, agent_ids={"product": "assistant"}, **kwargs,
    )


async def _invoke(gateway, operation, message, principal=PRINCIPAL):
    if operation == "stop":
        return await gateway.stop_authenticated(message, principal, "original-message")
    if operation == "history":
        return await gateway.history_authenticated(message, principal, 17)
    if operation == "continue":
        return [event async for event in gateway.continue_authenticated(
            message, principal, "original-message"
        )]
    return [event async for event in gateway.dispatch_authenticated(message, principal)]


def test_controls_and_dispatch_share_full_authenticated_scope_and_routing():
    async def check():
        application = _Application()
        authenticator = _Authenticator(None)
        access = _Access()
        gateway = _gateway(application, authenticator=authenticator,
                           product_access_authorizer=access)
        message = _message(metadata={"principal_id": "attacker", "resume_state": {}})
        for operation in ("dispatch", "stop", "history", "continue"):
            result = await _invoke(gateway, operation, message)
            if operation == "stop":
                assert result is RunStopOutcome.REQUESTED
            elif operation == "history":
                assert result == ()
            else:
                assert result[0].payload == {"output": "done"}
        scope = application.calls[0][2]
        assert scope.path.tenant_id == "company"
        assert scope.path.product_id == "product"
        assert scope.path.agent_id == "assistant"
        assert scope.principal_id == scope.session_owner_id == "seller"
        assert scope.roles == frozenset({"sales"})
        assert scope.channel_id == "web"
        assert all(call[2] == scope for call in application.calls)
        assert application.calls[1][3] == application.calls[3][3] == "original-message"
        assert application.calls[2][3] == (17, True)
        assert application.calls[3][1] is message
        assert authenticator.calls == 0
        assert len(access.calls) == 4
        assert all(call == (PRINCIPAL, dict(channel=ChannelKind.WEB,
                   conversation_id="customer1", product_id="product")) for call in access.calls)
    asyncio.run(check())


@pytest.mark.parametrize("operation", ["stop", "history", "continue"])
@pytest.mark.parametrize("rejection", ["tenant", "sender", "channel", "product", "route", "access", "membership", "missing_membership"])
def test_controls_reject_unauthorized_scope_before_calling_application(operation, rejection):
    async def check():
        application = _Application()
        message, principal, options = _message(), PRINCIPAL, {}
        if rejection == "tenant":
            principal = PRINCIPAL.model_copy(update={"tenant_id": "another-company"})
        elif rejection == "sender":
            principal = PRINCIPAL.model_copy(update={"principal_id": "another-seller"})
        elif rejection == "channel":
            message = _message(channel=ChannelKind.FEISHU)
        elif rejection == "product":
            message = _message(product_id="inactive")
        elif rejection == "route":
            options["router"] = ProductRouter(("product", "other"), (
                ChannelRoute(ChannelKind.WEB, "customer1", "other"),
            ))
        elif rejection == "access":
            options["product_access_authorizer"] = _Access(False)
        else:
            options["share_conversation_sessions"] = True
            if rejection == "membership":
                options["conversation_authorizer"] = _Access(False)
        gateway = _gateway(application, **options)
        with pytest.raises(ChannelAdmissionError):
            await _invoke(gateway, operation, message, principal)
        assert application.calls == []
    asyncio.run(check())


def test_control_identity_isolated_by_seller_customer_and_product():
    async def check():
        application = _Application()
        gateway = _gateway(application)
        for message, principal in (
            (_message(), PRINCIPAL),
            (_message(sender_external_id="another-seller"), PRINCIPAL.model_copy(update={"principal_id": "another-seller"})),
            (_message(conversation_id="customer2"), PRINCIPAL),
            (_message(product_id="other"), PRINCIPAL),
        ):
            await _invoke(gateway, "stop", message, principal)
        scopes = [call[2] for call in application.calls]
        assert len({scope.path.session_id for scope in scopes}) == 4
        assert scopes[1].principal_id == scopes[1].session_owner_id == "another-seller"
    asyncio.run(check())


def test_feishu_stop_authenticates_directory_identity_and_trusted_thread_like_dispatch():
    async def check():
        application = _Application()
        authenticator = _Authenticator()
        gateway = _gateway(application, authenticator=authenticator)
        message = _message(channel=ChannelKind.FEISHU, sender_external_id="external-id",
                           metadata={"session_scope_id": "chat:thread"})
        assert [event async for event in gateway.dispatch(message)]
        assert await gateway.stop(message, None) is RunStopOutcome.REQUESTED
        assert application.calls[0][2] == application.calls[1][2]
        assert application.calls[1][3] is None
        assert authenticator.calls == 2
        authenticator.principal = None
        with pytest.raises(ChannelAdmissionError):
            await gateway.stop(message, "original-message")
        assert len(application.calls) == 2
    asyncio.run(check())


@pytest.mark.parametrize("operation", ["stop", "history", "continue", "dispatch", "generic_stop", "generic_dispatch"])
def test_host_closed_blocks_every_control_and_dispatch_entrypoint(operation):
    async def check():
        application, authenticator = _Application(), _Authenticator()
        closed = False
        gateway = _HostedEnterpriseChannelGateway(
            tenant_id="company", authenticator=authenticator,
            router=ProductRouter(("product",)), application=application,
            host_closed=lambda: closed,
        )
        await _invoke(gateway, "dispatch", _message())
        closed = True
        with pytest.raises(RuntimeError, match="runtime is closed"):
            if operation == "generic_stop":
                await gateway.stop(_message(), None)
            elif operation == "generic_dispatch":
                [event async for event in gateway.dispatch(_message())]
            else:
                await _invoke(gateway, operation, _message())
        assert len(application.calls) == 1
        assert authenticator.calls == 0
    asyncio.run(check())
