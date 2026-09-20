"""Policy 测试 —— P4 最核心的一组断言。

Policy 是纯函数，所以这里可以把"教学策略"这件事钉得非常死：
每一条阈值规则、规则的优先级、LLM 提案的拦截、mastery 的增量公式，
全部逐条断言。

如果哪天有人想"把难度判断交给模型"，这个文件会立刻失败 —— 而那正是需要停下来
想清楚的一次变更。
"""

from __future__ import annotations

import pytest

from app.agent.policy import (
    FORCED_RULES,
    THRESHOLDS,
    DecisionContext,
    RejectReason,
    RuleId,
    apply_mastery,
    decide,
    next_streaks,
    status_for,
    thresholds_snapshot,
    validate,
)
from app.models.answer_evaluation import AssessmentLevel
from app.models.learner_kp_state import LearnerStatus
from app.models.message import ActionType


def ctx(**overrides) -> DecisionContext:
    """构造决策上下文，默认是一个"学过几次、掌握一般"的学习者。"""
    base = dict(
        mastery=0.30,
        attempt_count=3,
        consecutive_correct=0,
        consecutive_wrong=0,
        is_correct=True,
        score=0.90,
        level=AssessmentLevel.MASTERED,
    )
    base.update(overrides)
    return DecisionContext(**base)


# --------------------------------------------------------------------------- #
# 六条阈值规则
# --------------------------------------------------------------------------- #
def test_mastered_triggers_summarize() -> None:
    """达到掌握阈值 → summarize。"""
    decision = decide(ctx(mastery=0.85))
    assert decision.action == ActionType.SUMMARIZE
    assert decision.rule == RuleId.MASTERED
    assert decision.forced is True


def test_mastered_takes_priority_over_everything() -> None:
    """刚达成掌握、同时又连错两次时，仍然应该收束 —— 终态优先。"""
    decision = decide(ctx(mastery=0.90, consecutive_wrong=2, is_correct=False, score=0.1))
    assert decision.action == ActionType.SUMMARIZE


def test_three_consecutive_wrong_triggers_easier() -> None:
    """连续错误 3 次 → easier。"""
    decision = decide(ctx(consecutive_wrong=3, is_correct=False, score=0.2))
    assert decision.action == ActionType.EASIER
    assert decision.rule == RuleId.WRONG_STREAK_3
    assert decision.forced is True


def test_two_consecutive_wrong_triggers_rephrase() -> None:
    """连续错误 2 次 → rephrase。"""
    decision = decide(ctx(consecutive_wrong=2, is_correct=False, score=0.3))
    assert decision.action == ActionType.REPHRASE
    assert decision.rule == RuleId.WRONG_STREAK_2
    assert decision.forced is True


def test_easier_beats_rephrase_at_three_wrong() -> None:
    """连错 3 次同时满足 rephrase 与 easier 的条件，必须判 easier。

    连错三次说明换讲法也没用 —— 该降难度了。这条优先级是刻意的。
    """
    decision = decide(ctx(consecutive_wrong=5, is_correct=False))
    assert decision.action == ActionType.EASIER
    assert decision.action != ActionType.REPHRASE


def test_two_consecutive_correct_triggers_harder() -> None:
    """连续正确 2 次 → harder。"""
    decision = decide(ctx(consecutive_correct=2, score=0.95))
    assert decision.action == ActionType.HARDER
    assert decision.rule == RuleId.CORRECT_STREAK_2
    assert decision.forced is True


def test_shallow_correct_beats_correct_streak() -> None:
    """答对但浅，且已连对两次时，应当 probe 而不是 harder。

    这是刻意的优先级：浅答对时加难度只会让人更懵，先把没答透的地方问清。
    """
    decision = decide(
        ctx(consecutive_correct=2, score=0.55, level=AssessmentLevel.VAGUE)
    )
    assert decision.action == ActionType.PROBE
    assert decision.rule == RuleId.SHALLOW_CORRECT


def test_vague_level_alone_marks_shallow() -> None:
    """即使分数不低，评估判为 vague 也算"浅"。"""
    decision = decide(ctx(consecutive_correct=0, score=0.80, level=AssessmentLevel.VAGUE))
    assert decision.action == ActionType.PROBE


def test_wrong_and_weak_triggers_explain() -> None:
    """错误且基础薄弱 → explain。"""
    decision = decide(
        ctx(mastery=0.20, consecutive_wrong=1, is_correct=False, score=0.2)
    )
    assert decision.action == ActionType.EXPLAIN
    assert decision.rule == RuleId.WRONG_AND_WEAK


def test_wrong_but_strong_does_not_explain() -> None:
    """掌握度不低时答错一次，不该退回从头讲解。"""
    decision = decide(
        ctx(mastery=0.70, consecutive_wrong=1, is_correct=False, score=0.3)
    )
    assert decision.action != ActionType.EXPLAIN


