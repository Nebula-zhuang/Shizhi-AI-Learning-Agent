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
    AgentLoop,
    LoopDecision,
    LoopLimits,
    LoopObservation,
    LoopState,
    _describe_parameters,
    _missing_required,
    _summarize_arguments,
    format_observations,
)
from app.agent.tools.registry import ToolRunner
from app.agent.tools.specs import ToolRegistry, ToolSpec, ToolResult


# --------------------------------------------------------------------------- #
# 测试台
# --------------------------------------------------------------------------- #
def _spec(name: str = "web_search", handler=None, *, required=("query",)) -> ToolSpec:
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
    )


class _Scripted:
    """按剧本一步步给出决策。用完剧本后一直收尾。"""

    def __init__(self, decisions: list[LoopDecision]) -> None:
        self._decisions = decisions
        self.calls = 0

    async def __call__(self, observation: LoopObservation, tools: list[dict]) -> LoopDecision:
        self.calls += 1
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
    observation: LoopObservation | None = None,
) -> dict:
    """跑一轮，返回 done 事件的 data。"""
    registry = ToolRegistry()
    for spec in specs or [_spec()]:
        registry.register(spec)

    decider = _Scripted(decisions)
    loop = AgentLoop(
        registry=registry,
        runner=ToolRunner(max_calls=max_calls),
        decider=decider,
        generate=_generate,
        limits=limits or LoopLimits(max_steps=20, max_tool_calls=99, total_seconds=60),
    )

    result: dict = {}
    async for event in loop.run(observation or LoopObservation(question="q")):
        if event.event == "done":
            result = event.data
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
