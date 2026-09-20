"""联网搜索的统一入口。

## 三种后端

    RealWebSearchProvider     Tavily REST（阶段 1 就在用）
    MCPWebSearchProvider      Tavily 官方 MCP Server（本阶段新增）
    MockWebSearchProvider     离线回归用

三种实现背后是同一个 `WebSearchProvider` 协议，调用方只依赖协议。

## 三种模式（`WEB_SEARCH_BACKEND`）

    auto    优先 MCP；**运行时故障**回退 Tavily 并上报回退原因
    mcp     只用 MCP。失败就明确失败 —— 便于验证 MCP 真的在工作
    tavily  只用 Tavily，**不发起任何 MCP 请求**

## ⚠️ 回退规则里有一条是刻意的

MCP **连不上 / 401 / 超时** → 可以回退（网络抖一下而已）。

MCP **连上了但清单里没有搜索工具** → **不许回退**。

后者是**配置/兼容性错误**，不是网络问题。静默回退会把
"工具名对不上"这种确定性错误永远掩盖成"网络偶尔不好" ——
你会一直以为 MCP 在工作，直到某天 Tavily 也不可用。

现实中真发生过：Tavily MCP 的官方文档写 `tavily-search`，
而 `tools/list` 实际返回 `tavily_search`。**差一个字符，功能全废。**

## 一条不能让步的规矩：不许伪造

没配 Key、或者在用 mock 时，**绝不能产出看起来像真实来源的东西**。
mock 的 URL 用 RFC 2606 保留后缀 `.invalid`（物理上不可能指向真实站点）。
"""

from __future__ import annotations

import hashlib
import time
from typing import Protocol, runtime_checkable

from app.core.config import settings
from app.core.logging import get_logger
from app.search.mcp_client import McpClient, McpError, pick_search_tool
from app.search.tavily_client import (
    SearchErrorCode,
    SearchOutcome,
    TavilyClient,
    TavilyResult,
)
from app.search.tavily_client import tavily_client as default_tavily_client

logger = get_logger(__name__)

#: RFC 2606 保留后缀。用它构造假 URL，永远不会误指向真实站点。
MOCK_HOST = "search.example.invalid"


@runtime_checkable
class WebSearchProvider(Protocol):
    """联网搜索的能力契约。

    刻意只有两个成员：一个问"能不能用"，一个真的去搜。
    多出来的方法都会成为将来换实现时的负担。
    """

    @property
    def available(self) -> bool:
        """当前是否具备搜索能力（Key 配了、服务可达）。"""
        ...

    async def search(self, query: str, *, max_results: int | None = None) -> SearchOutcome:
        """执行一次搜索。**失败也返回 SearchOutcome，不抛异常。**

        理由：调用方几乎总要在失败时降级（用已有知识回答 + 说明没联网），
        如果这里抛异常，每个调用点都得包一层 try —— 那是把同一个决定重复写 N 遍。
        """
        ...

    def capabilities(self) -> dict[str, object]:
        """供能力自检接口使用。**绝不回显 Key。**"""
        ...


# --------------------------------------------------------------------------- #
# Tavily REST
# --------------------------------------------------------------------------- #
class RealWebSearchProvider:
    """真实搜索（Tavily REST）。"""

    name = "tavily"

    def __init__(self, client: TavilyClient | None = None) -> None:
        self._client = client or default_tavily_client

    @property
    def available(self) -> bool:
        return self._client.available

    async def search(self, query: str, *, max_results: int | None = None) -> SearchOutcome:
        outcome = await self._client.search_safe(query, max_results=max_results)
        # 标上"这次是谁干的" —— 上层据此在界面上说清走的是哪条通道
        outcome.provider = self.name
        outcome.provider_detail = "tavily-rest"
        return outcome

    def capabilities(self) -> dict[str, object]:
        return {
            "provider": self.name,
            "available": self.available,
            "simulated": False,
        }


