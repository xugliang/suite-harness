"""Tenant/product/session routing for authenticated enterprise channels."""

from __future__ import annotations

import asyncio
import hashlib
import math
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Protocol

from suiteharness.runtime import RequestScope, ScopePath

from .models import AuthenticatedPrincipal, ChannelKind, InboundMessage, OutboundEvent


class ChannelAdmissionError(PermissionError):
    """Safe rejection at the company-channel boundary."""


class PrincipalAuthenticator(Protocol):
    async def authenticate(self, message: InboundMessage) -> AuthenticatedPrincipal | None: ...


class ConversationAuthorizer(Protocol):
    """Authorize membership in a client-addressable shared Web conversation."""

    async def authorize(
        self,
        principal: AuthenticatedPrincipal,
        *,
        channel: ChannelKind,
        conversation_id: str,
        product_id: str,
    ) -> bool: ...


class ProductAccessAuthorizer(Protocol):
    """Authorize one authenticated company principal for one active product."""

    async def authorize(
        self,
        principal: AuthenticatedPrincipal,
        *,
        channel: ChannelKind,
        conversation_id: str,
        product_id: str,
    ) -> bool: ...


class ChannelApplication(Protocol):
    def handle(
        self,
        message: InboundMessage,
        scope: RequestScope,
    ) -> AsyncIterator[OutboundEvent]: ...


@dataclass(frozen=True, slots=True)
class ChannelRoute:
    channel: ChannelKind
    conversation_id: str
    product_id: str


class ProductRouter:
    """Require an unambiguous active product for every inbound conversation."""

    def __init__(
        self,
        product_ids: tuple[str, ...],
        routes: tuple[ChannelRoute, ...] = (),
    ) -> None:
        if not product_ids or len(product_ids) != len(set(product_ids)):
            raise ValueError("product_ids must be a non-empty unique tuple")
        self._products = frozenset(product_ids)
        indexed: dict[tuple[ChannelKind, str], str] = {}
        for route in routes:
            if route.product_id not in self._products:
                raise ValueError("channel route references an inactive product")
            key = (route.channel, route.conversation_id)
            if key in indexed:
                raise ValueError("duplicate channel conversation route")
            indexed[key] = route.product_id
        self._routes: Mapping[tuple[ChannelKind, str], str] = indexed

    def resolve(self, message: InboundMessage) -> str:
        configured = self._routes.get((message.channel, message.conversation_id))
        if configured is not None:
            if message.product_id is not None and message.product_id != configured:
                raise ChannelAdmissionError(
                    "the selected product conflicts with the configured conversation route"
                )
            return configured
        if message.product_id is not None:
            if message.product_id not in self._products:
                raise ChannelAdmissionError("the selected product is not active")
            return message.product_id
        if len(self._products) == 1:
            return next(iter(self._products))
        raise ChannelAdmissionError(
            "multiple products are active; select a product or configure a conversation route"
        )


