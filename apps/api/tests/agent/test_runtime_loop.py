"""Agent Loop 的硬限制与健壮性测试。

## 这个文件守的是什么

循环的价值全在"**行为可预测**"：无论模型做什么，都要有明确的上限、
明确的降级、明确的错误语义。所以下面大部分是**边界与异常**用例，
而不是"正常路径能不能跑通"。

## 为什么要注入决策函数

要验证"MAX_STEPS 超限会降级"，就得让决策函数**按剧本连续给出 N 个工具调用**。
如果循环内部直接调 LLM，这个测试只能靠真实模型碰运气 ——
而我不能指望模型每次都配合我调满 6 步。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from app.agent.runtime_loop import (
    MIN_USEFUL_TOOL_SECONDS,
    AgentLoop,
    BudgetSnapshot,
    LoopDecision,
    LoopLimits,
    LoopObservation,
    LoopState,
    _describe_parameters,
    _missing_required,
    _summarize_arguments,
    format_budget,
    format_observations,
)
from app.agent.tools.registry import ToolRunner
from app.agent.tools.specs import ToolRegistry, ToolSpec, ToolResult
# --------------------------------------------------------------------------- #
# 测试台
# --------------------------------------------------------------------------- #
def _spec(
    name: str = "web_search",
    handler=None,
    *,
    required=("query",),
    timeout: float | None = None,
) -> ToolSpec:
    async def default_handler(**kwargs) -> ToolResult:
        return ToolResult(ok=True, content=f"结果：{kwargs.get('query', '')}")

    return ToolSpec(
        name=name,
        description="测试工具",
        parameters={
            "type": "object",
            "required": list(required),
            "properties": {key: {"type": "string"} for key in required},
        },
        handler=handler or default_handler,
        timeout=timeout,
    )


class _Scripted:
    """按剧本一步步给出决策。用完剧本后一直收尾。

    顺带记下**每次**决策时观察对象里的预算快照 ——
    "模型看到的是不是最新预算"只能从这里验证。
    """

    def __init__(self, decisions: list[LoopDecision]) -> None:
        self._decisions = decisions
        self.calls = 0
        self.seen_budgets: list[BudgetSnapshot | None] = []

    async def __call__(self, observation: LoopObservation, tools: list[dict]) -> LoopDecision:
        self.calls += 1
        self.seen_budgets.append(observation.budget)
        if self.calls <= len(self._decisions):
            return self._decisions[self.calls - 1]
        return LoopDecision(thought="剧本用完了", final=True)


async def _generate(observation: LoopObservation) -> AsyncIterator[str]:
    yield "最终回答"


async def _run(
    *,
    decisions: list[LoopDecision],
    specs: list[ToolSpec] | None = None,
    limits: LoopLimits | None = None,
    max_calls: int = 99,
    max_seconds: float | None = None,
    observation: LoopObservation | None = None,
) -> dict:
    """跑一轮，返回 done 事件的 data。

    `max_seconds` **默认不传**（沿用 `ToolRunner` 的默认值）——
    这样既有用例的行为一字不变。要测"剩余预算不足"的用例才需要它，
    因为生产代码 `free_study.stream_turn` 是**把 `limits.total_seconds` 传给 runner 的**，
    而这里原来没传，两者本来就不一致。
    """
    registry = ToolRegistry()
    for spec in specs or [_spec()]:
        registry.register(spec)

    decider = _Scripted(decisions)
    runner_kwargs: dict = {"max_calls": max_calls}
    if max_seconds is not None:
        runner_kwargs["max_seconds"] = max_seconds
    loop = AgentLoop(
        registry=registry,
        runner=ToolRunner(**runner_kwargs),
        decider=decider,
        generate=_generate,
        limits=limits or LoopLimits(max_steps=20, max_tool_calls=99, total_seconds=60),
    )

    result: dict = {}
    async for event in loop.run(observation or LoopObservation(question="q")):
        if event.event == "done":
            result = event.data
    # 附上决策函数的痕迹 —— "有没有空转"看 calls，"看到的是不是最新预算"看 budgets
    result["decider_calls"] = decider.calls
    result["seen_budgets"] = decider.seen_budgets
    return result


def _tool_steps(done: dict) -> list[dict]:
    return [s for s in done["steps"] if s.get("tool")]


# --------------------------------------------------------------------------- #
# 一、正常路径
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_direct_answer_needs_no_tool() -> None:
    """场景 A 的循环侧：模型直接收尾 → 零工具调用。"""
    done = await _run(decisions=[LoopDecision(final=True)])
    assert done["tool_calls"] == 0
    assert _tool_steps(done) == []
    assert done["degraded"] is False


@pytest.mark.asyncio
async def test_loop_can_call_two_different_tools() -> None:
    """**循环的核心价值**：第一个工具的结果不够，再调第二个。

    流水线做不到这件事 —— 它只能按预先写好的顺序跑。
    """
    done = await _run(
        decisions=[
            LoopDecision(tool="web_search", arguments={"query": "a"}),
            LoopDecision(tool="web_search", arguments={"query": "b"}),
            LoopDecision(final=True),
        ]
    )
    assert done["tool_calls"] == 2
    assert [s["tool"] for s in _tool_steps(done)] == ["web_search", "web_search"]


@pytest.mark.asyncio
async def test_decide_is_called_again_after_each_result() -> None:
    """决策次数 = 工具次数 + 1（收尾那次）。

    这条断言的是"每拿到一个结果都会重新判断" —— 也就是**循环**本身。
    """
    registry = ToolRegistry()
    registry.register(_spec())
    decider = _Scripted(
        [
            LoopDecision(tool="web_search", arguments={"query": "a"}),
            LoopDecision(tool="web_search", arguments={"query": "b"}),
            LoopDecision(final=True),
        ]
    )
    loop = AgentLoop(
        registry=registry,
        runner=ToolRunner(max_calls=99),
        decider=decider,
        generate=_generate,
        limits=LoopLimits(max_steps=20, max_tool_calls=99, total_seconds=60),
    )
    async for _ in loop.run(LoopObservation(question="q")):
        pass
    assert decider.calls == 3


# --------------------------------------------------------------------------- #
# 二、三条硬限制
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_max_tool_calls_is_enforced() -> None:
    """工具调用次数上限 → 超出的被拒，并降级收尾。"""
    done = await _run(
        decisions=[LoopDecision(tool="web_search", arguments={"query": f"q{i}"}) for i in range(10)],
        max_calls=3,
        limits=LoopLimits(max_steps=20, max_tool_calls=3, total_seconds=60),
    )
    assert done["tool_calls"] == 3
    assert done["degraded"] is True
    assert "用完" in done["stop_note"]


@pytest.mark.asyncio
async def test_max_steps_is_enforced() -> None:
    """步数上限 → 即使调用次数还很多，也会在步数到顶时收尾。

    这一条只有在 `max_tool_calls` 设得很大时才咬得到 ——
    它是**外层兜底**，两个限制都要有。
    """
    done = await _run(
        decisions=[LoopDecision(tool="web_search", arguments={"query": f"q{i}"}) for i in range(20)],
        limits=LoopLimits(max_steps=4, max_tool_calls=99, total_seconds=60),
    )
    assert len(done["steps"]) <= 5  # 4 步 + 触发上限的那一步
    assert done["degraded"] is True
    assert "还没收尾" in done["stop_note"]


@pytest.mark.asyncio
async def test_total_timeout_is_enforced() -> None:
    """总时限 → 每次调用都不慢，但累计超时就停。"""

    async def slow(**kwargs) -> ToolResult:
        await asyncio.sleep(0.35)
        return ToolResult(ok=True, content="慢")

    done = await _run(
        decisions=[LoopDecision(tool="web_search", arguments={"query": "q"}) for _ in range(20)],
        specs=[_spec(handler=slow)],
        limits=LoopLimits(max_steps=50, max_tool_calls=99, total_seconds=1.0),
    )
    assert done["degraded"] is True
    assert "查得比较久" in done["stop_note"]
    # 印证"只有总时限能挡住这种情形" —— 调用次数远没到上限
    assert done["tool_calls"] < 20


# --------------------------------------------------------------------------- #
# 二之二、剩余预算不足时不要硬发调用
#
# 背景：`ToolBudget` 只判断"已经花了多久"，不管"剩下的够不够做一件事"。
# 于是循环会在只剩零点几秒时照常发起调用 —— 那次必然超时，
# **失败但占用一次调用配额**，等待时间还计入整轮耗时。
# 下面四条守住修好之后的行为。
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_low_remaining_budget_skips_the_handler() -> None:
    """剩余预算低于下限 → **处理器一次都不该被调用**。

    构造：runner 的预算只有 1.0s（< MIN_USEFUL_TOOL_SECONDS），
    而循环的总时限是 60s（所以下限就是那个常量本身）。
    此时不该再发起任何工具调用。
    """
    called: list[str] = []

    async def spy(**kwargs) -> ToolResult:
        called.append(kwargs.get("query", ""))
        return ToolResult(ok=True, content="不该被调用")

    done = await _run(
        decisions=[LoopDecision(tool="web_search", arguments={"query": "q"}) for _ in range(10)],
        specs=[_spec(handler=spy)],
        limits=LoopLimits(max_steps=20, max_tool_calls=99, total_seconds=60),
        max_seconds=1.0,
    )

    assert called == [], "剩余预算不足时仍然发起了工具调用"
    assert done["tool_calls"] == 0


@pytest.mark.asyncio
async def test_low_remaining_budget_does_not_spin() -> None:
    """剩余预算不足 → **立刻停**，不能空转。

    这条是这次修改最容易踩的坑：如果改法是"工具调用器里拒绝并返回失败"，
    那么被拒绝的调用**既不消耗次数也不耗时**（`ToolBudget.used` 会滤掉
    `rejected` 的条目），时间不推进 → 循环顶部的判断永远为假 →
    一直拒绝到 `max_steps` 耗尽。表现就是"步数暴涨但什么也没做"。

    所以判断必须放在**循环自己的 out_of_time()** 里。
    """
    done = await _run(
        decisions=[LoopDecision(tool="web_search", arguments={"query": "q"}) for _ in range(10)],
        limits=LoopLimits(max_steps=20, max_tool_calls=99, total_seconds=60),
        max_seconds=1.0,
    )

    # 决策函数一次都没被调用 = 没有空转
    assert done["decider_calls"] == 0, "空转了：预算不足还在反复向模型要决策"
    assert len(done["steps"]) == 1, f"应当一步就收尾，实际 {len(done['steps'])} 步"


@pytest.mark.asyncio
async def test_low_remaining_budget_uses_existing_degrade_path() -> None:
    """走的必须是**已有的降级路径**，不是新造一条。

    `out_of_time()` 的两个条件共用同一段 `stop_note` ——
    对用户来说"这次查得比较久"在两种情况下都成立，
    而新增一条独立文案只会让前端多一个要处理的分支。
    """
    done = await _run(
        decisions=[LoopDecision(tool="web_search", arguments={"query": "q"}) for _ in range(10)],
        limits=LoopLimits(max_steps=20, max_tool_calls=99, total_seconds=60),
        max_seconds=1.0,
    )

    assert done["degraded"] is True
    assert "查得比较久" in done["stop_note"]
    # 降级路径必须仍然产出回答，而不是空手而归
    assert done["answer"] == "最终回答"


@pytest.mark.asyncio
async def test_small_total_budget_still_exercises_cumulative_timeout() -> None:
    """总预算本身就很小的时候，下限要跟着收缩，否则会**一步就停**。

    `test_total_timeout_is_enforced` 用 `total_seconds=1.0` 模拟超时。
    如果下限死用常量 2.0，那个用例会变成"第 1 步就停、0 次调用"——
    断言仍会通过，但**它守的"累计超时"逻辑根本没被走到**，等于空转通过。
    所以下限取 `min(常量, 总预算 / 2)`。
    """
    async def quick(**kwargs) -> ToolResult:
        await asyncio.sleep(0.1)
        return ToolResult(ok=True, content="快")

    done = await _run(
        decisions=[LoopDecision(tool="web_search", arguments={"query": "q"}) for _ in range(20)],
        specs=[_spec(handler=quick)],
        limits=LoopLimits(max_steps=50, max_tool_calls=99, total_seconds=1.0),
        max_seconds=1.0,
    )

    assert done["degraded"] is True
    assert "查得比较久" in done["stop_note"]
    # 关键：确实**发起过**调用，说明累积到超时才停，而不是一上来就判定时间不够
    assert done["tool_calls"] > 0, "一步都没调就停了 —— 下限没有随小总预算收缩"


# --------------------------------------------------------------------------- #
# 二之三、ToolRunner 的超时下限：两类调用的策略是相反的
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_counted_tool_does_not_borrow_time() -> None:
    """外部工具的调用**不再**被补到 1s —— 报错里的数字就是真实上限。

    修之前是 `max(1.0, min(...))`：只剩 0.2s 也会给 1.0s，
    等于凭空多花 0.8s 去等一次注定失败的调用。
    """
    async def slow(**kwargs) -> ToolResult:
        await asyncio.sleep(2.0)
        return ToolResult(ok=True, content="慢")

    # 预算 0.3s，调用必然超时；关键看它给的上限是多少
    runner = ToolRunner(max_calls=9, max_seconds=0.3)
    outcome = await runner.call("web_search", slow, query="q")

    assert outcome.ok is False
    assert "超时" in (outcome.error or "")
    # 上限应当是真实的剩余 0.3s，而不是被抬到 1.0s
    assert ">0.3s" in (outcome.error or ""), f"上限被抬高了：{outcome.error}"


@pytest.mark.asyncio
async def test_uncounted_state_op_keeps_one_second_floor() -> None:
    """本地状态操作**保留** 1s 下限 —— 它们必须能跑完。

    这类调用不碰外部服务、耗时以毫秒计，但在预算刚好耗尽时
    给 0 会让学习状态**静默写不进去**，而那是最难发现的一类失败。
    """
    async def local_write(**kwargs) -> ToolResult:
        await asyncio.sleep(0.3)
        return ToolResult(ok=True, content="写好了")

    # 预算已经耗尽（0 秒）
    runner = ToolRunner(max_calls=9, max_seconds=0.0)
    outcome = await runner.state_op("state_op", local_write)

    assert outcome.ok is True, f"本地状态操作被饿死了：{outcome.error}"
    assert outcome.rejected is False


# --------------------------------------------------------------------------- #
# 二之四、ToolSpec.timeout 接线
#
# 背景：`ToolSpec.timeout` 曾经是**声明了却从未被读取**的字段 ——
# `image_analysis` 写着 40.0，实际生效的一直是 Runner 默认的 20.0。
# 这种"看起来配了、其实没生效"比没有更危险：下一个人会照着 40 去理解行为。
#
# 接线后的语义：最终上限 = `min(spec.timeout, runner.tool_timeout, 剩余预算)`。
# **只能收紧，不能突破** —— 一个工具不该有能力吃掉整轮。
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_spec_timeout_tightens_the_call() -> None:
    """`spec.timeout=1s` + 处理器要 3s → 实际约 1s 就被掐断。"""

    async def slow(**kwargs) -> ToolResult:
        await asyncio.sleep(3.0)
        return ToolResult(ok=True, content="本该跑 3 秒")

    runner = ToolRunner(max_calls=9, max_seconds=60.0)  # Runner 默认 20s
    started = asyncio.get_event_loop().time()
    outcome = await runner.call("web_search", slow, timeout=1.0, query="q")
    elapsed = asyncio.get_event_loop().time() - started

    assert outcome.ok is False
    assert ">1.0s" in (outcome.error or ""), f"没按声明收紧：{outcome.error}"
    assert elapsed < 2.0, f"实际跑了 {elapsed:.1f}s，说明声明没生效"
    assert elapsed >= 0.9


@pytest.mark.asyncio
async def test_spec_timeout_none_falls_back_to_runner_default() -> None:
    """`spec.timeout=None` → 完全走 Runner 默认（既有行为不变）。

    用一个很小的 Runner 默认值来观察，避免为了证明 20s 而真等 20 秒。
    """

    async def slow(**kwargs) -> ToolResult:
        await asyncio.sleep(2.0)
        return ToolResult(ok=True, content="慢")

    runner = ToolRunner(max_calls=9, max_seconds=60.0, tool_timeout=0.5)
    outcome = await runner.call("web_search", slow, timeout=None, query="q")

    assert outcome.ok is False
    assert ">0.5s" in (outcome.error or ""), f"没回落到 Runner 默认：{outcome.error}"


@pytest.mark.asyncio
async def test_spec_timeout_cannot_exceed_runner_default() -> None:
    """`spec.timeout=999s` → **仍被 Runner 默认卡住**（只能收紧不能突破）。"""

    async def slow(**kwargs) -> ToolResult:
        await asyncio.sleep(2.0)
        return ToolResult(ok=True, content="慢")

    runner = ToolRunner(max_calls=9, max_seconds=60.0, tool_timeout=0.5)
    outcome = await runner.call("web_search", slow, timeout=999.0, query="q")

    assert outcome.ok is False
    assert ">0.5s" in (outcome.error or ""), f"声明突破了 Runner 默认：{outcome.error}"


@pytest.mark.asyncio
async def test_spec_timeout_cannot_exceed_remaining_budget() -> None:
    """三者的**最小者**说了算：剩余预算比声明更小时，听剩余预算的。"""

    async def slow(**kwargs) -> ToolResult:
        await asyncio.sleep(3.0)
        return ToolResult(ok=True, content="慢")

    # 剩余预算只有 0.4s；声明 1.0s；Runner 默认 20s → 应当取 0.4s
    runner = ToolRunner(max_calls=9, max_seconds=0.4)
    outcome = await runner.call("web_search", slow, timeout=1.0, query="q")

    assert outcome.ok is False
    assert ">0.4s" in (outcome.error or ""), f"没受剩余预算约束：{outcome.error}"


@pytest.mark.asyncio
async def test_spec_timeout_does_not_break_the_uncounted_floor() -> None:
    """⚠️ **阶段一确定的两类下限策略不能被这次接线改掉。**

    本地状态操作有 1s 保底 —— 即使声明了一个更紧的 timeout，
    这个保底仍然生效：本地操作"跑不完"比"慢一点"糟得多
    （学习状态会静默写不进去）。
    """

    async def local_write(**kwargs) -> ToolResult:
        await asyncio.sleep(0.3)
        return ToolResult(ok=True, content="写好了")

    runner = ToolRunner(max_calls=9, max_seconds=0.0)
    # 声明 0.1s，比 1s 保底更紧 —— 保底应当压过它
    outcome = await runner.state_op("state_op", local_write, timeout=0.1)

    assert outcome.ok is True, f"本地状态操作被更紧的声明掐断了：{outcome.error}"


def test_registered_tool_timeouts_never_exceed_the_runner_default() -> None:
    """**所有注册工具声明的 timeout 都不得超过 Runner 默认值。**

    这是一条防回归的护栏：将来有人给某个工具写上 40、999 这种"想多要点时间"的数，
    会被这里拦下 —— 因为那种声明**永远不会生效**（`min()` 会把它压回去），
    写出来只会误导下一个读代码的人。
    """
    from app.agent.tool_specs import register_all
    from app.agent.tools.registry import DEFAULT_TOOL_TIMEOUT
    from app.agent.tools.specs import ToolRegistry

    registry = ToolRegistry()
    register_all(registry)

    offenders = [
        (s.name, s.timeout)
        for s in registry.all()
        if s.timeout is not None and s.timeout > DEFAULT_TOOL_TIMEOUT
    ]
    assert not offenders, (
        f"这些工具声明了超过 Runner 默认值（{DEFAULT_TOOL_TIMEOUT}s）的 timeout，"
        f"而它永远不会生效：{offenders}"
    )


def test_image_analysis_declares_the_runner_default() -> None:
    """`image_analysis` 是**最慢的工具**，声明"要满额时间"。

    它原来写的是 40.0 —— 比 Runner 默认还宽，是个**永远不生效**的声明。
    改成 Runner 默认值之后：语义正确（只能收紧）、
    同时让"视觉很慢"这件事在代码里可见，而不是藏在默认值里。
    """
    from app.agent.tool_specs import register_all
    from app.agent.tools.registry import DEFAULT_TOOL_TIMEOUT
    from app.agent.tools.specs import ToolRegistry

    registry = ToolRegistry()
    register_all(registry)

    spec = registry.get("image_analysis")
    assert spec is not None, "image_analysis 应当已注册"
    assert spec.timeout is not None, "视觉是最慢的工具，应当显式声明它需要满额时间"
    assert spec.timeout == DEFAULT_TOOL_TIMEOUT, (
        f"应当等于 Runner 默认值 {DEFAULT_TOOL_TIMEOUT}，实际 {spec.timeout}"
    )


# --------------------------------------------------------------------------- #
# 二之五、决策可见性：模型要能看见自己还剩多少预算
#
# 背景：模型此前**完全看不见**剩余预算。实测后果是它把额度全烧在一个
# 注定失败的看图调用上（先等 20 秒、又重试 4.8 秒），其余三件事一件没做 ——
# 它不是判断错了，而是**缺信息**。
#
# ⚠️ 这一组只验证"信息有没有送到、是不是最新的"，
# **不验证模型怎么用** —— 那是模型的事。提示词给事实，模型做判断。
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_first_decide_sees_the_initial_budget() -> None:
    """第一次 DECIDE 就能看到**初始**剩余预算。"""
    done = await _run(
        decisions=[LoopDecision(final=True)],
        max_calls=3,
        max_seconds=30.0,
    )

    budgets = done["seen_budgets"]
    assert len(budgets) == 1, f"应当只决策一次，实际 {len(budgets)} 次"
    first = budgets[0]
    assert first is not None, "第一次决策就该带上预算快照"
    assert first.remaining_calls == 3
    assert first.max_calls == 3
    assert first.max_seconds == 30.0
    # 刚进循环，剩余时间应当接近总预算
    assert first.remaining_seconds > 29.0
    assert first.can_call_tool is True


@pytest.mark.asyncio
async def test_second_decide_sees_the_updated_budget() -> None:
    """**关键用例**：工具跑过一次之后，第二次 DECIDE 看到的必须是**扣减后**的预算。

    如果循环只在进入时取一次快照，第二次决策看到的就还是 3 次 / 30 秒 ——
    模型会照着过期信息规划。这条就是钉住"每次刷新"的。
    """

    async def slow(**kwargs) -> ToolResult:
        await asyncio.sleep(0.25)
        return ToolResult(ok=True, content="检索结果")

    done = await _run(
        decisions=[
            LoopDecision(tool="web_search", arguments={"query": "q"}),
            LoopDecision(final=True),
        ],
        specs=[_spec(handler=slow)],
        max_calls=3,
        max_seconds=30.0,
    )

    budgets = done["seen_budgets"]
    assert len(budgets) == 2, f"应当决策两次，实际 {len(budgets)} 次"
    first, second = budgets[0], budgets[1]
    assert first is not None and second is not None

    # 调用次数被真实扣减
    assert first.remaining_calls == 3
    assert second.remaining_calls == 2, "第二次决策看到的次数没有扣减 —— 快照是过期的"

    # 时间也在走
    assert second.remaining_seconds < first.remaining_seconds
    assert second.max_calls == first.max_calls, "上限不该变，变的只是剩余量"


@pytest.mark.asyncio
async def test_budget_updates_on_every_decide_not_just_once() -> None:
    """连调两次工具 → 三次决策看到的剩余次数应当是 3 → 2 → 1。"""

    async def quick(**kwargs) -> ToolResult:
        await asyncio.sleep(0.05)
        return ToolResult(ok=True, content="结果")

    done = await _run(
        decisions=[
            LoopDecision(tool="web_search", arguments={"query": "a"}),
            LoopDecision(tool="web_search", arguments={"query": "b"}),
            LoopDecision(final=True),
        ],
        specs=[_spec(handler=quick)],
        max_calls=3,
        max_seconds=30.0,
    )

    remaining = [b.remaining_calls for b in done["seen_budgets"] if b is not None]
    assert remaining == [3, 2, 1], f"每次决策都该看到最新值，实际 {remaining}"


def test_budget_text_says_stop_when_calls_are_exhausted() -> None:
    """剩余**次数为 0** → 上下文必须明确说"不能再调用工具了"。"""
    text = format_budget(
        BudgetSnapshot(
            remaining_calls=0,
            remaining_seconds=20.0,
            max_calls=3,
            max_seconds=30.0,
            useful_floor_seconds=MIN_USEFUL_TOOL_SECONDS,
        )
    )

    assert "不能" in text and "调用工具" in text, f"没写清不能调用工具：{text}"
    assert "0 次" in text
    assert "次数已经用完" in text, "应当说明是次数用完了"
    # 必须给出出路，否则模型只会卡住
    assert "最终回答" in text


def test_budget_text_says_stop_when_time_is_insufficient() -> None:
    """剩余**时间低于 useful floor** → 上下文必须明确说该收尾了。"""
    text = format_budget(
        BudgetSnapshot(
            remaining_calls=2,
            remaining_seconds=1.0,  # < 2.0 的门槛
            max_calls=3,
            max_seconds=30.0,
            useful_floor_seconds=MIN_USEFUL_TOOL_SECONDS,
        )
    )

    assert "不能" in text and "调用工具" in text, f"没写清不能调用工具：{text}"
    assert "时间不足" in text, "应当说明是时间不够了"
    assert "最终回答" in text


def test_budget_text_is_neutral_when_budget_is_healthy() -> None:
    """预算还够时**不要**下"必须收尾"的判断 —— 那会把判断权从模型手里拿走。"""
    text = format_budget(
        BudgetSnapshot(
            remaining_calls=2,
            remaining_seconds=25.0,
            max_calls=3,
            max_seconds=30.0,
            useful_floor_seconds=MIN_USEFUL_TOOL_SECONDS,
        )
    )

    assert "不能再调用工具" not in text
    assert "剩余工具调用" in text and "剩余时间" in text
    # 要说明这是只读的、模型改不了
    assert "不能修改" in text


def test_budget_text_is_empty_without_a_snapshot() -> None:
    """没有快照时**不输出空标题** —— 免得提示词里出现一段没有内容的"预算"。"""
    assert format_budget(None) == ""


def test_can_call_tool_uses_the_same_floor_as_the_loop() -> None:
    """`can_call_tool` 与 `AgentLoop.out_of_time()` 必须是**同一个门槛**。

    否则会出现"提示词说还能调、真调了却被拒"的自相矛盾 ——
    那比不给信息更糟，模型会学着不信任提示词。
    """
    healthy = BudgetSnapshot(2, 10.0, 3, 30.0, MIN_USEFUL_TOOL_SECONDS)
    no_calls = BudgetSnapshot(0, 10.0, 3, 30.0, MIN_USEFUL_TOOL_SECONDS)
    low_time = BudgetSnapshot(2, 1.9, 3, 30.0, MIN_USEFUL_TOOL_SECONDS)

    assert healthy.can_call_tool is True
    assert no_calls.can_call_tool is False
    assert low_time.can_call_tool is False


def test_hard_limits_are_unchanged() -> None:
    """**硬限制的数值不许被这次改动碰掉。**"""
    limits = LoopLimits()
    assert limits.max_steps == 6
    assert limits.max_tool_calls == 3
    assert limits.total_seconds == 30.0
    assert limits.max_retry_per_tool == 2
    # 门槛常量本身也不该被顺手调大
    assert MIN_USEFUL_TOOL_SECONDS == 2.0


# --------------------------------------------------------------------------- #
# 三、模型输出不合法时的健壮性
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_unknown_tool_reports_available_names() -> None:
    """工具名不存在 → 把可用清单回给模型，让它自己纠正，而不是崩。"""
    done = await _run(
        decisions=[
            LoopDecision(tool="不存在的工具"),
            LoopDecision(final=True),
        ],
        specs=[_spec("web_search")],
    )
    assert done["answer"] == "最终回答"
    unknown = [s for s in done["steps"] if s.get("tool_error") == "unknown_tool"]
    assert unknown, "未知工具应当留下错误留痕"


@pytest.mark.asyncio
async def test_missing_required_argument_is_caught_before_calling() -> None:
    """**缺必填参数必须在调用之前拦住。**

    实测踩过：模型返回 `{"document_ids": [...]}` 却没有 `query`，
    直接调用抛 `TypeError: missing 1 required positional argument: 'query'` ——
    那句报错对模型毫无帮助（它不知道该补什么），对人也不好排查。
    """
    called = {"n": 0}

    async def handler(**kwargs) -> ToolResult:
        called["n"] += 1
        return ToolResult(ok=True, content="ok")

    done = await _run(
        decisions=[
            LoopDecision(tool="web_search", arguments={"document_ids": [1]}),
            LoopDecision(final=True),
        ],
        specs=[_spec(handler=handler)],
    )

    assert called["n"] == 0, "缺参数却还是调用了 handler"
    errs = [s["tool_error"] for s in done["steps"]]
    assert "missing_arguments" in errs


@pytest.mark.asyncio
async def test_blank_string_counts_as_missing() -> None:
    """空字符串等同于没给 —— 模型偶尔会填个空串占位。"""
    called = {"n": 0}

    async def handler(**kwargs) -> ToolResult:
        called["n"] += 1
        return ToolResult(ok=True, content="ok")

    await _run(
        decisions=[
            LoopDecision(tool="web_search", arguments={"query": "   "}),
            LoopDecision(final=True),
        ],
        specs=[_spec(handler=handler)],
    )
    assert called["n"] == 0, "空串应当被当成没给参数"


@pytest.mark.asyncio
async def test_repeated_failure_of_same_tool_is_blocked_in_code() -> None:
    """⚠️ **同一工具反复失败要被代码拦住，不能只靠提示词。**

    实测踩过：提示词里写了"同一个工具最多试两次"，
    模型仍然连续 4 次调用同一个失败的工具。
    **涉及成本与时间的限制，代码是唯一的执行者。**
    """
    calls = {"n": 0}

    async def always_fail(**kwargs) -> ToolResult:
        calls["n"] += 1
        return ToolResult(ok=False, content="挂了", error="boom")

    done = await _run(
        decisions=[LoopDecision(tool="web_search", arguments={"query": f"q{i}"}) for i in range(6)],
        specs=[_spec(handler=always_fail)],
        limits=LoopLimits(max_steps=20, max_tool_calls=99, total_seconds=60, max_retry_per_tool=2),
    )

    assert calls["n"] == 2, f"handler 被调用了 {calls['n']} 次，重试上限没生效"
    assert "retry_limit" in [s["tool_error"] for s in done["steps"]]


@pytest.mark.asyncio
async def test_decider_exception_degrades_to_answer() -> None:
    """决策函数抛异常 → 退化为直接回答，**不中断对话**。"""

    async def boom(observation: LoopObservation, tools: list[dict]) -> LoopDecision:
        raise RuntimeError("模型挂了")

    registry = ToolRegistry()
    registry.register(_spec())
    loop = AgentLoop(
        registry=registry,
        runner=ToolRunner(max_calls=3),
        decider=boom,
        generate=_generate,
        limits=LoopLimits(),
    )
    done = {}
    async for event in loop.run(LoopObservation(question="q")):
        if event.event == "done":
            done = event.data
    assert done["answer"] == "最终回答"


@pytest.mark.asyncio
async def test_unavailable_tool_is_refused_with_reason() -> None:
    """当前不可用的工具（如没配联网）→ 把原因回给模型，让它换路。"""

    async def handler(**kwargs) -> ToolResult:
        return ToolResult(ok=True, content="不该被调到")

    spec = ToolSpec(
        name="web_search",
        description="d",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        available=lambda: (False, "没配 Key"),
    )
    done = await _run(
        decisions=[
            LoopDecision(tool="web_search", arguments={}),
            LoopDecision(final=True),
        ],
        specs=[spec],
    )
    assert "unavailable" in [s["tool_error"] for s in done["steps"]]


@pytest.mark.asyncio
async def test_tool_exception_is_wrapped_not_raised() -> None:
    """工具抛异常 → 包成失败的 ToolResult，让模型看到错误后换策略。"""

    async def boom(**kwargs) -> ToolResult:
        raise ValueError("内部炸了")

    done = await _run(
        decisions=[
            LoopDecision(tool="web_search", arguments={"query": "q"}),
            LoopDecision(final=True),
        ],
        specs=[_spec(handler=boom)],
    )
    assert done["answer"] == "最终回答"
    assert done["tool_calls"] == 1


# --------------------------------------------------------------------------- #
# 四、辅助函数
# --------------------------------------------------------------------------- #
def test_missing_required_detects_absent_and_blank() -> None:
    spec = _spec(required=("query", "max_results"))
    assert set(_missing_required(spec, {})) == {"query", "max_results"}
    # 空串等同于没给；max_results 本来也没给，两个都应该被报出来
    assert set(_missing_required(spec, {"query": "  "})) == {"query", "max_results"}
    assert _missing_required(spec, {"query": "x", "max_results": 3}) == []


def test_describe_parameters_marks_required() -> None:
    text = _describe_parameters(_spec(required=("query",)))
    assert "query" in text
    assert "必填" in text


def test_summarize_arguments_keeps_it_short() -> None:
    """参数摘要是给界面看的，不能把长文本塞进事件流。"""
    text = _summarize_arguments({"query": "x" * 200, "ids": [1, 2, 3], "n": 5, "flag": True})
    assert len(text) < 200
    assert "ids=[3 项]" in text


def test_format_observations_includes_failures() -> None:
    """失败的调用也要写进素材 —— 模型需要知道"这条路走不通了"。"""
    obs = LoopObservation(question="q")
    obs.record("web_search", ToolResult(ok=False, error="timeout", content="超时"))
    text = format_observations(obs)
    assert "没成功" in text


def test_observation_counts_failures_per_tool() -> None:
    obs = LoopObservation(question="q")
    obs.record("web_search", ToolResult(ok=False, error="a"))
    obs.record("web_search", ToolResult(ok=False, error="b"))
    obs.record("retrieve_knowledge", ToolResult(ok=True, content="ok"))

    assert obs.failed_count("web_search") == 2
    assert obs.failed_count("retrieve_knowledge") == 0
    assert len(obs.ok_results) == 1


def test_loop_state_enum_covers_the_flow() -> None:
    """状态枚举要覆盖 Observe→Decide→Execute→ObserveResult→FinalAnswer。"""
    values = {s.value for s in LoopState}
    assert {"OBSERVE", "DECIDE", "EXECUTE", "OBSERVE_RESULT", "FINAL_ANSWER"} <= values