# --------------------------------------------------------------------------- #
# MCP
# --------------------------------------------------------------------------- #
class MCPWebSearchProvider:
    """通过 MCP 协议调用外部搜索服务。

    **工具名不硬编码**：每次会话先 `tools/list`，再用启发式挑出搜索工具，
    最后用**读回来的那个名字**去调。见 `pick_search_tool`。
    """

    name = "mcp"

    def __init__(self, client: McpClient | None = None) -> None:
        self._client = client or McpClient(
            url=settings.mcp_web_search_url,
            api_key=settings.tavily_api_key,
            timeout=settings.mcp_timeout_s,
        )

    @property
    def available(self) -> bool:
        # 只判断"配了没"。真正的可达性要发起一次请求才知道 ——
        # 在能力自检里做网络探测会让接口变慢且不稳定。
        return bool(self._client.configured and settings.tavily_api_key.strip())

    @property
    def trace(self):
        """协议级留痕。**"确实经过 MCP"的证据在这里，不在类名里。**"""
        return self._client.trace

    async def search(self, query: str, *, max_results: int | None = None) -> SearchOutcome:
        started = time.perf_counter()

        def elapsed() -> int:
            return int((time.perf_counter() - started) * 1000)

        if not self.available:
            return SearchOutcome(
                ok=False,
                code=SearchErrorCode.NOT_CONFIGURED,
                message="未配置 MCP 搜索服务（需要 MCP_WEB_SEARCH_URL 与 TAVILY_API_KEY）",
                provider=self.name,
            )

        try:
            info = await self._client.ensure_initialized()
            tools = await self._client.list_tools()
        except McpError as exc:
            # 握手失败属于运行时故障 —— 允许回退
            return SearchOutcome(
                ok=False,
                code=_runtime_code(exc),
                message=exc.message,
                provider=self.name,
                elapsed_ms=elapsed(),
            )

        tool = pick_search_tool(tools)
        if tool is None:
            # ⚠️ **配置错误，不允许回退。** 见模块文档的说明。
            names = ", ".join(t.name for t in tools) or "（空清单）"
            return SearchOutcome(
                ok=False,
                code=SearchErrorCode.CONFIG_ERROR,
                message=(
                    f"MCP 服务（{info.name}）的能力清单里没有可用的搜索工具。"
                    f"它提供了：{names}。请检查服务版本，或改用 tavily 后端。"
                ),
                provider=self.name,
                provider_detail=f"{info.name} v{info.version}",
                elapsed_ms=elapsed(),
            )

        # **只传工具自己声明的参数** —— 传一个它没声明的参数，
        # 严格实现的服务端会直接拒绝整次调用。
        arguments: dict[str, object] = {"query": query}
        if "max_results" in tool.properties:
            arguments["max_results"] = max_results or settings.tavily_max_results
        if "search_depth" in tool.properties:
            arguments["search_depth"] = settings.tavily_search_depth

        result = await self._client.call_tool(tool.name, arguments)

        if not result.ok:
            return SearchOutcome(
                ok=False,
                code=SearchErrorCode.HTTP_ERROR,
                message=result.error or "MCP 搜索调用失败",
                provider=self.name,
                provider_detail=f"{info.name} v{info.version}",
                elapsed_ms=elapsed(),
            )

        rows = result.as_search_results()
        if not rows:
            return SearchOutcome(
                ok=False,
                code=SearchErrorCode.EMPTY,
                message="MCP 搜索没有返回结果",
                provider=self.name,
                provider_detail=f"{info.name} v{info.version}",
                elapsed_ms=elapsed(),
            )

        limit = settings.tavily_snippet_chars
        results = [
            TavilyResult(
                title=str(row.get("title") or ""),
                url=str(row.get("url") or ""),
                content=str(row.get("content") or row.get("raw_content") or "")[:limit],
                score=float(row.get("score") or 0.0),
            )
            for row in rows
        ]

        logger.info(
            "MCP 搜索命中 %d 条（工具 %s，服务 %s v%s）",
            len(results),
            tool.name,
            info.name,
            info.version,
        )
        return SearchOutcome(
            ok=True,
            results=results,
            provider=self.name,
            provider_detail=f"{info.name} v{info.version}",
            elapsed_ms=elapsed(),
        )

    def capabilities(self) -> dict[str, object]:
        return {
            "provider": self.name,
            "available": self.available,
            "simulated": False,
            #: 已发现的工具名（连上过才有值）。**这是动态读回来的，不是常量。**
            "discovered_tools": list(self._client.trace.discovered_tools),
            "server_info": (
                self._client.trace.server_info.as_dict()
                if self._client.trace.server_info
                else None
            ),
        }