class EnterpriseChannelGateway:
    """Authenticate first, then construct the only RequestScope seen by products."""

    def __init__(
        self,
        *,
        tenant_id: str,
        authenticator: PrincipalAuthenticator,
        router: ProductRouter,
        application: ChannelApplication,
        agent_ids: Mapping[str, str] | None = None,
        share_conversation_sessions: bool = False,
        conversation_authorizer: ConversationAuthorizer | None = None,
        product_access_authorizer: ProductAccessAuthorizer | None = None,
        authentication_timeout_seconds: float = 10.0,
        authorization_timeout_seconds: float = 10.0,
    ) -> None:
        if (
            isinstance(authentication_timeout_seconds, bool)
            or not isinstance(authentication_timeout_seconds, int | float)
            or not math.isfinite(authentication_timeout_seconds)
            or authentication_timeout_seconds <= 0
            or authentication_timeout_seconds > 60
        ):
            raise ValueError(
                "authentication_timeout_seconds must be a finite number in (0, 60]"
            )
        if (
            isinstance(authorization_timeout_seconds, bool)
            or not isinstance(authorization_timeout_seconds, int | float)
            or not math.isfinite(authorization_timeout_seconds)
            or authorization_timeout_seconds <= 0
            or authorization_timeout_seconds > 60
        ):
            raise ValueError(
                "authorization_timeout_seconds must be a finite number in (0, 60]"
            )
        self._tenant_id = tenant_id
        self._authenticator = authenticator
        self._router = router
        self._application = application
        self._agent_ids = dict(agent_ids or {})
        self._share_conversation_sessions = share_conversation_sessions
        self._conversation_authorizer = conversation_authorizer
        self._product_access_authorizer = product_access_authorizer
        self._authentication_timeout_seconds = float(authentication_timeout_seconds)
        self._authorization_timeout_seconds = float(authorization_timeout_seconds)

    @staticmethod
    def _safe_id(prefix: str, *values: str) -> str:
        digest = hashlib.sha256("\x1f".join(values).encode("utf-8")).hexdigest()[:40]
        return f"{prefix}-{digest}"

    async def dispatch(self, message: InboundMessage) -> AsyncIterator[OutboundEvent]:
        try:
            async with asyncio.timeout(self._authentication_timeout_seconds):
                principal = await self._authenticator.authenticate(message)
        except TimeoutError as exc:
            raise ChannelAdmissionError(
                "channel identity authentication failed closed"
            ) from exc
        except Exception as exc:
            raise ChannelAdmissionError(
                "channel identity authentication failed closed"
            ) from exc
        if principal is None or principal.tenant_id != self._tenant_id:
            raise ChannelAdmissionError("channel identity is not authorized for this deployment")
        async for event in self._dispatch_principal(message, principal):
            yield event

    async def dispatch_authenticated(
        self,
        message: InboundMessage,
        principal: AuthenticatedPrincipal,
    ) -> AsyncIterator[OutboundEvent]:
        """Dispatch a Web message using its already authenticated handshake identity.

        The WebSocket boundary must pass the complete principal here.  Re-running
        a message authenticator would either lose roles or trust a weaker identity
        representation reconstructed from client-controlled message data.
        """

        if message.channel is not ChannelKind.WEB:
            raise ChannelAdmissionError(
                "pre-authenticated dispatch is available only to the Web channel"
            )
        if (
            not isinstance(principal, AuthenticatedPrincipal)
            or principal.tenant_id != self._tenant_id
            or message.sender_external_id != principal.principal_id
        ):
            raise ChannelAdmissionError("authenticated Web identity does not match the message")
        async for event in self._dispatch_principal(message, principal):
            yield event

    async def _dispatch_principal(
        self,
        message: InboundMessage,
        principal: AuthenticatedPrincipal,
    ) -> AsyncIterator[OutboundEvent]:
        product_id = self._router.resolve(message)
        product_authorizer = self._product_access_authorizer
        if product_authorizer is not None:
            product_allowed = await self._authorize(
                product_authorizer,
                principal,
                message=message,
                product_id=product_id,
                failure_message="product access authorization failed closed",
            )
            if product_allowed is not True:
                raise ChannelAdmissionError(
                    "the authenticated principal may not access this product"
                )
        if self._share_conversation_sessions and message.channel is ChannelKind.WEB:
            authorizer = self._conversation_authorizer
            if authorizer is None:
                raise ChannelAdmissionError(
                    "the authenticated principal is not a member of this shared conversation"
                )
            conversation_allowed = await self._authorize(
                authorizer,
                principal,
                message=message,
                product_id=product_id,
                failure_message="conversation membership authorization failed closed",
            )
            if conversation_allowed is not True:
                raise ChannelAdmissionError(
                    "the authenticated principal is not a member of this shared conversation"
                )
        session_values = [message.channel.value, message.conversation_id, product_id]
        if not self._share_conversation_sessions:
            session_values.append(principal.principal_id)
        session_id = self._safe_id("session", *session_values)
        request_id = self._safe_id("request", message.channel.value, message.event_id)
        correlation_id = self._safe_id(
            "correlation", message.channel.value, message.conversation_id, message.message_id
        )
        agent_id = self._agent_ids.get(product_id, "default")
        session_owner_id = (
            self._safe_id(
                "shared",
                self._tenant_id,
                message.channel.value,
                message.conversation_id,
                product_id,
            )
            if self._share_conversation_sessions
            else principal.principal_id
        )
        scope = RequestScope(
            path=ScopePath.agent(
                self._tenant_id,
                product_id,
                agent_id,
                session_id,
            ),
            principal_id=principal.principal_id,
            channel_id=message.channel.value,
            roles=principal.roles,
            purpose="company-channel-message",
            request_id=request_id,
            correlation_id=correlation_id,
            session_owner_id=session_owner_id,
        )
        async for event in self._application.handle(message, scope):
            yield event

    async def _authorize(
        self,
        authorizer: ConversationAuthorizer | ProductAccessAuthorizer,
        principal: AuthenticatedPrincipal,
        *,
        message: InboundMessage,
        product_id: str,
        failure_message: str,
    ) -> bool:
        try:
            async with asyncio.timeout(self._authorization_timeout_seconds):
                return await authorizer.authorize(
                    principal,
                    channel=message.channel,
                    conversation_id=message.conversation_id,
                    product_id=product_id,
                )
        except TimeoutError as exc:
            raise ChannelAdmissionError(failure_message) from exc
        except Exception as exc:
            raise ChannelAdmissionError(failure_message) from exc


__all__ = [
    "ChannelAdmissionError",
    "ChannelApplication",
    "ChannelRoute",
    "ConversationAuthorizer",
    "EnterpriseChannelGateway",
    "PrincipalAuthenticator",
    "ProductAccessAuthorizer",
    "ProductRouter",
]
