from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import cast

import pytest

from suiteharness.execution import ToolCallContext
from suiteharness.tools import ProductEgressArgumentPolicy
from suiteharness.tools.web import WebSearchTool
from suiteharness.web import (
    FetchRoute,
    FetchTransportBinding,
    FetchTransportRegistry,
    HtmlSearchClient,
    HtmlSearchProvider,
    HttpResponse,
    SearchProviderRegistry,
    SearchRequest,
    SearchResponse,
    SearchResult,
    ValidatedTarget,
    WebAccessError,
    WebPolicyDenied,
    WebSearchService,
)
from suiteharness.web.network import HttpTransport
from suiteharness.web.search import WebSearchProvider


class _PublicPolicy:
    def __init__(self) -> None:
        self.validated_urls: list[str] = []

    async def validate(self, url: str) -> ValidatedTarget:
        self.validated_urls.append(url)
        return ValidatedTarget(
            url=url,
            hostname="search.example",
            port=443,
            addresses=frozenset({"203.0.113.10"}),
        )

    def verify_connected_peer(self, target: ValidatedTarget, connected_ip: str) -> None:
        if connected_ip not in target.addresses:
            raise WebPolicyDenied("unexpected peer")


class _StaticTransport(HttpTransport):
    def __init__(self, body: str, *, status: int = 200) -> None:
        self._body = body.encode("utf-8")
        self._status = status
        self.requests = []

    async def send(self, request):
        self.requests.append(request)
        return HttpResponse(
            status=self._status,
            headers={"content-type": "text/html; charset=utf-8"},
            body=self._body,
            connected_target_ip="203.0.113.10",
        )


def _client(body: str) -> tuple[HtmlSearchClient, _StaticTransport, _PublicPolicy]:
    transport = _StaticTransport(body)
    registry = FetchTransportRegistry(
        FetchTransportBinding("direct", FetchRoute.DIRECT, transport)
    )
    policy = _PublicPolicy()
    return HtmlSearchClient(registry, network_policy=policy), transport, policy


def test_bing_html_provider_encodes_query_and_parses_organic_results() -> None:
    client, transport, policy = _client(
        """
        <ol>
          <li class="b_algo">
            <img src="https://img.example/icon.png">
            <h2><a href="https://shop.example/lipstick?a=1&amp;b=2">春季口红推荐</a></h2>
            <div class="b_caption"><p>适合日常通勤<br>的热门色号。</p></div>
          </li>
          <li class="b_algo">
            <h2><a href="https://second.example/item">第二条结果</a></h2>
            <div class="b_caption"><p>第二条摘要。</p></div>
          </li>
        </ol>
        """
    )
    provider = HtmlSearchProvider(
        client,
        provider_id="bing-html",
        engine="bing",
        endpoint="https://cn.bing.com/search",
    )

    response = asyncio.run(provider.search(SearchRequest("口红 100-500 元", count=1)))

    assert response == SearchResponse(
        provider_id="bing-html",
        query="口红 100-500 元",
        results=(
            SearchResult(
                title="春季口红推荐",
                url="https://shop.example/lipstick?a=1&b=2",
                snippet="适合日常通勤 的热门色号。",
            ),
        ),
    )
    assert len(transport.requests) == 1
    assert transport.requests[0].method == "GET"
    assert transport.requests[0].headers["User-Agent"].startswith("Mozilla/5.0")
    assert policy.validated_urls == [
        "https://cn.bing.com/search?q=%E5%8F%A3%E7%BA%A2+100-500+%E5%85%83"
    ]


def test_baidu_html_provider_parses_standard_result_containers() -> None:
    client, _, _ = _client(
        """
        <div id="content_left">
          <div class="result c-container">
            <h3 class="t"><a href="https://www.baidu.com/link?url=abc">百元口红购买指南</a></h3>
            <div class="c-abstract">整理了不同肤色可选的品牌和价格。</div>
          </div>
        </div>
        """
    )
    provider = HtmlSearchProvider(
        client,
        provider_id="baidu-html",
        engine="baidu",
        endpoint="https://www.baidu.com/s",
    )

    response = asyncio.run(provider.search(SearchRequest("口红推荐", count=5)))

    assert response.results == (
        SearchResult(
            title="百元口红购买指南",
            url="https://www.baidu.com/link?url=abc",
            snippet="整理了不同肤色可选的品牌和价格。",
        ),
    )


