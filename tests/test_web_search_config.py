from __future__ import annotations

import pytest
from pydantic import ValidationError

from suiteharness.config import HtmlSearchConfig, WebSearchConfig


def _provider(engine: str, provider_id: str, endpoint: str) -> HtmlSearchConfig:
    return HtmlSearchConfig(
        engine=engine,
        provider_id=provider_id,
        endpoint=endpoint,
    )


def test_html_search_config_accepts_bing_with_baidu_fallback() -> None:
    config = WebSearchConfig(
        default_provider="bing-html",
        fallback_providers=("baidu-html",),
        providers=(
            _provider("bing", "bing-html", "https://cn.bing.com/search"),
            _provider("baidu", "baidu-html", "https://www.baidu.com/s"),
        ),
    )

    assert config.default_provider == "bing-html"
    assert config.fallback_providers == ("baidu-html",)


@pytest.mark.parametrize(
    ("engine", "endpoint"),
    [
        ("bing", "https://www.baidu.com/s"),
        ("baidu", "https://cn.bing.com/search"),
        ("bing", "http://cn.bing.com/search"),
        ("baidu", "https://www.baidu.com/s?wd=hard-coded"),
    ],
)
def test_html_search_config_rejects_wrong_or_unsafe_engine_endpoint(
    engine: str, endpoint: str
) -> None:
    with pytest.raises(ValidationError):
        _provider(engine, f"{engine}-html", endpoint)


@pytest.mark.parametrize(
    "fallbacks",
    [
        ("missing-html",),
        ("bing-html",),
        ("baidu-html", "baidu-html"),
    ],
)
def test_search_config_rejects_invalid_fallback_chain(fallbacks: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError):
        WebSearchConfig(
            default_provider="bing-html",
            fallback_providers=fallbacks,
            providers=(
                _provider("bing", "bing-html", "https://cn.bing.com/search"),
                _provider("baidu", "baidu-html", "https://www.baidu.com/s"),
            ),
        )
