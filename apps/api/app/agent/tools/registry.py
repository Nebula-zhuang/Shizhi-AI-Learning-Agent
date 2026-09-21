"""Tool 调用层：统一预算、超时与异常包装。

## 预算怎么算（需求：每轮最多 3 次 Tool Call、最长 30 秒）

预算管的是**有外部成本的三件事**：

| 计入预算的 Tool | 为什么 |
|---|---|
| `retrieve_knowledge` | 要调 embedding API + 查向量库 |
| `generate_question` | 要调 LLM |
| `evaluate_answer` | 要调 LLM |

而 `get_learner_state` / `update_learning_state` 走 `state_op()`，**不计入预算** ——
它们只读写本地数据库，不调外部服务、不花钱、耗时可忽略，
本质上是**状态机自身的状态转移**，不是"调用工具"。

这样安排之后每一轮的调用数天然有上界：
  - 开始轮：`retrieve` + `generate_question` = 2 次
  - 作答轮：`evaluate` + `retrieve` + `generate_question` = 3 次

`budget.exhausted` 一旦为真，后续调用会被直接拒绝（而不是等到超时），
运行时就据此跳过非必需步骤 —— 保证"无论如何都能给用户一个回复"。

## 为什么不把 Tool 做成原生 function calling

模型只需按 schema 输出一个 JSON。原生 `tools` 参数各家供应商支持程度不一，
而且会多一轮往返。这与 P1/P2 已在用的 `chat_json` 是同一套通道 —— **不引新协议**。
"""

from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.core.logging import get_logger

logger = get_logger(__name__)


def _is_awaitable(value: Any) -> bool:
    """判断工具返回值是否需要 await。

    不能只看 `asyncio.iscoroutinefunction(func)` ——
    有些工具是同步函数但返回协程对象（例如包了一层的偏函数），
    还有的用 `functools.partial` 包装过。直接检查返回值最稳。
    """
    return inspect.isawaitable(value)

#: 每轮最多调用几次「有外部成本」的 Tool
DEFAULT_MAX_TOOL_CALLS = 3
#: 每轮的总时限（秒）
DEFAULT_MAX_SECONDS = 30.0
#: 单个 Tool 的时限（秒）。比总时限小，避免一个调用吃光整轮预算。
DEFAULT_TOOL_TIMEOUT = 20.0


@dataclass
class ToolCallRecord:
    """一次调用的留痕。用于演示与测试断言"这一轮到底调了什么"。"""

    name: str
    ok: bool
    elapsed_ms: int
    counted: bool = True
    error: str = ""
    degraded: bool = False
    #: 因预算耗尽被直接拒绝（没有真的执行）。**不计入已用次数** ——
    #: 拒绝不是"调用过"，把它算进去会让 `used` 虚高，
    #: 于是"预算上限 3 次"这条约定在统计上就自相矛盾了。
    rejected: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "elapsed_ms": self.elapsed_ms,
            "counted": self.counted,
            "degraded": self.degraded,
            "rejected": self.rejected,
            "error": self.error,
        }


@dataclass
class ToolOutcome:
    """调用结果。**永不抛异常** —— 失败以 `ok=False` 表达，由调用方决定降级策略。"""

    ok: bool
    value: Any = None
    error: str = ""
    degraded: bool = False
    rejected: bool = False

    def __bool__(self) -> bool:
        return self.ok


@dataclass
class ToolBudget:
    """一轮的调用预算。"""

    max_calls: int = DEFAULT_MAX_TOOL_CALLS
    max_seconds: float = DEFAULT_MAX_SECONDS
    records: list[ToolCallRecord] = field(default_factory=list)
    _started: float = field(default_factory=time.perf_counter)

    @property
    def used(self) -> int:
        """已用掉的预算次数。被拒绝的调用不算 —— 它们没有真的执行。"""
        return sum(1 for r in self.records if r.counted and not r.rejected)

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self._started

    @property
    def remaining_calls(self) -> int:
        return max(0, self.max_calls - self.used)

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.max_seconds - self.elapsed)

    @property
    def exhausted(self) -> bool:
        return self.remaining_calls <= 0 or self.remaining_seconds <= 0.0

    def exhausted_reason(self) -> str:
        if self.remaining_calls <= 0:
            return f"本轮 Tool 调用次数已达上限（{self.max_calls} 次）"
        if self.remaining_seconds <= 0:
            return f"本轮总耗时已达上限（{self.max_seconds:.0f} 秒）"
        return ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_calls": self.max_calls,
            "used_calls": self.used,
            "max_seconds": self.max_seconds,
            "elapsed_ms": int(self.elapsed * 1000),
            "exhausted": self.exhausted,
            "records": [r.as_dict() for r in self.records],
        }