def test_baidu_challenge_page_is_a_provider_failure() -> None:
    client, _, _ = _client(
        '<html><title>百度安全验证</title><a href="https://wappass.baidu.com/static/captcha/">验证</a></html>'
    )
    provider = HtmlSearchProvider(
        client,
        provider_id="baidu-html",
        engine="baidu",
        endpoint="https://www.baidu.com/s",
    )

    with pytest.raises(WebAccessError, match="challenge"):
        asyncio.run(provider.search(SearchRequest("测试")))


@dataclass
class _Provider(WebSearchProvider):
    _provider_id: str
    outcome: SearchResponse | Exception
    calls: int = 0

    @property
    def provider_id(self) -> str:
        return self._provider_id

    async def search(self, request: SearchRequest) -> SearchResponse:
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _response(provider_id: str, query: str, *titles: str) -> SearchResponse:
    return SearchResponse(
        provider_id=provider_id,
        query=query,
        results=tuple(
            SearchResult(title=title, url=f"https://example.com/{index}")
            for index, title in enumerate(titles)
        ),
    )


@pytest.mark.parametrize(
    "primary_outcome", [WebAccessError("offline"), _response("bing-html", "q")]
)
def test_default_search_falls_back_after_failure_or_empty_results(
    primary_outcome,
) -> None:
    primary = _Provider("bing-html", primary_outcome)
    fallback = _Provider("baidu-html", _response("baidu-html", "q", "百度结果"))
    registry = SearchProviderRegistry(primary)
    registry.register(fallback)
    service = WebSearchService(registry, fallback_provider_ids=("baidu-html",))

    response = asyncio.run(service.search(SearchRequest("q")))

    assert response.provider_id == "baidu-html"
    assert [item.title for item in response.results] == ["百度结果"]
    assert primary.calls == 1
    assert fallback.calls == 1


def test_policy_denial_is_not_bypassed_by_fallback() -> None:
    primary = _Provider("bing-html", WebPolicyDenied("private target"))
    fallback = _Provider("baidu-html", _response("baidu-html", "q", "不应调用"))
    registry = SearchProviderRegistry(primary)
    registry.register(fallback)
    service = WebSearchService(registry, fallback_provider_ids=("baidu-html",))

    with pytest.raises(WebPolicyDenied, match="private target"):
        asyncio.run(service.search(SearchRequest("q")))

    assert fallback.calls == 0


def test_explicit_provider_selection_never_uses_fallback() -> None:
    primary = _Provider("bing-html", _response("bing-html", "q", "unused"))
    fallback = _Provider("baidu-html", WebAccessError("offline"))
    registry = SearchProviderRegistry(primary)
    registry.register(fallback)
    service = WebSearchService(registry, fallback_provider_ids=("baidu-html",))

    with pytest.raises(WebAccessError, match="offline"):
        asyncio.run(service.search(SearchRequest("q"), provider_id="baidu-html"))

    assert primary.calls == 0
    assert fallback.calls == 1


def test_web_tool_preserves_omitted_provider_for_server_owned_fallback() -> None:
    primary = _Provider("bing-html", WebAccessError("offline"))
    fallback = _Provider("baidu-html", _response("baidu-html", "q", "百度结果"))
    registry = SearchProviderRegistry(primary)
    registry.register(fallback)
    service = WebSearchService(registry, fallback_provider_ids=("baidu-html",))
    policy = ProductEgressArgumentPolicy(
        default_search_provider="bing-html",
        default_fetch_route="direct",
        bash_profiles_by_product={},
        search_providers_by_product={},
        fetch_routes_by_product={},
    )
    tool = WebSearchTool(service, policy)
    context = cast(
        ToolCallContext,
        SimpleNamespace(scope=SimpleNamespace(product_id="sales-profile")),
    )

    output = asyncio.run(tool(context, {"query": "q"}))

    assert output["provider"] == "baidu-html"
    assert primary.calls == 1
    assert fallback.calls == 1
