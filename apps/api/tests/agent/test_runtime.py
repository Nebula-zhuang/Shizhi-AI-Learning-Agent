"""Tutor Runtime 集成测试 —— P4 的行为契约。

需求点名要求的场景全部覆盖：
  正确回答 / 错误回答 / 连续正确 2 次→harder / 连续错误 2 次→rephrase /
  连续错误 3 次→easier / 达到掌握阈值→summarize / 跨 Session 状态保留 /
  非法 action 被 Policy 拦截 / RAG 无结果 / LLM timeout / 状态更新失败

全部用脚本化假 LLM + mock 向量化，不打网络、结果确定。
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from app.agent import policy
from app.agent.runtime import RuntimeState, TutorError, TutorRuntime
from app.agent.tools.registry import ToolRunner
from app.db.session import SessionLocal
from app.models.learner_kp_state import LearnerStatus
from app.models.message import ActionType, Message
from app.models.session import Session as TutorSession
from app.rag.embedding import MockEmbeddingProvider
from app.services import assessment_service, learner_service

from tests.conftest import ScriptedLLM


def runtime(db, llm, *, embedder=None, **kwargs) -> TutorRuntime:
    return TutorRuntime(db, llm=llm, embedder=embedder or MockEmbeddingProvider(), **kwargs)


@pytest_asyncio.fixture()
async def started(tutor_kp):
    """已开始第一轮学习的会话（首轮动作固定是 explain）。"""
    with SessionLocal() as db:
        llm = ScriptedLLM()
        turn = await runtime(db, llm).start(knowledge_point_id=tutor_kp["kp_id"])
        return {"session_id": turn.session_id, "kp_id": tutor_kp["kp_id"], "first": turn}


async def answer(session_id: int, text: str, llm: ScriptedLLM):
    with SessionLocal() as db:
        return await runtime(db, llm).submit_answer(session_id=session_id, user_answer=text)


# --------------------------------------------------------------------------- #
# 首轮与基础链路
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_start_explains_first_contact(started) -> None:
    """初次接触 → explain，且状态机按序走完。"""
    turn = started["first"]
    assert turn.action == ActionType.EXPLAIN
    assert turn.decision["rule"] == policy.RuleId.FIRST_CONTACT
    assert turn.state_before["mastery"] == 0.0
    assert turn.trace == [
        RuntimeState.LOAD_STATE.value,
        # P5 插入：装载长期记忆（讲法偏好 / 复习状态 / 薄弱点），
        # 必须在 RETRIEVE 之前 —— 讲法偏好要跟着后面的决策与内容生成一路传下去
        RuntimeState.LOAD_MEMORY.value,
        RuntimeState.RETRIEVE.value,
        RuntimeState.DECIDE_ACTION.value,
        RuntimeState.EXECUTE_ACTION.value,
        RuntimeState.WAIT_USER.value,
    ]
    assert turn.content.strip()


@pytest.mark.asyncio
async def test_start_rejects_unknown_knowledge_point() -> None:
    with SessionLocal() as db:
        with pytest.raises(TutorError) as exc:
            await runtime(db, ScriptedLLM()).start(knowledge_point_id=99999999)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_answer_cycle_trace(started) -> None:
    """作答轮的状态机轨迹：ASSESS → UPDATE_STATE → NEXT_TURN → …→ WAIT_USER。"""
    turn = await answer(started["session_id"], "我的回答", ScriptedLLM())
    assert turn.trace[:3] == [
        RuntimeState.ASSESS.value,
        RuntimeState.UPDATE_STATE.value,
        RuntimeState.NEXT_TURN.value,
    ]
    assert turn.trace[-1] == RuntimeState.WAIT_USER.value


@pytest.mark.asyncio
async def test_empty_answer_is_rejected(started) -> None:
    with SessionLocal() as db:
        with pytest.raises(TutorError) as exc:
            await runtime(db, ScriptedLLM()).submit_answer(
                session_id=started["session_id"], user_answer="   "
            )
    assert exc.value.status_code == 422


# --------------------------------------------------------------------------- #
# 正确 / 错误回答
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_correct_answer_raises_mastery(started) -> None:
    turn = await answer(started["session_id"], "进程是资源分配的基本单位", ScriptedLLM([ScriptedLLM.correct()]))

    assert turn.assessment["correct"] is True
    assert turn.state_updated is True
    assert turn.state_after["mastery"] > turn.state_before["mastery"]
    assert turn.state_after["correct_count"] == 1
    assert turn.state_after["consecutive_correct"] == 1
    assert turn.state_after["consecutive_wrong"] == 0


@pytest.mark.asyncio
async def test_content_comes_from_model_in_happy_path(started) -> None:
    """**正常路径下内容必须来自模型，而不是兜底模板。**

    这条守着一个真实踩过的坑：把 `generate_question` 的返回类型改成 dataclass 时
    忘了真正定义那个类，于是它每次都在内部抛 NameError、静默回退到兜底模板 ——
    而所有只断言"内容非空"的用例全都照样通过。

    教训：**兜底路径是"成功"的，所以只检查"有没有内容"发现不了兜底被误用。**
    必须显式断言"内容不是兜底产物"。
    """
    turn = await answer(started["session_id"], "回答", ScriptedLLM([ScriptedLLM.correct()]))

    assert "脚本化教学内容" in turn.content, f"内容不是模型产出：{turn.content[:80]}"
    assert "降级模板" not in turn.content
    assert turn.degraded is False
    assert turn.notes == []


@pytest.mark.asyncio
async def test_assessment_comes_from_model_in_happy_path(started) -> None:
    """同理：正常路径的评估必须来自模型，而不是启发式兜底。"""
    turn = await answer(
        started["session_id"], "进程是资源分配的基本单位", ScriptedLLM([ScriptedLLM.correct()])
    )
    assert turn.assessment["engine"] == "scripted"
    assert not turn.assessment["engine"].startswith("heuristic")
    assert turn.assessment["confidence"] == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_wrong_answer_lowers_mastery(started) -> None:
    # 先把掌握度抬高，否则 0 掌握度答错也不掉分（没什么可掉的）
    with SessionLocal() as db:
        state = learner_service.get_or_create_learner_state(db, started["kp_id"])
        state.mastery = learner_service._quantize(0.60)
        db.commit()

    turn = await answer(started["session_id"], "答错了", ScriptedLLM([ScriptedLLM.wrong()]))

    assert turn.assessment["correct"] is False
    assert turn.state_after["mastery"] < turn.state_before["mastery"]
    assert turn.state_after["consecutive_wrong"] == 1
    assert turn.state_after["consecutive_correct"] == 0
    # 掌握度还有 0.6（不低），所以不该退回从头讲解
    assert turn.action != ActionType.EXPLAIN


@pytest.mark.asyncio
async def test_wrong_answer_on_weak_kp_triggers_explain(started) -> None:
    """错误且基础薄弱 → explain。"""
    llm = ScriptedLLM([ScriptedLLM.wrong()])
    turn = await answer(started["session_id"], "答错了", llm)

    assert turn.decision["rule"] == policy.RuleId.WRONG_AND_WEAK
    assert turn.action == ActionType.EXPLAIN


@pytest.mark.asyncio
async def test_shallow_correct_triggers_probe(started) -> None:
    """正确但回答浅 → probe。"""
    llm = ScriptedLLM([ScriptedLLM.correct(score=0.55, level="vague")])
    turn = await answer(started["session_id"], "大概是这样吧", llm)

    assert turn.assessment["correct"] is True
    assert turn.decision["rule"] == policy.RuleId.SHALLOW_CORRECT
    assert turn.action == ActionType.PROBE


# --------------------------------------------------------------------------- #
# 连击驱动的动作切换（需求点名的三条硬阈值）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_two_consecutive_correct_triggers_harder(started) -> None:
    """连续正确 2 次 → harder。"""
    session_id = started["session_id"]

    first = await answer(session_id, "答对了第一次", ScriptedLLM([ScriptedLLM.correct()]))
    assert first.state_after["consecutive_correct"] == 1
    assert first.decision["rule"] != policy.RuleId.CORRECT_STREAK_2

    second = await answer(session_id, "答对了第二次", ScriptedLLM([ScriptedLLM.correct()]))
    assert second.state_after["consecutive_correct"] == 2
    assert second.decision["rule"] == policy.RuleId.CORRECT_STREAK_2
    assert second.decision["forced"] is True
    assert second.action == ActionType.HARDER


@pytest.mark.asyncio
async def test_two_consecutive_wrong_triggers_rephrase(started) -> None:
    """连续错误 2 次 → rephrase。"""
    session_id = started["session_id"]

    first = await answer(session_id, "错了第一次", ScriptedLLM([ScriptedLLM.wrong()]))
    assert first.state_after["consecutive_wrong"] == 1
    assert first.action != ActionType.REPHRASE

    second = await answer(session_id, "错了第二次", ScriptedLLM([ScriptedLLM.wrong()]))
    assert second.state_after["consecutive_wrong"] == 2
    assert second.decision["rule"] == policy.RuleId.WRONG_STREAK_2
    assert second.action == ActionType.REPHRASE


@pytest.mark.asyncio
async def test_three_consecutive_wrong_triggers_easier(started) -> None:
    """连续错误 3 次 → easier（而不是继续 rephrase）。"""
    session_id = started["session_id"]

    await answer(session_id, "错 1", ScriptedLLM([ScriptedLLM.wrong()]))
    await answer(session_id, "错 2", ScriptedLLM([ScriptedLLM.wrong()]))

    third = await answer(session_id, "错 3", ScriptedLLM([ScriptedLLM.wrong()]))
    assert third.state_after["consecutive_wrong"] == 3
    assert third.decision["rule"] == policy.RuleId.WRONG_STREAK_3
    assert third.action == ActionType.EASIER
    assert third.action != ActionType.REPHRASE


@pytest.mark.asyncio
async def test_correct_answer_resets_wrong_streak(started) -> None:
    """答对会清零连错 —— 否则一次失误会永久影响后续判断。"""
    session_id = started["session_id"]
    await answer(session_id, "错 1", ScriptedLLM([ScriptedLLM.wrong()]))
    ok = await answer(session_id, "对了", ScriptedLLM([ScriptedLLM.correct()]))

    assert ok.state_after["consecutive_wrong"] == 0
    assert ok.state_after["consecutive_correct"] == 1


# --------------------------------------------------------------------------- #
# 达成掌握 → summarize
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_reaching_mastery_triggers_summarize(started) -> None:
    """达到掌握阈值 → summarize，且会话收束。

    从 0 开始一路全对推到阈值之上 —— 这也是演示脚本要走的那条路。
    """
    session_id = started["session_id"]
    llm = ScriptedLLM()
    turn = None

    for index in range(12):
        turn = await answer(session_id, f"第 {index + 1} 次全对", llm)
        if turn.action == ActionType.SUMMARIZE:
            break

    assert turn is not None, "应当在有限轮次内达成掌握"
    assert turn.action == ActionType.SUMMARIZE
    assert turn.decision["rule"] == policy.RuleId.MASTERED
    assert turn.state_after["mastery"] >= policy.THRESHOLDS.mastery_threshold
    assert turn.state_after["status"] == LearnerStatus.MASTERED
    assert turn.session_finished is True

    with SessionLocal() as db:
        session = db.get(TutorSession, session_id)
        assert session.status == "finished"


@pytest.mark.asyncio
async def test_summarize_is_terminal(started) -> None:
    """达成掌握后继续提交答案，动作仍然锁定在 summarize（终态不可被覆盖）。"""
    session_id = started["session_id"]
    llm = ScriptedLLM()
    for index in range(12):
        turn = await answer(session_id, f"第 {index + 1} 次", llm)
        if turn.action == ActionType.SUMMARIZE:
            break

    again = await answer(session_id, "再答一次", ScriptedLLM([ScriptedLLM.correct()]))
    assert again.action == ActionType.SUMMARIZE
    assert again.decision["rule"] == policy.RuleId.MASTERED


# --------------------------------------------------------------------------- #
# LLM 提案被 Policy 拦截
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_illegal_action_is_blocked_by_policy(started) -> None:
    """模型返回未定义动作 → 被 Policy 拦截，回退到 Policy 的动作。"""
    llm = ScriptedLLM(decision_action="illegal")
    turn = await answer(started["session_id"], "回答", llm)

    assert turn.proposal is not None
    assert turn.proposal["ok"] is False
    assert turn.proposal["reject_reason"] == policy.RejectReason.UNKNOWN_ACTION
    # 动作必须是 Policy 算出来的那个，绝不能是模型说的 dance
    assert turn.action != "dance"
    assert turn.action in set(ActionType)
    assert "被拒" in turn.reason


@pytest.mark.asyncio
async def test_llm_cannot_override_forced_action(started) -> None:
    """连对两次后动作被硬阈值锁定，模型改选别的会被拦下。"""
    session_id = started["session_id"]
    await answer(session_id, "第一次对", ScriptedLLM([ScriptedLLM.correct()]))

    # 第二轮已经连对 2 次 → 强制 harder；让模型故意提议 easier
    llm = ScriptedLLM([ScriptedLLM.correct()], decision_action="easier")
    turn = await answer(session_id, "第二次对", llm)

    assert turn.decision["forced"] is True
    assert turn.decision["rule"] == policy.RuleId.CORRECT_STREAK_2
    assert turn.action == ActionType.HARDER, "硬阈值动作不能被模型改掉"
    assert turn.proposal["ok"] is False
    assert turn.proposal["reject_reason"] == policy.RejectReason.ACTION_NOT_ALLOWED


@pytest.mark.asyncio
async def test_llm_can_choose_within_allowed_set(started) -> None:
    """自由区里模型可以合法地另选一个动作 —— 这是 Agent 决策权的体现。"""
    llm = ScriptedLLM(decision_action="harder")
    turn = await answer(started["session_id"], "回答", llm)

    # 首答正确、连对 1 次 → 自由区，允许集合含 harder
    assert turn.decision["forced"] is False
    assert turn.action == ActionType.HARDER
    assert turn.proposal["ok"] is True


@pytest.mark.asyncio
async def test_llm_cannot_summarize_before_mastery(started) -> None:
    """掌握度不足时模型不能自选 summarize。

    这是终端动作，只能由掌握度阈值触发 —— 否则模型会在学到一半时说"可以收口了"，
    "达到掌握阈值才总结"这条规则就形同虚设。
    """
    llm = ScriptedLLM(decision_action="summarize")
    turn = await answer(started["session_id"], "回答", llm)

    assert turn.decision["forced"] is False
    assert turn.action != ActionType.SUMMARIZE
    assert turn.proposal["ok"] is False
    assert turn.proposal["reject_reason"] == policy.RejectReason.ACTION_NOT_ALLOWED


# --------------------------------------------------------------------------- #
# 跨 Session 状态保留
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_state_persists_across_sessions(tutor_kp) -> None:
    """**学习状态跨会话累积** —— 这是"长期记忆"的核心断言。

    会话是临时的过程记录，状态是长期的学习结果。开新会话不该让状态归零，
    否则"越学越难"根本无从谈起。
    """
    kp_id = tutor_kp["kp_id"]

    with SessionLocal() as db:
        first_session = await runtime(db, ScriptedLLM()).start(knowledge_point_id=kp_id)
        session_a = first_session.session_id

    await answer(session_a, "第一次对", ScriptedLLM([ScriptedLLM.correct()]))
    await answer(session_a, "第二次对", ScriptedLLM([ScriptedLLM.correct()]))

    with SessionLocal() as db:
        state = learner_service.get_learner_state(db, kp_id)
        mastery_before = float(state.mastery)
        assert state.consecutive_correct == 2

    # 开一个全新会话
    with SessionLocal() as db:
        second_session = await runtime(db, ScriptedLLM()).start(knowledge_point_id=kp_id)

    assert second_session.session_id != session_a
    assert second_session.state_before["mastery"] == pytest.approx(mastery_before)
    assert second_session.state_before["consecutive_correct"] == 2
    assert second_session.state_before["attempt_count"] == 2
    # 连对 2 次的状态下开新会话，动作直接就是 harder
    assert second_session.decision["rule"] == policy.RuleId.CORRECT_STREAK_2
    assert second_session.action == ActionType.HARDER


@pytest.mark.asyncio
async def test_resuming_same_session_keeps_state(started) -> None:
    """在同一会话上继续（start 带 session_id）也不该重置状态。"""
    await answer(started["session_id"], "第一次对", ScriptedLLM([ScriptedLLM.correct()]))

    with SessionLocal() as db:
        resumed = await runtime(db, ScriptedLLM()).start(
            knowledge_point_id=started["kp_id"], session_id=started["session_id"]
        )
    assert resumed.session_id == started["session_id"]
    assert resumed.state_before["attempt_count"] == 1


# --------------------------------------------------------------------------- #
# 降级路径
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_rag_no_result_still_works(started) -> None:
    """RAG 检索不到任何资料时，教学照常进行（内容生成用知识点自身素材兜底）。"""
    with SessionLocal() as db:
        # 用一个空集合，必然检索不到
        from app.core.config import get_settings
        from app.rag.vectorstore import VectorStore

        empty = VectorStore(get_settings().model_copy(update={"chroma_collection": "lb-p4-empty"}))
        try:
            empty.client().delete_collection("lb-p4-empty")
        except Exception:  # noqa: BLE001
            pass

        llm = ScriptedLLM([ScriptedLLM.correct()])
        turn = await runtime(db, llm, embedder=MockEmbeddingProvider()).__class__(
            db, llm=llm, embedder=MockEmbeddingProvider(), store=empty
        ).submit_answer(session_id=started["session_id"], user_answer="回答")

    assert turn.sources == []
    assert turn.content.strip(), "没有资料也要给出教学内容"
    assert turn.action in set(ActionType)


@pytest.mark.asyncio
async def test_llm_timeout_degrades_gracefully(started) -> None:
    """决策模型超时 → 回退到 Policy 动作，本轮照常完成。"""
    llm = ScriptedLLM([ScriptedLLM.correct()], fail_on={"decision"})
    turn = await answer(started["session_id"], "回答", llm)

    assert turn.proposal is not None
    assert turn.proposal["ok"] is False
    assert turn.proposal["reject_reason"] == "llm_unavailable"
    # 动作仍然来自 Policy，而不是空的
    assert turn.action in set(ActionType)
    assert turn.content.strip()
    assert turn.degraded is True


@pytest.mark.asyncio
async def test_assessment_timeout_falls_back_to_heuristic(started) -> None:
    """评估模型超时 → 启发式评估，并在 engine 里标明。"""
    llm = ScriptedLLM(fail_on={"assessment"})
    turn = await answer(
        started["session_id"], "进程是资源分配的基本单位，线程是调度的基本单位", llm
    )

    assert turn.assessment is not None
    assert turn.assessment["engine"].startswith("heuristic")
    assert turn.assessment["confidence"] < 0.7, "降级路径的把握应当如实给低"
    assert turn.state_updated is True, "评估降级了，状态仍应正常更新"


@pytest.mark.asyncio
async def test_content_generation_failure_uses_template(started, monkeypatch) -> None:
    """内容生成失败 → 用兜底模板，绝不返回空内容。"""
    llm = ScriptedLLM([ScriptedLLM.correct()], fail_on={"content"})
    turn = await answer(started["session_id"], "回答", llm)

    assert turn.content.strip()
    assert "降级模板" in turn.content
    assert turn.degraded is True


@pytest.mark.asyncio
async def test_state_update_failure_is_reported_honestly(started, monkeypatch) -> None:
    """**状态更新失败必须如实上报，不能假装成功。**

    学习状态是后续所有教学决策的依据。静默失败会让整个教学策略失真 ——
    用户以为自己在进步，实际状态一直没动。
    """
    from app.services import learner_service as ls

    def broken_update(db, state, **kwargs):  # noqa: ANN001, ANN202
        from app.services.learner_service import StateUpdateResult

        return StateUpdateResult(ok=False, mastery_before=0.0, mastery_after=0.0, error="模拟写入失败")

    monkeypatch.setattr(ls, "update_learning_state", broken_update)

    llm = ScriptedLLM([ScriptedLLM.correct()])
    turn = await answer(started["session_id"], "回答", llm)

    assert turn.state_updated is False
    assert turn.tools["state_update"]["ok"] is False
    # 状态没变，所以决策依据也是旧的 —— 如实反映
    assert turn.state_after["mastery"] == turn.state_before["mastery"]


@pytest.mark.asyncio
async def test_state_update_exception_is_caught(started, monkeypatch) -> None:
    """状态更新抛异常也要被 ToolRunner 兜住，不让整轮失败。"""
    from app.services import learner_service as ls

    def exploding(db, state, **kwargs):  # noqa: ANN001, ANN202
        raise RuntimeError("数据库连接断了")

    monkeypatch.setattr(ls, "update_learning_state", exploding)

    turn = await answer(started["session_id"], "回答", ScriptedLLM([ScriptedLLM.correct()]))
    assert turn.state_updated is False
    assert turn.content.strip(), "状态更新失败不该让本轮学习白费"


@pytest.mark.asyncio
async def test_assessment_is_persisted_with_confidence(started) -> None:
    """评估落库，且 confidence 与 mastery 是两回事。"""
    from app.models.answer_evaluation import AnswerEvaluation

    turn = await answer(started["session_id"], "回答", ScriptedLLM([ScriptedLLM.correct()]))

    with SessionLocal() as db:
        row = (
            db.query(AnswerEvaluation)
            .filter(AnswerEvaluation.session_id == started["session_id"])
            .order_by(AnswerEvaluation.id.desc())
            .first()
        )
        assert row is not None
        assert bool(row.correct) is True
        assert float(row.confidence) == pytest.approx(0.9)
        # mastery 存在学习状态表里，评估表里没有 mastery 字段
        assert not hasattr(row, "mastery")


# --------------------------------------------------------------------------- #
# Tool 预算
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_tool_budget_within_limit_per_turn(started) -> None:
    """每轮计入预算的 Tool 调用不超过 3 次。"""
    turn = await answer(started["session_id"], "回答", ScriptedLLM([ScriptedLLM.correct()]))

    used = turn.tools["used_calls"]
    assert used <= 3, f"本轮调用了 {used} 次，超出上限"
    counted = [r["name"] for r in turn.tools["records"] if r["counted"]]
    assert set(counted) <= {"retrieve_knowledge", "generate_question", "evaluate_answer"}


@pytest.mark.asyncio
async def test_tool_budget_is_recorded(started) -> None:
    """预算是可见的，且用满后如实标记为耗尽。"""
    turn = await answer(started["session_id"], "回答", ScriptedLLM([ScriptedLLM.correct()]))

    assert turn.tools["max_calls"] == 3
    assert turn.tools["max_seconds"] == 30.0
    assert turn.tools["elapsed_ms"] >= 0
    # 作答轮的三个预算内调用：评估 + 检索 + 内容生成
    assert turn.tools["used_calls"] == 3
    # 用满之后 exhausted 应当是 True（表示"不能再调了"，而不是"出问题了"）
    assert turn.tools["exhausted"] is True
    assert turn.tools["records"], "每次调用都要留痕"


@pytest.mark.asyncio
async def test_rejected_calls_do_not_count_as_used(started) -> None:
    """被预算拒绝的调用不算"已用"。

    否则统计上会自相矛盾：上限是 3 次，却显示用了 4 次。
    """
    with SessionLocal() as db:
        llm = ScriptedLLM([ScriptedLLM.correct()])
        rt = TutorRuntime(db, llm=llm, embedder=MockEmbeddingProvider(), max_tool_calls=1)
        turn = await rt.submit_answer(session_id=started["session_id"], user_answer="回答")

    assert turn.tools["used_calls"] <= 1
    rejected = [r for r in turn.tools["records"] if r["rejected"]]
    assert rejected, "超出预算的调用应当留下被拒绝的记录"
    assert all(r["counted"] for r in rejected)


@pytest.mark.asyncio
async def test_exhausted_budget_skips_retrieval(started) -> None:
    """预算耗尽时跳过检索，保证仍能给出教学内容。"""
    with SessionLocal() as db:
        llm = ScriptedLLM([ScriptedLLM.correct()])
        # 把预算设成 1 次：evaluate 用掉后，retrieve 会被跳过
        rt = TutorRuntime(db, llm=llm, embedder=MockEmbeddingProvider(), max_tool_calls=1)
        turn = await rt.submit_answer(session_id=started["session_id"], user_answer="回答")

    assert turn.tools["used_calls"] <= 1
    assert turn.content.strip()


def test_tool_runner_rejects_when_exhausted() -> None:
    """预算用尽后，后续调用被直接拒绝而不是抛错。"""
    import asyncio

    runner = ToolRunner(max_calls=1)

    async def noop() -> str:
        return "ok"

    async def scenario() -> tuple[bool, bool]:
        first = await runner.call("a", noop)
        second = await runner.call("b", noop)
        return first.ok, second.ok

    first_ok, second_ok = asyncio.run(scenario())
    assert first_ok is True
    assert second_ok is False
    assert runner.budget.exhausted is True


def test_tool_runner_handles_sync_functions() -> None:
    """同步工具也必须能调用 —— 学习状态的读写就是同步的。

    这条守着 `_invoke` 里那段"同步/异步都支持"的逻辑：
    漏了它会对同步返回值 await，报 "can't be used in 'await' expression"，
    而且会被当成业务失败静默降级，表现为"状态总是更新不成功"。
    """
    import asyncio

    runner = ToolRunner()

    def sync_tool(value: int) -> int:
        return value * 2

    async def scenario() -> int:
        outcome = await runner.state_op("sync", sync_tool, value=21)
        assert outcome.ok is True
        return outcome.value

    assert asyncio.run(scenario()) == 42


def test_tool_runner_captures_exceptions() -> None:
    import asyncio

    runner = ToolRunner()

    def boom() -> None:
        raise ValueError("炸了")

    async def scenario() -> tuple[bool, str]:
        outcome = await runner.call("boom", boom)
        return outcome.ok, outcome.error

    ok, error = asyncio.run(scenario())
    assert ok is False
    assert "ValueError" in error


# --------------------------------------------------------------------------- #
# 留痕：证明"动作由状态驱动"
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_message_records_state_snapshot(started) -> None:
    """助手消息必须留下"决策时的状态"与理由。

    P4 要证明的是「Agent 根据学习状态改变策略」。没有这两样，
    就只能看到"Agent 讲了一句话"，证明不了它是**根据状态**讲的。
    """
    turn = await answer(started["session_id"], "回答", ScriptedLLM([ScriptedLLM.correct()]))

    with SessionLocal() as db:
        message = (
            db.query(Message)
            .filter(
                Message.session_id == started["session_id"],
                Message.role == "assistant",
            )
            .order_by(Message.id.desc())
            .first()
        )
        assert message.action_type == turn.action
        assert message.reason
        assert message.state_snapshot is not None
        assert "mastery" in message.state_snapshot
        assert "consecutive_wrong" in message.state_snapshot


@pytest.mark.asyncio
async def test_action_sequence_reflects_state_changes(tutor_kp) -> None:
    """**综合断言**：动作序列随状态变化，而不是每轮随机或固定。

    这条是整个 P4 的验收核心 —— 用同一条会话走一遍
    「首见 → 对 → 对 → 错 → 错 → 错」，动作应当依次为
    explain → probe/… → harder → … → rephrase → easier。
    """
    kp_id = tutor_kp["kp_id"]
    with SessionLocal() as db:
        turn = await runtime(db, ScriptedLLM()).start(knowledge_point_id=kp_id)
        session_id = turn.session_id

    actions = [turn.action]
    script = [
        ScriptedLLM.correct(),
        ScriptedLLM.correct(),
        ScriptedLLM.wrong(),
        ScriptedLLM.wrong(),
        ScriptedLLM.wrong(),
    ]
    rules = [turn.decision["rule"]]
    for index, verdict in enumerate(script):
        turn = await answer(session_id, f"第 {index + 1} 答", ScriptedLLM([verdict]))
        actions.append(turn.action)
        rules.append(turn.decision["rule"])

    assert actions[0] == ActionType.EXPLAIN, "首见应当先讲解"
    assert ActionType.HARDER in actions, "连对两次后应当升难度"
    assert ActionType.REPHRASE in actions, "连错两次后应当换讲法"
    assert ActionType.EASIER in actions, "连错三次后应当降难度"
    assert len(set(rules)) >= 4, f"规则命中应当随状态变化，实际只有 {rules}"