def test_first_contact_triggers_explain() -> None:
    """初次接触 → 先讲再问。"""
    decision = decide(
        DecisionContext(mastery=0.0, attempt_count=0, is_correct=None, score=None)
    )
    assert decision.action == ActionType.EXPLAIN
    assert decision.rule == RuleId.FIRST_CONTACT
    assert decision.forced is True


def test_default_is_probe() -> None:
    """没有命中特殊规则时默认追问。"""
    decision = decide(
        ctx(mastery=0.60, attempt_count=2, consecutive_correct=1, score=0.92)
    )
    assert decision.action == ActionType.PROBE
    assert decision.rule == RuleId.DEFAULT
    assert decision.forced is False


def test_forced_rules_are_exactly_the_threshold_rules() -> None:
    """强制规则集合必须覆盖全部阈值类规则。

    这条断言在守护「阈值由 Policy 控制」这个约定本身：
    只要有一条阈值规则没被标成 forced，模型就能绕过它。
    """
    assert FORCED_RULES == {
        RuleId.MASTERED,
        RuleId.WRONG_STREAK_3,
        RuleId.WRONG_STREAK_2,
        RuleId.CORRECT_STREAK_2,
        RuleId.FIRST_CONTACT,
    }
    # 每一条强制规则的实际产出都必须是 forced
    for c in (
        ctx(mastery=0.9),
        ctx(consecutive_wrong=3),
        ctx(consecutive_wrong=2),
        ctx(consecutive_correct=2),
        DecisionContext(attempt_count=0),
    ):
        decision = decide(c)
        assert decision.forced is True, f"{decision.rule} 应当是强制规则"
        assert len(decision.allowed) == 1


def test_decision_is_deterministic() -> None:
    c = ctx(consecutive_correct=1)
    assert decide(c).as_dict() == decide(c).as_dict()


# --------------------------------------------------------------------------- #
# LLM 提案校验（阈值不可被绕过的执行点）
# --------------------------------------------------------------------------- #
def test_valid_proposal_is_accepted_in_free_zone() -> None:
    """自由区里，模型在允许集合内的选择应当被采纳。"""
    decision = decide(ctx(mastery=0.60, attempt_count=2, score=0.92))  # 默认 probe，自由区
    assert decision.forced is False

    result = validate(
        {"action": "harder", "reason": "学生已能自述，可以推进", "confidence": 0.8},
        decision,
        allowed_kp_ids=[1],
    )
    assert result.ok is True
    assert result.action == ActionType.HARDER
    assert result.confidence == pytest.approx(0.8)


def test_illegal_action_is_rejected() -> None:
    """未定义的动作必须被拒。"""
    decision = decide(ctx(mastery=0.60, attempt_count=2, score=0.92))
    result = validate({"action": "dance"}, decision, allowed_kp_ids=[1])
    assert result.ok is False
    assert result.reject_reason == RejectReason.UNKNOWN_ACTION
    assert result.action == decision.action, "被拒时必须回退到 Policy 的动作"


def test_action_outside_allowed_set_is_rejected() -> None:
    decision = decide(ctx(mastery=0.30, consecutive_wrong=1, is_correct=False, score=0.2))
    # 答错后的允许集合是 explain/rephrase/easier/probe，不含 harder
    result = validate({"action": "harder"}, decision, allowed_kp_ids=[1])
    assert result.ok is False
    assert result.reject_reason == RejectReason.ACTION_NOT_ALLOWED


def test_llm_cannot_override_forced_action() -> None:
    """**核心约定**：硬阈值锁定动作后，模型换成别的都必须被拒。"""
    decision = decide(ctx(consecutive_correct=2, score=0.95))
    assert decision.forced is True and decision.action == ActionType.HARDER

    result = validate(
        {"action": "easier", "reason": "我觉得学生累了"}, decision, allowed_kp_ids=[1]
    )
    assert result.ok is False
    assert result.reject_reason == RejectReason.ACTION_NOT_ALLOWED
    assert result.action == ActionType.HARDER, "必须是 Policy 锁定的动作"


def test_llm_cannot_downgrade_mastery_decision() -> None:
    """达成掌握后模型不能改成"再练一轮"。"""
    decision = decide(ctx(mastery=0.95))
    result = validate({"action": "harder"}, decision, allowed_kp_ids=[1])
    assert result.ok is False
    assert result.action == ActionType.SUMMARIZE


def test_kp_out_of_scope_is_rejected() -> None:
    """模型不能决定去教别的知识点。"""
    decision = decide(ctx(mastery=0.60, attempt_count=2, score=0.92))
    result = validate(
        {"action": "probe", "knowledge_point_id": 999}, decision, allowed_kp_ids=[1, 2]
    )
    assert result.ok is False
    assert result.reject_reason == RejectReason.KP_NOT_IN_SCOPE


