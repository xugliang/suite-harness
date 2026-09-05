from __future__ import annotations

import asyncio
import gzip
import json
from collections.abc import Mapping, Sequence

import pytest
from pydantic import SecretStr

from suiteharness.web import (
    BaiduQianfanSearchProvider,
    BrowserWorkerHttpTransport,
    FetchLimits,
    FetchRoute,
    FetchTransportBinding,
    FetchTransportRegistry,
    HttpResponse,
    ManagedProxyConfig,
    MicrosoftFoundryGroundingProvider,
    PublicNetworkPolicy,
    SearchRequest,
    StaticAccessTokenProvider,
    StructuredJsonClient,
    WebFetchService,
    WebPolicyDenied,
    WebResponseTooLarge,
)


class StaticDns:
    def __init__(self, records: Mapping[str, frozenset[str]]) -> None:
        self.records = records

    async def resolve(self, hostname: str, port: int) -> frozenset[str]:
        del port
        return self.records.get(hostname, frozenset())


class ScriptedTransport:
    def __init__(self, responses: Sequence[HttpResponse]) -> None:
        self.responses = list(responses)
        self.requests = []

    async def send(self, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        return self.responses.pop(0)


def _registry(transport: ScriptedTransport) -> FetchTransportRegistry:
    return FetchTransportRegistry(
        FetchTransportBinding("china-direct", FetchRoute.DIRECT, transport)
    )


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://user:pass@public.example/",
        "http://localhost/admin",
        "http://127.0.0.1/admin",
        "http://[::1]/admin",
        "http://169.254.169.254/latest/meta-data",
        "http://100.64.0.1/carrier-internal",
        "http://192.0.2.1/documentation-network",
    ],
)
def test_network_policy_rejects_non_http_userinfo_and_non_public_targets(url: str) -> None:
    resolver = StaticDns(
        {
            "public.example": frozenset({"93.184.216.34"}),
            "127.0.0.1": frozenset({"127.0.0.1"}),
            "::1": frozenset({"::1"}),
            "169.254.169.254": frozenset({"169.254.169.254"}),
            "100.64.0.1": frozenset({"100.64.0.1"}),
            "192.0.2.1": frozenset({"192.0.2.1"}),
        }
    )
    with pytest.raises(WebPolicyDenied):
        asyncio.run(PublicNetworkPolicy(resolver).validate(url))


def test_policy_rejects_a_host_when_any_dns_answer_is_private() -> None:
    policy = PublicNetworkPolicy(
        StaticDns({"mixed.example": frozenset({"93.184.216.34", "10.0.0.7"})})
    )
    with pytest.raises(WebPolicyDenied, match="non-public"):
        asyncio.run(policy.validate("https://mixed.example"))


def test_fetch_validates_every_redirect_before_sending_next_request() -> None:
    resolver = StaticDns(
        {
            "public.example": frozenset({"93.184.216.34"}),
            "internal.example": frozenset({"10.0.0.8"}),
        }
    )
    transport = ScriptedTransport(
        [
            HttpResponse(
                302,
                {"Location": "http://internal.example/secret"},
                b"",
                "93.184.216.34",
            )
        ]
    )
    service = WebFetchService(
        _registry(transport), network_policy=PublicNetworkPolicy(resolver)
    )

    with pytest.raises(WebPolicyDenied, match="non-public"):
        asyncio.run(service.fetch("https://public.example/start"))
    assert len(transport.requests) == 1


def test_fetch_rejects_dns_rebinding_when_transport_attests_another_peer() -> None:
    resolver = StaticDns({"public.example": frozenset({"93.184.216.34"})})
    transport = ScriptedTransport(
        [HttpResponse(200, {"Content-Type": "text/plain"}, b"ok", "8.8.8.8")]
    )
    service = WebFetchService(
        _registry(transport), network_policy=PublicNetworkPolicy(resolver)
    )

    with pytest.raises(WebPolicyDenied, match="changed"):
        asyncio.run(service.fetch("https://public.example"))


def test_fetch_decodes_gb18030_and_extracts_html_without_scripts() -> None:
    resolver = StaticDns({"example.cn": frozenset({"93.184.216.34"})})
    html = "<html><head><title>中文标题</title><script>secret()</script></head><body>正文</body></html>"
    transport = ScriptedTransport(
        [
            HttpResponse(
                200,
                {"Content-Type": "text/html; charset=gb18030"},
                html.encode("gb18030"),
                "93.184.216.34",
            )
        ]
    )
    service = WebFetchService(
        _registry(transport), network_policy=PublicNetworkPolicy(resolver)
    )

    document = asyncio.run(service.fetch("https://example.cn/article"))
    assert document.title == "中文标题"
    assert "正文" in document.text
    assert "secret" not in document.text


