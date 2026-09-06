"""Company channel identities and root-owned tool authorization policy."""

from .approvals import ApprovalChallenge, ApprovalPublisher, InteractiveApprovalCoordinator
from .attachments import AttachmentContentBuilder, AttachmentResolver, ResolvedAttachment
from .auth import CompanyIdentity, CompanyIdentityDirectory, FeishuCompanyAuthenticator
from .gateway import (
    ChannelAdmissionError,
    ChannelApplication,
    ChannelRoute,
    ConversationAuthorizer,
    EnterpriseChannelGateway,
    PrincipalAuthenticator,
    ProductAccessAuthorizer,
    ProductRouter,
)
from .models import (
    AuthenticatedPrincipal,
    ChannelAttachment,
    ChannelKind,
    InboundMessage,
    OutboundEvent,
    OutboundEventKind,
)
from .policy import CompanyChannelAuthorizationPolicy, WorkspaceWriteRule

__all__ = [
    "ApprovalChallenge",
    "ApprovalPublisher",
    "AuthenticatedPrincipal",
    "AttachmentContentBuilder",
    "AttachmentResolver",
    "ChannelAdmissionError",
    "ChannelApplication",
    "ChannelAttachment",
    "ChannelKind",
    "ChannelRoute",
    "ConversationAuthorizer",
    "CompanyIdentity",
    "CompanyIdentityDirectory",
    "CompanyChannelAuthorizationPolicy",
    "FeishuCompanyAuthenticator",
    "InteractiveApprovalCoordinator",
    "EnterpriseChannelGateway",
    "InboundMessage",
    "OutboundEvent",
    "OutboundEventKind",
    "PrincipalAuthenticator",
    "ProductAccessAuthorizer",
    "ProductRouter",
    "ResolvedAttachment",
    "WorkspaceWriteRule",
]
