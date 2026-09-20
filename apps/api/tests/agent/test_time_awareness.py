"""时钟类问题的触发、预注入与 Time Tool 测试。

## 这个文件守的是一个**确定性错误**

实测：

    用户问"今天几号？"
    → 没被识别成实时问题，走了快通道
    → 模型靠记忆回答："今天是2024年6月13日"
    → 实际是 2026年9月20日，**差了两年多**

它比"不回答"更糟 —— **答得非常自信**，用户没有理由怀疑，
除非他自己知道今天几号（那他就不会问了）。

所以要守两件事：
  ① 必须识别成"需要真实时间"的问题
  ② **必须真的把真实时间给它**，而不是指望模型自觉去调工具
"""

from __future__ import annotations

import re
from datetime import datetime

import pytest

from app.agent import free_study
from app.agent.runtime_loop import LoopObservation
from app.agent.tool_specs import _WEEKDAYS, current_time
from app.agent.tools.specs import ToolRegistry


# =========================================================================== #
# 一、触发：时钟问题必须进循环
# =========================================================================== #
@pytest.mark.parametrize(
    "question",
    [
        "今天几号？",
        "今天是几号",
        "今天星期几？",
        "现在几点了？",
        "现在是几月？",
        "今天是哪一天",
        "今天是什么日子",
        "当前时间是多少",
        "此刻的日期",
        "今年是哪一年",
    ],
)
def test_clock_questions_enter_the_loop(question: str) -> None:
    """这些问题的答案**完全由本机时钟决定**，不能靠模型记忆。"""
    assert (
        free_study.quick_route(
            question, has_attachments=False, kb_size=0, web_available=True
        )
        is None
    ), f"{question} 必须进循环去拿真实时间"


@pytest.mark.parametrize(
    "question",
    [
        "什么是 JVM？",
        "解释一下快速排序",
        "进程和线程有什么区别",
        "根据我的资料讲讲第三章",
    ],
)
def test_concept_questions_still_take_fast_path(question: str) -> None:
    """**修触发不能把普通问题也拖进循环** —— 那会让每个问题都慢两秒。"""
    plan = free_study.quick_route(
        question, has_attachments=False, kb_size=0, web_available=True
    )
    assert plan is not None
    assert plan.capabilities == ["general"]


def test_time_hint_matches_the_reported_bug() -> None:
    """直接锁住那个踩过的字符串 —— 它是这个文件存在的理由。"""
    assert free_study._TIME_HINTS.search("今天几号？")


# =========================================================================== #
# 二、Time Tool 本身
# =========================================================================== #
@pytest.mark.asyncio
async def test_current_time_returns_real_clock() -> None:
    """返回的必须是**本机真实时间**，不是编的、也不是缓存的。"""
    result = await current_time()
    assert result.ok is True

    now = datetime.now()
    assert now.strftime("%Y") in result.content, "年份不是当前的"
    assert now.strftime("%m月") in result.content or now.strftime("%m月") in result.content

    assert result.display["kind"] == "time"
    assert result.display["date"] == now.strftime("%Y-%m-%d")
    assert result.display["weekday"] in _WEEKDAYS


@pytest.mark.asyncio
async def test_current_time_includes_weekday_in_chinese() -> None:
    """星期几**自己查表而不是用 locale** —— 容器里不保证装了中文 locale。"""
    result = await current_time()
    assert any(day in result.content for day in _WEEKDAYS)
    # 数字星期（如 "0"）不应该是主要呈现方式
    assert "星期" in result.content


@pytest.mark.asyncio
async def test_current_time_tells_model_to_trust_it() -> None:
    """素材里要明确说"这是权威值，别凭记忆推断" ——

    否则模型可能看到日期仍然写自己的印象版本。
    """
    result = await current_time()
    assert "权威" in result.content
    assert "不要凭记忆" in result.content