def test_kp_in_scope_is_accepted() -> None:
    decision = decide(ctx(mastery=0.60, attempt_count=2, score=0.92))
    result = validate(
        {"action": "probe", "knowledge_point_id": 2}, decision, allowed_kp_ids=[1, 2]
    )
    assert result.ok is True


def test_bad_confidence_is_rejected() -> None:
    decision = decide(ctx(mastery=0.60, attempt_count=2, score=0.92))
    assert validate({"action": "probe", "confidence": 1.7}, decision, allowed_kp_ids=[1]).ok is False
    assert validate({"action": "probe", "confidence": "极高"}, decision, allowed_kp_ids=[1]).ok is False


def test_non_dict_proposal_is_rejected() -> None:
    decision = decide(ctx(mastery=0.60, attempt_count=2, score=0.92))
    result = validate("我觉得应该讲一下", decision, allowed_kp_ids=[1])
    assert result.ok is False
    assert result.reject_reason == RejectReason.NOT_A_DICT


def test_missing_reason_falls_back_to_policy_reason() -> None:
    decision = decide(ctx(mastery=0.60, attempt_count=2, score=0.92))
    result = validate({"action": "probe"}, decision, allowed_kp_ids=[1])
    assert result.ok is True
    assert result.reason == decision.reason


# --------------------------------------------------------------------------- #
# mastery 增量公式
# --------------------------------------------------------------------------- #
def test_correct_answer_increases_mastery() -> None:
    after = apply_mastery(0.0, correct=True, score=1.0)
    assert after == pytest.approx(THRESHOLDS.learn_gain)


def test_gain_shrinks_as_mastery_grows() -> None:
    """越接近掌握，增益越小 —— 渐进逼近而不是一步登天。"""
    first = apply_mastery(0.0, correct=True, score=1.0) - 0.0
    second = apply_mastery(0.7, correct=True, score=1.0) - 0.7
    assert second < first


def test_wrong_answer_decreases_mastery() -> None:
    assert apply_mastery(0.8, correct=False, score=0.0) < 0.8


def test_wrong_answer_on_unknown_kp_does_not_go_negative() -> None:
    """本来就不熟的知识，答错也没什么可掉的。"""
    assert apply_mastery(0.0, correct=False, score=0.0) == 0.0


def test_mastery_is_clamped_to_unit_interval() -> None:
    assert apply_mastery(1.0, correct=True, score=1.0) <= 1.0
    assert apply_mastery(0.0, correct=False, score=0.0) >= 0.0
    assert apply_mastery(0.01, correct=False, score=1.0) >= 0.0


def test_partial_score_gives_partial_gain() -> None:
    full = apply_mastery(0.0, correct=True, score=1.0)
    half = apply_mastery(0.0, correct=True, score=0.5)
    assert half < full


def test_repeated_correct_answers_reach_mastery_in_reasonable_steps() -> None:
    """演示可行性检查：从 0 开始要多少次全对才能达到掌握阈值。

    这个数字直接决定演示体验 —— 太长会让人失去耐心，太短则看不出教学策略的变化。
    """
    mastery = 0.0
    steps = 0
    while mastery < THRESHOLDS.mastery_threshold and steps < 50:
        mastery = apply_mastery(mastery, correct=True, score=0.95)
        steps += 1
    assert 3 <= steps <= 10, f"达成掌握需要 {steps} 次，节奏不合理"


def test_confidence_never_affects_mastery() -> None:
    """**需求特别强调的区分**：confidence 是评估质量，不参与 mastery 计算。

    `apply_mastery` 的签名里根本没有 confidence 参数 —— 这条断言在守护那个签名，
    防止将来有人"顺手"把 confidence 混进来。
    """
    import inspect

    params = set(inspect.signature(apply_mastery).parameters)
    assert "confidence" not in params
    assert params >= {"mastery", "correct", "score"}


# --------------------------------------------------------------------------- #
# 连击与状态标签
# --------------------------------------------------------------------------- #
def test_streaks_reset_on_opposite_outcome() -> None:
    assert next_streaks(correct=True, consecutive_correct=1, consecutive_wrong=2) == (2, 0)
    assert next_streaks(correct=False, consecutive_correct=3, consecutive_wrong=0) == (0, 1)


def test_status_labels() -> None:
    assert status_for(mastery=0.9, attempt_count=5, consecutive_wrong=0) == LearnerStatus.MASTERED
    assert status_for(mastery=0.1, attempt_count=3, consecutive_wrong=3) == LearnerStatus.WEAK
    assert status_for(mastery=0.0, attempt_count=0, consecutive_wrong=0) == LearnerStatus.NEW
    assert status_for(mastery=0.5, attempt_count=3, consecutive_wrong=0) == LearnerStatus.LEARNING


