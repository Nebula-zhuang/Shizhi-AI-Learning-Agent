"""把现有能力包成 Tool，注册进统一的工具表。

## 这里**不实现任何新能力**

四个 Tool 全部是对既有服务的薄包装：

    retrieve_knowledge   → app/services/rag_service.retrieve_knowledge（P3 就有）
    web_search           → app/search/provider（阶段 2 加了 MCP 后端）
    document_analysis    → 同上，但限定到指定的资料
    image_analysis       → LLM 网关的多模态通道

**加一个 Tool 的成本应该是"写一个 handler + register 一次"。**
如果哪天发现要改 Agent 主流程才能加工具，说明这层抽象漏了。
"""

from __future__ import annotations

import base64
import mimetypes
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.llm import LLMError, llm_gateway, message_text, user_message, vision_gateway
from app.core.logging import get_logger
from app.ingestion import storage
from app.search.provider import get_search_provider
from app.services import rag_service

logger = get_logger(__name__)

#: 单张图片转 data URI 的大小上限（字节）。
#: 超过就不发了 —— base64 会让请求体膨胀 33%，太大会被服务商直接拒。
MAX_IMAGE_BYTES = 4 * 1024 * 1024


# --------------------------------------------------------------------------- #
# ① retrieve_knowledge
# --------------------------------------------------------------------------- #
async def retrieve_knowledge(
    query: str,
    *,
    document_ids: list[int] | None = None,
    **_ignored: Any,
) -> "ToolResultLike":
    """检索用户资料。"""
    from app.agent.tools.specs import ToolResult

    text = (query or "").strip()
    if not text:
        return ToolResult(ok=False, content="检索词是空的。", error="empty_query")

    try:
        sources = await rag_service.retrieve_knowledge(
            text, document_ids=document_ids or None
        )
    except Exception as exc:  # noqa: BLE001
        # 检索失败要**如实告诉模型**，它才能决定"换个说法再试"还是"直接回答"
        logger.warning("retrieve_knowledge 失败：%s", exc)
        return ToolResult(
            ok=False,
            content=f"检索你的资料时出错了：{exc}。可以换个说法再试，或者直接回答。",
            error=f"retrieve_failed: {exc}",
            retryable=True,
        )

    if not sources:
        return ToolResult(
            ok=True,
            content=(
                "在你的资料里没有检索到与这个问题相关的内容。"
                "如果问题需要资料支撑，可以如实说明资料里没有；"
                "如果它涉及时效信息，可以考虑联网查一下。"
            ),
            display={"kind": "knowledge_base", "count": 0},
        )

    blocks: list[str] = []
    citations: list[dict[str, Any]] = []
    for source in sources:
        where = f"《{source.file_name}》"
        if source.page_start:
            where += f" 第 {source.page_start} 页"
        if source.heading_path:
            where += f"（{' > '.join(source.heading_path)}）"
        blocks.append(f"[{source.index}] {where}\n{source.content.strip()}")
        citations.append(
            {
                "kind": "knowledge_base",
                "document_id": source.document_id,
                "file_name": source.file_name,
                "page": source.page_start,
                "heading_path": source.heading_path,
            }
        )

    return ToolResult(
        ok=True,
        content="## 用户资料里的相关片段\n\n" + "\n\n".join(blocks),
        display={
            "kind": "knowledge_base",
            "count": len(sources),
            "preview": sources[0].content[:160],
            "file_name": sources[0].file_name,
            "page": sources[0].page_start,
            "heading_path": sources[0].heading_path,
        },
        citations=citations,
    )