def test_time_tool_does_not_consume_external_budget() -> None:
    """**`counted=False`：本地读不该占用户的工具调用额度。**

    让它跟联网搜索抢那 3 次配额没有道理 ——
    那会在"先问日期、再联网查别的"这种组合里无谓地吃掉一次。
    """
    registry = free_study.build_registry()
    spec = registry.get("current_time")
    assert spec is not None
    assert spec.counted is False


def test_time_tool_needs_no_parameters() -> None:
    """零参数 —— 调用它不需要模型构造任何东西，这也是它可靠的原因之一。"""
    spec = free_study.build_registry().get("current_time")
    assert spec is not None
    assert spec.parameters.get("properties") == {}
    assert not spec.parameters.get("required")


def test_time_tool_description_forbids_web_search() -> None:
    """描述里必须写明"这类问题不要联网" ——

    否则模型可能用 web_search 去问日期，那是又慢又不可靠的做法。
    """
    spec = free_study.build_registry().get("current_time")
    assert spec is not None
    assert "不要用联网搜索" in spec.description


# =========================================================================== #
# 三、预注入：**不依赖模型自觉**
# =========================================================================== #
@pytest.mark.asyncio
async def test_time_is_preinjected_before_the_loop() -> None:
    """⚠️ **时钟问题的真实时间必须在进循环之前就拿到。**

    只做"识别 + 进循环"是不够的 —— 那仍然**指望模型主动去调时钟工具**，
    而实测它就是不调、直接凭记忆答。

    预注入之后，模型看到的素材里就有正确日期，想答错都难。
    """
    events = []
    async for event in free_study.stream_turn(question="今天几号？"):
        events.append(event)

    done = next(e for e in events if e.event == "done")
    steps = done.data.get("steps") or []

    # 时钟是**预注入**的，所以不该出现"模型主动调 current_time"这一步 ——
    # 它已经在观察里了，模型直接收尾即可
    assert not any(s.get("tool") == "current_time" for s in steps), (
        "预注入之后不该再让模型自己去调一次，那是多花一次决策"
    )

    # 但回答必须包含今天的真实日期
    answer = "".join(e.data.get("text", "") for e in events if e.event == "delta")
    today = datetime.now()
    assert today.strftime("%Y") in answer, f"回答里没有当前年份：{answer[:120]}"
    assert today.strftime("%m").lstrip("0") in answer.replace("0", "").replace(
        "月", "月"
    ) or f"{today.month}月" in answer, f"回答里没有当前月份：{answer[:120]}"


@pytest.mark.asyncio
async def test_non_clock_question_does_not_preinject_time() -> None:
    """**没有时钟信号的问题不该被注入时间** ——

    那会往素材里塞无关内容，干扰模型，也白花一次本地调用。
    """
    events = []
    async for event in free_study.stream_turn(question="什么是进程？"):
        events.append(event)

    answer = "".join(e.data.get("text", "") for e in events if e.event == "delta")
    # 普通概念问题的回答里不该出现完整的今天日期
    today = datetime.now().strftime("%Y年%m月%d日")
    assert today not in answer


def test_preinjection_is_recorded_in_observation() -> None:
    """预注入走的是 `observation.record()` —— 与工具调用同一条路径，

    这样"素材拼接"与"失败留痕"的逻辑不需要为它开特例。
    """
    obs = LoopObservation(question="今天几号？")
    from app.agent.tools.specs import ToolResult

    obs.record("current_time", ToolResult(ok=True, content="2026年09月20日", display={"kind": "time"}))
    assert obs.call_count("current_time") == 1
    assert len(obs.ok_results) == 1


# =========================================================================== #
# 四、年号别写死在正则里
# =========================================================================== #
def test_time_regex_does_not_hardcode_a_year() -> None:
    """⚠️ 正则里**不能出现具体年份**。

    写死 `2026` 的话，过一年这个功能就开始漏判 ——
    而那种 bug 要等到明年才会被发现。
    """
    pattern = free_study._TIME_HINTS.pattern
    assert not re.search(r"20\d{2}", pattern), f"正则里写死了年份：{pattern}"
