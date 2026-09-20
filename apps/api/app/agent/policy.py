"""教学动作策略（Policy）—— P4 的硬约束层。

**本模块是"教学策略"这件事的唯一权威。** 所有阈值都写在这里，
Prompt 里一个数字都没有。LLM 可以提建议，但最终动作由 Policy 说了算。

## 六个动作的规则与优先级

顺序本身就是设计，必须固定下来，否则规则会互相打架：

| # | 条件 | 动作 | 是否可被 LLM 覆盖 |
|---|---|---|---|
| 1 | `mastery >= 0.85` | `summarize` | ❌ 强制 |
| 2 | `consecutive_wrong >= 3` | `easier` | ❌ 强制 |
| 3 | `consecutive_wrong >= 2` | `rephrase` | ❌ 强制 |
| 4 | 本轮答对但浅 | `probe` | ✅ 推荐（自由区） |
| 5 | `consecutive_correct >= 2` | `harder` | ❌ 强制 |
| 6 | 本轮答错且 `mastery < 0.40` | `explain` | ✅ 推荐（自由区） |
| 7 | `attempt_count == 0` | `explain` | ❌ 强制 |
| 8 | 其它 | `probe` | ✅ 推荐（自由区） |

**规则 4 为什么排在规则 5 前面**：一个学生连续答对，但每次都答得很浅，
此时加难度只会让他更懵 —— 先把浅的地方问透才是对的。这条顺序是刻意的，
有专门的测试守着（`test_shallow_correct_beats_correct_streak`）。

**规则 2 为什么排在规则 3 前面**：连错 3 次同时满足"连错 2 次"，
但连错 3 次说明换讲法也没用，该降难度了。谁更靠前谁赢。

## mastery 与 confidence 的分工（需求特别强调的区分）

- `mastery`：**学习状态**。由本模块的 `apply_mastery()` 增量更新，存 `learner_kp_states`。
- `confidence`：**评估质量**。来自 LLM 的结构化输出，存 `answer_evaluations`。

`validate()` 会检查 confidence 是否越界，但**绝不会**把它写进 mastery。
有测试断言这件事（`test_confidence_never_affects_mastery`）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Sequence

from app.models.answer_evaluation import AssessmentLevel, ErrorType
from app.models.learner_kp_state import LearnerStatus
from app.models.message import ActionType, ALL_ACTIONS

# --------------------------------------------------------------------------- #
# 阈值（唯一权威来源）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PolicyThresholds:
    """全部教学阈值。改策略只改这里，Prompt 与前端都不许出现数字。"""

    #: 达到该掌握度 → summarize（收束）
    mastery_threshold: float = 0.85
    #: 低于该掌握度视为"基础薄弱" → explain
    weak_mastery: float = 0.40
    #: 得分低于该值视为"答得浅" → probe
    shallow_score: float = 0.70
    #: 连续答对几次 → harder
    harder_after_correct: int = 2
    #: 连续答错几次 → rephrase（换讲法）
    rephrase_after_wrong: int = 2
    #: 连续答错几次 → easier（降难度）
    easier_after_wrong: int = 3
    #: 答对时的学习增益系数
    learn_gain: float = 0.35
    #: 答错时的回退系数
    wrong_penalty: float = 0.30


#: 全局唯一的阈值实例
THRESHOLDS = PolicyThresholds()

#: 各动作的允许集合（自由区用）。Policy 约束**空间**，LLM 在空间内选。
#:
#: **`summarize` 刻意不出现在任何自由区集合里。** 它是"达成掌握"这个阈值的终端动作，
#: 一旦允许模型自选，模型就会在掌握度还很低时说"这个知识点可以收口了"——
#: 那正是"让 LLM 自己定义阈值"，与需求直接冲突。
#: 实测踩过这个坑：真实模型在 mastery 0.60 时就选了 summarize，把会话提前收束了。
#: `summarize` 现在**只能由 R1（掌握度规则）产生**。
_ALLOWED_AFTER_CORRECT: tuple[str, ...] = (
    ActionType.PROBE,
    ActionType.HARDER,
)
_ALLOWED_AFTER_WRONG: tuple[str, ...] = (
    ActionType.EXPLAIN,
    ActionType.REPHRASE,
    ActionType.EASIER,
    ActionType.PROBE,
)
_ALLOWED_AT_START: tuple[str, ...] = (ActionType.EXPLAIN, ActionType.PROBE)

#: 只允许由 Policy 的阈值规则产生的动作（模型无权自选）
THRESHOLD_ONLY_ACTIONS: frozenset[str] = frozenset({ActionType.SUMMARIZE})


class RuleId(StrEnum):
    """命中的规则标识。写进决策理由，便于演示与排查"为什么选了这个动作"。"""

    MASTERED = "R1_mastered"
    WRONG_STREAK_3 = "R2_wrong_streak_3"
    WRONG_STREAK_2 = "R3_wrong_streak_2"
    SHALLOW_CORRECT = "R4_shallow_correct"
    CORRECT_STREAK_2 = "R5_correct_streak_2"
    WRONG_AND_WEAK = "R6_wrong_and_weak"
    FIRST_CONTACT = "R7_first_contact"
    DEFAULT = "R8_default"


#: 强制规则：命中后 LLM 无权改选。阈值类规则必须在这里，否则"阈值由 Policy 控制"就是空话。
FORCED_RULES: frozenset[str] = frozenset(
    {
        RuleId.MASTERED,
        RuleId.WRONG_STREAK_3,
        RuleId.WRONG_STREAK_2,
        RuleId.CORRECT_STREAK_2,
        RuleId.FIRST_CONTACT,
    }
)


# --------------------------------------------------------------------------- #
# 输入 / 输出
# --------------------------------------------------------------------------- #
@dataclass
class DecisionContext:
    """决策所需的全部信息。

    `is_correct` / `score` / `level` 为 None 表示**本轮还没有评估**
    （比如会话刚开始的第一轮），此时只有连击类与首见类规则可能命中。
    """

    mastery: float = 0.0
    attempt_count: int = 0
    consecutive_correct: int = 0
    consecutive_wrong: int = 0
    is_correct: bool | None = None
    score: float | None = None
    level: str | None = None
    error_type: str | None = None

    @classmethod
    def from_state(cls, state: Any, assessment: Any | None = None) -> DecisionContext:
        """从 LearnerKpState 与（可选的）评估结果构造。"""
        ctx = cls(
            mastery=float(getattr(state, "mastery", 0) or 0),
            attempt_count=int(getattr(state, "attempt_count", 0) or 0),
            consecutive_correct=int(getattr(state, "consecutive_correct", 0) or 0),
            consecutive_wrong=int(getattr(state, "consecutive_wrong", 0) or 0),
            error_type=getattr(state, "last_error_type", None),
        )
        if assessment is not None:
            ctx.is_correct = bool(getattr(assessment, "correct", False))
            ctx.score = float(getattr(assessment, "score", 0) or 0)
            ctx.level = getattr(assessment, "level", None)
            ctx.error_type = getattr(assessment, "error_type", None)
        return ctx

    def snapshot(self) -> dict[str, Any]:
        return {
            "mastery": round(self.mastery, 4),
            "attempt_count": self.attempt_count,
            "consecutive_correct": self.consecutive_correct,
            "consecutive_wrong": self.consecutive_wrong,
            "is_correct": self.is_correct,
            "score": None if self.score is None else round(self.score, 4),
            "level": self.level,
        }


@dataclass
class PolicyDecision:
    """Policy 给出的决策基线。"""

    action: str
    reason: str
    rule: str
    #: True 表示动作被硬阈值锁定，LLM 无权改选
    forced: bool
    #: 允许的动作集合。forced 时只有一个元素。
    allowed: tuple[str, ...] = field(default_factory=tuple)
    #: 推荐置信度（Policy 侧的把握，不是 LLM 的）
    confidence: float = 0.8

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "reason": self.reason,
            "rule": self.rule,
            "forced": self.forced,
            "allowed": list(self.allowed),
            "confidence": self.confidence,
        }


# --------------------------------------------------------------------------- #
# 决策
# --------------------------------------------------------------------------- #
def decide(ctx: DecisionContext, *, thresholds: PolicyThresholds = THRESHOLDS) -> PolicyDecision:
    """按固定优先级求教学动作。纯函数 —— 同样的输入永远同样的输出。"""
    t = thresholds

    # R1 达成掌握 → 收束。放第一位是因为它是终态，不该被任何"继续练习"的规则盖过。
    if ctx.mastery >= t.mastery_threshold:
        return PolicyDecision(
            action=ActionType.SUMMARIZE,
            reason=(
                f"掌握度已达 {ctx.mastery:.2f}（阈值 {t.mastery_threshold}），"
                "该知识点可以收束总结了"
            ),
            rule=RuleId.MASTERED,
            forced=True,
            allowed=(ActionType.SUMMARIZE,),
            confidence=0.95,
        )

    # R2 连错三次 → 降难度。必须排在 R3 之前：连错三次说明换讲法也没用。
    if ctx.consecutive_wrong >= t.easier_after_wrong:
        return PolicyDecision(
            action=ActionType.EASIER,
            reason=(
                f"已连续答错 {ctx.consecutive_wrong} 次（阈值 {t.easier_after_wrong}），"
                "换讲法也没能奏效，退回更基础的表述"
            ),
            rule=RuleId.WRONG_STREAK_3,
            forced=True,
            allowed=(ActionType.EASIER,),
            confidence=0.9,
        )

    # R3 连错两次 → 换讲法（同样内容换个角度讲一遍）
    if ctx.consecutive_wrong >= t.rephrase_after_wrong:
        error_hint = ""
        if ctx.error_type and ctx.error_type != ErrorType.NONE:
            error_hint = f"，错因是 {ctx.error_type}，换讲法时需对症"
        return PolicyDecision(
            action=ActionType.REPHRASE,
            reason=(
                f"已连续答错 {ctx.consecutive_wrong} 次（阈值 {t.rephrase_after_wrong}）"
                f"{error_hint}"
            ),
            rule=RuleId.WRONG_STREAK_2,
            forced=True,
            allowed=(ActionType.REPHRASE,),
            confidence=0.9,
        )

    # R4 答对但浅 → 追问。**刻意排在 R5 之前**：浅答对时加难度只会更懵。
    if _is_shallow_correct(ctx, t):
        return PolicyDecision(
            action=ActionType.PROBE,
            reason=(
                f"答案方向正确但不够深入（得分 {ctx.score:.2f}，"
                f"低于 {t.shallow_score}），先追问把没答透的地方问清"
            ),
            rule=RuleId.SHALLOW_CORRECT,
            forced=False,
            allowed=_ALLOWED_AFTER_CORRECT,
            confidence=0.7,
        )

    # R5 稳定答对 → 加难度
    if ctx.consecutive_correct >= t.harder_after_correct:
        return PolicyDecision(
            action=ActionType.HARDER,
            reason=(
                f"已连续答对 {ctx.consecutive_correct} 次（阈值 {t.harder_after_correct}），"
                "可以提升难度了"
            ),
            rule=RuleId.CORRECT_STREAK_2,
            forced=True,
            allowed=(ActionType.HARDER,),
            confidence=0.9,
        )

    # R6 答错且基础薄弱 → 重新讲解
    if ctx.is_correct is False and ctx.mastery < t.weak_mastery:
        return PolicyDecision(
            action=ActionType.EXPLAIN,
            reason=(
                f"答错且掌握度仅 {ctx.mastery:.2f}（低于 {t.weak_mastery}），"
                "基础还薄弱，需要重新讲解概念"
            ),
            rule=RuleId.WRONG_AND_WEAK,
            forced=False,
            allowed=_ALLOWED_AFTER_WRONG,
            confidence=0.75,
        )

    # R7 初次接触 → 先讲再问
    if ctx.attempt_count == 0:
        return PolicyDecision(
            action=ActionType.EXPLAIN,
            reason="该知识点尚未作答过，先把概念讲清再提问",
            rule=RuleId.FIRST_CONTACT,
            forced=True,
            allowed=(ActionType.EXPLAIN,),
            confidence=0.9,
        )

    # R8 默认 → 追问
    return PolicyDecision(
        action=ActionType.PROBE,
        reason="当前状态没有命中特殊规则，继续追问以推进理解",
        rule=RuleId.DEFAULT,
        forced=False,
        allowed=_ALLOWED_AFTER_CORRECT if ctx.is_correct else _ALLOWED_AFTER_WRONG,
        confidence=0.5,
    )


def _is_shallow_correct(ctx: DecisionContext, t: PolicyThresholds) -> bool:
    """判断"答对了但没答透"。

    两个信号满足其一即可：分数偏低，或评估给出的深度等级是 vague。
    必须有评估结果才可能成立 —— 首轮没有评估时不该命中这条。
    """
    if ctx.is_correct is not True:
        return False
    if ctx.level == AssessmentLevel.VAGUE:
        return True
    return ctx.score is not None and ctx.score < t.shallow_score


# --------------------------------------------------------------------------- #
# LLM 输出校验（最后一道闸门）
# --------------------------------------------------------------------------- #
class RejectReason(StrEnum):
    NOT_A_DICT = "not_a_dict"
    UNKNOWN_ACTION = "unknown_action"
    ACTION_NOT_ALLOWED = "action_not_allowed"
    KP_NOT_IN_SCOPE = "knowledge_point_not_in_scope"
    BAD_CONFIDENCE = "bad_confidence"
    NONE = "none"


@dataclass
class ValidationResult:
    """校验结果。`ok=False` 时调用方必须回退到 `decision.action`。"""

    ok: bool
    action: str
    reason: str
    confidence: float
    reject_reason: str = RejectReason.NONE
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "action": self.action,
            "reason": self.reason,
            "confidence": round(self.confidence, 3),
            "reject_reason": self.reject_reason,
            "detail": self.detail,
        }


def validate(
    proposal: Any,
    decision: PolicyDecision,
    *,
    allowed_kp_ids: Sequence[int],
) -> ValidationResult:
    """校验 LLM 的决策提案。

    **这是"阈值由 Policy 控制"这条约定的执行点。** 无论模型返回什么，
    只要与 Policy 的硬约束冲突，一律丢弃并回退到 Policy 的动作。

    被拒绝时 `ok=False`，调用方用 `decision.action` 兜底 —— 绝不把非法动作传下去。
    """

    def fallback(reject: RejectReason, detail: str) -> ValidationResult:
        return ValidationResult(
            ok=False,
            action=decision.action,
            reason=f"{decision.reason}（模型提案被拒：{detail}）",
            confidence=decision.confidence,
            reject_reason=reject,
            detail=detail,
        )

    if not isinstance(proposal, dict):
        return fallback(RejectReason.NOT_A_DICT, "模型返回的不是 JSON 对象")

    action = str(proposal.get("action") or "").strip().lower()
    if action not in ALL_ACTIONS:
        return fallback(RejectReason.UNKNOWN_ACTION, f"未定义的动作 {action!r}")

    # 强制规则：动作被硬阈值锁定，模型的任何其他选择都无效
    if decision.forced and action != decision.action:
        return fallback(
            RejectReason.ACTION_NOT_ALLOWED,
            f"硬规则 {decision.rule} 已锁定动作为 {decision.action}",
        )

    # 自由区：允许集合内的选择有效
    if action not in decision.allowed:
        return fallback(
            RejectReason.ACTION_NOT_ALLOWED,
            f"动作 {action} 不在允许集合 {list(decision.allowed)} 内",
        )

    # 知识点越权：模型不能决定去教别的知识点
    raw_kp = proposal.get("knowledge_point_id")
    if raw_kp is not None:
        try:
            kp_id = int(raw_kp)
        except (TypeError, ValueError):
            return fallback(RejectReason.KP_NOT_IN_SCOPE, f"知识点 id 非法：{raw_kp!r}")
        if kp_id not in set(int(x) for x in allowed_kp_ids):
            return fallback(
                RejectReason.KP_NOT_IN_SCOPE, f"知识点 {kp_id} 不在本会话范围内"
            )

    # confidence 只做范围校验，**绝不写进 mastery**（两者是不同的东西，见模块说明）
    confidence = decision.confidence
    raw_conf = proposal.get("confidence")
    if raw_conf is not None:
        try:
            parsed = float(raw_conf)
        except (TypeError, ValueError):
            return fallback(RejectReason.BAD_CONFIDENCE, f"confidence 非法：{raw_conf!r}")
        if not 0.0 <= parsed <= 1.0:
            return fallback(RejectReason.BAD_CONFIDENCE, f"confidence 越界：{parsed}")
        confidence = parsed

    reason = str(proposal.get("reason") or "").strip() or decision.reason
    return ValidationResult(
        ok=True,
        action=action,
        reason=reason[:480],
        confidence=confidence,
        reject_reason=RejectReason.NONE,
    )


# --------------------------------------------------------------------------- #
# 状态更新（mastery 公式也在 Policy 里）
# --------------------------------------------------------------------------- #
def apply_mastery(
    mastery: float,
    *,
    correct: bool,
    score: float,
    thresholds: PolicyThresholds = THRESHOLDS,
) -> float:
    """按增量公式更新掌握度。

    - 答对：`+= gain * score * (1 - mastery)` —— 越接近掌握，增益越小，
      渐进逼近 1 而不会一次跳满；
    - 答错：`-= penalty * (1 - score) * mastery` —— 按当前掌握程度比例回退，
      本来就不熟的知识掉得少（没什么可掉的）。

    结果钳制在 [0, 1]。**`score` 是作答质量，`mastery` 是学习状态；
    这里的计算完全不含 `confidence`。**
    """
    t = thresholds
    value = float(mastery or 0.0)
    score = max(0.0, min(1.0, float(score or 0.0)))

    if correct:
        value += t.learn_gain * score * (1.0 - value)
    else:
        value -= t.wrong_penalty * (1.0 - score) * value

    return max(0.0, min(1.0, value))


def next_streaks(
    *,
    correct: bool,
    consecutive_correct: int,
    consecutive_wrong: int,
) -> tuple[int, int]:
    """更新连击计数。答对清零连错，答错清零连对。"""
    if correct:
        return consecutive_correct + 1, 0
    return 0, consecutive_wrong + 1


def status_for(
    *,
    mastery: float,
    attempt_count: int,
    consecutive_wrong: int,
    thresholds: PolicyThresholds = THRESHOLDS,
) -> str:
    """由状态推导标签。供列表与前端展示用，不参与动作决策。"""
    if mastery >= thresholds.mastery_threshold:
        return LearnerStatus.MASTERED
    if consecutive_wrong >= thresholds.easier_after_wrong:
        return LearnerStatus.WEAK
    if attempt_count == 0:
        return LearnerStatus.NEW
    if mastery < thresholds.weak_mastery and consecutive_wrong > 0:
        return LearnerStatus.WEAK
    return LearnerStatus.LEARNING


def thresholds_snapshot() -> dict[str, float]:
    """阈值快照，随 API 返回。前端与文档据此展示当前策略，避免把数字抄两遍。"""
    t = THRESHOLDS
    return {
        "mastery_threshold": t.mastery_threshold,
        "weak_mastery": t.weak_mastery,
        "shallow_score": t.shallow_score,
        "harder_after_correct": t.harder_after_correct,
        "rephrase_after_wrong": t.rephrase_after_wrong,
        "easier_after_wrong": t.easier_after_wrong,
        "learn_gain": t.learn_gain,
        "wrong_penalty": t.wrong_penalty,
    }


#: 规则 → **不含阈值**的情境描述。用来给模型一句"系统为什么倾向这个动作"。
#:
#: 为什么不直接用 `decision.reason`：那是写给人看的，里面带着
#: "连续答错 3 次（阈值 3）""掌握度 0.31（低于 0.4）"这类数字。
#: 把它们塞进 Prompt 等于把阈值交给模型，模型就会开始自己判断难度 ——
#: 而它看不到完整状态，判断必然不稳。「阈值由 Policy 控制」这条约定就破了。
_HINT_BY_RULE: dict[str, str] = {
    RuleId.MASTERED: "这个知识点看起来已经掌握得不错了，适合收束",
    RuleId.WRONG_STREAK_3: "学习者最近连续受挫，直接讲难点恐怕听不进去",
    RuleId.WRONG_STREAK_2: "学习者反复出错，之前的讲法显然没奏效",
    RuleId.SHALLOW_CORRECT: "上次答对了，但只答到表面，没往深处说",
    RuleId.CORRECT_STREAK_2: "学习者最近表现稳定，可以往前推一步",
    RuleId.WRONG_AND_WEAK: "学习者答错了，而且这个知识点的基础还比较薄",
    RuleId.FIRST_CONTACT: "这是学习者第一次接触这个知识点",
    RuleId.DEFAULT: "当前没有特殊情况，需要继续保持推进",
}


def hint_text(decision: PolicyDecision) -> str:
    """给模型看的阈值无关提示。"""
    return _HINT_BY_RULE.get(decision.rule, _HINT_BY_RULE[RuleId.DEFAULT])


def free_zone_actions() -> tuple[str, ...]:
    """自由区里模型可能选到的全部动作（去掉只能由阈值触发的终端动作）。

    供能力探测与测试使用，避免各处硬编码。
    """
    pool = set(_ALLOWED_AFTER_CORRECT) | set(_ALLOWED_AFTER_WRONG) | set(_ALLOWED_AT_START)
    return tuple(sorted(pool - THRESHOLD_ONLY_ACTIONS))