# --------------------------------------------------------------------------- #
# ② web_search
# --------------------------------------------------------------------------- #
async def web_search(
    query: str,
    *,
    max_results: int | None = None,
    **_ignored: Any,
) -> "ToolResultLike":
    """联网搜索。**结果里带上"实际用了哪个后端"。**"""
    from app.agent.tools.specs import ToolResult

    text = (query or "").strip()
    if not text:
        return ToolResult(ok=False, content="搜索词是空的。", error="empty_query")

    provider = get_search_provider()
    outcome = await provider.search(text, max_results=max_results or settings.tavily_max_results)

    display: dict[str, Any] = {
        "kind": "web_search",
        "count": len(outcome.results),
        # 这三个字段是"到底走了哪条通道"的唯一真相来源
        "provider": outcome.provider,
        "provider_detail": outcome.provider_detail,
        "fell_back": outcome.fell_back,
        "fallback_reason": outcome.fallback_reason,
    }

    if not outcome.ok:
        if outcome.config_error:
            # ⚠️ 配置错误（服务端没有搜索工具）—— **不能提示"没联网"搪塞过去**，
            # 那是把确定性错误说成临时故障。
            return ToolResult(
                ok=False,
                content=(
                    f"联网搜索服务配置有问题：{outcome.message}。"
                    "请如实告诉用户这条暂时查不了，不要臆造搜索结果。"
                ),
                display=display,
                error="search_config_error",
            )
        if outcome.skipped:
            return ToolResult(
                ok=False,
                content=(
                    "当前没有可用的联网搜索能力。"
                    "请不要声称查过外部资料；涉及时效的问题就直说暂时没法联网核实。"
                ),
                display=display,
                error="search_not_configured",
            )
        return ToolResult(
            ok=False,
            content=(
                f"联网搜索没成功（{outcome.code}）：{outcome.summary()}。"
                "可以换个说法再试一次，或者基于已有信息回答并说明没查到。"
            ),
            display=display,
            error=f"search_{outcome.code}",
            retryable=True,
        )

    blocks: list[str] = []
    citations: list[dict[str, Any]] = []
    for index, item in enumerate(outcome.results, start=1):
        blocks.append(f"[{index}] {item.title}\n    来源：{item.url}\n    {item.content.strip()}")
        citations.append(
            {
                "kind": "web",
                "title": item.title,
                "url": item.url,
                "provider": outcome.provider,
                "provider_detail": outcome.provider_detail,
                "fell_back": outcome.fell_back,
                "fallback_reason": outcome.fallback_reason,
            }
        )

    header = "## 联网搜索的结果"
    if outcome.fell_back:
        header += f"\n（首选通道没连上，已改用备用通道：{outcome.fallback_reason}）"
    if outcome.provider == "mock":
        header += "（⚠️ 以下为离线模拟数据，不是真实网页，不得作为资料来源引用）"

    return ToolResult(
        ok=True,
        content=f"{header}\n\n" + "\n\n".join(blocks),
        display=display,
        citations=citations,
    )


# --------------------------------------------------------------------------- #
# ③ document_analysis
# --------------------------------------------------------------------------- #
async def document_analysis(
    query: str,
    *,
    document_ids: list[int] | None = None,
    **_ignored: Any,
) -> "ToolResultLike":
    """在**指定资料**里找内容（"讲讲第三章"这类）。

    与 `retrieve_knowledge` 的区别只在检索范围：
    这个必须带 `document_ids`，不指定就用不了 ——
    否则它会变成 `retrieve_knowledge` 的重复品，而重复的工具会互相干扰模型的判断。
    """
    from app.agent.tools.specs import ToolResult

    if not document_ids:
        return ToolResult(
            ok=False,
            content=(
                "这个工具需要指定要看哪份资料，但当前没有指定。"
                "如果用户问的是「我的资料」里某一份，可以先用 retrieve_knowledge；"
                "如果他没有指定资料，就直接回答。"
            ),
            error="document_ids_required",
            retryable=False,
        )

    # 复用同一个检索实现，只是把范围收窄 —— 检索排序与门槛逻辑不需要第二套
    return await retrieve_knowledge(query, document_ids=document_ids)


