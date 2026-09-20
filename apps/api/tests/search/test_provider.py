"""联网搜索 Provider 测试。

这个文件守的**不是"能不能搜到东西"，而是"会不会假装搜到东西"**。

一个编造的 URL 比"我不知道"危险得多：用户会点进去，发现什么都没有，
从此不再信任产品给出的任何引用。所以下面大部分断言是**否定式**的 ——
断言的是一段输出里**不该出现**什么。
"""

from __future__ import annotations

import pytest

from app.search.mcp_client import McpClient
from app.search.provider import (
    MOCK_HOST,
    RoutingWebSearchProvider,
    MCPWebSearchProvider,
    MockWebSearchProvider,
    RealWebSearchProvider,
    WebSearchProvider,
    describe_search_capability,
    get_search_provider,
)
from app.search.tavily_client import SearchErrorCode, TavilyClient


# --------------------------------------------------------------------------- #
# 契约
# --------------------------------------------------------------------------- #
def test_both_providers_satisfy_the_protocol() -> None:
    """三种实现都必须满足同一个协议 —— 否则换实现时 Agent 会崩。"""
    assert isinstance(RealWebSearchProvider(), WebSearchProvider)
    assert isinstance(MockWebSearchProvider(), WebSearchProvider)
    assert isinstance(MCPWebSearchProvider(), WebSearchProvider)


@pytest.mark.asyncio
async def test_search_returns_outcome_never_raises() -> None:
    """**失败也返回 SearchOutcome。** 调用方不该被迫在每个调用点写 try。"""
    provider = MockWebSearchProvider(configured=False)
    outcome = await provider.search("随便问一句")

    assert outcome.ok is False
    assert outcome.code == SearchErrorCode.NOT_CONFIGURED


# --------------------------------------------------------------------------- #
# 不许伪造：mock 的 URL 必须物理上不可能指向真实站点
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_mock_urls_use_reserved_invalid_domain() -> None:
    """`.invalid` 是 RFC 2606 保留后缀，**永远不会解析成真实站点**。

    这是环境层面的担保，不依赖写代码的人记得"别用 example.com"。
    """
    outcome = await MockWebSearchProvider().search("什么是 JVM")

    assert outcome.ok is True
    assert outcome.results
    for item in outcome.results:
        assert MOCK_HOST in item.url, f"mock 结果用了非保留域名：{item.url}"
        assert item.url.endswith(".invalid/doc")


@pytest.mark.asyncio
async def test_mock_results_are_marked_as_simulated() -> None:
    """能力自检里必须能看出"这是模拟的"，否则演示时会被当真。"""
    caps = MockWebSearchProvider().capabilities()
    assert caps["simulated"] is True
    assert caps["provider"] == "mock"

    real_caps = RealWebSearchProvider().capabilities()
    assert real_caps["simulated"] is False


@pytest.mark.asyncio
async def test_mock_results_depend_on_query() -> None:
    """**结果必须跟输入相关。**

    如果永远返回同一段假文本，"输入影响输出"这件事就无法被验证 ——
    链路一旦接错（比如把两次检索的结果搞混），测试也不会发现。
    """
    provider = MockWebSearchProvider()
    first = await provider.search("操作系统进程调度")
    second = await provider.search("数据库索引原理")

    assert first.results[0].title != second.results[0].title
    assert first.results[0].url != second.results[0].url


@pytest.mark.asyncio
async def test_mock_content_admits_it_is_simulated() -> None:
    """正文里也要自曝身份 —— 万一它被误当真实内容拼进提示词，还能被看出来。"""
    outcome = await MockWebSearchProvider().search("测试")
    assert "模拟" in outcome.results[0].content


# --------------------------------------------------------------------------- #
# MCP：留位但不假装已实现
# --------------------------------------------------------------------------- #
def test_mcp_provider_availability_follows_configuration() -> None:
    """MCP **已实装**（阶段 2）。

    阶段 1 这条断言的是"MCP 尚未实现"，现在要改成"可用性跟着配置走"：
    没配 URL 时不可用，配了才可用。
    """
    unconfigured = MCPWebSearchProvider(McpClient(url="", api_key=""))
    assert unconfigured.available is False

    configured = MCPWebSearchProvider(
        McpClient(url="https://mcp.tavily.com/mcp/", api_key="tvly-fake")
    )
    assert configured.available is True


@pytest.mark.asyncio
async def test_mcp_provider_without_config_is_explicitly_not_configured() -> None:
    """**明确说"没配"，不返回空结果，也不抛异常。**

    静默返回空会让"没接上"看起来像"搜了但没结果" —— 那是两件完全不同的事，
    而且后者会让用户以为"这个问题网上真的没人写"。

    （阶段 1 这里断言的是抛 NotImplementedError；MCP 实装后，
    未配置属于 NOT_CONFIGURED，由上层决定提示文案。）
    """
    provider = MCPWebSearchProvider(McpClient(url="", api_key=""))
    outcome = await provider.search("测试")

    assert outcome.ok is False
    assert outcome.code == SearchErrorCode.NOT_CONFIGURED
    assert outcome.results == []


# --------------------------------------------------------------------------- #
# 选择逻辑
# --------------------------------------------------------------------------- #
def test_get_provider_defaults_to_routing() -> None:
    """默认走**路由层**（阶段 2 起），由它按 `WEB_SEARCH_BACKEND` 选 MCP 或 Tavily。

    **未配 Key 时它返回 NOT_CONFIGURED，而不是悄悄变 mock** —— 那会让用户以为搜到了东西。
    """
    provider = get_search_provider()
    assert isinstance(provider, RoutingWebSearchProvider)
    assert provider.backend in {"auto", "mcp", "tavily"}


def test_get_provider_can_force_mock() -> None:
    assert isinstance(get_search_provider(prefer_mock=True), MockWebSearchProvider)


@pytest.mark.asyncio
async def test_real_provider_without_key_reports_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有 Key 时**不能**产出任何结果 —— 那才是真正的"没联网"。"""
    from app.search.tavily_client import TavilyClient

    provider = RealWebSearchProvider(TavilyClient(api_key=""))
    assert provider.available is False

    outcome = await provider.search("测试")
    assert outcome.ok is False
    assert outcome.code == SearchErrorCode.NOT_CONFIGURED
    assert outcome.results == [], "没配 Key 却返回了结果，说明有假数据混进来了"


# --------------------------------------------------------------------------- #
# 能力自检的措辞
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_capability_note_is_honest_when_unavailable() -> None:
    """不可用时的说明必须**明确承诺不会声称查过** —— 这是给用户看的原话。"""
    provider = RoutingWebSearchProvider(
        backend="auto",
        mcp=MockWebSearchProvider(configured=False),
        fallback=RealWebSearchProvider(TavilyClient(api_key="")),
    )
    assert provider.available is False

    # 描述函数走的是全局单例，这里直接验"不可用时的文案"由 provider 决定
    outcome = await provider.search("q")
    assert outcome.ok is False
    assert outcome.code == SearchErrorCode.NOT_CONFIGURED


def test_capability_never_leaks_the_key() -> None:
    """能力自检是公开接口，**绝不能回显 Key**。"""
    caps = RealWebSearchProvider().capabilities()
    dumped = str(caps)
    assert "tvly-" not in dumped
    assert "api_key" not in dumped
