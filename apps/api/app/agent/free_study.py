"""自由学习 Agent。

## 两条路径，按"要不要花一次决策调用"分流

    普通问题（没提资料、没时效信号）
        → quick_route 一眼定性
        → **直接生成**（1 次 LLM 调用）

    其余问题
        → 进入 Agent Loop（Observe → Decide → Tool → Observe → Decide …）
        → 每一步一次决策调用 + 一次最终生成

为什么保留快通道：你明确要求"简单问题一次 LLM 调用即可"。
而"先问模型该不该检索、再生成"天然是两次 —— 对"什么是 JVM"这种问题，
第一次完全浪费，还让用户多等一两秒。

**这个判断写在代码里而不是提示词里**：要不要花一次调用是**成本决策**，
不该由模型来定。

## 三条不可让的规矩（从阶段 1 继承，一行没松）

1. **没联网就不许说查过。** 不联网时给模型的素材段是**一段明确的禁令**，
   不是空白 —— 留空会让模型自己脑补"大概没资料，我自由发挥吧"，
   而它发挥出来的恰恰是"我查了一下…"。
2. **不编造引用。** 引用只来自工具真实返回的 `citations`。
3. **超限降级不报错。** 拿已有信息回答并说明，比抛一个超时错误有用。
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agent.runtime_loop import (
    AgentLoop,
    LoopDecision,
    LoopLimits,
    LoopObservation,
    format_budget,
    format_observations,
)
from app.agent.tool_specs import register_all
from app.agent.tools.registry import ToolRunner
from app.agent.tools.specs import ToolRegistry, ToolResult
from app.core.config import settings
from app.core.identity import DEFAULT_LEARNER_ID
from app.core.llm import Message, llm_gateway
from app.core.logging import get_logger

logger = get_logger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

#: 时效性信号词。命中就倾向联网。
_RECENT_HINTS = re.compile(
    r"(最新|最近|现在|目前|当前|今年|明年|去年|刚刚|新闻|发布|更新|版本|"
    r"20\d{2}\s*年|v?\d{1,2}\.\d+\.\d+|还支持吗|现在还|过时)"
)

#: **显式要求联网**。用户说了"帮我查一下"，那就是要查 —— 不该再让模型猜。
_SEARCH_INTENT = re.compile(
    r"(查一下|查查|搜一下|搜搜|帮我查|帮我搜|搜一搜|上网查|网上有没有|有没有人|"
    r"现在.{0,4}怎么样|最新.{0,4}(情况|进展|动态))"
)

#: 产品名 + 版本号，形如 "Java 25"、"Python 3.13"。
#: 单独一个版本号不构成时效问题，但**在"问一个具体版本的情况"这个语境下它几乎总是**。
_VERSION_REF = re.compile(r"[A-Za-z][A-Za-z0-9.+#-]*\s+\d{1,3}(?:\.\d+)*\b")

#: **学习意图信号**（5B）。命中就进 Loop，让决策模型去判断"这是想问一个概念，
#: 还是想学一个主题"。
#:
#: ⚠️ 为什么必须有这条：`quick_route` 的兜底是"通用问题直接回答、不进 Loop" ——
#: 于是"我想学 Java 线程"会被**快通道直接答掉，决策模型根本没机会看到它**，
#: 5B 的学习意图永远产不出来。这条规则是把这类问题**送进 Loop** 的唯一入口。
#:
#: ⚠️ 命中**只表示"值得让模型看一眼"**，不代表真的会给提议：
#: 是否成意图由决策模型判断（见 `loop_decide.md`）。所以这里宁可宽一点 ——
#: 误命中只是多花一次决策调用，漏命中则功能完全失效。
_LEARN_INTENT_HINTS = re.compile(
    r"(想学|要学|学一下|学着|教我|带我学|帮我学|系统学|系统性地学|从头学|"
    r"入门|怎么学|如何学|从哪.*开始学|从什么.*开始学|学习路线|学习路径|"
    r"讲一遍|过一遍|帮我梳理|带我过)"
)

#: **指向"我保存过的知识"的信号**（P1-2）。命中就进 Loop，让决策模型去判断
#: 该不该调 `search_saved_knowledge`。
#:
#: ⚠️ 为什么必须有这条：`search_saved_knowledge` 是**无条件**注册进工具表的
#: （没有 available 门槛），所以"模型能不能用它"从来不取决于工具本身 ——
#: 取决于**这一轮有没有进 Loop**。而 `quick_route` 的兜底是"通用问题直接回答"，
#: 于是「我保存过什么关于 Java 多态的内容」会被快通道**直接答掉**，
#: 模型连工具都看不到，只能凭上下文硬答 —— 实测它会答「你没有上传过任何……」（答错）✗
#:
#: ⚠️ 命中**只表示"值得让模型看一眼"**，**不是**"直接去调工具"：
#: 真正的取舍（该调工具、还是该直接回答）由决策模型做 —— 和 `_LEARN_INTENT_HINTS` 同一范式。
#: 所以这里宁可宽一点：误命中只多花一次决策调用，漏命中则功能完全失效。
#:
#: ⚠️ 与 `_MATERIAL_HINTS` 是**两件事**，刻意不合并：那条指向"我上传的资料"（PDF/讲义），
#: 这条指向"我主动保存的问答"，分属两个工具、两个向量集合（见
#: `search_saved_knowledge` 的 docstring）。而且它**不要求 kb_size > 0** ——
#: 用户可能一份资料都没传，却保存过好几条问答。
_SAVED_KNOWLEDGE_HINTS = re.compile(
    r"(我.{0,4}(保存|存|记|收藏|整理|学)[过的下来了]|"
    r"我的?知识库|知识库里)"
)

#: **时钟类信号**。命中就必须拿到真实时间，**不能靠模型的记忆**。
#:
#: 实测踩过的确定性错误：
#:
#:     用户问"今天几号？"
#:     → 没被识别成实时问题 → 走快通道
#:     → 模型靠记忆回答："今天是2024年6月13日"
#:     → 实际是 2026年9月20日，**差了两年多**
#:
#: 这类错误的可怕之处是**答得非常自信** —— 用户没有理由怀疑，
#: 除非他自己知道今天几号（那他就不会问了）。
#:
#: ⚠️ 日期问题**不该联网查**：本机时钟才是权威且免费。
_TIME_HINTS = re.compile(
    r"(今天|明天|昨天|后天|前天|"
    r"几号|几日|几月|星期几|礼拜几|周几|哪一天|什么日子|"
    r"几点|当前时间|当前日期|此刻|现在的时间|现在的时间|"
    r"今年|明年|去年|这个月|下个月|上个?月)"
)

#: 指向用户自己资料的信号词
_MATERIAL_HINTS = re.compile(
    r"(我的资料|我上传|我那份|这份|那个文件|这个文件|文档|课件|讲义|幻灯片|PPT|PDF|"
    r"第\s*[0-9一二三四五六七八九十]+\s*章|第\s*[0-9一二三四五六七八九十]+\s*节|"
    r"根据.{0,6}(资料|文件|课件|讲义)|我之前学)"
)


@dataclass
class StudyPlan:
    """快通道的结论。"""

    capabilities: list[str]
    reason: str
    question_type: str = "other"
    decided_by: str = "heuristic"


@dataclass
class TurnEvent:
    """推给前端的一个事件。"""

    event: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class TurnOutcome:
    """一轮结束后要落库的东西。"""

    content: str = ""
    sources: list[dict[str, Any]] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    status_trace: list[str] = field(default_factory=list)
    degraded_reason: str | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    provider: str = ""
    fell_back: bool = False
    fallback_reason: str = ""


# --------------------------------------------------------------------------- #
# 提示词
# --------------------------------------------------------------------------- #
def _load_prompt(name: str) -> tuple[str, str]:
    text = (PROMPTS_DIR / name).read_text(encoding="utf-8")
    system = ""
    user = text
    if "--- SYSTEM ---" in text:
        head, _, rest = text.partition("--- SYSTEM ---")
        system, _, user = rest.partition("--- USER ---")
        if not system.strip():
            system = head
    return system.strip(), user.strip()


def _fill(template: str, values: dict[str, str]) -> str:
    out = template
    for key, value in values.items():
        out = out.replace(f"{{{{{key}}}}}", value)
    return out


# --------------------------------------------------------------------------- #
# 快通道
# --------------------------------------------------------------------------- #
def quick_route(
    question: str,
    *,
    has_attachments: bool,
    kb_size: int,
    web_available: bool,
) -> StudyPlan | None:
    """一眼能定性的问题直接定性，**省掉一次决策调用**。

    返回 None 表示"可能有工具需求，交给 Agent Loop"。
    """
    text = (question or "").strip()
    if not text:
        return StudyPlan(["general"], "空问题", "other")

    mentions_material = bool(_MATERIAL_HINTS.search(text))
    is_time_sensitive = bool(
        _RECENT_HINTS.search(text) or _SEARCH_INTENT.search(text) or _VERSION_REF.search(text)
    )

    # 带了图片 → 必须进 Loop（要看图才知道能答什么）
    if has_attachments:
        return None

    # 提到自己的资料，且资料库非空 → 进 Loop 去检索
    if mentions_material and kb_size > 0:
        return None

    # 时效问题 → 进 Loop 去联网
    if is_time_sensitive:
        return None

    # **时钟问题 → 进 Loop**（而且下面会预先把真实时间塞进观察里）
    if _TIME_HINTS.search(text):
        return None

    # **像"想学某个主题"的说法 → 进 Loop**。
    # 快通道是一句规则，判断不了"问概念"和"想开一门课"的区别；
    # 把这一步交给决策模型（见 `_LEARN_INTENT_HINTS` 的说明）。
    if _LEARN_INTENT_HINTS.search(text):
        return None

    # **指向"我自己保存过什么"的说法 → 进 Loop**（P1-2）。
    # 同样不在这里下结论：进 Loop 之后由决策模型决定要不要调
    # `search_saved_knowledge`（见 `_SAVED_KNOWLEDGE_HINTS` 的说明）。
    if _SAVED_KNOWLEDGE_HINTS.search(text):
        return None

    # 既没提资料、也没时效信号、也没指向保存内容 → **直接回答，不进 Loop**
    return StudyPlan(["general"], "通用问题，无需检索", "concept")


# --------------------------------------------------------------------------- #
# 素材与禁令
# --------------------------------------------------------------------------- #
#: 不能联网时的禁令。**刻意写成整段而不是留空** ——
#: 留空会让模型自己脑补"大概是没有资料，我自由发挥吧"，
#: 而它发挥出来的恰恰是"我查了一下…"。
_NO_WEB_BAN = """## ⚠️ 本轮没有联网核验

