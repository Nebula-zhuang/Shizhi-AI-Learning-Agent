"""Tutor 长期记忆的运行时测试（P5）。

**这个文件承载 P5 的验收标准。** 技术方案的原话是：

> 关闭浏览器重开会话，Tutor 能主动提到上次的薄弱点并优先复习，讲解风格与上次一致。

拆成三条可测的断言，分别对应下面三个测试：

| 验收点 | 测试 |
|---|---|
| 主动提到上次的薄弱点 | `test_second_session_recalls_previous_weak_point` |
| 优先复习（到期信号进入决策） | `test_recall_note_marks_due_for_review` |
| 讲解风格与上次一致 | `test_style_is_consistent_across_sessions` |
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import pytest_asyncio

from app.agent.runtime import RuntimeState, TutorRuntime
from app.db.session import SessionLocal
from app.models.answer_evaluation import ErrorType
from app.models.knowledge_point import KnowledgePoint
from app.models.learner_kp_state import LearnerKpState, LearnerStatus
from app.models.learner_profile import ExplanationStyle
from app.models.message import Message
from app.rag.embedding import MockEmbeddingProvider
from app.services import learner_service, memory_service as mem

from tests.conftest import ScriptedLLM


def runtime(db, llm, learner_id: str, **kwargs) -> TutorRuntime:
    return TutorRuntime(
        db, llm=llm, embedder=MockEmbeddingProvider(), learner_id=learner_id, **kwargs
    )


@pytest_asyncio.fixture()
async def learner(tutor_kp):
    """一个独立学习者 + 一个知识点。每个用例用自己的标识，互不干扰。"""
    from uuid import uuid4

    learner_id = f"test-p5-{uuid4().hex[:10]}"
    yield {"learner_id": learner_id, "kp_id": tutor_kp["kp_id"], "document_id": tutor_kp["document_id"]}

    with SessionLocal() as db:
        db.query(LearnerKpState).filter(LearnerKpState.learner_id == learner_id).delete()
        from app.models.learner_profile import LearnerProfile

        db.query(LearnerProfile).filter(LearnerProfile.learner_id == learner_id).delete()
        from app.models.session import Session as TutorSession

        for item in db.query(TutorSession).filter(TutorSession.learner_id == learner_id).all():
            db.delete(item)
        db.commit()


async def start(learner: dict, llm: ScriptedLLM):
    with SessionLocal() as db:
        return await runtime(db, llm, learner["learner_id"]).start(
            knowledge_point_id=learner["kp_id"]
        )


async def answer(session_id: int, text: str, llm: ScriptedLLM, learner: dict):
    with SessionLocal() as db:
        return await runtime(db, llm, learner["learner_id"]).submit_answer(
            session_id=session_id, user_answer=text
        )


def force_due(learner_id: str, kp_id: int, *, hours_ago: int = 3) -> None:
    """把事情伪装成"已经过了一段时间"。

    真正跑一段遗忘曲线要等 5 分钟以上，测试里不现实。
    直接把 `next_review_at` 拨到过去，等价于"用户关了浏览器、隔天再回来"。
    """
    with SessionLocal() as db:
        state = learner_service.get_learner_state(db, kp_id, learner_id=learner_id)
        assert state is not None
        state.next_review_at = datetime.now() - timedelta(hours=hours_ago)
        db.commit()


# --------------------------------------------------------------------------- #
# 状态机
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_load_memory_is_in_trace(learner) -> None:
    """`LOAD_MEMORY` 必须出现在状态轨迹里，且排在 RETRIEVE 之前 ——
    讲法偏好要跟着后面的决策与内容生成一路传下去。"""
    turn = await start(learner, ScriptedLLM())
    assert RuntimeState.LOAD_MEMORY.value in turn.trace
    assert turn.trace.index(RuntimeState.LOAD_MEMORY.value) < turn.trace.index(
        RuntimeState.RETRIEVE.value
    )


@pytest.mark.asyncio
async def test_fresh_knowledge_point_has_no_recall(learner) -> None:
    """第一次学这个知识点，没有什么可回顾的 —— 不能无中生有。"""
    turn = await start(learner, ScriptedLLM())
    assert turn.memory["recalled"] is False
    assert turn.memory["recall_note"] == ""
    assert turn.memory["should_recall"] is False


# --------------------------------------------------------------------------- #
# 验收 1：主动提到上次的薄弱点
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_second_session_recalls_previous_weak_point(learner) -> None:
    """**P5 验收核心**：会话一留下薄弱点 → 会话二首轮主动提到它。

    这里刻意用两个**不同的会话**（而不是同一会话的下一轮）——
    验收要求的是"关闭浏览器重开会话"，也就是跨会话。
    """
    kp_id = learner["kp_id"]

    # ---------- 会话一：连错两次，留下薄弱记录 ----------
    first = await start(learner, ScriptedLLM())
    wrong_llm = ScriptedLLM([ScriptedLLM.wrong(), ScriptedLLM.wrong()])
    await answer(first.session_id, "第一次答错", wrong_llm, learner)
    second = await answer(first.session_id, "第二次答错", wrong_llm, learner)

    assert second.state_after["consecutive_wrong"] == 2
    assert second.state_after["status"] == LearnerStatus.WEAK

    # 模拟"过了一段时间再回来"
    force_due(learner["learner_id"], kp_id)

    # ---------- 会话二：全新会话，应当主动回顾 ----------
    fresh_llm = ScriptedLLM()
    third = await start(learner, fresh_llm)

    assert third.session_id != first.session_id, "应当是一个新会话"
    assert third.memory["recalled"] is True, "新会话首轮必须主动回顾"
    assert third.memory["recall_note"], "回顾提示不能为空"

    note = third.memory["recall_note"]
    assert "连续答错 2 次" in note, f"要提到上次连错的次数，实际：{note}"
    assert "概念混淆" in note, f"要提到上次的错因，实际：{note}"
    assert note in third.content, "回顾提示必须出现在发给学习者的正文里"
    assert third.memory["is_due"] is True
    assert "该回顾了" in note, "到期时应当点明该复习了"


@pytest.mark.asyncio
async def test_recall_only_happens_on_first_turn(learner) -> None:
    """只在首轮回顾。后续轮次学习者刚做完题、注意力在当下，
    每轮都提"上次"会变成噪音。"""
    first_llm = ScriptedLLM([ScriptedLLM.wrong(), ScriptedLLM.wrong()])
    session = await start(learner, ScriptedLLM())
    await answer(session.session_id, "错 1", first_llm, learner)
    await answer(session.session_id, "错 2", first_llm, learner)
    force_due(learner["learner_id"], learner["kp_id"])

    fresh = await start(learner, ScriptedLLM())
    assert fresh.memory["recalled"] is True

    # 新会话的第二次出题（仍属于同一会话）不该再提
    follow_up = await answer(fresh.session_id, "这次答对", ScriptedLLM([ScriptedLLM.correct()]), learner)
    assert follow_up.memory["recalled"] is False
    assert "上次" not in follow_up.content


@pytest.mark.asyncio
async def test_recall_reflects_mastered_state(learner) -> None:
    """已经掌握的知识点再开会话，回顾语不该说"卡在某处"。"""
    session = await start(learner, ScriptedLLM())
    good = ScriptedLLM([ScriptedLLM.correct()] * 8)
    for index in range(8):
        turn = await answer(session.session_id, f"第 {index + 1} 次全对", good, learner)
        if turn.action == "summarize":
            break

    with SessionLocal() as db:
        kp_id = learner["kp_id"]
        state = learner_service.get_learner_state(db, kp_id, learner_id=learner["learner_id"])
        assert float(state.mastery) >= mem.CURVE.tiers[-2][0] or state.status == LearnerStatus.MASTERED

    fresh = await start(learner, ScriptedLLM())
    assert fresh.memory["recalled"] is True, "学过就该回顾"
    assert "连续答错" not in fresh.memory["recall_note"], "已经掌握了就别再提连错"


# --------------------------------------------------------------------------- #
# 验收 2：到期信号进入决策
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_recall_note_marks_due_for_review(learner) -> None:
    """到期的知识点，回顾提示里要点明"该复习了" —— 这就是"优先复习"的表达。"""
    session = await start(learner, ScriptedLLM())
    await answer(session.session_id, "答错一次", ScriptedLLM([ScriptedLLM.wrong()]), learner)

    # 未到期：不得到期提示
    not_due = await start(learner, ScriptedLLM())
    assert not_due.memory["is_due"] is False
    assert "该回顾了" not in not_due.memory["recall_note"]

    force_due(learner["learner_id"], learner["kp_id"])
    due = await start(learner, ScriptedLLM())
    assert due.memory["is_due"] is True
    assert "该回顾了" in due.memory["recall_note"]


@pytest.mark.asyncio
async def test_due_review_appears_in_dashboard(learner) -> None:
    session = await start(learner, ScriptedLLM())
    await answer(session.session_id, "答错", ScriptedLLM([ScriptedLLM.wrong()]), learner)
    force_due(learner["learner_id"], learner["kp_id"])

    with SessionLocal() as db:
        data = mem.build_dashboard(db, learner_id=learner["learner_id"])
    assert any(item["knowledge_point_id"] == learner["kp_id"] for item in data["due_reviews"])
    assert any(item["knowledge_point_id"] == learner["kp_id"] for item in data["weak_points"])


# --------------------------------------------------------------------------- #
# 验收 3：讲解风格与上次一致
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_style_is_consistent_across_sessions(learner) -> None:
    """**P5 验收核心**：跨会话的讲法必须一致。

    会话一里通过**真实作答**积累出错因证据、推导出偏好，
    会话二必须读到**同一份**偏好、注入**同一段**指令。

    这条守着"画像必须存下来而不是每轮重推"这个设计 ——
    如果每轮重推，风格会随最近几次错因而来回横跳，学习者只会觉得
    "这老师每次讲得都不一样"。
    """
    learner_id = learner["learner_id"]

    # ---------- 会话一：连错几次，其中三次是概念混淆 ----------
    first_llm = ScriptedLLM([ScriptedLLM.wrong()] * 3)
    session = await start(learner, ScriptedLLM())
    for index in range(3):
        await answer(session.session_id, f"第 {index + 1} 次答错", first_llm, learner)

    with SessionLocal() as db:
        profile = mem.refresh_profile(db, learner_id=learner_id)
        assert profile.preferred_style == ExplanationStyle.CONTRAST, (
            f"三次概念混淆应当推出对比式，实际 {profile.preferred_style}"
        )

    contrast_text = mem.style_instruction(ExplanationStyle.CONTRAST)

    # ---------- 会话二：全新会话，讲法必须一致 ----------
    second_llm = ScriptedLLM()
    turn = await start(learner, second_llm)

    assert turn.memory["preferred_style"] == ExplanationStyle.CONTRAST
    assert turn.memory["style_instruction"] == contrast_text, (
        "接口返回的指令必须与注入 Prompt 的逐字一致，不能是两套说法"
    )

    prompts = second_llm.prompts_of("decision") + second_llm.prompts_of("content")
    assert prompts, "应当有过决策或内容生成调用"
    assert any(contrast_text in text for text in prompts), "会话二应注入与上次相同的讲法指令"


@pytest.mark.asyncio
async def test_style_survives_repeated_sessions(learner) -> None:
    """连开三次会话，讲法一次都不能变 —— 这是"一致"的更强断言。"""
    learner_id = learner["learner_id"]
    first_llm = ScriptedLLM([ScriptedLLM.wrong()] * 3)
    session = await start(learner, ScriptedLLM())
    for index in range(3):
        await answer(session.session_id, f"错 {index}", first_llm, learner)

    with SessionLocal() as db:
        mem.refresh_profile(db, learner_id=learner_id)

    styles = []
    for _ in range(3):
        turn = await start(learner, ScriptedLLM())
        styles.append(turn.memory["preferred_style"])
    assert len(set(styles)) == 1, f"讲法在多次会话间发生了漂移：{styles}"


@pytest.mark.asyncio
async def test_style_instruction_reaches_both_prompts(learner) -> None:
    """讲法偏好要同时进**决策**与**内容生成**两个 Prompt。

    只进一个不够：决策决定"用哪个动作"，内容决定"怎么讲"，
    两者都要知道这个人的偏好才算真的生效。
    """
    with SessionLocal() as db:
        mem.set_manual_style(db, ExplanationStyle.STRUCTURED, learner_id=learner["learner_id"])

    llm = ScriptedLLM()
    session = await start(learner, llm)
    await answer(session.session_id, "回答", ScriptedLLM([ScriptedLLM.correct()]), learner)

    text = mem.style_instruction(ExplanationStyle.STRUCTURED)
    decision_prompts = llm.prompts_of("decision")
    content_prompts = llm.prompts_of("content")
    assert decision_prompts and any(text in p for p in decision_prompts), "决策 Prompt 应含讲法"
    assert content_prompts and any(text in p for p in content_prompts), "内容 Prompt 应含讲法"


@pytest.mark.asyncio
async def test_default_style_still_injects_something(learner) -> None:
    """没有偏好时也要给一句明确的"用常规讲法"，而不是留个空占位符。"""
    llm = ScriptedLLM()
    await start(learner, llm)
    prompts = llm.prompts_of("decision")
    assert prompts
    assert "{{style_instruction}}" not in prompts[0], "占位符必须被替换掉"


# --------------------------------------------------------------------------- #
# 记忆不拖垮教学
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_teaching_continues_when_memory_broken(learner, monkeypatch) -> None:
    """长期记忆读不到，教学也要照常开始 —— 记忆是增强项，不是前置条件。"""
    monkeypatch.setattr(
        mem, "load_memory", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("记忆坏了"))
    )
    turn = await start(learner, ScriptedLLM())

    assert turn.content.strip(), "记忆坏了也要能讲课"
    assert turn.memory["recalled"] is False
    assert turn.memory["preferred_style"] == ExplanationStyle.BALANCED


@pytest.mark.asyncio
async def test_recall_is_persisted_in_message(learner) -> None:
    """回顾提示要落进消息记录，事后能查到"当时系统提过这件事"。"""
    session = await start(learner, ScriptedLLM())
    wrong = ScriptedLLM([ScriptedLLM.wrong(), ScriptedLLM.wrong()])
    await answer(session.session_id, "错 1", wrong, learner)
    await answer(session.session_id, "错 2", wrong, learner)
    force_due(learner["learner_id"], learner["kp_id"])

    fresh = await start(learner, ScriptedLLM())
    with SessionLocal() as db:
        row = (
            db.query(Message)
            .filter(Message.session_id == fresh.session_id, Message.role == "assistant")
            .order_by(Message.id)
            .first()
        )
        assert row is not None
        assert "连续答错" in row.content


@pytest.mark.asyncio
async def test_knowledge_point_is_unchanged(learner, tutor_kp) -> None:
    """长期记忆只读知识点，不写 —— P5 不该动 P1 的数据。"""
    with SessionLocal() as db:
        before = db.get(KnowledgePoint, learner["kp_id"]).title
    await start(learner, ScriptedLLM())
    with SessionLocal() as db:
        assert db.get(KnowledgePoint, learner["kp_id"]).title == before