class ToolRunner:
    """工具调用器。所有外部调用都必须经过它。"""

    def __init__(
        self,
        *,
        max_calls: int = DEFAULT_MAX_TOOL_CALLS,
        max_seconds: float = DEFAULT_MAX_SECONDS,
        tool_timeout: float = DEFAULT_TOOL_TIMEOUT,
    ) -> None:
        self.budget = ToolBudget(max_calls=max_calls, max_seconds=max_seconds)
        self.tool_timeout = tool_timeout

    async def call(
        self,
        name: str,
        func: Callable[..., Awaitable[Any]],
        /,
        *,
        counted: bool = True,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> ToolOutcome:
        """调用一个工具。

        三种拒绝/失败都会返回 `ok=False` 而不是抛异常：
          - 预算耗尽 → `rejected=True`
          - 超时 → `error` 里写明
          - 抛异常 → `error` 里带异常类型

        `counted=False` 用于两类不算"工具调用"的步骤：
          - Agent 自身的决策推理（它是 Agent 的一步，不是工具）；
          - 本地状态读写（见 `state_op`）。
        它们仍然受单次超时与总时限约束，只是不占调用次数。

        `timeout` 来自 `ToolSpec.timeout`，是**该工具自己声明的上限**。
        它**只能收紧**，不能突破 Runner 默认值或本轮剩余预算 ——
        一个工具不该有能力吃掉整轮。传 `None` 表示没有单独声明，
        完全走 Runner 的默认行为。
        """
        if counted and self.budget.exhausted:
            reason = self.budget.exhausted_reason()
            logger.warning("拒绝调用 %s：%s", name, reason)
            self.budget.records.append(
                ToolCallRecord(
                    name=name, ok=False, elapsed_ms=0, counted=True, error=reason, rejected=True
                )
            )
            return ToolOutcome(ok=False, error=reason, rejected=True)

        return await self._invoke(name, func, counted=counted, timeout=timeout, **kwargs)

    async def state_op(
        self,
        name: str,
        func: Callable[..., Awaitable[Any]],
        /,
        *,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> ToolOutcome:
        """调用一个**不计入预算**的本地状态操作（读/写学习状态）。

        仍然走这里统一异常包装与留痕，只是不占用调用次数。
        理由见模块开头：它们不调外部服务，属于状态机自身的状态转移。
        """
        return await self.call(name, func, counted=False, timeout=timeout, **kwargs)

    async def _invoke(
        self,
        name: str,
        func: Callable[..., Awaitable[Any]],
        /,
        *,
        counted: bool,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> ToolOutcome:
        # 单次调用的超时受**三个**上限共同约束，谁最小听谁的：
        #
        #   ① `timeout`（来自 `ToolSpec.timeout`）—— 工具自己声明的上限。
        #      **只能收紧**：`min()` 保证它永远压不过 Runner 默认值，
        #      所以一个工具不可能靠声明大数字来吃掉整轮。
        #   ② `self.tool_timeout`（Runner 默认）—— 所有工具的兜底上限。
        #   ③ `budget.remaining_seconds` —— 本轮还剩多少，绝不允许单次调用把它拖穿。
        #
        # ⚠️ 声明了 `ToolSpec.timeout` 就必须走到这里来 ——
        # 它曾经是个**从未被读取的字段**（声明 40s 而实际恒为 20s），
        # 这种"看起来配了、其实没生效"的状态比没有更危险。
        ceiling = self.tool_timeout if timeout is None else min(self.tool_timeout, timeout)

        # ⚠️ **两类调用的下限策略是相反的**，这一点很容易写错：
        #
        #   `counted=True`（外部工具）—— **不加下限**。
        #       剩余时间不够时，循环层已经不会再发起调用了
        #       （见 `runtime_loop.MIN_USEFUL_TOOL_SECONDS`）。
        #       这里若再补一个 1s 下限，就成了"只剩 0.2s 却发一次注定超时的调用"：
        #       它必然失败、**仍占用一次调用配额**，而且那段等待还会计入整轮耗时。
        #
        #   `counted=False`（本地状态读写）—— **保留 1s 下限**。
        #       它们不调外部服务、耗时以毫秒计，必须能跑完；
        #       给 0 会让学习状态在预算刚好耗尽时静默写不进去，
        #       而"状态没更新"这种失败很难被发现。
        #       这个保底**优先于**更紧的声明 —— 本地操作"跑不完"比"慢一点"糟得多。
        if counted:
            limit = max(0.0, min(ceiling, self.budget.remaining_seconds))
        else:
            limit = max(1.0, min(ceiling, self.budget.remaining_seconds or 1.0))
        started = time.perf_counter()
        try:
            # 工具既有异步的（调模型 / 查向量库）也有同步的（本地读写学习状态）。
            # 统一在这里判一次：对同步函数直接调用，否则 await。
            # 不判的话会对同步返回值执行 await，报
            # "object X can't be used in 'await' expression" —— 而且会被当成
            # 业务失败静默降级，表现为"状态总是更新不成功"，很难定位。
            produced = func(**kwargs)
            if _is_awaitable(produced):
                value = await asyncio.wait_for(produced, timeout=limit)  # type: ignore[arg-type]
            else:
                # 同步函数是本地数据库操作，耗时可忽略，不做超时包装
                value = produced
        except asyncio.TimeoutError:
            elapsed = int((time.perf_counter() - started) * 1000)
            error = f"{name} 超时（>{limit:.1f}s）"
            logger.warning(error)
            self.budget.records.append(
                ToolCallRecord(name, False, elapsed, counted, error=error, degraded=True)
            )
            return ToolOutcome(ok=False, error=error, degraded=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 任何失败都降级，不中断整轮
            elapsed = int((time.perf_counter() - started) * 1000)
            error = f"{type(exc).__name__}: {exc}"
            logger.warning("%s 调用失败：%s", name, error)
            self.budget.records.append(
                ToolCallRecord(name, False, elapsed, counted, error=error[:300], degraded=True)
            )
            return ToolOutcome(ok=False, error=error, degraded=True)

        elapsed = int((time.perf_counter() - started) * 1000)
        self.budget.records.append(ToolCallRecord(name, True, elapsed, counted))
        return ToolOutcome(ok=True, value=value)

    # ------------------------------------------------------------------ 查询
    @property
    def trace(self) -> list[str]:
        return [r.name for r in self.budget.records]

    @property
    def degraded(self) -> bool:
        """本轮是否发生过降级。"""
        return any(r.degraded for r in self.budget.records)

    def as_dict(self) -> dict[str, Any]:
        return self.budget.as_dict()
