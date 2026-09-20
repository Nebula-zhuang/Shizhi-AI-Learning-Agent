"""Tavily 搜索客户端。

**解耦要求（P2 硬约束）**：本模块是纯 HTTP 客户端 —— 它不认识「知识点」「文档」「校验状态」
这些业务概念，只有一个入参 `query` 和一个出参 `list[TavilyResult]`。
将来换成 Serper 只需新写一个同签名的客户端，业务层一行都不用改。

错误状态是显式的（`SearchErrorCode`），**失败/超时/无结果各自可区分**：
  not_configured —— 没配 TAVILY_API_KEY（不是故障，是设计上的降级）
  timeout        —— 超时
  http_error     —— 服务端返回非 2xx
  parse_error    —— 返回体结构不符合预期
  empty          —— 请求成功但没有任何结果

为什么把 `not_configured` 和 `timeout` 分开：前者"没配 Key"，后者"配了但调用失败"。
在界面上这是两种完全不同的状态 —— 一个该提示用户去配 Key，一个该提示重试。
混成一个 `error` 会让用户无从判断。

提供两个公开方法：
  `search()`      —— 抛异常，库风格，调用方自己处理
  `search_safe()` —— 永不抛异常，返回 SearchOutcome，业务层用这个，避免 try/except 满地爬
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import httpx

from app.core.config import Settings, settings
from app.core.logging import get_logger

logger = get_logger(__name__)

TAVILY_ENDPOINT = "https://api.tavily.com/search"


class SearchErrorCode(StrEnum):
    NOT_CONFIGURED = "not_configured"
    TIMEOUT = "timeout"
    HTTP_ERROR = "http_error"
    PARSE_ERROR = "parse_error"
    EMPTY = "empty"
    #: **配置/兼容性错误**：服务连上了，但它没有我们需要的那个工具。
    #:
    #: 与上面的"运行时故障"刻意分开，因为两者的处置完全不同：
    #:   · 运行时故障 → auto 模式可以回退到备用通道（网络抖一下而已）
    #:   · 配置错误   → **不许回退**。这是"接错了"，静默回退会把
    #:     "工具名对不上"这种确定性错误永远掩盖成"网络偶尔不好"。
    #:
    #: 现实中真发生过：Tavily MCP 的官方文档写 `tavily-search`，
    #: 而 `tools/list` 实际返回 `tavily_search`。
    CONFIG_ERROR = "config_error"


class SearchError(RuntimeError):
    """带明确错误码的检索异常。"""

    def __init__(
        self,
        message: str,
        *,
        code: SearchErrorCode,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


@dataclass(frozen=True)
class TavilyResult:
    """一条搜索结果。字段刻意保持最小，不掺业务语义。"""

    title: str
    url: str
    content: str
    score: float = 0.0


@dataclass
class SearchOutcome:
    """一次检索的结构化结果。`ok=False` 时 `code` 说明原因。"""

    ok: bool
    code: SearchErrorCode | None = None
    message: str = ""
    results: list[TavilyResult] = field(default_factory=list)
    elapsed_ms: int = 0
    #: **实际**执行这次检索的后端：mcp / tavily / mock。空串表示尚未确定。
    #:
    #: 它存在的理由：用户不关心走哪条路，但**系统必须知道**。
    #: 没有这个字段，"MCP 到底有没有在工作"就只能靠代码里有没有那个类来猜。
    provider: str = ""
    #: 服务端自报的标识（如 MCP 的 `tavily-mcp v4.0.4`）
    provider_detail: str = ""
    #: 是否发生过回退（首选后端失败、改用了备用）
    fell_back: bool = False
    #: 回退原因。**为空的 fell_back 是没有意义的** ——
    #: 只说"回退了"不说"为什么"，等于把运维线索丢掉。
    fallback_reason: str = ""

    @property
    def skipped(self) -> bool:
        """是否属于「本该跳过」而非「执行失败」。未配置 Key 属于此类。"""
        return self.code == SearchErrorCode.NOT_CONFIGURED

    @property
    def config_error(self) -> bool:
        """是否属于配置/兼容性错误（如服务端没有预期工具）。

        **这类错误不允许静默回退**，见 `SearchErrorCode.CONFIG_ERROR`。
        """
        return self.code == SearchErrorCode.CONFIG_ERROR

    def summary(self) -> str:
        if self.ok:
            return f"命中 {len(self.results)} 条"
        return f"{self.code}: {self.message}"


class TavilyClient:
    """Tavily Search API 的薄封装。"""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        timeout: float | None = None,
        max_results: int | None = None,
        search_depth: str | None = None,
        snippet_chars: int | None = None,
        config: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        cfg = config or settings
        # Key 一律从配置（← 环境变量）读取，绝不硬编码
        self._api_key = (api_key if api_key is not None else cfg.tavily_api_key).strip()
        self.timeout = timeout if timeout is not None else cfg.tavily_timeout_s
        self.max_results = max_results if max_results is not None else cfg.tavily_max_results
        self.search_depth = search_depth or cfg.tavily_search_depth
        self.snippet_chars = (
            snippet_chars if snippet_chars is not None else cfg.tavily_snippet_chars
        )
        # 测试可注入 MockTransport，从而完全不打真实网络
        self._transport = transport

    # ------------------------------------------------------------------ 状态
    @property
    def available(self) -> bool:
        """Key 是否已配置。调用前可用它做短路，避免无谓的异常开销。"""
        return bool(self._api_key)

    def capabilities(self) -> dict[str, Any]:
        """供能力探测接口使用。**只暴露布尔值，绝不回显 Key。**"""
        return {
            "provider": "tavily",
            "configured": self.available,
            "max_results": self.max_results,
            "search_depth": self.search_depth,
            "timeout_s": self.timeout,
        }

    # ------------------------------------------------------------------ 检索
    async def search(self, query: str, *, max_results: int | None = None) -> list[TavilyResult]:
        """执行检索。失败时抛 `SearchError`（带明确 code）。"""
        if not self.available:
            raise SearchError(
                "未配置 TAVILY_API_KEY，无法执行联网检索。",
                code=SearchErrorCode.NOT_CONFIGURED,
            )

        query = (query or "").strip()
        if not query:
            raise SearchError("检索关键词为空。", code=SearchErrorCode.PARSE_ERROR)

        payload = {
            "api_key": self._api_key,
            "query": query,
            "max_results": max_results or self.max_results,
            "search_depth": self.search_depth,
            "include_answer": False,
            "include_raw_content": False,
        }

        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, transport=self._transport
            ) as client:
                response = await client.post(TAVILY_ENDPOINT, json=payload)
        except httpx.TimeoutException as exc:
            raise SearchError(
                f"联网检索超时（>{self.timeout}s）。", code=SearchErrorCode.TIMEOUT
            ) from exc
        except httpx.HTTPError as exc:
            raise SearchError(
                f"联网检索请求失败：{type(exc).__name__}", code=SearchErrorCode.HTTP_ERROR
            ) from exc

        if response.status_code >= 400:
            raise SearchError(
                f"Tavily 返回 HTTP {response.status_code}：{response.text[:160]}",
                code=SearchErrorCode.HTTP_ERROR,
                status_code=response.status_code,
            )

        try:
            body = response.json()
            raw_items = body.get("results") or []
        except Exception as exc:  # noqa: BLE001 - 返回体不是 JSON
            raise SearchError(
                f"Tavily 返回体无法解析：{type(exc).__name__}", code=SearchErrorCode.PARSE_ERROR
            ) from exc

        results = [self._to_result(item) for item in raw_items if isinstance(item, dict)]
        results = [r for r in results if r.url or r.title]

        if not results:
            raise SearchError(
                "联网检索没有返回任何结果。", code=SearchErrorCode.EMPTY
            )
        return results

    async def search_safe(self, query: str, *, max_results: int | None = None) -> SearchOutcome:
        """不抛异常的版本。业务层用这个：把错误状态**记录**下来而不是中断流程。

        这正是校验层需要的语义 —— 检索失败应该记成一条 `error` 校验记录，
        而不是让整批校验崩掉。
        """
        started = time.perf_counter()
        try:
            results = await self.search(query, max_results=max_results)
        except SearchError as exc:
            return SearchOutcome(
                ok=False,
                code=exc.code,
                message=exc.message,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )
        except Exception as exc:  # noqa: BLE001 - 兜底，绝不让检索拖垮校验
            logger.warning("联网检索出现未预期异常：%s", exc)
            return SearchOutcome(
                ok=False,
                code=SearchErrorCode.HTTP_ERROR,
                message=f"未预期异常：{type(exc).__name__}",
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )

        return SearchOutcome(
            ok=True,
            results=results,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )

    # ------------------------------------------------------------------ 内部
    def _to_result(self, item: dict[str, Any]) -> TavilyResult:
        content = str(item.get("content") or "").strip()
        if len(content) > self.snippet_chars:
            content = content[: self.snippet_chars] + "…"
        try:
            score = float(item.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        return TavilyResult(
            title=str(item.get("title") or "").strip(),
            url=str(item.get("url") or "").strip(),
            content=content,
            score=score,
        )


#: 默认单例。业务层直接用它；测试注入自定义 client。
tavily_client = TavilyClient()