def _runtime_code(exc: McpError) -> SearchErrorCode:
    """把 MCP 错误码映射到检索错误码。

    ⚠️ **`config_error` 绝不被映射成可回退的码。** 这是本函数存在的意义。
    """
    if exc.code == "config_error":
        return SearchErrorCode.CONFIG_ERROR
    if exc.code == "timeout":
        return SearchErrorCode.TIMEOUT
    if exc.code == "unauthorized":
        return SearchErrorCode.HTTP_ERROR
    if exc.code == "empty_response" or exc.code == "bad_response":
        return SearchErrorCode.PARSE_ERROR
    return SearchErrorCode.HTTP_ERROR


# --------------------------------------------------------------------------- #
# Mock
# --------------------------------------------------------------------------- #
class MockWebSearchProvider:
    """离线回归用的假搜索。

    ## 为什么 url 要用 `.invalid`

    这是本项目里最容易被写坏的一处：mock 结果如果长得像真 URL
    （`https://example.com/...`），演示时截图出去、或者误接进生产，
    就会变成"产品展示了不存在的来源"。

    用 RFC 2606 的保留后缀 `.invalid`，**物理上不可能指向真实站点** ——
    这个约束是环境层面担保的，不依赖写代码的人记得。

    ## 为什么结果与输入相关

    如果永远返回同一段假文本，整条链路会因为"输入不影响输出"而无法发现数据流断裂。
    所以这里由 query 派生标题与片段 —— 换了问题，结果跟着变。
    """

    name = "mock"

    def __init__(self, *, configured: bool = True, simulate_mcp: bool = False) -> None:
        self._configured = configured
        #: 让测试能模拟"MCP 后端返回了什么"，而不必连真实网络
        self._simulate_mcp = simulate_mcp

    @property
    def available(self) -> bool:
        return self._configured

    async def search(self, query: str, *, max_results: int | None = None) -> SearchOutcome:
        if not self._configured:
            return SearchOutcome(
                ok=False,
                code=SearchErrorCode.NOT_CONFIGURED,
                message="未配置搜索服务",
                provider=self.name,
            )

        limit = max_results or settings.tavily_max_results
        results: list[TavilyResult] = []
        for index in range(min(limit, 3)):
            digest = hashlib.sha1(f"{query}:{index}".encode()).hexdigest()[:10]
            results.append(
                TavilyResult(
                    title=f"关于「{query}」的资料 {index + 1}",
                    url=f"https://{digest}.{MOCK_HOST}/doc",
                    content=(
                        f"（模拟结果）这里是与「{query}」相关的第 {index + 1} 段说明文字。"
                        "它由输入派生而来，用于离线回归，不对应任何真实网页。"
                    ),
                    score=round(0.9 - index * 0.1, 2),
                )
            )

        return SearchOutcome(
            ok=True,
            results=results,
            elapsed_ms=0,
            provider="mcp" if self._simulate_mcp else self.name,
            provider_detail="mock-server v0.0" if self._simulate_mcp else "mock",
        )

    def capabilities(self) -> dict[str, object]:
        return {
            "provider": self.name,
            "available": self.available,
            #: **显式声明这是模拟的**。调用方据此可以拒绝把结果当引用展示。
            "simulated": True,
        }