def test_fetch_stops_a_compressed_response_expansion() -> None:
    resolver = StaticDns({"public.example": frozenset({"93.184.216.34"})})
    bomb = gzip.compress(b"A" * 100_000)
    transport = ScriptedTransport(
        [
            HttpResponse(
                200,
                {"Content-Type": "text/plain", "Content-Encoding": "gzip"},
                bomb,
                "93.184.216.34",
            )
        ]
    )
    service = WebFetchService(
        _registry(transport),
        network_policy=PublicNetworkPolicy(resolver),
        limits=FetchLimits(
            max_compressed_bytes=10_000,
            max_content_bytes=20_000,
            max_text_characters=10_000,
        ),
    )

    with pytest.raises(WebResponseTooLarge, match="decompressed"):
        asyncio.run(service.fetch("https://public.example/bomb"))


def test_fetch_routes_are_explicit_and_never_selected_from_environment() -> None:
    direct = ScriptedTransport(
        [HttpResponse(200, {"Content-Type": "text/plain"}, b"direct", "93.184.216.34")]
    )
    proxy = ScriptedTransport(
        [HttpResponse(200, {"Content-Type": "text/plain"}, b"proxy", "93.184.216.34")]
    )
    registry = _registry(direct)
    registry.register(FetchTransportBinding("cn-proxy", FetchRoute.MANAGED_PROXY, proxy))
    policy = PublicNetworkPolicy(
        StaticDns({"public.example": frozenset({"93.184.216.34"})})
    )
    service = WebFetchService(registry, network_policy=policy)

    document = asyncio.run(
        service.fetch("https://public.example", egress_profile="cn-proxy")
    )
    assert document.text == "proxy"
    assert len(proxy.requests) == 1
    assert direct.requests == []
    with pytest.raises(WebPolicyDenied, match="not configured"):
        asyncio.run(service.fetch("https://public.example", egress_profile="HTTP_PROXY"))


def test_managed_proxy_configuration_is_explicit_and_hides_secret_headers() -> None:
    config = ManagedProxyConfig(
        "http://proxy.company.internal:8080",
        {"Proxy-Authorization": "Bearer proxy-secret"},
    )
    assert "proxy-secret" not in repr(config)
    with pytest.raises(ValueError, match="userinfo"):
        ManagedProxyConfig("http://user:password@proxy.company.internal:8080")
    with pytest.raises(ValueError, match="absolute http"):
        ManagedProxyConfig("https://proxy.company.internal:8443")


class FakeBrowserWorker:
    def __init__(self) -> None:
        self.requests = []

    async def fetch(self, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        return HttpResponse(200, {"Content-Type": "text/html"}, b"ok", "93.184.216.34")


def test_browser_worker_transport_is_an_explicit_get_only_seam() -> None:
    worker = FakeBrowserWorker()
    transport = BrowserWorkerHttpTransport(worker)
    request = type("Request", (), {"method": "POST"})()
    with pytest.raises(WebPolicyDenied, match="GET"):
        asyncio.run(transport.send(request))  # type: ignore[arg-type]


def test_baidu_provider_uses_structured_json_api_and_keeps_token_out_of_repr() -> None:
    resolver = StaticDns({"qianfan.baidubce.com": frozenset({"93.184.216.34"})})
    body = json.dumps(
        {
            "references": [
                {
                    "title": "结果",
                    "url": "https://example.cn/result",
                    "content": "摘要",
                    "date": "2026-09-04",
                }
            ]
        },
        ensure_ascii=False,
    ).encode()
    transport = ScriptedTransport(
        [HttpResponse(200, {"Content-Type": "application/json"}, body, "93.184.216.34")]
    )
    client = StructuredJsonClient(
        _registry(transport), network_policy=PublicNetworkPolicy(resolver)
    )
    provider = BaiduQianfanSearchProvider(
        client, StaticAccessTokenProvider(SecretStr("super-secret-token"))
    )

    response = asyncio.run(provider.search(SearchRequest("国产大模型", count=5)))

    assert response.provider_id == "baidu-qianfan"
    assert response.results[0].title == "结果"
    request = transport.requests[0]
    assert request.url.endswith("/v2/ai_search/web_search")
    assert json.loads(request.body)["query"] == "国产大模型"
    assert "super-secret-token" not in repr(request)


class FakeFoundry:
    async def grounded_search(self, query: str, *, count: int):  # type: ignore[no-untyped-def]
        return [{"title": query, "url": "https://example.com", "snippet": str(count)}]


def test_microsoft_adapter_uses_foundry_grounding_client_not_bing_html() -> None:
    provider = MicrosoftFoundryGroundingProvider(FakeFoundry())
    response = asyncio.run(provider.search(SearchRequest("SuiteHarness", count=3)))
    assert response.provider_id == "microsoft-foundry-bing-grounding"
    assert response.results[0].snippet == "3"
