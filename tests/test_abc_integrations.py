"""Nominal integration contracts reject incomplete and duck-typed adapters."""

import importlib
import inspect
from abc import ABC

import pytest

PORTS = {
    "mcp.protocols": "McpEgressPolicy McpTokenProvider OAuthMetadataResolver OAuthTokenIssuer SandboxedStdioSession SandboxedStdioSessionFactory McpTransport McpHttpResumeStore ResumableMcpHttpTransport LegacySseAdapter McpTransportFactory McpSampler McpElicitationHandler McpClientTaskHandler McpLogSink McpChangeSink",
    "web.network": "DnsResolver HttpTransport",
    "web.search": "AccessTokenProvider WebSearchProvider",
    "web.fetch": "PdfTextExtractor",
    "channels.attachments": "AttachmentResolver",
    "channels.auth": "CompanyIdentityDirectory",
    "channels.gateway": "PrincipalAuthenticator ConversationAuthorizer ProductAccessAuthorizer ChannelApplication",
    "channels.feishu.client": "FeishuHttpTransport TenantAccessTokenProvider",
    "channels.feishu.processor": "FeishuDispatcher EventDeduplicator",
    "channels.feishu.security": "RawEventDecryptor RawSignatureVerifier",
    "channels.websocket.server": "WebSocketConnection WebSocketAuthenticator WebSocketSessionRevalidator WebSocketDispatcher",
    "server.access": "ChannelGrantIssuer",
    "server.asgi": "CompanyHttpAuthenticator",
    "tools.workspace": "WorkspaceBindingResolver",
}


@pytest.mark.parametrize(
    "module,name",
    [(module, name) for module, names in PORTS.items() for name in names.split()],
)
def test_mandatory_integration_contract_cannot_be_omitted(module, name):
    contract = getattr(importlib.import_module("suiteharness." + module), name)
    assert issubclass(contract, ABC)
    assert inspect.isabstract(contract)

    class Incomplete(contract):
        pass

    with pytest.raises(TypeError):
        Incomplete()
    # Matching method names never silently registers a foreign implementation.
    duck = type(
        "Duck",
        (),
        {method: lambda self: None for method in contract.__abstractmethods__},
    )
    assert not isinstance(duck(), contract)


CONCRETE = {
    "channels.approvals": "InteractiveApprovalCoordinator",
    "channels.policy": "CompanyChannelAuthorizationPolicy",
    "server.foundation": "ModelRuntime",
    "server.extensions": "_ConfiguredPluginTrustPolicy",
    "mcp.auth": "ServerManagedOAuthTokenProvider",
    "mcp.callbacks": "GatewayMcpSampler",
    "mcp.transports": "SandboxedStdioTransport StreamableHttpTransport SecureMcpTransportFactory",
    "mcp.manager": "_DurableResumeTransport",
    "web.network": "SystemDnsResolver DirectHttpTransport ManagedProxyHttpTransport BrowserWorkerHttpTransport",
    "web.search": "StaticAccessTokenProvider HtmlSearchProvider BaiduQianfanSearchProvider MicrosoftFoundryGroundingProvider",
    "channels.auth": "FeishuCompanyAuthenticator",
    "channels.gateway": "EnterpriseChannelGateway",
    "channels.feishu.client": "CachedTenantAccessTokenProvider HttpxFeishuTransport",
    "channels.feishu.processor": "InMemoryEventDeduplicator",
    "channels.feishu.security": "FeishuHeaderSignatureVerifier IdentityDecryptor BoundedDecryptor",
    "channels.websocket.auth": "ServerSessionWebSocketAuthenticator",
    "channels.websocket.starlette": "StarletteWebSocketConnection",
    "server.access": "ConfiguredGrantIssuer",
    "server.application": "ChannelApplication",
    "server.host": "_RejectingAuthenticator",
    "tools.workspace": "MappingWorkspaceBindingResolver",
}


@pytest.mark.parametrize(
    "module,name",
    [(module, name) for module, names in CONCRETE.items() for name in names.split()],
)
def test_production_adapters_implement_all_mandatory_methods(module, name):
    concrete = getattr(importlib.import_module("suiteharness." + module), name)
    assert issubclass(concrete, ABC)
    assert not inspect.isabstract(concrete), concrete.__abstractmethods__


def test_resume_transport_and_dispatch_aliases_are_nominal():
    from suiteharness.channels.dispatch import FeishuDispatcher, WebSocketDispatcher
    from suiteharness.channels.feishu.processor import FeishuDispatcher as FeishuAlias
    from suiteharness.channels.gateway import EnterpriseChannelGateway
    from suiteharness.channels.websocket.server import (
        WebSocketDispatcher as SocketAlias,
    )
    from suiteharness.mcp.protocols import McpTransport, ResumableMcpHttpTransport
    from suiteharness.mcp.transports import StreamableHttpTransport

    assert issubclass(StreamableHttpTransport, ResumableMcpHttpTransport)
    assert issubclass(ResumableMcpHttpTransport, McpTransport)
    assert FeishuAlias is FeishuDispatcher
    assert SocketAlias is WebSocketDispatcher
    assert issubclass(EnterpriseChannelGateway, FeishuDispatcher)
    assert issubclass(EnterpriseChannelGateway, WebSocketDispatcher)
