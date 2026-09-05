"""Feishu company-channel adapter.

The package intentionally exposes protocol seams for callback decryption,
HTTP egress and the official long-connection SDK. No personal login or local
desktop deployment path exists.
"""

from suiteharness.channels.auth import (
    CompanyIdentity,
    CompanyIdentityDirectory,
    FeishuCompanyAuthenticator,
)

from .client import (
    CachedTenantAccessTokenProvider,
    FeishuHttpRequest,
    FeishuHttpTransport,
    FeishuMessageClient,
    HttpxFeishuTransport,
    TenantAccessTokenProvider,
)
from .long_connection import (
    FeishuLongConnectionRunner,
    FeishuLongConnectionSdk,
    VerifiedEventCallback,
)
from .models import (
    FeishuApiError,
    FeishuAuthenticationError,
    FeishuChannelError,
    FeishuEventOutcome,
    FeishuHttpResponse,
    FeishuPayloadError,
    FeishuSendResult,
    FeishuWebhookResponse,
)
from .processor import (
    EventDeduplicator,
    FeishuDispatcher,
    FeishuEventProcessor,
    FeishuWebhookHandler,
    InMemoryEventDeduplicator,
    OutboundSink,
    decode_json_object,
)
from .security import (
    BoundedDecryptor,
    FeishuHeaderSignatureVerifier,
    IdentityDecryptor,
    RawEventDecryptor,
    RawSignatureVerifier,
)
from .starlette import create_starlette_feishu_endpoint, create_starlette_feishu_route

__all__ = [
    "BoundedDecryptor",
    "CachedTenantAccessTokenProvider",
    "CompanyIdentity",
    "CompanyIdentityDirectory",
    "EventDeduplicator",
    "FeishuApiError",
    "FeishuAuthenticationError",
    "FeishuChannelError",
    "FeishuDispatcher",
    "FeishuEventOutcome",
    "FeishuEventProcessor",
    "FeishuCompanyAuthenticator",
    "FeishuHeaderSignatureVerifier",
    "FeishuHttpRequest",
    "FeishuHttpResponse",
    "FeishuHttpTransport",
    "FeishuLongConnectionRunner",
    "FeishuLongConnectionSdk",
    "FeishuMessageClient",
    "FeishuPayloadError",
    "FeishuSendResult",
    "FeishuWebhookHandler",
    "FeishuWebhookResponse",
    "HttpxFeishuTransport",
    "IdentityDecryptor",
    "InMemoryEventDeduplicator",
    "OutboundSink",
    "RawEventDecryptor",
    "RawSignatureVerifier",
    "TenantAccessTokenProvider",
    "VerifiedEventCallback",
    "create_starlette_feishu_endpoint",
    "create_starlette_feishu_route",
    "decode_json_object",
]
