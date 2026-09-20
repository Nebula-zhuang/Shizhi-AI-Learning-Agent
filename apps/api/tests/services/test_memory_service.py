"""长期记忆服务测试（P5）。

三组断言，对应 P5 的三个难点：

1. **遗忘曲线**：掌握得越牢间隔越长、答错就尽快再见、连错压到最短。
2. **薄弱点与待复习**：只算学过的、按紧迫度排序、到期才算到期。
3. **画像稳定性**：这是"讲法一致"的根 —— 证据不足时不乱切、证据充分才切、
   手动设定永不被覆盖。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from app.models.answer_evaluation import AnswerEvaluation, AssessmentLevel, ErrorType
from app.models.knowledge_point import KnowledgePoint
from app.models.learner_kp_state import LearnerKpState, LearnerStatus
from app.models.learner_profile import ExplanationStyle, LearnerProfile, StyleSource
from app.models.message import ActionType, Message, MessageRole
from app.models.session import Session as TutorSession
from app.services import memory_service as mem
from app.services.learner_service import DEFAULT_LEARNER_ID




# --------------------------------------------------------------------------- #
# 简化遗忘曲线
# --------------------------------------------------------------------------- #
def test_interval_grows_with_mastery() -> None:
    """掌握得越牢，下次看得越晚。"""
    weak = mem.review_interval_seconds(0.30, correct=True)
    mid = mem.review_interval_seconds(0.65, correct=True)
    strong = mem.review_interval_seconds(0.90, correct=True)
    assert weak < mid < strong


def test_interval_matches_declared_tiers() -> None:
    """每一档都落在预期的区间里 —— 防止有人改表时改错一档。"""
    assert mem.review_interval_seconds(0.10, correct=True) == 10 * 60
    assert mem.review_interval_seconds(0.50, correct=True) == 60 * 60
    assert mem.review_interval_seconds(0.70, correct=True) == 24 * 3600
    assert mem.review_interval_seconds(0.80, correct=True) == 3 * 24 * 3600
    assert mem.review_interval_seconds(0.95, correct=True) == 7 * 24 * 3600


def test_wrong_answer_shortens_interval() -> None:
    """答错了就要尽快再见。"""
    right = mem.review_interval_seconds(0.70, correct=True)
    wrong = mem.review_interval_seconds(0.70, correct=False, consecutive_wrong=1)
    assert wrong < right


def test_wrong_streak_pins_to_minimum() -> None:
    """连错达到阈值 → 直接压到最短，不再按比例缩。"""
    one_wrong = mem.review_interval_seconds(0.90, correct=False, consecutive_wrong=1)
    streak = mem.review_interval_seconds(0.90, correct=False, consecutive_wrong=2)
    assert streak == mem.CURVE.min_seconds
    assert streak < one_wrong


def test_interval_never_below_floor() -> None:
    """兜底：任何组合都不能排出"几秒后再见"这种没意义的调度。"""
    for mastery in (0.0, 0.2, 0.5, 0.9, 1.0):
        for streak in (0, 1, 2, 9):
            seconds = mem.review_interval_seconds(
                mastery, correct=False, consecutive_wrong=streak
            )
            assert seconds >= mem.CURVE.min_seconds


def test_schedule_next_review_adds_interval_to_now() -> None:
    now = datetime(2026, 9, 19, 10, 0, 0)
    got = mem.schedule_next_review(0.30, correct=True, now=now)
    assert got == now + timedelta(seconds=10 * 60)


def test_curve_is_deterministic() -> None:
    assert mem.review_interval_seconds(0.55, correct=True) == mem.review_interval_seconds(
        0.55, correct=True
    )


# --------------------------------------------------------------------------- #
# 薄弱点聚合
# --------------------------------------------------------------------------- #
def test_urgency_prefers_lower_mastery() -> None:
    low = mem.compute_urgency(mastery=0.1, importance=3, consecutive_wrong=0, due=False)
    high = mem.compute_urgency(mastery=0.8, importance=3, consecutive_wrong=0, due=False)
    assert low > high


def test_urgency_rewards_importance() -> None:
    important = mem.compute_urgency(mastery=0.3, importance=5, consecutive_wrong=0, due=False)
    minor = mem.compute_urgency(mastery=0.3, importance=1, consecutive_wrong=0, due=False)
    assert important > minor


def test_urgency_adds_for_wrong_streak_and_due() -> None:
    base = mem.compute_urgency(mastery=0.3, importance=3, consecutive_wrong=0, due=False)
    streaked = mem.compute_urgency(mastery=0.3, importance=3, consecutive_wrong=2, due=False)
    due = mem.compute_urgency(mastery=0.3, importance=3, consecutive_wrong=0, due=True)
    assert streaked > base
    assert due > base


def test_weak_points_skip_untouched_knowledge_points(seeded_states) -> None:
    """**没学过的不叫薄弱，叫未学。**"""
    points = mem.aggregate_weak_points(seeded_states["db"], learner_id=seeded_states["learner_id"], limit=20)
    ids = {item.knowledge_point_id for item in points}
    assert seeded_states["untouched_id"] not in ids
    assert seeded_states["weak_id"] in ids


def test_weak_points_exclude_mastered_by_default(seeded_states) -> None:
    points = mem.aggregate_weak_points(seeded_states["db"], learner_id=seeded_states["learner_id"], limit=20)
    assert seeded_states["mastered_id"] not in {item.knowledge_point_id for item in points}

    with_mastered = mem.aggregate_weak_points(
        seeded_states["db"], learner_id=seeded_states["learner_id"], limit=20, include_mastered=True
    )
    assert seeded_states["mastered_id"] in {item.knowledge_point_id for item in with_mastered}


def test_weak_points_sorted_by_urgency(seeded_states) -> None:
    points = mem.aggregate_weak_points(seeded_states["db"], learner_id=seeded_states["learner_id"], limit=20)
    urgencies = [item.urgency for item in points]
    assert urgencies == sorted(urgencies, reverse=True)


def test_weak_points_order_is_stable(seeded_states) -> None:
    """同样紧迫度的排序必须稳定 —— 否则界面每次刷新都在跳。"""
    first = [(i.knowledge_point_id, i.urgency) for i in mem.aggregate_weak_points(seeded_states["db"], learner_id=seeded_states["learner_id"])]
    second = [(i.knowledge_point_id, i.urgency) for i in mem.aggregate_weak_points(seeded_states["db"], learner_id=seeded_states["learner_id"])]
    assert first == second


def test_weak_points_respect_limit(seeded_states) -> None:
    assert len(mem.aggregate_weak_points(seeded_states["db"], learner_id=seeded_states["learner_id"], limit=1)) == 1


# --------------------------------------------------------------------------- #
# 待复习
# --------------------------------------------------------------------------- #
def test_due_reviews_only_returns_overdue(seeded_states) -> None:
    now = seeded_states["now"]
    due = mem.due_reviews(
        seeded_states["db"], learner_id=seeded_states["learner_id"], now=now
    )
    ids = {item.knowledge_point_id for item in due}
    assert seeded_states["overdue_id"] in ids
    assert seeded_states["future_id"] not in ids, "还没到期的不能出现"


def test_due_reviews_skip_never_scheduled(seeded_states) -> None:
    """从没排过复习的不算"到期" —— 那是"还没开始学"，归薄弱点清单管。"""
    due = mem.due_reviews(seeded_states["db"], learner_id=seeded_states["learner_id"], now=seeded_states["now"])
    assert seeded_states["untouched_id"] not in {item.knowledge_point_id for item in due}


def test_due_reviews_sorted_by_overdue_time(seeded_states) -> None:
    due = mem.due_reviews(seeded_states["db"], learner_id=seeded_states["learner_id"], now=seeded_states["now"])
    times = [item.next_review_at for item in due]
    assert times == sorted(times)


def test_due_reviews_marks_due_flag(seeded_states) -> None:
    for item in mem.due_reviews(seeded_states["db"], learner_id=seeded_states["learner_id"], now=seeded_states["now"]):
        assert item.due is True


# --------------------------------------------------------------------------- #
# 画像推导
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("error_type", "expected"),
    [
        (ErrorType.CONCEPT_CONFUSION, ExplanationStyle.CONTRAST),
        (ErrorType.MEMORY_GAP, ExplanationStyle.STRUCTURED),
        (ErrorType.REASONING_BREAK, ExplanationStyle.STEPWISE),
        (ErrorType.MISREAD, ExplanationStyle.CLARIFY),
    ],
)
def test_style_derivation_per_error_type(error_type: str, expected: str) -> None:
    """**据"为什么错"决定"怎么讲"** —— 这是 P5 的核心映射。"""
    style, note = mem.derive_style({error_type: 5})
    assert style == expected
    assert note


def test_style_derivation_needs_enough_samples() -> None:
    """样本不足就老实说"判断不了"，而不是硬给一个风格。"""
    style, note = mem.derive_style({ErrorType.CONCEPT_CONFUSION: 1})
    assert style == ExplanationStyle.BALANCED
    assert "不足以判断" in note


def test_style_derivation_rejects_scattered_errors() -> None:
    """错因分散说明没有单一偏好，别硬套。"""
    style, _ = mem.derive_style(
        {
            ErrorType.CONCEPT_CONFUSION: 3,
            ErrorType.MEMORY_GAP: 3,
            ErrorType.MISREAD: 2,
        }
    )
    assert style == ExplanationStyle.BALANCED


def test_style_derivation_accepts_dominant_error() -> None:
    style, _ = mem.derive_style({ErrorType.CONCEPT_CONFUSION: 7, ErrorType.MEMORY_GAP: 3})
    assert style == ExplanationStyle.CONTRAST


def test_every_style_has_an_instruction() -> None:
    """每个风格都必须有对应的 Prompt 指令 —— 漏一个就会注入空话。"""
    for style in ExplanationStyle:
        text = mem.style_instruction(style)
        assert text and len(text) > 10
        assert "根据画像" not in text, "指令要具体，不能是空话"
    # 不同风格的指令必须不同，否则等于没区分
    texts = {mem.style_instruction(s) for s in ExplanationStyle}
    assert len(texts) == len(set(ExplanationStyle))


# --------------------------------------------------------------------------- #
# 画像稳定性（"讲法一致"的根）
# --------------------------------------------------------------------------- #
def test_profile_starts_balanced(seeded_states) -> None:
    profile = mem.get_or_create_profile(seeded_states["db"], learner_id=seeded_states["learner_id"])
    assert profile.preferred_style == ExplanationStyle.BALANCED
    assert profile.style_source == StyleSource.DEFAULT


def test_profile_switches_when_evidence_is_dominant(seeded_states) -> None:
    db = seeded_states["db"]
    seed_errors(db, seeded_states["session_id"], {ErrorType.CONCEPT_CONFUSION: 5}, seeded_states["weak_id"])

    profile = mem.refresh_profile(db, learner_id=seeded_states["learner_id"])
    assert profile.preferred_style == ExplanationStyle.CONTRAST
    assert profile.style_source == StyleSource.DERIVED


def test_profile_keeps_style_when_evidence_is_thin(seeded_states) -> None:
    """**稳定性规则**：证据不足时保持现状，不因为多错一次就换风格。

    技术方案验收要求"讲解风格与上次一致"。如果每轮都按最近几次错因重推，
    风格会来回横跳，学习者只会觉得"这老师每次讲得都不一样"。
    """
    db = seeded_states["db"]
    seed_errors(db, seeded_states["session_id"], {ErrorType.CONCEPT_CONFUSION: 5}, seeded_states["weak_id"])
    mem.refresh_profile(db, learner_id=seeded_states["learner_id"])  # 先建立对比式

    # 再来 2 条别的错因 —— 总数 7 但不足 3 条、且不占优，不该切
    seed_errors(db, seeded_states["session_id"], {ErrorType.MEMORY_GAP: 2}, seeded_states["weak_id"])
    profile = mem.refresh_profile(db, learner_id=seeded_states["learner_id"])

    assert profile.preferred_style == ExplanationStyle.CONTRAST, "证据不足时不该切换"


def test_profile_stays_stable_across_repeated_refresh(seeded_states) -> None:
    """反复刷新同一份证据，风格必须纹丝不动。"""
    db = seeded_states["db"]
    seed_errors(db, seeded_states["session_id"], {ErrorType.REASONING_BREAK: 6}, seeded_states["weak_id"])
    first = mem.refresh_profile(db, learner_id=seeded_states["learner_id"]).preferred_style
    for _ in range(5):
        assert mem.refresh_profile(db, learner_id=seeded_states["learner_id"]).preferred_style == first
    assert first == ExplanationStyle.STEPWISE


def test_manual_style_is_never_overridden(seeded_states) -> None:
    """手动设定过的讲法，自动推导永不能改 —— 用户说了算。"""
    db = seeded_states["db"]
    mem.set_manual_style(db, ExplanationStyle.CLARIFY, learner_id=seeded_states["learner_id"])
    seed_errors(db, seeded_states["session_id"], {ErrorType.CONCEPT_CONFUSION: 9}, seeded_states["weak_id"])

    profile = mem.refresh_profile(db, learner_id=seeded_states["learner_id"])
    assert profile.preferred_style == ExplanationStyle.CLARIFY
    assert profile.style_source == StyleSource.MANUAL


def test_manual_style_rejects_unknown_value(seeded_states) -> None:
    with pytest.raises(ValueError):
        mem.set_manual_style(seeded_states["db"], "随便编一个", learner_id=seeded_states["learner_id"])


def test_profile_single_row_per_learner(seeded_states) -> None:
    db = seeded_states["db"]
    a = mem.get_or_create_profile(db, learner_id=seeded_states["learner_id"])
    b = mem.get_or_create_profile(db, learner_id=seeded_states["learner_id"])
    assert a.id == b.id


# --------------------------------------------------------------------------- #
# 记忆上下文
# --------------------------------------------------------------------------- #
def test_recall_note_mentions_error_and_count() -> None:
    """回顾提示必须说清"上次是卡在哪"，而且是拼出来的、不是模型编的。"""
    state = LearnerKpState(
        learner_id=DEFAULT_LEARNER_ID,
        knowledge_point_id=1,
        mastery=0.2,
        attempt_count=3,
        consecutive_wrong=2,
        last_error_type=ErrorType.CONCEPT_CONFUSION,
        status=LearnerStatus.WEAK,
    )
    note = mem.build_recall_note(state, is_due=True)
    assert "概念混淆" in note
    assert "连续答错 2 次" in note
    assert "该回顾了" in note


def test_load_memory_on_fresh_kp_has_no_recall(seeded_states) -> None:
    """没学过的知识点不该有"回顾上次" —— 那是无中生有。"""
    context = mem.load_memory(
        seeded_states["db"], learner_id=seeded_states["learner_id"],
        knowledge_point_id=seeded_states["untouched_id"],
    )
    assert context.should_recall is False
    assert context.recall_note == ""
    # 但跨知识点的薄弱点仍然有意义
    assert isinstance(context.other_weak_points, list)


def test_load_memory_on_known_kp_recalls(seeded_states) -> None:
    context = mem.load_memory(
        seeded_states["db"], learner_id=seeded_states["learner_id"],
        knowledge_point_id=seeded_states["weak_id"],
    )
    assert context.should_recall is True
    assert context.recall_note
    assert context.knowledge_state is not None
    assert context.last_error_type == ErrorType.CONCEPT_CONFUSION


def test_load_memory_excludes_current_kp_from_other_weak(seeded_states) -> None:
    """"别的薄弱点"里不该出现当前这个知识点。"""
    context = mem.load_memory(
        seeded_states["db"], learner_id=seeded_states["learner_id"],
        knowledge_point_id=seeded_states["weak_id"],
    )
    assert seeded_states["weak_id"] not in {
        item["knowledge_point_id"] for item in context.other_weak_points
    }


def test_load_memory_carries_style(seeded_states) -> None:
    db = seeded_states["db"]
    seed_errors(db, seeded_states["session_id"], {ErrorType.MISREAD: 5}, seeded_states["weak_id"])
    mem.refresh_profile(db, learner_id=seeded_states["learner_id"])

    context = mem.load_memory(db, learner_id=seeded_states["learner_id"], knowledge_point_id=seeded_states["weak_id"])
    assert context.preferred_style == ExplanationStyle.CLARIFY
    assert "看错题" in mem.style_instruction(context.preferred_style)


def test_load_memory_never_raises_on_broken_db(seeded_states, monkeypatch) -> None:
    """读不到记忆不该让一轮教学开始不了。"""
    monkeypatch.setattr(
        mem, "refresh_profile", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("坏了"))
    )
    context = mem.load_memory(
        seeded_states["db"], learner_id=seeded_states["learner_id"],
        knowledge_point_id=seeded_states["weak_id"],
    )
    assert context.preferred_style == ExplanationStyle.BALANCED, "失败要退化成默认讲法"


# --------------------------------------------------------------------------- #
# 看板
# --------------------------------------------------------------------------- #
def test_dashboard_shape(seeded_states) -> None:
    data = mem.build_dashboard(seeded_states["db"], learner_id=seeded_states["learner_id"])
    for key in ("overview", "profile", "due_reviews", "weak_points", "review_curve"):
        assert key in data, f"看板缺少 {key}"
    assert data["overview"]["tracked"] >= 1
    assert data["profile"]["style_label"]
    assert data["review_curve"]["tiers"]


def test_mastery_overview_counts(seeded_states) -> None:
    overview = mem.mastery_overview(seeded_states["db"], learner_id=seeded_states["learner_id"])
    assert overview["mastered"] >= 1
    assert overview["weak"] >= 1
    assert overview["untouched"] >= 1
    assert overview["knowledge_point_total"] == (
        overview["tracked"] + overview["untouched"]
    )


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def seed_errors(db, session_id: int, counts: dict[str, int], kp_id: int) -> None:
    """往会话里塞作答评估记录，用于驱动画像推导。

    `kp_id` 必须是**真实存在**的知识点：`answer_evaluations.knowledge_point_id`
    有外键约束，硬编码一个假 id 会直接撞约束（第一次写时就踩到了）。
    """
    for error_type, count in counts.items():
        for _ in range(count):
            db.add(
                AnswerEvaluation(
                    session_id=session_id,
                    knowledge_point_id=kp_id,
                    question="q",
                    user_answer="a",
                    correct=False,
                    score=0.2,
                    confidence=0.8,
                    level=AssessmentLevel.NOT_MASTERED,
                    error_type=error_type,
                    feedback="f",
                    engine="test",
                )
            )
    db.commit()


@pytest.fixture()
def seeded_states(tutor_kp):
    """准备一份有各种状态的学习数据。

    刻意覆盖五种情形：薄弱 / 已掌握 / 到期未复习 / 未到期 / 从未学过 ——
    聚合与筛选逻辑的边界都在这里面。
    """
    from app.db.session import SessionLocal

    db = SessionLocal()
    now = datetime(2026, 9, 19, 12, 0, 0)
    # **每个夹具用独立的学习者标识**：画像的错因统计是按 learner 全局汇总的
    # （这正是设计意图 —— 画像必须跨会话），所以共享默认学习者会让测试读到我
    # 历次实验留下的真实数据，断言就飘了。独立标识既保证隔离，又不动真实数据。
    learner_id = f"test-mem-{uuid4().hex[:10]}"
    document_id = tutor_kp["document_id"]

    # 再补几个知识点
    extra_ids: list[int] = []
    for index, title in enumerate(["死锁的必要条件", "进程的状态转换", "页面置换算法"]):
        point = KnowledgePoint(
            document_id=document_id,
            title=title,
            title_norm=title,
            summary=f"{title}的摘要",
            details=f"{title}的讲解",
            key_points=["要点"],
            difficulty=3,
            importance=4 if index == 0 else 2,
            heading_path=["第三章"],
            source_chunk_indexes=[0],
            source_pages=[1],
            order_index=index + 1,
        )
        db.add(point)
        db.flush()
        extra_ids.append(point.id)

    weak_id = tutor_kp["kp_id"]
    overdue_id, future_id, mastered_id = extra_ids[0], extra_ids[1], extra_ids[2]
    untouched_id = extra_ids[2]  # 先占位，稍后改成"没有状态"的那个

    session = TutorSession(
        learner_id=learner_id,
        document_id=document_id,
        knowledge_point_id=weak_id,
        title="测试会话",
    )
    db.add(session)
    db.flush()

    def state(kp_id: int, **kwargs) -> LearnerKpState:
        item = LearnerKpState(
            learner_id=learner_id,
            knowledge_point_id=kp_id,
            **kwargs,
        )
        db.add(item)
        return item

    # 薄弱：连错两次、掌握度低
    state(
        weak_id,
        mastery=0.20,
        attempt_count=3,
        correct_count=1,
        consecutive_wrong=2,
        last_error_type=ErrorType.CONCEPT_CONFUSION,
        status=LearnerStatus.WEAK,
        next_review_at=now - timedelta(hours=2),
    )
    # 到期未复习：掌握度一般但已过期
    state(
        overdue_id,
        mastery=0.55,
        attempt_count=2,
        correct_count=1,
        consecutive_wrong=0,
        status=LearnerStatus.LEARNING,
        next_review_at=now - timedelta(hours=1),
    )
    # 未到期
    state(
        future_id,
        mastery=0.60,
        attempt_count=2,
        correct_count=2,
        consecutive_correct=2,
        status=LearnerStatus.LEARNING,
        next_review_at=now + timedelta(days=3),
    )
    # 已掌握
    state(
        mastered_id,
        mastery=0.90,
        attempt_count=6,
        correct_count=6,
        consecutive_correct=4,
        status=LearnerStatus.MASTERED,
        next_review_at=now + timedelta(days=7),
    )

    db.commit()

    # 再造一个"从未学过"的知识点
    untouched = KnowledgePoint(
        document_id=document_id,
        title="从未学过的知识点",
        title_norm="从未学过的知识点",
        summary="s",
        details="d",
        key_points=[],
        difficulty=3,
        importance=3,
        heading_path=["第三章"],
        source_chunk_indexes=[0],
        source_pages=[1],
        order_index=9,
    )
    db.add(untouched)
    db.commit()
    untouched_id = untouched.id

    yield {
        "db": db,
        "now": now,
        "learner_id": learner_id,
        "session_id": session.id,
        "weak_id": weak_id,
        "overdue_id": overdue_id,
        "future_id": future_id,
        "mastered_id": mastered_id,
        "untouched_id": untouched_id,
    }

    # 清理：先删状态与画像，再删知识点（FK 顺序）
    db.rollback()
    db.query(LearnerKpState).filter(LearnerKpState.learner_id == learner_id).delete()
    db.query(LearnerProfile).filter(LearnerProfile.learner_id == learner_id).delete()
    for item in db.query(TutorSession).filter(TutorSession.id == session.id).all():
        db.delete(item)
    db.commit()
    for kp_id in [*extra_ids, untouched_id]:
        point = db.get(KnowledgePoint, kp_id)
        if point is not None:
            db.delete(point)
    db.commit()
    db.close()
