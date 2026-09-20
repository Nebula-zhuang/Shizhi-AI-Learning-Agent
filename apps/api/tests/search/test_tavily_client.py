"""Tavily 客户端测试。

全部使用 `httpx.MockTransport` 打桩传输层 —— **不打真实网络**，
所以这些用例在没网、没 Key 的环境下也能跑，且结果确定。

重点验证「失败 / 超时 / 无结果 / 未配置」这四种状态各自可区分。
把它们混成一个笼统的 error，用户就无法判断下一步该去配 Key 还是该重试。
"""

from __future__ import annotations

import httpx
import pytest

from app.core.config import get_settings
from app.search.tavily_client import (
    TAVILY_ENDPOINT,
    SearchError,
    SearchErrorCode,
    TavilyClient,
)


def make_client(handler, **kwargs) -> TavilyClient:
    """构造一个把网络请求交给 handler 处理的客户端。"""
    return TavilyClient(api_key=kwargs.pop("api_key", "tvly-test-key"),
                        transport=httpx.MockTransport(handler), **kwargs)


def ok_handler(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == TAVILY_ENDPOINT
        return httpx.Response(200, json=payload)

    return handler


SAMPLE = {
    "results": [
        {
            "title": "进程与线程的区别",
            "url": "https://example.com/process-thread",
            "content": "进程是资源分配的基本单位，线程是处理机调度的基本单位。",
            "score": 0.93,
        },
        {
            "title": "操作系统进程管理",
            "url": "https://example.com/os",
            "content": "进程具有动态性、并发性、独立性和异步性。",
            "score": 0.81,
        },
    ]
}


# --------------------------------------------------------------------------- #
# 正常路径
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_search_parses_results() -> None:
    client = make_client(ok_handler(SAMPLE))
    results = await client.search("进程与线程")

    assert len(results) == 2
    assert results[0].title == "进程与线程的区别"
    assert results[0].url == "https://example.com/process-thread"
    assert results[0].score == pytest.approx(0.93)
    assert "基本单位" in results[0].content


@pytest.mark.asyncio
async def test_snippet_is_truncated() -> None:
    long_text = "甲" * 500
    client = make_client(
        ok_handler({"results": [{"title": "t", "url": "u", "content": long_text}]}),
        snippet_chars=50,
    )
    results = await client.search("q")
    assert len(results[0].content) <= 51  # 50 + 省略号
    assert results[0].content.endswith("…")


@pytest.mark.asyncio
async def test_search_safe_returns_ok_outcome() -> None:
    client = make_client(ok_handler(SAMPLE))
    outcome = await client.search_safe("q")
    assert outcome.ok is True
    assert outcome.code is None
    assert len(outcome.results) == 2
    assert outcome.elapsed_ms >= 0
    assert "2" in outcome.summary()


# --------------------------------------------------------------------------- #
# 未配置 Key —— 不是故障，是设计上的降级
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_missing_key_raises_not_configured() -> None:
    client = TavilyClient(api_key="")
    assert client.available is False
    with pytest.raises(SearchError) as exc:
        await client.search("任意关键词")
    assert exc.value.code == SearchErrorCode.NOT_CONFIGURED


@pytest.mark.asyncio
async def test_missing_key_marks_outcome_as_skipped() -> None:
    """业务层靠 `skipped` 区分「没配 Key」与「配了但失败」。"""
    outcome = await TavilyClient(api_key="").search_safe("q")
    assert outcome.ok is False
    assert outcome.code == SearchErrorCode.NOT_CONFIGURED
    assert outcome.skipped is True


def test_capabilities_never_leaks_key() -> None:
    """能力探测只能暴露布尔值，绝不能回显 Key。"""
    client = TavilyClient(api_key="tvly-secret-value")
    caps = client.capabilities()
    assert caps["configured"] is True
    serialized = str(caps)
    assert "tvly-secret-value" not in serialized


# --------------------------------------------------------------------------- #
# 超时
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_timeout_is_reported_as_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client = make_client(handler)
    outcome = await client.search_safe("q")
    assert outcome.ok is False
    assert outcome.code == SearchErrorCode.TIMEOUT
    # 超时是故障，不是「设计上跳过」
    assert outcome.skipped is False


# --------------------------------------------------------------------------- #
# HTTP 错误
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_http_error_status_is_reported() -> None:
    client = make_client(lambda request: httpx.Response(429, text="rate limited"))
    with pytest.raises(SearchError) as exc:
        await client.search("q")
    assert exc.value.code == SearchErrorCode.HTTP_ERROR
    assert exc.value.status_code == 429


@pytest.mark.asyncio
async def test_connection_error_is_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    outcome = await make_client(handler).search_safe("q")
    assert outcome.code == SearchErrorCode.HTTP_ERROR


# --------------------------------------------------------------------------- #
# 无结果 —— 与「失败」区分开
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_empty_results_is_distinct_state() -> None:
    outcome = await make_client(ok_handler({"results": []})).search_safe("q")
    assert outcome.ok is False
    assert outcome.code == SearchErrorCode.EMPTY
    # 无结果既不是「跳过」也不是「故障」，业务层会把它记成 suspicious
    assert outcome.skipped is False


@pytest.mark.asyncio
async def test_missing_results_key_is_empty() -> None:
    outcome = await make_client(ok_handler({})).search_safe("q")
    assert outcome.code == SearchErrorCode.EMPTY


# --------------------------------------------------------------------------- #
# 返回体异常
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_non_json_body_is_parse_error() -> None:
    client = make_client(lambda request: httpx.Response(200, text="<html>not json</html>"))
    outcome = await client.search_safe("q")
    assert outcome.code == SearchErrorCode.PARSE_ERROR


@pytest.mark.asyncio
async def test_items_without_url_or_title_are_dropped() -> None:
    """返回体里有脏数据时要过滤，而不是把空壳塞给上层。"""
    payload = {"results": [{"title": "", "url": "", "content": "x"}, {"title": "有效", "url": "u"}]}
    results = await make_client(ok_handler(payload)).search("q")
    assert len(results) == 1
    assert results[0].title == "有效"


@pytest.mark.asyncio
async def test_blank_query_is_rejected() -> None:
    outcome = await make_client(ok_handler(SAMPLE)).search_safe("   ")
    assert outcome.code == SearchErrorCode.PARSE_ERROR


# --------------------------------------------------------------------------- #
# 配置来源
# --------------------------------------------------------------------------- #
def test_key_is_read_from_settings_not_hardcoded() -> None:
    """Key 一律来自配置（← 环境变量），构造时可被显式覆盖。"""
    settings = get_settings()
    from_settings = TavilyClient()
    assert from_settings.available == settings.is_tavily_configured
    assert from_settings.timeout == settings.tavily_timeout_s
    assert from_settings.max_results == settings.tavily_max_results

    overridden = TavilyClient(api_key="  spaced-key  ")
    assert overridden.available is True
    # 前后空白被清理，避免 .env 里带空格导致 401
    assert overridden._api_key == "spaced-key"
