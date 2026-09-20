"""MCP 客户端 + 后端路由测试。

这个文件守的核心是**"回退规则"** —— 它看起来只是一个 if/else，
但错一个分支的后果是"你以为 MCP 在工作，其实一直在偷偷用 Tavily"。

所以下面大部分用例是**否定式**的：断言某种情况下**不该**发生回退。
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.search.mcp_client import (
    McpCallResult,
    McpClient,
    McpError,
    McpTool,
    _extract_payload,
    pick_search_tool,
)
from app.search.provider import (
    MCPWebSearchProvider,
    MockWebSearchProvider,
    RoutingWebSearchProvider,
)
from app.search.tavily_client import SearchErrorCode, SearchOutcome, TavilyResult


# =========================================================================== #
# 一、SSE 剥壳与协议解析
# =========================================================================== #
def test_extract_payload_handles_sse_wrapper() -> None:
    """实测：MCP 远程端点返回的是 SSE 包装，不是裸 JSON。

    直接 `json.loads(body)` 会在这里炸掉。
    """
    body = 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n'
    assert _extract_payload(body)["result"] == {"ok": True}


def test_extract_payload_also_accepts_plain_json() -> None:
    """有些实现直接返回裸 JSON —— 两种都要认，不能只支持一种。"""
    assert _extract_payload('{"jsonrpc":"2.0","result":{"ok":true}}')["result"] == {"ok": True}


def test_extract_payload_rejects_garbage() -> None:
    with pytest.raises(McpError):
        _extract_payload("这不是 JSON")


# =========================================================================== #
# 二、动态发现工具名（**不许硬编码**）
# =========================================================================== #
def _tool(name: str) -> McpTool:
    return McpTool(name=name, input_schema={"properties": {"query": {"type": "string"}}})


def test_picks_search_tool_from_real_tavily_list() -> None:
    """用实测得到的真实清单：必须挑中 `tavily_search`。"""
    tools = [
        _tool("tavily_search"),
        _tool("tavily_extract"),
        _tool("tavily_crawl"),
        _tool("tavily_map"),
        _tool("tavily_research"),
    ]
    picked = pick_search_tool(tools)
    assert picked is not None
    assert picked.name == "tavily_search"


def test_research_tool_is_not_mistaken_for_search() -> None:
    """**`tavily_research` 的名字里含 "search" 子串，但它不是通用搜索。**

    它是"做一份研究报告"，比一次搜索贵得多。纯子串匹配会让它被误选，
    所以判据是**分词后的 token**，外加排除词。
    """
    tools = [_tool("tavily_search"), _tool("tavily_research")]
    assert pick_search_tool(tools).name == "tavily_search"  # type: ignore[union-attr]


def test_picks_search_when_only_research_exists_is_none() -> None:
    """只有 research/extract/crawl/map 时，**不该**硬挑一个来用。"""
    tools = [_tool("tavily_research"), _tool("tavily_extract"), _tool("tavily_map")]
    assert pick_search_tool(tools) is None


def test_picks_camel_case_search_tool() -> None:
    """换个 server 可能叫 `webSearchSearch` 这种驼峰名，拆分后要能认出来。"""
    assert pick_search_tool([_tool("webSearch")]).name == "webSearch"  # type: ignore[union-attr]
    assert pick_search_tool([_tool("brave-search")]).name == "brave-search"  # type: ignore[union-attr]


def test_pick_returns_none_on_empty_list() -> None:
    assert pick_search_tool([]) is None


# =========================================================================== #
# 三、MCP 客户端（用假 transport，不打真实网络）
# =========================================================================== #
def _sse(payload: dict) -> str:
    return f"event: message\ndata: {json.dumps(payload)}\n\n"


def _client(handler) -> McpClient:
    return McpClient(
        url="https://mcp.test.invalid/mcp/",
        api_key="fake-key",
        timeout=5,
        transport=httpx.MockTransport(handler),
    )


def _rpc_router(overrides: dict | None = None, status: int = 200):
    """按 method 分派响应的假 transport。"""
    overrides = overrides or {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body["method"]
        if method in overrides:
            override = overrides[method]
            if isinstance(override, int):  # 用整数表示 HTTP 状态码
                return httpx.Response(override, text="")
            return httpx.Response(status, text=_sse(override))

        if method == "initialize":
            return httpx.Response(
                status,
                text=_sse(
                    {
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {
                            "protocolVersion": "2025-06-18",
                            "serverInfo": {"name": "tavily-mcp", "version": "4.0.4"},
                        },
                    }
                ),
            )
        if method == "tools/list":
            return httpx.Response(
                status,
                text=_sse(
                    {
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {
                            "tools": [
                                {
                                    "name": "tavily_search",
                                    "description": "search",
                                    "inputSchema": {
                                        "type": "object",
                                        "required": ["query"],
                                        "properties": {"query": {"type": "string"}},
                                    },
                                }
                            ]
                        },
                    }
                ),
            )
        if method == "tools/call":
            return httpx.Response(
                status,
                text=_sse(
                    {
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(
                                        {"results": [{"title": "T", "url": "https://a", "content": "C"}]}
                                    ),
                                }
                            ]
                        },
                    }
                ),
            )
        return httpx.Response(404)

    return handler


@pytest.mark.asyncio
async def test_client_records_protocol_trace() -> None:
    """**这是"确实经过 MCP"最核心的证据。**

    断言的是协议层面的调用顺序与服务端自报的身份 ——
    不是"代码里有个 McpClient 类"。
    """
    client = _client(_rpc_router())
    info = await client.initialize()
    tools = await client.list_tools()
    picked = pick_search_tool(tools)
    await client.call_tool(picked.name, {"query": "x"})  # type: ignore[union-attr]

    trace = client.trace
    assert trace.methods == ["initialize", "tools/list", "tools/call"]
    assert trace.server_info is not None
    assert trace.server_info.name == "tavily-mcp"  # ← 服务端自报的，不是我们写的常量
    assert trace.server_info.version == "4.0.4"
    assert trace.discovered_tools == ["tavily_search"]  # ← 从 tools/list 读的
    assert trace.called_tool == "tavily_search"


@pytest.mark.asyncio
async def test_client_maps_401_to_unauthorized() -> None:
    client = _client(_rpc_router({"initialize": 401}))
    with pytest.raises(McpError) as exc:
        await client.initialize()
    assert exc.value.code == "unauthorized"


@pytest.mark.asyncio
async def test_client_handles_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    client = _client(handler)
    with pytest.raises(McpError) as exc:
        await client.initialize()
    assert exc.value.code == "timeout"


@pytest.mark.asyncio
async def test_call_tool_parses_sse_json_string() -> None:
    """MCP 返回的是 content[0].text 里的 **JSON 字符串**（REST 是已解析对象）。

    这个形状差异正是"结果确实走了 MCP"的一条证据。
    """
    client = _client(_rpc_router())
    await client.initialize()
    result = await client.call_tool("tavily_search", {"query": "x"})

    assert result.ok is True
    rows = result.as_search_results()
    assert rows and rows[0]["url"] == "https://a"


# =========================================================================== #
# 四、回退规则（**本文件的重点**）
# =========================================================================== #
def _outcome(ok: bool, code=None, results=None, message: str = "") -> SearchOutcome:
    return SearchOutcome(ok=ok, code=code, message=message, results=results or [])


class _StubProvider:
    """可控的假后端，用来精确构造"首选失败"的每一种情形。"""

    def __init__(self, name: str, outcome: SearchOutcome | Exception) -> None:
        self.name = name
        self._outcome = outcome
        self.calls = 0

    @property
    def available(self) -> bool:
        return True

    async def search(self, query: str, *, max_results: int | None = None) -> SearchOutcome:
        self.calls += 1
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return SearchOutcome(
            ok=self._outcome.ok,
            code=self._outcome.code,
            message=self._outcome.message,
            results=self._outcome.results,
            provider=self.name,
        )

    def capabilities(self) -> dict:
        return {"provider": self.name, "available": True}


def _routing(mcp_outcome, backend="auto") -> tuple[RoutingWebSearchProvider, _StubProvider, _StubProvider]:
    mcp = _StubProvider("mcp", mcp_outcome)
    tavily = _StubProvider(
        "tavily",
        _outcome(True, results=[TavilyResult(title="T", url="https://t", content="c")]),
    )
    provider = RoutingWebSearchProvider(backend=backend, mcp=mcp, fallback=tavily)
    return provider, mcp, tavily


@pytest.mark.asyncio
async def test_tavily_mode_never_touches_mcp() -> None:
    """**`tavily` 模式下一次 MCP 请求都不该发。**

    这是"验证 MCP 真的在工作"的前提 —— 如果这个模式也暗地里发 MCP 请求，
    就没法用"关掉 MCP 后功能是否还正常"来反证。
    """
    provider, mcp, tavily = _routing(_outcome(True), backend="tavily")
    outcome = await provider.search("q")

    assert outcome.ok is True
    assert mcp.calls == 0, "tavily 模式居然发起了 MCP 请求"
    assert tavily.calls == 1


@pytest.mark.asyncio
async def test_mcp_mode_fails_loudly_without_fallback() -> None:
    """**`mcp` 模式失败必须明确失败，不许偷偷换通道。**"""
    provider, mcp, tavily = _routing(
        _outcome(False, code=SearchErrorCode.TIMEOUT), backend="mcp"
    )
    outcome = await provider.search("q")

    assert outcome.ok is False
    assert outcome.code == SearchErrorCode.TIMEOUT
    assert tavily.calls == 0, "mcp 模式居然回退了"
    assert outcome.fell_back is False


@pytest.mark.asyncio
async def test_auto_falls_back_on_runtime_failure() -> None:
    """运行时故障（超时/连不上/401）→ 允许回退，且**必须上报回退原因**。"""
    provider, mcp, tavily = _routing(
        _outcome(False, code=SearchErrorCode.TIMEOUT, message="连不上"), backend="auto"
    )
    outcome = await provider.search("q")

    assert outcome.ok is True
    assert tavily.calls == 1
    assert outcome.fell_back is True
    assert outcome.fallback_reason, "回退了却不说原因 —— 那是把运维线索丢掉"


@pytest.mark.asyncio
async def test_auto_does_not_fall_back_on_config_error() -> None:
    """⚠️ **本文件最重要的一条。**

    `tools/list` 里没有搜索工具 = **配置/兼容性错误**，不是网络问题。

    如果这里也回退，"文档写错一个字符"（实测：官方写 `tavily-search`、
    实际是 `tavily_search`）就会永远被掩盖成"网络偶尔不好" ——
    你会一直以为 MCP 在工作。
    """
    provider, mcp, tavily = _routing(
        _outcome(
            False,
            code=SearchErrorCode.CONFIG_ERROR,
            message="能力清单里没有搜索工具",
        ),
        backend="auto",
    )
    outcome = await provider.search("q")

    assert outcome.ok is False, "配置错误居然被回退掩盖了"
    assert outcome.code == SearchErrorCode.CONFIG_ERROR
    assert tavily.calls == 0, "配置错误不该回退到 Tavily"
    assert outcome.fell_back is False


@pytest.mark.asyncio
async def test_auto_returns_mcp_result_when_it_works() -> None:
    provider, mcp, tavily = _routing(
        _outcome(True, results=[TavilyResult(title="M", url="https://m", content="c")]),
        backend="auto",
    )
    outcome = await provider.search("q")

    assert outcome.provider == "mcp"
    assert outcome.fell_back is False
    assert tavily.calls == 0


@pytest.mark.asyncio
async def test_auto_without_any_backend_reports_not_configured() -> None:
    """MCP 没配、Tavily 也没配 → 明确说没配，而不是返回空结果。"""
    provider = RoutingWebSearchProvider(
        backend="auto",
        mcp=MockWebSearchProvider(configured=False),
        fallback=MockWebSearchProvider(configured=False),
    )
    outcome = await provider.search("q")
    assert outcome.ok is False
    assert outcome.code == SearchErrorCode.NOT_CONFIGURED


# =========================================================================== #
# 五、MCP Provider 的行为（含"工具名对不上"的真实情形）
# =========================================================================== #
@pytest.mark.asyncio
async def test_mcp_provider_reports_config_error_when_tool_missing() -> None:
    """服务连上了，但清单里只有 research/extract —— 报配置错误，不是网络错误。"""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "initialize":
            payload = {
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "protocolVersion": "2025-06-18",
                    "serverInfo": {"name": "some-other-server", "version": "1.0"},
                },
            }
        elif body["method"] == "tools/list":
            payload = {
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"tools": [{"name": "do_research", "inputSchema": {}}]},
            }
        else:
            return httpx.Response(404)
        return httpx.Response(200, text=_sse(payload))

    provider = MCPWebSearchProvider(
        McpClient(
            url="https://mcp.test.invalid/mcp/",
            api_key="k",
            transport=httpx.MockTransport(handler),
        )
    )
    outcome = await provider.search("q")

    assert outcome.ok is False
    assert outcome.code == SearchErrorCode.CONFIG_ERROR
    assert "do_research" in outcome.message, "错误信息里要列出它实际提供的能力，便于排查"


@pytest.mark.asyncio
async def test_mcp_provider_only_passes_declared_arguments() -> None:
    """**只传工具自己声明的参数。**

    传一个它没声明的参数，严格实现的服务端会拒绝整次调用 ——
    而报错会是"invalid arguments"，很难联想到是这里的问题。
    """
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "initialize":
            payload = {
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"serverInfo": {"name": "m", "version": "1"}, "protocolVersion": "p"},
            }
        elif body["method"] == "tools/list":
            payload = {
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "tools": [
                        {
                            "name": "web_search",
                            # 只声明了 query —— 没有 max_results / search_depth
                            "inputSchema": {
                                "type": "object",
                                "required": ["query"],
                                "properties": {"query": {"type": "string"}},
                            },
                        }
                    ]
                },
            }
        elif body["method"] == "tools/call":
            captured.update(body["params"]["arguments"])
            payload = {
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"content": [{"type": "text", "text": '{"results":[]}'}]},
            }
        else:
            return httpx.Response(404)
        return httpx.Response(200, text=_sse(payload))

    provider = MCPWebSearchProvider(
        McpClient(
            url="https://mcp.test.invalid/mcp/",
            api_key="k",
            transport=httpx.MockTransport(handler),
        )
    )
    await provider.search("hello", max_results=3)

    assert captured == {"query": "hello"}, f"传了未声明的参数：{captured}"


def test_mcp_capabilities_expose_server_identity() -> None:
    """能力自检要能看到服务端自报的身份与动态发现的工具名。"""
    provider = MCPWebSearchProvider(
        McpClient(url="https://mcp.test.invalid/mcp/", api_key="k")
    )
    caps = provider.capabilities()
    assert caps["provider"] == "mcp"
    # 还没连过时是空的 —— 这本身也是诚实的（连过之后才有值）
    assert caps["discovered_tools"] == []
    assert caps["server_info"] is None