# --------------------------------------------------------------------------- #
# 路由：三种模式
# --------------------------------------------------------------------------- #
class RoutingWebSearchProvider:
    """按 `WEB_SEARCH_BACKEND` 选择后端，并实现回退规则。

    这一层是"用哪条通道"的**唯一**决策点。
    调用方（Agent / Tool）只看结果里的 `provider` / `fell_back` 字段，
    不需要知道上面有几个后端。
    """

    name = "routing"

    def __init__(
        self,
        *,
        backend: str | None = None,
        mcp: WebSearchProvider | None = None,
        fallback: WebSearchProvider | None = None,
    ) -> None:
        self.backend = (backend or settings.web_search_backend or "auto").strip().lower()
        if self.backend not in {"auto", "mcp", "tavily"}:
            logger.warning("未知的 WEB_SEARCH_BACKEND=%s，按 auto 处理", self.backend)
            self.backend = "auto"
        self._mcp = mcp or MCPWebSearchProvider()
        self._fallback = fallback or RealWebSearchProvider()

    @property
    def available(self) -> bool:
        if self.backend == "tavily":
            return self._fallback.available
        if self.backend == "mcp":
            return self._mcp.available
        return self._mcp.available or self._fallback.available

    async def search(self, query: str, *, max_results: int | None = None) -> SearchOutcome:
        if self.backend == "tavily":
            # 这个模式下**一次 MCP 请求都不发** —— 有测试守着这条
            return await self._fallback.search(query, max_results=max_results)

        if self.backend == "mcp":
            # 只用 MCP。失败就失败，不偷偷换 —— 否则没法验证 MCP 真的在工作。
            return await self._mcp.search(query, max_results=max_results)

        # ---- auto：优先 MCP
        if not self._mcp.available:
            outcome = await self._fallback.search(query, max_results=max_results)
            outcome.fell_back = True
            outcome.fallback_reason = "MCP 未配置"
            return outcome

        outcome = await self._mcp.search(query, max_results=max_results)
        if outcome.ok:
            return outcome

        if outcome.config_error:
            # ⚠️ **配置错误不回退。** 见模块文档。
            logger.error("MCP 配置错误，按规则不回退：%s", outcome.message)
            return outcome

        # 运行时故障 → 回退，并**如实上报**
        reason = f"MCP 不可用（{outcome.code}: {outcome.message[:120]}）"
        logger.warning("回退到 Tavily：%s", reason)

        fallback = await self._fallback.search(query, max_results=max_results)
        fallback.fell_back = True
        fallback.fallback_reason = reason
        return fallback

    def capabilities(self) -> dict[str, object]:
        caps = self._fallback.capabilities()
        caps["provider"] = self.backend
        caps["mcp"] = self._mcp.capabilities()
        return caps


# --------------------------------------------------------------------------- #
# 选择实现
# --------------------------------------------------------------------------- #
_routing_provider = RoutingWebSearchProvider()


def get_search_provider(*, prefer_mock: bool = False) -> WebSearchProvider:
    """按当前配置挑一个搜索实现。

    **没有 Key 时不会悄悄降级成 mock** —— 那会让用户以为搜到了东西。
    真实实现会返回 NOT_CONFIGURED，调用方据此明确告诉用户"暂时没法联网核实"。
    """
    if prefer_mock:
        return MockWebSearchProvider()
    return _routing_provider


def describe_search_capability() -> dict[str, object]:
    """给能力自检接口用的说明。

    **包含 MCP 的真实状态**（服务端自报的名称版本、动态发现的工具名），
    而不只是"配了一个 Provider"。
    """
    provider = get_search_provider()
    caps = provider.capabilities()

    backend = str(caps.get("provider") or "tavily")
    mcp_info = caps.get("mcp") if isinstance(caps.get("mcp"), dict) else {}
    discovered = list((mcp_info or {}).get("discovered_tools") or [])
    server_info = (mcp_info or {}).get("server_info") or None

    return {
        "backend": backend,
        "available": provider.available,
        "simulated": bool(caps.get("simulated", False)),
        "mcp": {
            "configured": bool((mcp_info or {}).get("available")),
            "url": settings.mcp_web_search_url,
            #: 连上过才有值。**这两个字段是"确实经过 MCP"的可核对证据。**
            "server_info": server_info,
            "discovered_tools": discovered,
        },
        "note": (
            "可以联网核实"
            if provider.available
            else "暂时无法联网核实，回答将基于已有知识，且不会声称查过外部资料"
        ),
    }
