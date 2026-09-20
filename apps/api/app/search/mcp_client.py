"""MCP（Model Context Protocol）客户端：JSON-RPC over streamable HTTP。

## 一句话：让"按名字调工具"这件事真的成立

MCP 的核心是 `tools/list` —— 服务端告诉你"我有哪些工具、参数是什么"。
客户端**读回来之后按读到的名字调**，而不是照文档写死。

## ⚠️ 为什么必须动态发现（这是实测踩出来的）

Tavily 官方 MCP 的 README 写的是 `tavily-search`（连字符），
而实际 `tools/list` 返回的是 **`tavily_search`**（下划线）。

**照文档硬编码会静默不工作**，而且报错是一句难懂的 "tool not found" ——
你会去查网络、查 Key、查配额，最后才发现是文档写错了一个字符。

所以这里的规矩是：**任何外部工具名都不出现在代码里**，
只出现在"从清单里挑一个像搜索工具"的启发式里。

## 响应是 SSE 包装的

实测：请求发 JSON-RPC，响应体是

    event: message
    data: {"jsonrpc":"2.0","id":1,"result":{...}}

所以要**先剥 SSE 外壳再解析 JSON**，不能直接 `json.loads(body)`。
但也不能假死后一种 —— 有些 MCP 实现直接返回裸 JSON。
所以 `_extract_payload` 两种都认。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.logging import get_logger

logger = get_logger(__name__)

#: 我们声明的协议版本。与服务端协商后以服务端返回的为准。
CLIENT_PROTOCOL_VERSION = "2025-06-18"

#: 排除词：这些是高价值但**语义不同**的工具，不能被当成通用搜索。
#: 注意 `tavily_research` —— 它的名字里含 "search" 子串，
#: 但它是"做一份研究报告"，比一次搜索贵得多，不该被通用搜索误用。
_NON_SEARCH_MARKERS = ("extract", "crawl", "map", "research", "fetch", "browse", "sitemap")


class McpError(RuntimeError):
    """MCP 调用失败。带一个粗粒度原因，供上层决定要不要回退。"""

    def __init__(self, message: str, *, code: str = "mcp_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass
class McpServerInfo:
    """`initialize` 握手拿到的服务端身份。

    **这是"确实经过 MCP"的证据之一** —— 它不是我们写的常量，
    是服务端在协议握手时自报的。
    """

    name: str = ""
    version: str = ""
    protocol_version: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "protocol_version": self.protocol_version,
        }


@dataclass
class McpTool:
    """`tools/list` 里的一个工具。"""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)

    @property
    def required(self) -> list[str]:
        return list(self.input_schema.get("required") or [])

    @property
    def properties(self) -> dict[str, Any]:
        return dict(self.input_schema.get("properties") or {})


@dataclass
class McpCallResult:
    """一次 `tools/call` 的结果。"""

    ok: bool
    #: 服务端返回的文本内容（多个 content 块拼起来）
    text: str = ""
    #: 服务端自己标记的错误
    is_error: bool = False
    error: str | None = None
    #: 原始 content 数组，留给需要结构化解析的调用方
    raw_content: list[dict[str, Any]] = field(default_factory=list)

    def as_search_results(self) -> list[dict[str, Any]]:
        """尝试把文本内容解析成 `{"results": [...]}` 结构。

        ⚠️ **MCP 与 REST 结果形状不同**：MCP 的 `tavily_search` 返回的是
        `content[0].text` 里的一段 **JSON 字符串**，而 Tavily REST 返回的是
        已解析好的对象。这个差异正好是"结果确实走了 MCP"的一条证据。
        """
        try:
            payload = json.loads(self.text)
        except (TypeError, ValueError):
            return []
        if isinstance(payload, dict):
            results = payload.get("results")
            if isinstance(results, list):
                return [r for r in results if isinstance(r, dict)]
        if isinstance(payload, list):
            return [r for r in payload if isinstance(r, dict)]
        return []


@dataclass
class McpCallTrace:
    """一次 MCP 会话的**协议级留痕**。

    存在的唯一目的：让"确实经过 MCP"这件事**可以被断言**，
    而不是靠"代码里有个 McpClient 类"来相信。
    """

    methods: list[str] = field(default_factory=list)
    server_info: McpServerInfo | None = None
    discovered_tools: list[str] = field(default_factory=list)
    called_tool: str | None = None
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "methods": list(self.methods),
            "server_info": self.server_info.as_dict() if self.server_info else None,
            "discovered_tools": list(self.discovered_tools),
            "called_tool": self.called_tool,
            "errors": list(self.errors),
        }


def pick_search_tool(tools: list[McpTool]) -> McpTool | None:
    """从服务端的能力清单里挑出"通用网络搜索"工具。

    **不硬编码任何具体名字。** 判据是名字的语义：

      加分：名字里含 `search`（分词后）
      加分：名字里含 `web`
      排除：含 extract / crawl / map / research / fetch / browse ——
            它们是"抽取/爬取/站点地图/做研究报告"，语义与"搜一下"不同，
            而且通常贵得多，被通用搜索误用会很浪费。

    这个函数是**唯一的**"名字形状知识"所在地。
    换一个 MCP server（Brave / Exa / 自建）时，只可能改这里，不改调用方。
    """
    best: McpTool | None = None
    best_score = -1

    for tool in tools:
        normalized = (tool.name or "").strip().lower().replace("-", "_")
        if not normalized:
            continue

        # 分词：下划线 + 驼峰
        tokens = {t for t in normalized.split("_") if t}
        for token in list(tokens):
            # camelCase → 拆开
            parts = _split_camel(token)
            tokens.update(parts)

        if any(marker in tokens for marker in _NON_SEARCH_MARKERS):
            continue

        score = 0
        if "search" in tokens:
            score += 10
        elif "search" in normalized:
            # 兜底：形状不认识但子串里有 search
            score += 5
        if "web" in tokens or "web" in normalized:
            score += 3
        if score > best_score:
            best, best_score = tool, score

    return best if best_score > 0 else None


def _split_camel(token: str) -> list[str]:
    """把 `webSearch` 拆成 `["web", "search"]`。"""
    parts: list[str] = []
    current = ""
    for char in token:
        if char.isupper() and current:
            parts.append(current.lower())
            current = char
        else:
            current += char
    if current:
        parts.append(current.lower())
    return [p for p in parts if p]


def _extract_payload(body: str) -> dict[str, Any]:
    """从响应体里取出 JSON-RPC 对象。

    实测的 MCP 远程端点返回的是 **SSE 包装**：

        event: message
        data: {"jsonrpc":"2.0",...}

    但有些实现直接返回裸 JSON，所以两种都认。
    """
    text = (body or "").strip()
    if not text:
        raise McpError("MCP 返回了空响应", code="empty_response")

    # 裸 JSON
    if text.startswith("{"):
        try:
            return json.loads(text)
        except ValueError:
            pass

    # SSE：逐行找 data:
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("data:"):
            chunk = line[5:].strip()
            if not chunk or chunk == "[DONE]":
                continue
            try:
                return json.loads(chunk)
            except ValueError:
                continue

    raise McpError(f"MCP 响应无法解析：{text[:160]}", code="bad_response")


class McpClient:
    """一个极薄的 MCP 客户端。只实现这个项目需要的三个方法。

    不引官方 SDK 的原因：我们只需要 `initialize` / `tools/list` / `tools/call`，
    而这三个都是普通 POST。引 SDK 会带来一处需要跟着升级的依赖，
    收益却只是替我们省掉 `_extract_payload` 里那十行。
    （如果后续要用 resources / prompts / 通知，再换 SDK 也不迟。）
    """

    def __init__(
        self,
        *,
        url: str,
        api_key: str = "",
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = (url or "").strip()
        self._api_key = (api_key or "").strip()
        self.timeout = timeout
        self._transport = transport
        self.trace = McpCallTrace()
        self._server_info: McpServerInfo | None = None
        self._tools: list[McpTool] | None = None
        self._request_id = 0

    # ------------------------------------------------------------------ 状态
    @property
    def configured(self) -> bool:
        return bool(self.url)

    def capabilities(self) -> dict[str, Any]:
        """供能力自检用。**绝不回显 Key。**"""
        return {
            "provider": "mcp",
            "configured": self.configured,
            "url": self.url,
            "server_info": self._server_info.as_dict() if self._server_info else None,
            "discovered_tools": list(self.trace.discovered_tools),
        }

    # ------------------------------------------------------------------ 传输
    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            # 这个 Accept 是 MCP streamable HTTP 要求的：
            # 服务端可能在响应里用 SSE 推流，不接受就会 406
            "Accept": "application/json, text/event-stream",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """发一次 JSON-RPC 并返回 result。失败抛 `McpError`。"""
        if not self.configured:
            raise McpError("未配置 MCP 服务地址", code="not_configured")

        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id(), "method": method}
        if params is not None:
            payload["params"] = params

        self.trace.methods.append(method)

        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as client:
                response = await client.post(self.url, json=payload, headers=self._headers())
        except httpx.TimeoutException as exc:
            self.trace.errors.append(f"{method}: timeout")
            raise McpError(f"MCP 请求超时（{self.timeout}s）", code="timeout") from exc
        except httpx.HTTPError as exc:
            self.trace.errors.append(f"{method}: {type(exc).__name__}")
            raise McpError(f"MCP 连接失败：{exc}", code="unreachable") from exc

        if response.status_code in (401, 403):
            self.trace.errors.append(f"{method}: {response.status_code}")
            raise McpError(
                f"MCP 拒绝这次请求（HTTP {response.status_code}），请检查 API Key。",
                code="unauthorized",
            )
        if response.status_code >= 400:
            self.trace.errors.append(f"{method}: HTTP {response.status_code}")
            raise McpError(
                f"MCP 返回 HTTP {response.status_code}：{response.text[:120]}",
                code="http_error",
            )

        envelope = _extract_payload(response.text)

        if "error" in envelope:
            error = envelope.get("error") or {}
            message = str(error.get("message") or error)[:200]
            self.trace.errors.append(f"{method}: {message}")
            raise McpError(f"MCP 返回错误：{message}", code="rpc_error")

        result = envelope.get("result")
        if not isinstance(result, dict):
            raise McpError("MCP 响应里没有 result", code="bad_response")
        return result

    # ------------------------------------------------------------------ 协议
    async def initialize(self) -> McpServerInfo:
        """握手。返回服务端自报的身份 —— 这是"确实经过 MCP"的第一条证据。"""
        result = await self._rpc(
            "initialize",
            {
                "protocolVersion": CLIENT_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "learning-buddy", "version": "0.2.0"},
            },
        )

        info = result.get("serverInfo") or {}
        self._server_info = McpServerInfo(
            name=str(info.get("name") or ""),
            version=str(info.get("version") or ""),
            protocol_version=str(result.get("protocolVersion") or ""),
        )
        self.trace.server_info = self._server_info
        logger.info(
            "MCP 已连接：%s v%s（协议 %s）",
            self._server_info.name,
            self._server_info.version,
            self._server_info.protocol_version,
        )
        return self._server_info

    async def ensure_initialized(self) -> McpServerInfo:
        if self._server_info is None:
            await self.initialize()
        assert self._server_info is not None
        return self._server_info

    async def list_tools(self, *, refresh: bool = False) -> list[McpTool]:
        """拿能力清单。结果会缓存 —— 一次会话里清单不会变。"""
        if self._tools is not None and not refresh:
            return self._tools

        await self.ensure_initialized()
        result = await self._rpc("tools/list", {})

        raw = result.get("tools") or []
        tools: list[McpTool] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            schema = item.get("inputSchema")
            tools.append(
                McpTool(
                    name=name,
                    description=str(item.get("description") or ""),
                    input_schema=schema if isinstance(schema, dict) else {},
                )
            )

        self._tools = tools
        self.trace.discovered_tools = [t.name for t in tools]
        logger.info("MCP 发现 %d 个工具：%s", len(tools), ", ".join(self.trace.discovered_tools))
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> McpCallResult:
        """按**从清单里读到的名字**调用工具。"""
        await self.ensure_initialized()
        try:
            result = await self._rpc("tools/call", {"name": name, "arguments": arguments})
        except McpError as exc:
            return McpCallResult(ok=False, error=exc.message)

        self.trace.called_tool = name

        content = result.get("content")
        blocks = content if isinstance(content, list) else []
        texts = [
            str(block.get("text") or "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        ]

        is_error = bool(result.get("isError"))
        return McpCallResult(
            ok=not is_error,
            text="\n".join(t for t in texts if t),
            is_error=is_error,
            error="服务端标记这次调用失败" if is_error else None,
            raw_content=[b for b in blocks if isinstance(b, dict)],
        )