def test_thresholds_snapshot_is_serializable() -> None:
    snap = thresholds_snapshot()
    assert snap["mastery_threshold"] == THRESHOLDS.mastery_threshold
    assert all(isinstance(v, (int, float)) for v in snap.values())


# --------------------------------------------------------------------------- #
# Prompt 里不许出现阈值数字
# --------------------------------------------------------------------------- #
def test_summarize_is_never_in_a_free_zone() -> None:
    """**`summarize` 绝不能出现在自由区集合里。**

    它是"达成掌握"这个阈值的终端动作。允许模型自选的话，模型会在掌握度还很低时
    说"可以收口了" —— 那就等于让 LLM 自己定义掌握阈值，与需求直接冲突。

    这是真实踩过的坑：live 模式下模型在 mastery 0.60 时就选了 summarize，
    把会话提前收束了，导致"达到掌握阈值才总结"这条规则形同虚设。
    """
    from app.agent.policy import (
        THRESHOLD_ONLY_ACTIONS,
        _ALLOWED_AT_START,
        _ALLOWED_AFTER_CORRECT,
        _ALLOWED_AFTER_WRONG,
        free_zone_actions,
    )

    assert ActionType.SUMMARIZE in THRESHOLD_ONLY_ACTIONS
    for pool in (_ALLOWED_AT_START, _ALLOWED_AFTER_CORRECT, _ALLOWED_AFTER_WRONG):
        assert ActionType.SUMMARIZE not in pool, f"summarize 不该出现在 {pool}"
    assert ActionType.SUMMARIZE not in free_zone_actions()

    # 掌握度不足时，模型提议 summarize 必须被拒
    decision = decide(ctx(mastery=0.60, attempt_count=3, score=0.95))
    assert decision.action != ActionType.SUMMARIZE
    result = validate({"action": "summarize"}, decision, allowed_kp_ids=[1])
    assert result.ok is False
    assert result.reject_reason == RejectReason.ACTION_NOT_ALLOWED


def test_summarize_is_allowed_only_via_mastery_rule() -> None:
    """掌握度达标时，动作由 R1 给出，模型选同一个动作应当被接受。"""
    decision = decide(ctx(mastery=0.90))
    assert decision.action == ActionType.SUMMARIZE
    result = validate({"action": "summarize"}, decision, allowed_kp_ids=[1])
    assert result.ok is True


def test_prompts_do_not_contain_thresholds() -> None:
    """阈值只存在于 policy.py。

    如果 Prompt 里写了"连续答对 2 次就加难度"，模型就会开始自己判断难度，
    而它看不到真实状态（只看得到我们喂给它的那部分），判断必然不稳。
    这条断言防止阈值被抄进 Prompt，导致两处定义漂移。

    检查方式刻意分成两类，避免误报：
      - 小数阈值按字面匹配（0.85 / 0.35 这类数字在 Prompt 里没有别的用途）；
      - "连续答对 N 次""mastery >= x"这类句式按正则匹配
        （不能简单查数字 "2"/"3"，那会让"输出 3 个字段"这种正常描述误报）。
    """
    import re
    from pathlib import Path

    decimal_thresholds = [
        str(THRESHOLDS.mastery_threshold),
        str(THRESHOLDS.weak_mastery),
        str(THRESHOLDS.shallow_score),
        str(THRESHOLDS.learn_gain),
        str(THRESHOLDS.wrong_penalty),
    ]
    patterns = [
        re.compile(r"连续\s*(?:答对|正确)\s*\d+\s*次"),
        re.compile(r"连续\s*(?:答错|错误)\s*\d+\s*次"),
        re.compile(r"mastery\s*[<>=≥≤]"),
        re.compile(r"掌握度\s*[<>=≥≤]"),
    ]

    prompts_dir = Path(__file__).resolve().parents[2] / "app" / "agent" / "prompts"
    # 显式列出 P4 用到的 Prompt。用 glob 会漏 —— 它们并不都以 tutor 开头。
    targets = [
        prompts_dir / "tutor_decision.md",
        prompts_dir / "tutor_actions.md",
        prompts_dir / "answer_assessment.md",
    ]
    for path in targets:
        assert path.exists(), f"缺少 Prompt 文件：{path.name}"
        text = path.read_text(encoding="utf-8")
        for number in decimal_thresholds:
            assert number not in text, f"{path.name} 里出现了阈值数字 {number}"
        for pattern in patterns:
            hit = pattern.search(text)
            assert hit is None, f"{path.name} 里出现了阈值句式：{hit.group(0) if hit else ''}"
