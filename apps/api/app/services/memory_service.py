"""长期记忆服务（P5）—— 让 Tutor「记得住上一轮」。

四件事，各自都是独立可测的：

| 能力 | 函数 | 性质 |
|---|---|---|
| 简化遗忘曲线 | `review_interval_seconds` / `schedule_next_review` | 纯函数 |
| 待复习队列 | `due_reviews` | 查询 |
| 薄弱点聚合 | `aggregate_weak_points` | 查询 + 排序 |
| 学习画像（偏好讲法） | `refresh_profile` / `style_instruction` | 推导 + 稳定性规则 |
| 首屏看板 | `build_dashboard` | 聚合上面几项 |

## 关于遗忘曲线为什么是"简化"的

真正的遗忘曲线（SM-2 / FSRS）需要每个学习者的大量历史留存数据来拟合参数。
我们现在的作答量还远不够回填，硬套一个公式只会得到看起来精确、实际没有依据的数字。

所以这里用的是**可解释的间隔映射**：掌握得越牢、下次看得越晚；答错了就尽快再见。
每一档间隔都能用一句人话解释清楚，也能被测试钉住。
参数集中在 `ReviewCurve` 里，将来数据够了想换成真实曲线，替换这一个类即可。

## 关于讲法风格为什么必须"存下来"

技术方案的验收是「讲解风格与上次一致」。如果每轮都从最近的作答重新推导，
风格会随最近几次错因来回横跳 —— 学习者会觉得"这个老师每次讲得都不一样"。
所以：**推导一次、存下来、只在证据足够充分时才切换**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.answer_evaluation import AnswerEvaluation, ErrorType
from app.models.knowledge_point import KnowledgePoint
from app.models.learner_kp_state import LearnerKpState, LearnerStatus
from app.models.learner_profile import ExplanationStyle, LearnerProfile, StyleSource
from app.models.session import Session as TutorSession
from app.services.learner_service import DEFAULT_LEARNER_ID

logger = get_logger(__name__)


# --------------------------------------------------------------------------- #
# 简化遗忘曲线
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ReviewCurve:
    """间隔映射表。改策略只改这里。"""

    #: (掌握度上界, 间隔秒数)。按上界从小到大排列，取第一个满足 `mastery < 上界` 的档位。
    tiers: tuple[tuple[float, int], ...] = (
        (0.40, 10 * 60),  # 还不熟 → 10 分钟后再见
        (0.60, 60 * 60),  # 勉强有印象 → 1 小时
        (0.75, 24 * 3600),  # 基本明白 → 明天
        (0.85, 3 * 24 * 3600),  # 比较稳 → 三天后
        (1.01, 7 * 24 * 3600),  # 已掌握 → 一周后
    )
    #: 答错时把间隔乘这个系数 —— 错了就要尽快再见
    wrong_factor: float = 0.25
    #: 连错达到该次数时，间隔固定为最短（明显没懂，别再拖）
    wrong_streak_threshold: int = 2
    #: 间隔下限，避免出现"1 秒后再见"这种没意义的调度
    min_seconds: int = 5 * 60


CURVE = ReviewCurve()


def review_interval_seconds(
    mastery: float,
    *,
    correct: bool,
    consecutive_wrong: int = 0,
    curve: ReviewCurve = CURVE,
) -> int:
    """算出这次作答之后，隔多久该再看到这个知识点。纯函数。

    - 掌握度越高 → 间隔越长（`tiers` 表）
    - 答错 → 间隔按 `wrong_factor` 缩短
    - 连错达到阈值 → 直接压到最短间隔
    """
    base = curve.tiers[-1][1]
    for upper, seconds in curve.tiers:
        if float(mastery) < upper:
            base = seconds
            break

    if not correct:
        if consecutive_wrong >= curve.wrong_streak_threshold:
            return curve.min_seconds
        base = int(base * curve.wrong_factor)

    return max(curve.min_seconds, base)


def schedule_next_review(
    mastery: float,
    *,
    correct: bool,
    consecutive_wrong: int = 0,
    now: datetime | None = None,
    curve: ReviewCurve = CURVE,
) -> datetime:
    """算出 `next_review_at` 的具体时刻。"""
    interval = review_interval_seconds(
        mastery, correct=correct, consecutive_wrong=consecutive_wrong, curve=curve
    )
    return (now or datetime.now()) + timedelta(seconds=interval)


# --------------------------------------------------------------------------- #
# 薄弱点聚合
# --------------------------------------------------------------------------- #
@dataclass
class WeakPoint:
    """一个待加强的知识点。`urgency` 越高越该先看。"""

    knowledge_point_id: int
    title: str
    mastery: float
    status: str
    attempt_count: int
    consecutive_wrong: int
    importance: int
    last_error_type: str | None
    next_review_at: datetime | None
    urgency: float = 0.0
    due: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "knowledge_point_id": self.knowledge_point_id,
            "title": self.title,
            "mastery": round(self.mastery, 3),
            "status": self.status,
            "attempt_count": self.attempt_count,
            "consecutive_wrong": self.consecutive_wrong,
            "importance": self.importance,
            "last_error_type": self.last_error_type,
            "next_review_at": self.next_review_at.isoformat() if self.next_review_at else None,
            "urgency": round(self.urgency, 3),
            "due": self.due,
        }


def compute_urgency(
    *,
    mastery: float,
    importance: int,
    consecutive_wrong: int,
    due: bool,
) -> float:
    """薄弱点的紧迫度。纯函数，便于单独调参。

    三个信号叠加：
      - **掌握度缺口**：`1 - mastery` 是主体（越不熟越紧迫）
      - **重要度**：知识点本身的重要性（1~5 归一化到 0.8~1.2 的倍率）
      - **连错**：连错 2 次以上额外加 0.2 —— 连续出错比单次低分更值得干预
      - **到期**：已过复习时间再加 0.15
    """
    gap = 1.0 - max(0.0, min(1.0, mastery))
    weight = 0.8 + 0.1 * max(1, min(5, importance))
    score = gap * weight
    if consecutive_wrong >= 2:
        score += 0.2
    if due:
        score += 0.15
    return round(score, 4)


def _to_weak_point(
    state: LearnerKpState,
    point: KnowledgePoint | None,
    *,
    now: datetime,
) -> WeakPoint:
    due = bool(state.next_review_at and state.next_review_at <= now)
    importance = int(getattr(point, "importance", 3) or 3)
    return WeakPoint(
        knowledge_point_id=state.knowledge_point_id,
        title=(getattr(point, "title", None) or f"知识点 {state.knowledge_point_id}"),
        mastery=float(state.mastery or 0),
        status=str(state.status or LearnerStatus.NEW),
        attempt_count=int(state.attempt_count or 0),
        consecutive_wrong=int(state.consecutive_wrong or 0),
        importance=importance,
        last_error_type=state.last_error_type,
        next_review_at=state.next_review_at,
        urgency=compute_urgency(
            mastery=float(state.mastery or 0),
            importance=importance,
            consecutive_wrong=int(state.consecutive_wrong or 0),
            due=due,
        ),
        due=due,
    )


def _states_with_points(
    db: Session, learner_id: str, *, only_attempted: bool = True
) -> list[tuple[LearnerKpState, KnowledgePoint | None]]:
    """取该学习者的学习状态，连同知识点信息。"""
    rows = db.execute(
        select(LearnerKpState, KnowledgePoint)
        .outerjoin(KnowledgePoint, KnowledgePoint.id == LearnerKpState.knowledge_point_id)
        .where(LearnerKpState.learner_id == learner_id)
    ).all()
    result: list[tuple[LearnerKpState, KnowledgePoint | None]] = []
    for state, point in rows:
        if only_attempted and int(state.attempt_count or 0) <= 0:
            continue
        result.append((state, point))
    return result


def aggregate_weak_points(
    db: Session,
    *,
    learner_id: str = DEFAULT_LEARNER_ID,
    limit: int = 20,
    include_mastered: bool = False,
    now: datetime | None = None,
) -> list[WeakPoint]:
    """按紧迫度降序给出薄弱点清单。

    **只纳入作答过的知识点** —— 没学过的不叫薄弱，叫未学。
    已掌握的默认排除（它们不该出现在"该加强"的清单里），除非显式要求。

    排序键里带 `knowledge_point_id` 作为次级键，保证**结果稳定** ——
    否则紧迫度相同的几条在两次查询之间可能换序，界面会看起来在跳。
    """
    moment = now or datetime.now()
    points: list[WeakPoint] = []
    for state, point in _states_with_points(db, learner_id):
        if not include_mastered and str(state.status) == LearnerStatus.MASTERED:
            continue
        points.append(_to_weak_point(state, point, now=moment))

    points.sort(key=lambda item: (-item.urgency, item.knowledge_point_id))
    return points[:limit] if limit > 0 else points


def due_reviews(
    db: Session,
    *,
    learner_id: str = DEFAULT_LEARNER_ID,
    limit: int = 20,
    now: datetime | None = None,
) -> list[WeakPoint]:
    """到期待复习的知识点，按过期时间升序（最该看的排最前）。

    "到期"的定义是 `next_review_at <= now`。从没调度过的（`next_review_at` 为空）
    不算到期 —— 那种情况属于"还没开始学"，由薄弱点清单去覆盖。
    """
    moment = now or datetime.now()
    items: list[WeakPoint] = []
    for state, point in _states_with_points(db, learner_id):
        if state.next_review_at is None or state.next_review_at > moment:
            continue
        items.append(_to_weak_point(state, point, now=moment))

    # 过期越久越靠前；同样过期时用 id 保证稳定
    items.sort(key=lambda item: (item.next_review_at or moment, item.knowledge_point_id))
    return items[:limit] if limit > 0 else items


# --------------------------------------------------------------------------- #
# 学习画像（偏好讲法）
# --------------------------------------------------------------------------- #
#: 推导样本量的下限。少于这个数就保持现状 —— 避免"答错一次就换风格"。
MIN_STYLE_EVIDENCE = 3
#: 某一种错因占比达到这个值才认为它"占优"
DOMINANT_SHARE = 0.6

#: 错因 → 人话。回顾提示里要说明"上次是卡在哪"。
#:
#: **措辞必须与前端 `apps/web/src/api/tutor.ts` 的 `ERROR_TYPE_LABEL` 一致** ——
#: 同一个错因在回顾提示里叫"记不牢"、在状态标签里叫"记忆缺口"，
#: 用户会以为是两回事。跨语言没法用测试钉住，只能靠这条注释提醒：改一处要同时改另一处。
ERROR_LABEL: dict[str, str] = {
    ErrorType.CONCEPT_CONFUSION: "概念混淆",
    ErrorType.MEMORY_GAP: "记忆缺口",
    ErrorType.REASONING_BREAK: "推理断裂",
    ErrorType.MISREAD: "看错题意",
    ErrorType.NONE: "理解不到位",
}

#: 错因 → 讲法。这是 P5 的核心映射：**据"为什么错"决定"怎么讲"**。
STYLE_BY_ERROR: dict[str, str] = {
    ErrorType.CONCEPT_CONFUSION: ExplanationStyle.CONTRAST,
    ErrorType.MEMORY_GAP: ExplanationStyle.STRUCTURED,
    ErrorType.REASONING_BREAK: ExplanationStyle.STEPWISE,
    ErrorType.MISREAD: ExplanationStyle.CLARIFY,
}

#: 讲法 → 注入 Prompt 的**具体**指令。刻意不写"请根据画像调整"这种空话。
STYLE_INSTRUCTION: dict[str, str] = {
    ExplanationStyle.BALANCED: (
        "这位学习者还没有表现出明显的偏好，用清晰常规的讲法即可。"
    ),
    ExplanationStyle.CONTRAST: (
        "这位学习者的主要困难是**概念混淆**。讲解时优先把容易混淆的概念并排对比，"
        "明确指出它们的区别与适用边界，不要只单独讲一个概念。"
    ),
    ExplanationStyle.STRUCTURED: (
        "这位学习者的问题是**记不住**。讲解时先给一个可记忆的框架"
        "（分类、步骤或口诀），把零散的点挂到框架上，再展开细节。"
    ),
    ExplanationStyle.STEPWISE: (
        "这位学习者**推理链断了**。讲解时必须把中间步骤一步步补全，"
        "不要跳步 —— 他认为理所当然的地方往往正是断点。"
    ),
    ExplanationStyle.CLARIFY: (
        "这位学习者容易**看错题**。讲解前先用一句话澄清『这道题到底在问什么』，"
        "把关键词圈出来，再进入内容。"
    ),
}

#: 讲法 → 给用户看的中文名
STYLE_LABEL: dict[str, str] = {
    ExplanationStyle.BALANCED: "均衡讲法",
    ExplanationStyle.CONTRAST: "对比式讲法",
    ExplanationStyle.STRUCTURED: "结构化讲法",
    ExplanationStyle.STEPWISE: "分步式讲法",
    ExplanationStyle.CLARIFY: "审题式讲法",
}


def style_instruction(style: str) -> str:
    """讲法指令。给 Prompt 用，也给接口返回，保证两处一致。"""
    return STYLE_INSTRUCTION.get(style, STYLE_INSTRUCTION[ExplanationStyle.BALANCED])


def get_or_create_profile(
    db: Session, *, learner_id: str = DEFAULT_LEARNER_ID
) -> LearnerProfile:
    profile = db.execute(
        select(LearnerProfile).where(LearnerProfile.learner_id == learner_id)
    ).scalars().first()
    if profile is not None:
        return profile

    profile = LearnerProfile(
        learner_id=learner_id,
        preferred_style=ExplanationStyle.BALANCED,
        style_source=StyleSource.DEFAULT,
        style_evidence={},
        stats={},
    )
    db.add(profile)
    db.flush()
    return profile


def error_type_counts(db: Session, *, learner_id: str = DEFAULT_LEARNER_ID) -> dict[str, int]:
    """统计该学习者各类错因的出现次数。

    `answer_evaluations` 表上没有 learner_id（它挂在 session 上），
    所以这里要 join 到 `sessions` —— 多一层 join，但换来的是不用冗余一列且不会不一致。
    """
    rows = db.execute(
        select(AnswerEvaluation.error_type, func.count())
        .join(TutorSession, TutorSession.id == AnswerEvaluation.session_id)
        .where(
            TutorSession.learner_id == learner_id,
            AnswerEvaluation.correct.is_(False),
            AnswerEvaluation.error_type != ErrorType.NONE,
        )
        .group_by(AnswerEvaluation.error_type)
    ).all()
    return {str(error_type): int(count) for error_type, count in rows if error_type}


def derive_style(counts: dict[str, int]) -> tuple[str, str]:
    """从错因分布推导风格。返回 (风格, 说明)。

    证据不足或没有明显占优的错因时返回 `balanced` —— **宁可不说，也不要瞎说**。
    """
    total = sum(counts.values())
    if total < MIN_STYLE_EVIDENCE:
        return ExplanationStyle.BALANCED, f"错因样本仅 {total} 条，不足以判断偏好"

    dominant, count = max(counts.items(), key=lambda kv: kv[1])
    share = count / total
    if share < DOMINANT_SHARE:
        return ExplanationStyle.BALANCED, (
            f"错因分布分散（最多的一类占 {share:.0%}），没有明显偏好"
        )
    style = STYLE_BY_ERROR.get(dominant)
    if style is None:
        return ExplanationStyle.BALANCED, f"错因 {dominant} 暂无对应讲法"
    label = ERROR_LABEL.get(dominant, dominant)
    return style, f"错因以「{label}」为主（{count}/{total} 次）"


def refresh_profile(
    db: Session,
    *,
    learner_id: str = DEFAULT_LEARNER_ID,
    force: bool = False,
) -> LearnerProfile:
    """按错因证据刷新画像。**返回的画像保证已提交。**

    三条稳定性规则（这是"讲法一致"的关键）：

    1. **手动设定的永不被覆盖** —— 用户说了算；
    2. **证据不足时保持现状** —— 不因为多错了一次就换风格；
    3. **只有推导结果与当前不同才切换** —— 相同就只更新证据快照，不动风格。

    `force=True` 时跳过规则 2 的证据门槛（供测试与手工重算用）。
    """
    profile = get_or_create_profile(db, learner_id=learner_id)
    counts = error_type_counts(db, learner_id=learner_id)
    derived, note = derive_style(counts)

    profile.style_evidence = {**counts, "_note": note}
    profile.stats = {**(profile.stats or {}), "error_samples": sum(counts.values())}

    if profile.is_manual:
        logger.debug("画像 %s 为手动设定，跳过自动推导", learner_id)
        db.commit()
        return profile

    enough_evidence = sum(counts.values()) >= MIN_STYLE_EVIDENCE
    if force or enough_evidence:
        if derived != profile.preferred_style:
            logger.info(
                "学习者 %s 的讲法偏好切换：%s → %s（%s）",
                learner_id,
                profile.preferred_style,
                derived,
                note,
            )
            profile.preferred_style = derived
        profile.style_source = (
            StyleSource.DERIVED if derived != ExplanationStyle.BALANCED else profile.style_source
        )

    db.commit()
    return profile


def set_manual_style(
    db: Session,
    style: str,
    *,
    learner_id: str = DEFAULT_LEARNER_ID,
) -> LearnerProfile:
    """手动设定讲法偏好。写入后 `style_source=manual`，自动推导不再覆盖。"""
    if style not in set(ExplanationStyle):
        raise ValueError(f"未知的讲法：{style}")

    profile = get_or_create_profile(db, learner_id=learner_id)
    profile.preferred_style = style
    profile.style_source = StyleSource.MANUAL
    db.commit()
    logger.info("学习者 %s 手动设定讲法为 %s", learner_id, style)
    return profile


# --------------------------------------------------------------------------- #
# 运行时用的记忆上下文
# --------------------------------------------------------------------------- #
@dataclass
class MemoryContext:
    """一轮教学开始前，从长期记忆里捞出来的东西。"""

    learner_id: str
    #: 当前学习者的讲法偏好
    preferred_style: str = ExplanationStyle.BALANCED
    style_source: str = StyleSource.DEFAULT
    #: 该知识点的历史（没有历史时为 None）
    knowledge_state: dict[str, Any] | None = None
    #: 该知识点是否已到复习时间
    is_due: bool = False
    #: 跨知识点的薄弱点（不含当前这个，用于"你之前在别处也吃过这种亏"）
    other_weak_points: list[dict[str, Any]] = field(default_factory=list)
    #: 该知识点的上次错因
    last_error_type: str | None = None
    #: 是否应当在首轮主动回顾（有历史且曾经出错）
    should_recall: bool = False
    #: 主动回顾的提示语，`should_recall` 为真时才有
    recall_note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "learner_id": self.learner_id,
            "preferred_style": self.preferred_style,
            "style_label": STYLE_LABEL.get(self.preferred_style, self.preferred_style),
            "style_source": self.style_source,
            "style_instruction": style_instruction(self.preferred_style),
            "knowledge_state": self.knowledge_state,
            "is_due": self.is_due,
            "last_error_type": self.last_error_type,
            "other_weak_points": self.other_weak_points,
            "should_recall": self.should_recall,
            "recall_note": self.recall_note,
        }




def build_recall_note(state: LearnerKpState, *, is_due: bool) -> str:
    """生成"上次你在这里……"的提示语。由数据拼装，不让模型自由发挥。

    验收要求「Tutor 能**主动提到**上次的薄弱点」—— 主动的前提是这句话
    必须来自真实记录，所以它是拼出来的，不是模型编的。
    """
    attempts = int(state.attempt_count or 0)
    mastery = float(state.mastery or 0)
    wrong_streak = int(state.consecutive_wrong or 0)
    error = ERROR_LABEL.get(str(state.last_error_type or ErrorType.NONE), "理解不到位")

    parts = [f"你上次学过这个知识点，作答 {attempts} 次，掌握度 {mastery:.2f}。"]
    if wrong_streak >= 2:
        parts.append(f"结束时连续答错 {wrong_streak} 次，卡在**{error}**上。")
    elif state.status == LearnerStatus.WEAK:
        parts.append(f"当时的薄弱环节是**{error}**。")
    if is_due:
        parts.append("按复习计划，这个知识点现在正好该回顾了。")
    parts.append("我们先把上次没通的地方捡起来，再往下走。")
    return "".join(parts)


def load_memory(
    db: Session,
    *,
    learner_id: str = DEFAULT_LEARNER_ID,
    knowledge_point_id: int | None = None,
    weak_limit: int = 5,
    now: datetime | None = None,
) -> MemoryContext:
    """装载长期记忆。这是 Runtime 的 `LOAD_MEMORY` 状态的实现。

    刻意**不抛异常**：长期记忆读不到不该让一轮教学开始不了 ——
    退化成"没有记忆"照常教学即可。
    """
    moment = now or datetime.now()
    context = MemoryContext(learner_id=learner_id)

    try:
        profile = refresh_profile(db, learner_id=learner_id)
        context.preferred_style = profile.preferred_style
        context.style_source = profile.style_source
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取学习者画像失败，用默认讲法：%s", exc)
        db.rollback()

    if knowledge_point_id is None:
        return context

    state = db.execute(
        select(LearnerKpState).where(
            LearnerKpState.learner_id == learner_id,
            LearnerKpState.knowledge_point_id == int(knowledge_point_id),
        )
    ).scalars().first()

    if state is None or int(state.attempt_count or 0) <= 0:
        # 没学过这个知识点 —— 没有可回顾的，但薄弱点仍然有意义
        context.other_weak_points = [
            item.as_dict()
            for item in aggregate_weak_points(db, learner_id=learner_id, limit=weak_limit, now=moment)
        ]
        return context

    context.knowledge_state = state.snapshot()
    context.last_error_type = state.last_error_type
    context.is_due = bool(state.next_review_at and state.next_review_at <= moment)
    context.should_recall = True
    context.recall_note = build_recall_note(state, is_due=context.is_due)
    context.other_weak_points = [
        item.as_dict()
        for item in aggregate_weak_points(db, learner_id=learner_id, limit=weak_limit, now=moment)
        if item.knowledge_point_id != int(knowledge_point_id)
    ]
    return context


# --------------------------------------------------------------------------- #
# 首屏看板
# --------------------------------------------------------------------------- #
def mastery_overview(db: Session, *, learner_id: str = DEFAULT_LEARNER_ID) -> dict[str, int]:
    """掌握度概览：各类状态各有多少个。"""
    rows = db.execute(
        select(LearnerKpState.status, func.count())
        .where(LearnerKpState.learner_id == learner_id)
        .group_by(LearnerKpState.status)
    ).all()
    counts = {str(status): int(count) for status, count in rows}

    total_points = int(
        db.execute(select(func.count()).select_from(KnowledgePoint)).scalar_one() or 0
    )
    tracked = sum(counts.values())
    return {
        "knowledge_point_total": total_points,
        "tracked": tracked,
        "untouched": max(0, total_points - tracked),
        "new": counts.get(LearnerStatus.NEW, 0),
        "learning": counts.get(LearnerStatus.LEARNING, 0),
        "weak": counts.get(LearnerStatus.WEAK, 0),
        "mastered": counts.get(LearnerStatus.MASTERED, 0),
    }


def build_dashboard(
    db: Session,
    *,
    learner_id: str = DEFAULT_LEARNER_ID,
    weak_limit: int = 20,
    due_limit: int = 20,
    now: datetime | None = None,
) -> dict[str, Any]:
    """首屏要的全部东西：概览 + 待复习 + 薄弱点 + 画像。"""
    moment = now or datetime.now()
    profile = refresh_profile(db, learner_id=learner_id)
    weak = aggregate_weak_points(db, learner_id=learner_id, limit=weak_limit, now=moment)
    due = due_reviews(db, learner_id=learner_id, limit=due_limit, now=moment)
    overview = mastery_overview(db, learner_id=learner_id)

    # 汇总统计写回画像，下次看板不必重算（也是给用户看的"我的学习情况"）
    profile.stats = {**(profile.stats or {}), "overview": overview, "weak_count": len(weak)}
    db.commit()

    return {
        "learner_id": learner_id,
        "generated_at": moment.isoformat(),
        "overview": overview,
        "profile": {
            "preferred_style": profile.preferred_style,
            "style_label": STYLE_LABEL.get(profile.preferred_style, profile.preferred_style),
            "style_source": profile.style_source,
            "style_instruction": style_instruction(profile.preferred_style),
            "style_evidence": profile.style_evidence or {},
        },
        "due_reviews": [item.as_dict() for item in due],
        "weak_points": [item.as_dict() for item in weak],
        "review_curve": {
            "tiers": [
                {"mastery_below": upper, "interval_seconds": seconds}
                for upper, seconds in CURVE.tiers
            ],
            "wrong_factor": CURVE.wrong_factor,
            "wrong_streak_threshold": CURVE.wrong_streak_threshold,
            "min_seconds": CURVE.min_seconds,
        },
    }


__all__ = [
    "CURVE",
    "DOMINANT_SHARE",
    "ERROR_LABEL",
    "MIN_STYLE_EVIDENCE",
    "STYLE_BY_ERROR",
    "STYLE_INSTRUCTION",
    "STYLE_LABEL",
    "MemoryContext",
    "ReviewCurve",
    "WeakPoint",
    "aggregate_weak_points",
    "build_dashboard",
    "build_recall_note",
    "compute_urgency",
    "derive_style",
    "due_reviews",
    "error_type_counts",
    "get_or_create_profile",
    "load_memory",
    "mastery_overview",
    "refresh_profile",
    "review_interval_seconds",
    "schedule_next_review",
    "set_manual_style",
    "style_instruction",
]
