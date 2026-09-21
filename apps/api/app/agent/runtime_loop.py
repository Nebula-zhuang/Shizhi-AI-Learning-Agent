"""通用 Agent 循环：观察 → 决策 → 执行 → 再观察 → 再决策 → 最终回答。

## 与 P4 `runtime.py` 的关系：**并存，不改它**

P4 的 `RuntimeState` 是九态**线性**流水线（LOAD_STATE → … → NEXT_TURN），
其中 `DECIDE_ACTION` **一轮只执行一次**。

那是**刻意的设计**，不是没写完：教学动作一轮只能选一个
（"每轮只能选一个主教学动作"是 P4 的硬约束），所以一次决策是对的。

而自由学习要的是另一种东西：**看完工具结果再决定要不要继续**。
把这两种模型塞进同一个状态机会让"教学策略"和"工具编排"互相牵制 ——
下次改教学阈值时得先想清楚会不会影响工具循环，反之亦然。

所以这个文件是**新增的第二个循环**，`runtime.py` 一行不动。
两者共用的只有底层的 `ToolRunner` / `ToolBudget`。

## 三条硬限制，缺一不可

    MAX_STEPS        循环总轮数。防"模型反复不调工具也不回答"的空转。
    MAX_TOOL_CALLS   工具调用总次数。防"一步里并发调一堆工具"的成本失控。
    TOTAL_TIMEOUT    整轮墙钟上限。防"每次调用都很快但次数多"的累计等待。

只有任意一条都不够 —— 上面每条注释就是"少了它会怎样"。

## 超限不报错，降级到 FINAL_ANSWER

这是从 P4 继承的原则：**无论如何都要给用户一个回复**。
拿已有信息生成回答，并在回答里说明"还有一步没做完"，
比抛一个超时错误有用得多。
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.agent.tools.registry import ToolRunner
from app.agent.tools.specs import ToolRegistry, ToolResult
from app.core.logging import get_logger

logger = get_logger(__name__)

#: 剩余时间低于这个数，就**不值得再发起一次外部工具调用**了。
#:
#: 一次真实工具调用的量级是秒级（联网 2~8s、看图 15~20s），
#: 只剩一两秒时发起的调用几乎必然超时 —— 它失败、**仍占用一次调用配额**，
#: 而那段等待还会算进整轮耗时。与其这样，不如直接走降级回答。
#:
#: 注意与 `ToolBudget.max_seconds` 的分工：那一条管"已经花了多久"，
#: 这一条管"还剩下的够不够做一件事"。两条都必要 ——
#: 只有前者时，循环会在预算将尽的边界上发起注定失败的调用。
MIN_USEFUL_TOOL_SECONDS = 2.0


class LoopState(StrEnum):
    """循环的全部状态。"""

    OBSERVE = "OBSERVE"
    DECIDE = "DECIDE"
    EXECUTE = "EXECUTE"
    OBSERVE_RESULT = "OBSERVE_RESULT"
    FINAL_ANSWER = "FINAL_ANSWER"
    DONE = "DONE"


@dataclass(frozen=True)
class LoopLimits:
    """三条硬限制。默认值与本项目的既有约定对齐。"""

    #: 循环总轮数（含最终的 FINAL_ANSWER 那一轮）
    max_steps: int = 6
    #: 工具调用总次数。与 P4 的 `DEFAULT_MAX_TOOL_CALLS` 同值 —— 同一套节制思路。
    max_tool_calls: int = 3
    #: 整轮墙钟上限（秒）。与 P4 的 30s 一致。
    total_seconds: float = 30.0
    #: 同一个工具最多失败几次。
    #:
    #: ⚠️ 这个限制**必须写在这里而不是提示词里** ——
    #: 实测把"同一个工具最多试两次"写进提示词后，模型仍连续 4 次调同一个失败的工具。
    #: 涉及成本与时间的限制，代码是唯一的执行者。
    max_retry_per_tool: int = 2


DEFAULT_LIMITS = LoopLimits()


@dataclass
class LoopDecision:
    """模型的一步决策。"""

    #: 为什么这么决定。不显示给用户，但**必须留痕** ——
    #: 排障时"它为什么又搜了一次"只有这里能回答。
    thought: str = ""
    #: 要调用的工具名。为 None 表示"信息够了，可以回答"。
    tool: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    #: 是否直接给出最终回答
    final: bool = False

    @property
    def wants_tool(self) -> bool:
        return bool(self.tool) and not self.final


@dataclass
class LoopStep:
    """一步的留痕。Developer Mode 下推给前端。"""

    index: int
    state: str
    decision: LoopDecision | None = None
    tool_name: str | None = None
    tool_ok: bool | None = None
    tool_error: str | None = None
    elapsed_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "state": self.state,
            "thought": (self.decision.thought[:300] if self.decision else ""),
            "tool": self.tool_name,
            "tool_ok": self.tool_ok,
            "tool_error": self.tool_error,
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass
class LoopObservation:
    """当前掌握的全部信息。**每轮决策看到的都是它的最新快照。**"""

    question: str
    #: 历史对话（不含本轮）
    history: list[dict[str, str]] = field(default_factory=list)
    #: 本轮已经拿到工具结果，按发生顺序
    results: list[ToolResult] = field(default_factory=list)
    #: 本轮附带的图片（多模态）
    images: list[str] = field(default_factory=list)
    #: 本轮附带的资料 id
    document_ids: list[int] = field(default_factory=list)
    #: 已经试过但失败的工具，避免模型反复撞同一堵墙
    failed_tools: list[str] = field(default_factory=list)
    #: 每个工具失败了几次。**按次数计而不是按"有没有失败过"** ——
    #: 允许一次有意义的重试（换个查询），但不容忍无脑重复。
    _failure_counts: dict[str, int] = field(default_factory=dict)
    #: 每个工具被调用过几次（含成功）
    _call_counts: dict[str, int] = field(default_factory=dict)
    #: 收尾说明（超限降级时写进去，让回答知道该坦白什么）
    stop_note: str = ""

    def record(self, name: str, result: ToolResult) -> None:
        self.results.append(result)
        self._call_counts[name] = self._call_counts.get(name, 0) + 1
        if not result.ok:
            self._failure_counts[name] = self._failure_counts.get(name, 0) + 1
            if name not in self.failed_tools:
                self.failed_tools.append(name)

    def failed_count(self, name: str) -> int:
        return self._failure_counts.get(name, 0)

    def call_count(self, name: str) -> int:
        """这个工具被调用过几次（成功失败都算）。"""
        return self._call_counts.get(name, 0)

    def overused_tools(self, threshold: int = 2) -> dict[str, int]:
        """被反复调用、且**都成功**的工具 —— 收益可能已经递减了。

        实测动机：用户说"资料里没有的再联网查"，模型却把检索词换了三次、
        三次额度全花在检索上，一次都没轮到联网。
        这里把事实报给决策层，让它自己判断该不该换能力 ——
        **代码给事实，模型做判断**，这是两者该有的分工。
        """
        return {
            name: count
            for name, count in self._call_counts.items()
            if count >= threshold and self._failure_counts.get(name, 0) == 0
        }

    @property
    def ok_results(self) -> list[ToolResult]:
        return [r for r in self.results if r.ok]


@dataclass
class LoopEvent:
    """循环推给上层的事件。"""

    event: str
    data: dict[str, Any] = field(default_factory=dict)


#: 决策函数：看观察 → 决定下一步。生产环境走模型，测试注入假的。
Decider = Callable[[LoopObservation, list[dict[str, Any]]], Awaitable[LoopDecision]]

#: 生成最终回答。注入是为了让测试能用一个确定的假生成器。
AnswerGenerator = Callable[[LoopObservation], AsyncIterator[str]]


class AgentLoop:
    """受硬限制约束的 Agent 循环。

    ## 依赖注入的取舍

    `decider` 与 `generate` 都是**注入**的，不在这里直接调模型。

    理由是可测性，而且这个理由很实际：要验证"MAX_STEPS 超限会降级"，
    必须能让决策函数按剧本连续给出 N 个工具调用。
    如果循环内部直接调 LLM，这个测试就只能靠真实模型碰运气 ——
    而我不能指望模型每次都配合我调满 6 步。
    """

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        runner: ToolRunner,
        decider: Decider,
        generate: AnswerGenerator,
        limits: LoopLimits = DEFAULT_LIMITS,
    ) -> None:
        self.registry = registry
        self.runner = runner
        self.decider = decider
        self.generate = generate
        self.limits = limits

    # ------------------------------------------------------------------ 主循环
    async def run(self, observation: LoopObservation) -> AsyncIterator[LoopEvent]:
        """跑完一轮。**永不抛异常** —— 最坏也会给出一个回答。"""
        started = time.perf_counter()
        steps: list[LoopStep] = []
        step_index = 0

        def elapsed_ms() -> int:
            return int((time.perf_counter() - started) * 1000)

        def useful_floor() -> float:
            """低于这个剩余时间就不再发起工具调用。

            ⚠️ 取 `min(常量, 总预算的一半)`，而不是直接用常量 ——
            否则 `total_seconds` 本身比常量小的场景（测试里用 1.0s 模拟超时就是）
            会在**第一步**就判定"时间不够"，于是"累计超时才停"这条逻辑
            永远不会被走到，守着它的测试也就变成了空转通过。
            """
            return min(MIN_USEFUL_TOOL_SECONDS, self.limits.total_seconds / 2)

        def out_of_time() -> bool:
            """整轮时间是否已经不够继续。

            两个条件，缺一不可：

            1. **已经超了总时限** —— 原来的判断。
            2. **剩余的不足以再做一件事** —— 新增。只有第 1 条时，
               循环会在"还剩 0.3 秒"的边界上照常发起调用，那次调用必然超时。
            """
            if (time.perf_counter() - started) >= self.limits.total_seconds:
                return True
            return self.runner.budget.remaining_seconds < useful_floor()

        # ─────────────────────────────────────────── 循环
        while True:
            step_index += 1

            # 三条硬限制的检查点。**放在循环开头**，
            # 让"这一轮还能不能继续"在任何决策之前就有答案。
            if step_index > self.limits.max_steps:
                observation.stop_note = (
                    f"已经用了 {self.limits.max_steps} 步还没收尾，先给一个基于现有信息的回答。"
                )
                steps.append(LoopStep(step_index, LoopState.DONE))
                break

            if out_of_time():
                observation.stop_note = (
                    "这次查得比较久，先停下来用已经拿到的信息回答；"
                    "如果还差什么，你可以再问一次。"
                )
                steps.append(LoopStep(step_index, LoopState.DONE))
                break

            # ── OBSERVE ＋ DECIDE
            specs = self.registry.describe_for_llm()
            try:
                decision = await self.decider(observation, specs)
            except Exception as exc:  # noqa: BLE001
                # 决策失败不能中断对话 —— 退化成"直接回答"，
                # 那是"最坏情况下也有用"的选择。
                logger.warning("Agent 决策失败，退化为直接回答：%s", exc)
                decision = LoopDecision(thought=f"决策失败：{exc}", final=True)

            step = LoopStep(step_index, LoopState.DECIDE, decision=decision, elapsed_ms=elapsed_ms())

            # ── 不调工具 → 收尾
            if not decision.wants_tool:
                steps.append(step)
                break

            # ── 工具不在表里（或当前不可用）→ 把错误告诉模型，让它自己纠正
            spec = self.registry.get(decision.tool or "")
            if spec is None:
                available = ", ".join(self.registry.available_names()) or "（当前没有可用工具）"
                step.tool_name = decision.tool
                step.tool_ok = False
                step.tool_error = "unknown_tool"
                steps.append(step)
                unknown = ToolResult(
                    ok=False,
                    content=(
                        f"没有名为「{decision.tool}」的工具。可用的工具有：{available}。"
                        "请改用其中一个，或者直接回答。"
                    ),
                    error="unknown_tool",
                )
                observation.record(decision.tool or "", unknown)
                yield LoopEvent(
                    "tool_result",
                    {"tool": str(decision.tool or ""), "ok": False, "summary": "这个工具不存在"},
                )
                continue

            ok, reason = spec.availability()
            if not ok:
                step.tool_name = spec.name
                step.tool_ok = False
                step.tool_error = "unavailable"
                steps.append(step)
                unavailable = ToolResult(
                    ok=False,
                    content=f"工具「{spec.name}」现在用不了：{reason}。请换个办法或直接回答。",
                    error="unavailable",
                )
                observation.record(spec.name, unavailable)
                yield LoopEvent(
                    "tool_result",
                    {"tool": spec.name, "ok": False, "summary": f"用不了：{reason}"},
                )
                continue

            # ── 同一工具失败太多次 → **代码级拒绝**，不再指望提示词
            #
            # 实测踩过：提示词里写了"同一个工具最多试两次"，
            # 但模型无视了它，连续 4 次调用同一个失败的工具。
            # **涉及成本与时间的限制必须写在代码里**，提示词只能起引导作用。
            if observation.failed_count(spec.name) >= self.limits.max_retry_per_tool:
                step.tool_name = spec.name
                step.tool_ok = False
                step.tool_error = "retry_limit"
                steps.append(step)
                limited = ToolResult(
                    ok=False,
                    content=(
                        f"「{spec.name}」已经失败 {observation.failed_count(spec.name)} 次了，"
                        "不要再调用它。请改用别的工具，或者基于已有信息直接回答。"
                    ),
                    error="retry_limit",
                )
                observation.record(spec.name, limited)
                yield LoopEvent(
                    "tool_result",
                    {"tool": spec.name, "ok": False, "summary": "已经失败太多次，不再重试"},
                )
                continue

            # ── 参数校验：**在调用之前**，而不是让 TypeError 冒到调用方
            #
            # 实测踩过：模型返回 `{"document_ids": [...]}` 却没有 `query`，
            # 直接调用会抛 `TypeError: missing 1 required positional argument: 'query'`。
            # 那句报错对模型毫无帮助（它不知道该补什么），对人也不好排查。
            missing = _missing_required(spec, decision.arguments)
            if missing:
                step.tool_name = spec.name
                step.tool_ok = False
                step.tool_error = "missing_arguments"
                steps.append(step)
                incomplete = ToolResult(
                    ok=False,
                    content=(
                        f"调用「{spec.name}」少了必填参数：{'、'.join(missing)}。"
                        f"它的参数是：{_describe_parameters(spec)}。请补齐后重新调用。"
                    ),
                    error="missing_arguments",
                )
                observation.record(spec.name, incomplete)
                yield LoopEvent(
                    "tool_result",
                    {
                        "tool": spec.name,
                        "ok": False,
                        "summary": f"缺参数：{'、'.join(missing)}",
                    },
                )
                continue

            # ── EXECUTE
            steps.append(step)
            yield LoopEvent(
                "tool_start",
                {"tool": spec.name, "arguments_summary": _summarize_arguments(decision.arguments)},
            )

            tool_started = time.perf_counter()
            # 注意：**用 spec.handler 而不是从外面传函数** ——
            # 这正是"按名字查表执行"与"调用方手工传 func"的区别。
            #
            # `timeout=spec.timeout` 是**工具自己声明的上限**，
            # 由 `ToolRunner._invoke` 与 Runner 默认值、本轮剩余预算取最小 ——
            # 它只能收紧，不能突破。
            outcome = await self.runner.call(
                spec.name,
                spec.handler,
                counted=spec.counted,
                timeout=spec.timeout,
                **decision.arguments,
            )
            tool_elapsed = int((time.perf_counter() - tool_started) * 1000)

            if outcome.ok and isinstance(outcome.value, ToolResult):
                result = outcome.value
                result.elapsed_ms = tool_elapsed
            else:
                # 预算拒绝 / 超时 / handler 抛异常，都在这里归一成 ToolResult
                result = ToolResult(
                    ok=False,
                    content=(
                        f"调用「{spec.name}」没成功：{outcome.error or '未知原因'}。"
                        "你可以换个方式再试，或者基于已有信息回答。"
                    ),
                    error=outcome.error or "tool_failed",
                    retryable=not outcome.rejected,
                    elapsed_ms=tool_elapsed,
                )

            step.tool_name = spec.name
            step.tool_ok = result.ok
            step.tool_error = result.error

            # ── OBSERVE_RESULT
            observation.record(spec.name, result)
            yield LoopEvent(
                "tool_result",
                {
                    "tool": spec.name,
                    "ok": result.ok,
                    "summary": _summarize_result(result),
                    "display": result.display,
                    "provider": result.display.get("provider"),
                    "fell_back": result.display.get("fell_back", False),
                    "fallback_reason": result.display.get("fallback_reason"),
                },
            )
            for citation in result.citations:
                yield LoopEvent("citation", citation)

            if result.ok and result.display:
                yield LoopEvent("source", result.display)

            # 预算被拒 → 后面再调也会被拒，直接收尾（省掉一次注定失败的决策）
            if outcome.rejected:
                observation.stop_note = (
                    f"这一轮的工具调用次数已经用完了（上限 {self.limits.max_tool_calls} 次），"
                    "下面是基于已有信息的回答。"
                )
                break

        # ─────────────────────────────────────────── 收尾
        #
        # ⚠️ **最终生成不受 TOTAL_TIMEOUT 约束**，这是刻意的：
        #
        # 生成**就是**降级目标本身。给生成也设时限，超时后就什么都不能输出了 ——
        # 那正好违背"无论如何都要给用户一个回复"这条原则。
        #
        # 代价要说清楚：**用户实际等待 = 编排耗时（≤30s）+ 生成耗时**。
        # 实测一个"检索 → 判断 → 联网 → 判断"的完整回合约 32s
        # （其中编排约 28s、生成约 4s）。所以 30s 应当理解为
        # "我们愿意在工具编排上花多久"，而不是"整轮最多 30 秒"。
        answer = ""
        async for piece in self.generate(observation):
            if piece:
                answer += piece
                yield LoopEvent("delta", {"text": piece})

        yield LoopEvent(
            "done",
            {
                "answer": answer,
                "steps": [s.as_dict() for s in steps],
                "state": LoopState.DONE.value,
                "stop_note": observation.stop_note,
                # 用 `budget.used` 而不是 `len(records)` ——
                # records 里含"被拒绝"的条目，那些不算调用过。
                "tool_calls": self.runner.budget.used,
                "elapsed_ms": elapsed_ms(),
                "degraded": self.runner.degraded or bool(observation.stop_note),
            },
        )


def _missing_required(spec: Any, arguments: dict[str, Any]) -> list[str]:
    """检查必填参数有没有给。

    **必须在调用之前做**，否则会得到一个 `TypeError: missing 1 required
    positional argument` —— 实测踩过，那句报错对模型毫无帮助
    （它不知道该补什么参数），对人也不好排查。
    """
    schema = spec.parameters or {}
    required = schema.get("required") or []
    properties = schema.get("properties") or {}
    missing: list[str] = []
    for name in required:
        if name not in arguments:
            missing.append(str(name))
            continue
        value = arguments.get(name)
        # 空串等同于没给 —— 模型偶尔会填个空字符串占位
        spec_type = (properties.get(name) or {}).get("type")
        if spec_type == "string" and not str(value or "").strip():
            missing.append(str(name))
    return missing


def _describe_parameters(spec: Any) -> str:
    schema = spec.parameters or {}
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    if not properties:
        return "（无参数）"
    return "、".join(
        f"{name}{'(必填)' if name in required else ''}: {info.get('type', '?')}"
        for name, info in properties.items()
    )


def _summarize_arguments(arguments: dict[str, Any]) -> str:
    """给界面看的参数摘要。只取标量，避免把长文本塞进事件流。"""
    parts: list[str] = []
    for key, value in list(arguments.items())[:4]:
        if isinstance(value, str):
            parts.append(f"{key}={value[:40]}")
        elif isinstance(value, (int, float, bool)):
            parts.append(f"{key}={value}")
        elif isinstance(value, list):
            parts.append(f"{key}=[{len(value)} 项]")
    return ", ".join(parts)


def _summarize_result(result: ToolResult) -> str:
    if result.ok:
        return result.content[:160]
    return (result.error or "调用失败")[:160]


def format_observations(observation: LoopObservation) -> str:
    """把已获得的工具结果拼成给模型看的素材段。

    **失败的结果也要写进去** —— 模型需要知道"这条路走不通了"，
    否则它会一再重试同一个工具。写清失败原因，它才会换策略。
    """
    if not observation.results:
        return ""

    blocks: list[str] = []
    for result in observation.results:
        if result.ok:
            blocks.append(result.content.strip())
        else:
            blocks.append(
                f"（有一次工具调用没成功：{result.error}。"
                "如果任务需要那个信息，可以换个查询方式再试一次；否则就基于已有信息回答。）"
            )
    return "\n\n".join(b for b in blocks if b)