# --------------------------------------------------------------------------- #
# ④ image_analysis
# --------------------------------------------------------------------------- #
def _image_to_data_uri(path: str) -> str | None:
    """把本地图片读成 data URI。

    为什么不用文件路径让服务商自己取：图片存在本机磁盘上，
    **模型服务商访问不到**。data URI 是唯一不依赖公网可达性的做法。
    """
    target = Path(path)
    if not target.is_file():
        return None
    size = target.stat().st_size
    if size > MAX_IMAGE_BYTES:
        logger.warning("图片 %s 太大（%.1fMB），跳过", path, size / 1024 / 1024)
        return None
    mime = mimetypes.guess_type(target.name)[0] or "image/png"
    encoded = base64.b64encode(target.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


async def image_analysis(
    question: str = "",
    *,
    image_path: str = "",
    image_paths: list[str] | None = None,
    default_question: str = "",
    **_ignored: Any,
) -> "ToolResultLike":
    """看图。走 LLM 网关的多模态通道。

    ## `question` 是**可选**的

    实测踩过：把 `question` 声明成必填，结果模型调这个工具时不传参数，
    被校验拦下 → 重试 → 又拦下 → 最后回一句"我看不到图"。

    **那不是模型的错，是 schema 错了。** 工具已经绑定了本轮的图，
    "看一眼"这个动作本身不需要参数，模型省略它是合理的。

    缺省时用绑定的 `default_question`（也就是用户这一轮真正问的话），
    再退一步用一句通用的提取要求。
    """
    from app.agent.tools.specs import ToolResult

    # ⚠️ **能力闸门**：模型不支持视觉时直接拒绝，而不是发过去等一句"我看不到图"。
    #
    # 实测：图片按 OpenAI 多模态格式发给 DeepSeek，接口**不报错**，
    # 但模型回"我无法查看或分析图片"。那种情况下工具会返回 ok=True，
    # Agent 拿到一个没用的结果还会白转好几轮 —— 比直接失败糟糕得多。
    if not settings.llm_supports_vision:
        return ToolResult(
            ok=False,
            content=(
                "当前配置的模型**不支持看图**，所以这张图片的内容我读不到。"
                "请如实告诉用户：这条看不了，可以让他把图里的文字或代码贴出来。"
                "**不要猜测图里有什么。**"
            ),
            error="vision_not_supported",
        )

    candidates: list[str] = []
    if image_path:
        candidates.append(image_path)
    candidates.extend(image_paths or [])

    if not candidates:
        return ToolResult(
            ok=False,
            content="这次没有可分析的图片。",
            error="no_image",
        )

    data_uris: list[str] = []
    for item in candidates[:3]:  # 一次最多三张 —— 再多请求体会太大
        # 允许直接传已经构造好的 data URI
        if item.startswith("data:image/"):
            data_uris.append(item)
            continue
        # **只接受绝对路径或 data URI。**
        #
        # 相对路径有歧义（相对于谁？），所以不拿它去猜。
        # 曾经这里对任何非绝对路径都调 `storage.resolve` —— 那条路是给
        # "上传目录内的相对路径"用的，传别的进去会抛一句"路径越界"，
        # 对调用方毫无帮助，还会让人以为是自己越权了。
        path = Path(item)
        if not path.is_absolute():
            logger.warning("image_analysis 收到相对路径，已跳过：%s", item)
            continue
        uri = _image_to_data_uri(str(path))
        if uri:
            data_uris.append(uri)

    if not data_uris:
        return ToolResult(
            ok=False,
            content="图片读取失败（可能太大或路径不对）。请如实告诉用户这张图看不了。",
            error="image_unreadable",
        )

    # 缺省问题：优先用绑定的"用户这一轮真正问的话"，再退到通用要求。
    # 这样模型只写 `{}` 也能得到一次贴题的看图。
    ask = (question or "").strip() or (default_question or "").strip() or "这张图里有什么"

    prompt = (
        "请仔细看这张图片，把对回答下面这个问题有用的信息提取出来。\n\n"
        f"问题：{ask}\n\n"
        "要求：\n"
        "- 如果图里有代码，把**关键代码原样**列出来（保留标识符与结构），不要改写\n"
        "- 如果图里有图表或流程，说清它的结构和含义\n"
        "- 只描述你**真的看到**的内容，看不清的地方明确说看不清，不要猜测\n"
    )

    try:
        # **走视觉网关** —— 看图要用能看图的模型。
        # 主模型保持纯文本（更便宜），视觉得到用时才发生。
        result = await vision_gateway().chat(
            [user_message(prompt, data_uris)],
            max_tokens=1200,
        )
    except LLMError as exc:
        return ToolResult(
            ok=False,
            content=f"看图片时模型调用失败：{exc.message}。可以告诉用户稍后再试。",
            error="vision_failed",
            retryable=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("image_analysis 失败：%s", exc)
        return ToolResult(
            ok=False,
            content=f"分析图片时出错了：{exc}",
            error="vision_error",
            retryable=True,
        )

    text = (result.content or "").strip()
    if not text:
        return ToolResult(
            ok=False,
            content="模型没有从这张图里读出内容。请如实说明这张图没能识别出来。",
            error="vision_empty",
        )

    return ToolResult(
        ok=True,
        content=f"## 这是一张图片的分析结果\n\n{text}",
        display={"kind": "image", "image_count": len(data_uris)},
    )


# --------------------------------------------------------------------------- #
# 注册
# --------------------------------------------------------------------------- #
#: 类型别名，避免在函数签名里反复写 import（ToolResult 在 specs 里，
#: 而这个模块会被 specs 间接导入，用字符串注解打断循环）
ToolResultLike = Any


# --------------------------------------------------------------------------- #
# ⑤ current_time
# --------------------------------------------------------------------------- #
#: 星期几的中文说法。**自己查表而不是用 locale** ——
#: 容器与不同机器上的 locale 不保证装了中文，查表是确定性的。
_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


async def current_time(**_ignored: Any) -> "ToolResultLike":
    """返回当前的日期与时间。

    **这是本项目里唯一一个"答案完全由本机决定"的工具** ——
    它不查资料、不联网、不问模型。所以它的结果永远不会错，
    也永远不需要被"核验"。
    """
    from app.agent.tools.specs import ToolResult

    now = datetime.now().astimezone()
    weekday = _WEEKDAYS[now.weekday()]

    return ToolResult(
        ok=True,
        content=(
            "## 当前的日期与时间（来自运行本服务的机器时钟）\n\n"
            f"- 日期：{now.strftime('%Y年%m月%d日')}（{weekday}）\n"
            f"- 时间：{now.strftime('%H:%M:%S')}\n"
            f"- 时区：{now.strftime('%Z') or '本地时区'}（UTC{now.strftime('%z')}）\n"
            f"- ISO：{now.isoformat(timespec='seconds')}\n\n"
            "**这是权威值，直接用它回答，不要凭记忆推断日期。**"
        ),
        display={
            "kind": "time",
            "date": now.strftime("%Y-%m-%d"),
            "weekday": weekday,
            "time": now.strftime("%H:%M"),
        },
    )


def register_all(
    registry, *, images: Sequence[str] = (), user_question: str = ""
) -> None:
    """把工具注册进给定的注册表。

    `images` 是本轮对话附带的图片（路径或 data URI）。**它们会被绑定进
    `image_analysis`** —— 模型不需要、也不可能知道图片路径，
    它只需要说"关于这张图我想知道什么"。

    幂等：重复调用不会重复注册（`register` 会抛"名字重复"，
    所以这里先检查一下 —— 测试里会反复构造注册表）。
    """
    from functools import partial

    from app.agent.tools.specs import ToolSpec

    def web_available() -> tuple[bool, str]:
        provider = get_search_provider()
        if provider.available:
            return True, ""
        return False, "当前没有可用的联网搜索能力"

    specs = [
        ToolSpec(
            name="retrieve_knowledge",
            description=(
                "在**用户自己上传的资料**里检索相关内容。"
                "当问题指向他的资料时用它（例如「我那份讲义里怎么说的」「根据我的资料讲讲」）。"
                "**不要**因为「知识库里恰好有这个主题」就调用它 —— "
                "普通概念问题应当直接回答。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索词，用问句或关键词都行"},
                },
                "required": ["query"],
            },
            handler=retrieve_knowledge,
            counted=True,
        ),
        ToolSpec(
            name="web_search",
            description=(
                "联网搜索**会随时间变化**的信息：新版本、新发布、近期动态、某个具体版本的现状。"
                "经典概念（「什么是进程」）**不要**用它 —— 答案十年不变，联网只会多等几秒。"
                "如果返回结果里说没有可用能力，就如实告诉用户没联网，不要编造。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索词"},
                    "max_results": {"type": "integer", "description": "要几条结果，默认 5"},
                },
                "required": ["query"],
            },
            handler=web_search,
            counted=True,
            available=web_available,
        ),
        ToolSpec(
            name="document_analysis",
            description=(
                "在**指定的某一份资料**里找内容，用于「讲讲第三页」「这份文件里怎么说」这类请求。"
                "必须能确定是哪一份资料时才用它；不确定就用 retrieve_knowledge。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "要找什么"},
                    "document_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "资料 id 列表",
                    },
                },
                "required": ["query"],
            },
            handler=document_analysis,
            counted=True,
        ),
        ToolSpec(
            name="image_analysis",
            description=(
                "读取用户这一轮传来（或之前传过）的图片，把图里的信息提取出来。"
                "**只有当这一轮确实有图片时才用它。**"
                "如果图里有代码，它会原样列出关键代码，不做改写。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": (
                            "关于这张图想知道什么。"
                            "**可以不填** —— 不填时会按用户这一轮问的问题来看图。"
                        ),
                    },
                },
                # ⚠️ **刻意没有 required。** 工具已经绑定了本轮的图，
                # "看一眼"不需要参数；声明成必填会让模型调不通（实测踩过）。
                "required": [],
            },
            # 把本轮的图片**绑定**进 handler：模型不需要（也不可能）知道图片路径。
            handler=partial(
                image_analysis, image_paths=list(images), default_question=user_question
            ),
            counted=True,
            timeout=40.0,
            #: 不支持视觉时**不进给模型的清单** ——
            #: 让它看见一个注定失败的工具，只会浪费一次决策。
            available=lambda: (
                (True, "")
                if settings.llm_supports_vision
                else (False, "当前模型不支持看图")
            ),
        ),
    ]

    specs.append(
        ToolSpec(
            name="current_time",
            description=(
                "查询**当前的日期与时间**。"
                "凡是问「今天几号」「今天星期几」「现在几点」「今年是哪一年」"
                "「距离某个日期还有多久」这类问题，**必须用它** —— "
                "你对当前日期没有可靠记忆，凭印象回答会答错，"
                "而且会**答得很自信**（实测踩过：把 2026 年答成 2024 年）。\n"
                "⚠️ 这类问题**不要用联网搜索** —— 本机时钟才是权威且免费。"
            ),
            parameters={"type": "object", "properties": {}},
            handler=current_time,
            #: **不占用户的工具调用额度** —— 本地读，零外部成本、幂等、瞬时。
            #: 让它跟联网搜索抢那 3 次配额没有道理。
            counted=False,
        )
    )

    for spec in specs:
        if spec.name not in registry:
            registry.register(spec)