你**没有**可用的外部检索结果。因此：

- **不要出现「我上网查了」「网上资料显示」「据报道」「根据最新资料」这类表述。**
- 涉及时效的问题，直说"这个我暂时没法联网核实"，然后给出你已有知识的版本，
  并说明这可能不是最新的。

诚实地承认局限，好过给一个听起来确定但可能过时的答案。"""


def _render_material(observation: LoopObservation) -> str:
    """拼出回答用的素材段。**空的时候给禁令，不给空白。**"""
    parts: list[str] = []

    used = {r.display.get("kind") for r in observation.results if r.ok}
    body = format_observations(observation)
    if body:
        parts.append(body)

    if "web_search" not in used:
        # 要么试过没成，要么根本没打算查 —— 两种情况都不许声称查过
        parts.append(_NO_WEB_BAN)

    if not parts:
        parts.append("（本轮没有可用的资料或搜索结果，请基于你自己的知识回答，并如实说明。）")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# Loop 的两个注入点
# --------------------------------------------------------------------------- #
def _make_decider(
    *,
    images: Sequence[str],
    document_ids: Sequence[int],
    learn_sink: list[dict[str, str]] | None = None,
):
    """生产环境的决策函数：一次 `chat_json`，走项目的既有 JSON 通道。

    刻意**不用原生 function calling** —— 与 P1/P2/P4 同一套通道，
    改动面为零。这一点在 `registry.py` 里已有论证。

    `learn_sink` 是**可选的收集箱**：决策模型若判断出"这轮其实是想学某个主题"，
    会把结构化意图放进这里，由 `stream_turn` 挂到 `done` 事件上。
    传 None 表示不关心（测试里常用）。
    """
    system, template = _load_prompt("loop_decide.md")

    def mock_builder(messages: list[Message]) -> dict[str, Any]:
        # 离线回归：直接收尾，让链路能跑通
        return {"thought": "离线模式直接回答", "final": True}

    async def decider(
        observation: LoopObservation, tools: list[dict[str, Any]]
    ) -> LoopDecision:
        tool_lines: list[str] = []
        for tool in tools:
            params = tool.get("parameters", {}).get("properties", {})
            required = tool.get("parameters", {}).get("required", [])
            sig = ", ".join(
                f"{name}{'*' if name in required else ''}: {spec.get('type', '?')}"
                for name, spec in params.items()
            )
            tool_lines.append(f"- `{tool['name']}({sig})` —— {tool['description']}")
        tools_block = "\n".join(tool_lines) if tool_lines else "（当前没有可用工具）"

        attachment_lines: list[str] = []
        if images:
            # **必须点名工具名，但不再规定"第几步"。**
            #
            # 点名是**实测逼出来的**：只写"附了 1 张图片"时，模型会自己发明一个
            # 工具名（`extract_text_from_image`）而不用清单里的 `image_analysis`，
            # 然后调用失败、回一句"我看不到图"。所以"用哪个名字"必须写清楚。
            #
            # 但**"必须第一步"是多余的** —— 它带来过两个问题：
            #   ① 用户只是顺带问了句别的，也得先花十几二十秒看图；
            #   ② `image_analysis` 失败后，模型为了满足这条"必须"而无条件重试，
            #      把预算烧在同一个坑里（详见 `loop_decide.md` 的规则②）。
            # 现在改成"**按问题需要优先观察图片**"：该看就看，不依赖就先做别的。
            if settings.llm_supports_vision:
                attachment_lines.append(
                    f"**用户这一轮附了 {len(images)} 张图片 —— 要看就用清单里的 "
                    f"`image_analysis`（不要自己编工具名）。**\n"
                    "如果回答依赖图片内容，就优先看图；如果不依赖，可以先做别的事。"
                )
                # 已经失败过 → 必须给出退路，否则"看图"就成了一条死命令。
                if "image_analysis" in observation.failed_tools:
                    attachment_lines.append(
                        "⚠️ 上一轮 `image_analysis` 已经失败过。"
                        "**不要为了满足'看图'再原样调一次** —— 重新判断："
                        "换个更具体的问法是否真能拿到新信息、要不要改用别的能力、"
                        "还是如实告诉用户这次看不了、请他把图里的内容贴出来。"
                    )
            else:
                # 看不了图时**不能让它去调一个不可用的工具** ——
                # 那只会白转一轮，最后还是得说看不了。
                attachment_lines.append(
                    f"用户这一轮附了 {len(images)} 张图片，但**当前模型不支持看图**，"
                    "图片内容读不到。请如实告诉用户这条看不了，"
                    "请他把图里的文字或代码贴出来。**不要猜测图里有什么。**"
                )
        if document_ids:
            attachment_lines.append(
                f"用户指定了 {len(document_ids)} 份资料（document_ids={list(document_ids)}）。"
            )
        attachments = "\n".join(attachment_lines) or "（这一轮没有附件）"

        observations = format_observations(observation) or "（还没调用过工具）"
        if observation.failed_tools:
            observations += (
                f"\n\n已经失败过、先不要原样重试的工具：{'、'.join(observation.failed_tools)}"
            )

        # 收益递减提示：告诉模型"这个工具你已经用过几次了"。
        # **代码给事实，模型做判断** —— 这是两者的分工。
        # 实测动机：模型把检索词换了三次、把额度全花在检索上，一次都没轮到联网。
        overused = observation.overused_tools(threshold=2)
        if overused:
            detail = "、".join(f"{name}（{count} 次）" for name, count in overused.items())
            observations += (
                f"\n\n⚠️ 注意：{detail} 已经调用过多次，而且结果没有带来新信息。"
                "**不要再用同一个工具换个说法重试。**"
                "如果还需要补充信息，换**另一个**能力（比如资料里没有就去联网）；"
                "否则直接收尾回答。"
            )

        prompt = _fill(
            template,
            {
                "tools": tools_block,
                "question": observation.question,
                "attachments": attachments,
                # 预算来自观察对象，而观察对象由循环在**每次 DECIDE 前**刷新 ——
                # 所以这里读到的永远是最新值，不是第一次决策时的快照。
                "budget": format_budget(observation.budget),
                "observations": observations,
            },
        )

        try:
            raw = await llm_gateway.chat_json(
                [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                # ⚠️ 决策的输出上限**必须**放得下 `_parse_decision` 允许的 thought 长度。
                #
                # 这里曾经写死 400，而 `_parse_decision` 用 `thought[:300]` ——
                # 中文约 1~1.5 token/字，**300 字本身就要 200~400 tokens**，
                # 再加 JSON 外壳（thought/tool/arguments），400 根本不够。
                # 两个数字本来就互相矛盾，只是当时模型写不到那么长才没暴露。
                #
                # 2B1 把预算告诉模型后，thought 从 ~150 字涨到 300+ 字：
                # 输出在 thought 中途被硬截断 → 没有闭合的 `}` →
                # `extract_json` 的兜底（找**最后一个** `}`）也救不回来 →
                # 重试**参数一模一样**、再次同样截断 → 最终降级为直接回答，
                # **丢掉本该发生的多步工具编排**。
                #
                # 改用项目既有的 `settings.llm_max_tokens`（默认 1024）：
                # 不新增配置项，且与其它 LLM 调用的口径一致。
                max_tokens=settings.llm_max_tokens,
                mock_builder=mock_builder,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Agent 决策调用失败：%s", exc)
            raise

        # 学习意图（5B）：决策 JSON 里**可选**的一个字段。
        #
        # 为什么在这里取而不是让 LoopDecision 带上：
        # `LoopDecision` 是循环的通用结构，为了一个只属于自由学习的字段去改它，
        # 会把"提议学什么"这个产品概念渗进 runtime loop。
        # 而 `raw` 就在手边，取完就丢 —— 循环那边零改动。
        #
        # **只认第一个**：意图是从问题本身读出来的，第一步决策看到的是最原始的问题；
        # 后面几步看到的是工具结果，容易被"资料里提到了 X"带偏。
        suggestion = _parse_learn_suggestion(raw)
        if suggestion is not None and learn_sink is not None and not learn_sink:
            learn_sink.append(suggestion)

        return _parse_decision(raw)

    return decider


#: 允许的学习意图种类。
#: `topic` —— 想从零学一个主题（"我想学 Java 线程"）
#: `material` —— 想借自己的材料学（"帮我照资料把第三章讲一遍"）
LEARN_SUGGESTION_KINDS = frozenset({"topic", "material"})

#: 主题名的长度上限。与知识点标题同一口径（`KnowledgePoint.title` 是 255，
#: 但那是存库的宽限；**给用户看的主题名**应当短 —— 长了说明模型把一整句话
#: 当成了主题，那不是一个可以"去学"的东西）。
LEARN_TOPIC_MAX_CHARS = 30


def _parse_learn_suggestion(raw: Any) -> dict[str, str] | None:
    """从决策 JSON 里取**可选**的学习意图。**取不到就返回 None，绝不猜。**

    形状：

        {"learn_suggestion": {"topic": "Java 线程", "kind": "topic"}}

    ⚠️ 这里的默认是**"没有意图"**：字段缺失、类型不对、topic 为空，
    一律 None。**不能因为"看起来像是在问东西"就凑一个提议出来** ——
    凭空冒出的"要开始练习吗？"比不提议更烦人。

    `kind` 缺失或不在白名单里时归一成 `topic`：模型偶尔漏字段，
    而"漏了 kind"比"整条提议作废"更常见，也更无害。
    """
    if not isinstance(raw, dict):
        return None

    block = raw.get("learn_suggestion")
    if not isinstance(block, dict):
        return None

    topic = str(block.get("topic") or "").strip()
    if not topic:
        return None
    if len(topic) > LEARN_TOPIC_MAX_CHARS:
        topic = topic[:LEARN_TOPIC_MAX_CHARS]

    kind = str(block.get("kind") or "").strip()
    if kind not in LEARN_SUGGESTION_KINDS:
        kind = "topic"

    return {"topic": topic, "kind": kind}


def _parse_decision(raw: Any) -> LoopDecision:
    """把模型输出收束成一个合法决策。**永远返回可用的结果。**"""
    if not isinstance(raw, dict):
        return LoopDecision(thought="决策结果格式不对，直接回答", final=True)

    thought = str(raw.get("thought") or "").strip()[:300]

    if raw.get("final") is True:
        return LoopDecision(thought=thought, final=True)

    tool = raw.get("tool")
    if not isinstance(tool, str) or not tool.strip():
        # 既没说调工具也没说收尾 → 当成收尾（信息大概是够了）
        return LoopDecision(thought=thought, final=True)

    arguments = raw.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}

    return LoopDecision(thought=thought, tool=tool.strip(), arguments=arguments)


def _make_generator(*, history: Sequence[dict[str, str]], learner_memory: str = ""):
    """生产环境的最终生成：流式，且带上素材与禁令。

    `learner_memory`（P5）是这位学习者的**长期讲法偏好**，由**路由层**从
    `memory_service.load_memory` 读好、渲染成一句话传进来。
    本模块只负责把它填进提示词 —— **不碰记忆库，也不持有 DB session**。
    """
    system, template = _load_prompt("free_study_answer.md")

    async def generate(observation: LoopObservation) -> AsyncIterator[str]:
        material = _render_material(observation)

        messages: list[Message] = [Message(role="system", content=system)]
        for item in list(history)[-6:]:
            role = item.get("role")
            content = item.get("content") or ""
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content})

        tail = _fill(
            template,
            {
                "material": material,
                "question": observation.question,
                # 读不到记忆时给一句中性说明 —— **绝不能留空**：
                # 留空会让「## 这位学习者的讲法偏好」变成一个空标题，
                # 模型可能把它当成别的东西（比如以为要求它自己猜）。
                "learner_memory": learner_memory or "（暂无特别偏好，用常规讲法。）",
            },
        )
        if observation.stop_note:
            tail += f"\n\n（补充说明：{observation.stop_note}）"
        messages.append({"role": "user", "content": tail})

        async for piece in llm_gateway.stream_chat(messages, max_tokens=1400):
            if piece:
                yield piece

    return generate


# --------------------------------------------------------------------------- #
# 一轮
# --------------------------------------------------------------------------- #
def build_registry(
    *,
    images: Sequence[str] = (),
    user_question: str = "",
    learner_id: str = DEFAULT_LEARNER_ID,
) -> ToolRegistry:
    """构造一个装好工具的表。

    每次新建而不是复用全局单例：工具的可用性依赖运行期配置
    （比如测试里要模拟"没联网"），共享一份会让测试之间互相污染。

    `images` 是本轮附带的图片，会绑定进 `image_analysis`（见 `register_all`）。
    `learner_id` 会绑定进 `search_saved_knowledge` —— 跨对话检索必须知道是谁。
    """
    registry = ToolRegistry()
    register_all(
        registry, images=images, user_question=user_question, learner_id=learner_id
    )
    return registry


def _limits_from_settings() -> LoopLimits:
    return LoopLimits(
        max_steps=int(getattr(settings, "loop_max_steps", 6)),
        max_tool_calls=int(getattr(settings, "loop_max_tool_calls", 3)),
        total_seconds=float(getattr(settings, "loop_timeout_s", 30.0)),
    )


async def stream_turn(
    *,
    question: str,
    history: Sequence[dict[str, str]] = (),
    document_ids: Sequence[int] | None = None,
    images: Sequence[str] = (),
    kb_size: int = 0,
    has_attachments: bool = False,
    learner_id: str = DEFAULT_LEARNER_ID,
    learner_memory: str = "",
    provider: Any = None,
    allow_web: bool = True,
    registry: ToolRegistry | None = None,
    limits: LoopLimits | None = None,
) -> AsyncIterator[TurnEvent]:
    """跑完一轮，逐个产出事件。

    调用方（路由层）负责把事件转成 SSE、并把最终结果落库。
    """
    outcome = TurnOutcome()

    def status(text: str) -> TurnEvent:
        outcome.status_trace.append(text)
        return TurnEvent("status", {"text": text})

    yield status("正在理解你的问题…")

    observation = LoopObservation(
        question=question,
        history=list(history),
        images=list(images),
        document_ids=[int(x) for x in (document_ids or [])],
    )

    # **本轮的图片要绑定进工具** —— 这样模型调 image_analysis 时
    # 只需要说"我想知道什么"，不需要知道图片在哪。
    tool_registry = registry or build_registry(
        images=images, user_question=question, learner_id=learner_id
    )

    # ── 时钟问题：**在进循环之前就把真实时间放进观察里**
    #
    # 为什么不"让模型自己去调时钟工具"：那仍然**指望它自觉**。
    # 实测它不自觉地凭记忆答过（2026 答成 2024）。
    # 预先注入之后，模型看到的素材里就有正确日期，想答错都难。
    #
    # 这是"不依赖模型自觉"的思路 —— 与"硬限制写在代码里"是同一条原则。
    time_spec = tool_registry.get("current_time")
    if time_spec is not None and _TIME_HINTS.search(question):
        try:
            time_result = await time_spec.handler()
            if time_result.ok:
                observation.record(time_spec.name, time_result)
                logger.info("已预注入当前时间：%s", time_result.display.get("date"))
        except Exception as exc:  # noqa: BLE001
            # 时钟读失败不该中断对话 —— 退化成"让模型自己想办法"
            logger.warning("预注入当前时间失败：%s", exc)

    # ── 快通道：一眼能定性的问题直接答，不进 Loop
    from app.search.provider import get_search_provider

    search = provider or get_search_provider()
    quick = quick_route(
        question,
        has_attachments=has_attachments,
        kb_size=kb_size,
        web_available=bool(allow_web and search.available),
    )

    if quick is not None:
        logger.info("自由学习快通道：%s（%s）", quick.capabilities, quick.reason)
        yield status("正在整理答案…")
        async for piece in _make_generator(
            history=history, learner_memory=learner_memory
        )(observation):
            yield TurnEvent("delta", {"text": piece})
        outcome.content = ""
        yield TurnEvent(
            "done",
            {
                "capabilities": ["general"],
                "sources": [],
                "citations": [],
                "status_trace": outcome.status_trace,
                "degraded_reason": None,
                "steps": [],
            },
        )
        return

    # ── 进 Loop
    #
    # 学习意图的收集箱（5B）。决策模型若判断出"这轮是想学某个主题"，
    # 会把它放进来；收尾时**有才挂到 done 上**。
    #
    # ⚠️ 快通道（上面的 `if quick is not None`）**不参与** ——
    # 它只处理"一眼能答"的通用问题，那种问题本来就不该冒出"要开始练习吗"。
    learn_sink: list[dict[str, str]] = []
    runner = ToolRunner(
        max_calls=(limits or _limits_from_settings()).max_tool_calls,
        max_seconds=(limits or _limits_from_settings()).total_seconds,
    )
    loop = AgentLoop(
        registry=tool_registry,
        runner=runner,
        decider=_make_decider(
            images=images, document_ids=document_ids or (), learn_sink=learn_sink
        ),
        generate=_make_generator(history=history, learner_memory=learner_memory),
        limits=limits or _limits_from_settings(),
    )

    async for event in loop.run(observation):
        if event.event == "delta":
            yield TurnEvent("delta", event.data)
            continue

        if event.event == "tool_start":
            yield status(_tool_status_text(event.data.get("tool", "")))
            yield TurnEvent(event.event, event.data)
            continue

        if event.event == "tool_result":
            # 把"实际用了哪个后端"透出来 —— 这是 UI 显示"经由 MCP"的来源
            display = event.data.get("display") or {}
            if display.get("provider"):
                outcome.provider = str(display.get("provider"))
                outcome.fell_back = bool(display.get("fell_back"))
                outcome.fallback_reason = str(display.get("fallback_reason") or "")
            yield TurnEvent(event.event, event.data)
            continue

        if event.event in {"citation", "source"}:
            yield TurnEvent(event.event, event.data)
            continue

        if event.event == "done":
            outcome.steps = event.data.get("steps") or []
            outcome.degraded_reason = (
                event.data.get("stop_note") or None if event.data.get("degraded") else None
            )
            # 汇总来源与引用（供落库与右侧面板）
            for result in observation.results:
                if result.ok and result.display:
                    outcome.sources.append(result.display)
                outcome.citations.extend(result.citations)
            payload: dict[str, Any] = {
                "capabilities": sorted(
                    {str(r.display.get("kind", "")) for r in observation.results if r.ok}
                )
                or ["general"],
                "sources": outcome.sources,
                "citations": outcome.citations,
                "status_trace": outcome.status_trace,
                "degraded_reason": outcome.degraded_reason,
                "steps": outcome.steps,
                "provider": outcome.provider,
                "fell_back": outcome.fell_back,
                "fallback_reason": outcome.fallback_reason,
            }
            # 学习意图：**有才加这个键**。
            #
            # 刻意不做成 `"learn_suggestion": None` 常驻 —— 那样前端拿到的
            # 每一条消息都带一个恒为 null 的字段，判断"要不要弹提议"就得
            # 靠 `!= null`，而"字段在不在"这个更强的信号反而丢了。
            # 普通提问的 payload 与 5B 之前**逐字节一致**。
            if learn_sink:
                payload["learn_suggestion"] = learn_sink[0]
            yield TurnEvent("done", payload)
            continue

        yield TurnEvent(event.event, event.data)


def _tool_status_text(tool: str) -> str:
    """工具 → 给用户看的自然语言状态。

    **不暴露内部术语**：用户看到的是"正在翻你的资料"，不是 "retrieve_knowledge"。
    """
    return {
        "retrieve_knowledge": "正在翻你的资料…",
        "document_analysis": "正在那份资料里找…",
        "web_search": "正在查最新的说法…",
        "image_analysis": "正在看这张图…",
    }.get(tool, "正在处理…")


# --------------------------------------------------------------------------- #
# 能力自检
# --------------------------------------------------------------------------- #
def describe_capabilities(provider: Any = None) -> dict[str, Any]:
    """能力自检。**包含 MCP 的真实状态**，不只是"配了个 Provider"。"""
    from app.search.provider import describe_search_capability

    registry = build_registry()
    return {
        "free_study": True,
        "knowledge_base": True,
        "web_search": describe_search_capability(),
        "tools": registry.available_names(),
        "max_capabilities_per_turn": _limits_from_settings().max_tool_calls,
    }

