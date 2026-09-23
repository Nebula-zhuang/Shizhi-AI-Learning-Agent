"""P4 接口契约测试。

覆盖 5 个新增端点，并守住三条边界：
  - 学习状态跨会话保留（新会话不重置掌握度）
  - 决策被 Policy 校验（模型说 dance 不算数）
  - 会话详情能回放出"动作序列 + 每轮决策理由 + 每次评估"

强制 mock：不调真实 LLM、不调真实 embedding、不碰生产向量集合。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agent import policy
from app.agent import runtime as runtime_module
from app.main import app
from app.models.message import ActionType
from app.services import rag_service

from tests.conftest import ScriptedLLM

client = TestClient(app)


@pytest.fixture()
def tutor_env(monkeypatch, mock_embedder, test_store):
    """把 Tutor 链路的默认依赖全换成测试替身。"""
    llm = ScriptedLLM()
    monkeypatch.setattr(runtime_module, "llm_gateway", llm)
    monkeypatch.setattr(rag_service, "default_embedder", mock_embedder)
    monkeypatch.setattr(rag_service, "default_store", test_store)
    return {"llm": llm}


def start(kp_id: int, **extra) -> dict:
    res = client.post("/api/tutor/start", json={"knowledge_point_id": kp_id, **extra})
    assert res.status_code == 200, res.text
    return res.json()


def answer(session_id: int, text: str) -> dict:
    res = client.post(
        "/api/tutor/answer", json={"session_id": session_id, "answer": text}
    )
    assert res.status_code == 200, res.text
    return res.json()


# --------------------------------------------------------------------------- #
# 能力探测
# --------------------------------------------------------------------------- #
def test_capabilities_exposes_actions_and_thresholds(tutor_env) -> None:
    body = client.get("/api/tutor/capabilities").json()

    assert set(body["actions"]) == set(ActionType)
    # 阈值来自 Policy，前端不必硬编码
    assert body["thresholds"]["mastery_threshold"] == policy.THRESHOLDS.mastery_threshold
    assert body["action_labels"]["harder"] == "升难度"
    assert body["max_tool_calls"] == 3
    assert body["max_seconds"] == 30.0


# --------------------------------------------------------------------------- #
# 开始学习
# --------------------------------------------------------------------------- #
def test_start_returns_first_turn(tutor_kp, tutor_env) -> None:
    body = start(tutor_kp["kp_id"])

    assert body["action"] == ActionType.EXPLAIN
    assert body["decision"]["rule"] == policy.RuleId.FIRST_CONTACT
    assert body["content"]
    assert body["reason"]
    assert body["state_before"]["mastery"] == 0.0
    assert body["trace"][0] == "LOAD_STATE"
    assert body["trace"][-1] == "WAIT_USER"
    assert body["thresholds"]["mastery_threshold"] > 0


def test_start_unknown_knowledge_point(tutor_env) -> None:
    res = client.post("/api/tutor/start", json={"knowledge_point_id": 99999999})
    assert res.status_code == 404


def test_start_rejects_malformed_payload(tutor_env) -> None:
    assert client.post("/api/tutor/start", json={}).status_code == 422
    assert client.post("/api/tutor/start", json={"knowledge_point_id": "abc"}).status_code == 422


# --------------------------------------------------------------------------- #
# 提交作答
# --------------------------------------------------------------------------- #
def test_answer_returns_assessment_and_next_action(tutor_kp, tutor_env) -> None:
    session_id = start(tutor_kp["kp_id"])["session_id"]
    body = answer(session_id, "进程是资源分配的基本单位，线程是调度的基本单位。")

    assert body["assessment"] is not None
    assert body["assessment"]["correct"] is True
    # confidence 是评估质量，mastery 是学习状态 —— 两者都在但含义不同
    assert "confidence" in body["assessment"]
    assert "mastery" in body["state_after"]

    assert body["state_updated"] is True
    assert body["state_after"]["mastery"] > body["state_before"]["mastery"]
    assert body["action"] in set(ActionType)

    counted = [r["name"] for r in body["tools"]["records"] if r["counted"] and not r["rejected"]]
    assert len(counted) <= 3, f"本轮计费调用 {counted} 超出上限"
    assert body["session_id"] == session_id


def test_answer_rejects_empty(tutor_kp, tutor_env) -> None:
    session_id = start(tutor_kp["kp_id"])["session_id"]
    res = client.post("/api/tutor/answer", json={"session_id": session_id, "answer": "  "})
    assert res.status_code in {422, 400}


def test_answer_unknown_session(tutor_env) -> None:
    res = client.post("/api/tutor/answer", json={"session_id": 99999999, "answer": "回答"})
    assert res.status_code == 404


# --------------------------------------------------------------------------- #
# 教学策略随状态变化（P4 的核心验收）
# --------------------------------------------------------------------------- #
def test_action_sequence_changes_with_state(tutor_kp, tutor_env) -> None:
    """同一会话里，动作随状态依次变化 —— 不是每轮随机，也不是固定。"""
    session_id = start(tutor_kp["kp_id"])["session_id"]
    actions = ["explain"]

    for index in range(4):
        body = answer(session_id, f"第 {index + 1} 次回答")
        actions.append(body["action"])

    assert actions[0] == "explain"
    assert len(set(actions)) > 1, f"动作序列不应固定不变：{actions}"
    # 连续答对后必然出现升难度
    assert "harder" in actions


def test_illegal_llm_action_is_blocked(tutor_kp, tutor_env) -> None:
    """模型返回未定义动作时，接口层返回的仍是 Policy 的动作。"""
    tutor_env["llm"].decision_action = "illegal"
    session_id = start(tutor_kp["kp_id"])["session_id"]

    body = answer(session_id, "回答")
    assert body["proposal"]["ok"] is False
    assert body["proposal"]["reject_reason"] == policy.RejectReason.UNKNOWN_ACTION
    assert body["action"] in set(ActionType)
    assert body["action"] != "dance"


def test_forced_action_cannot_be_overridden(tutor_kp, tutor_env) -> None:
    """连对两次后动作被硬阈值锁定，模型改不掉。"""
    tutor_env["llm"].assessments = [
        ScriptedLLM.correct(),
        ScriptedLLM.correct(),
    ]
    session_id = start(tutor_kp["kp_id"])["session_id"]
    answer(session_id, "第一次")

    tutor_env["llm"].decision_action = "easier"  # 故意提议别的
    body = answer(session_id, "第二次")

    assert body["decision"]["forced"] is True
    assert body["decision"]["rule"] == policy.RuleId.CORRECT_STREAK_2
    assert body["action"] == ActionType.HARDER
    assert body["proposal"]["ok"] is False


# --------------------------------------------------------------------------- #
# 跨会话状态保留
# --------------------------------------------------------------------------- #
def test_state_survives_new_session(tutor_kp, tutor_env) -> None:
    """**新会话不重置学习状态** —— 长期记忆的关键契约。"""
    kp_id = tutor_kp["kp_id"]
    tutor_env["llm"].assessments = [ScriptedLLM.correct(), ScriptedLLM.correct()]

    first = start(kp_id)
    answer(first["session_id"], "第一次")
    answer(first["session_id"], "第二次")

    state_before = client.get(f"/api/tutor/learner-state/{kp_id}").json()
    assert state_before["mastery"] > 0
    assert state_before["consecutive_correct"] == 2

    second = start(kp_id)
    assert second["session_id"] != first["session_id"]
    assert second["state_before"]["mastery"] == state_before["mastery"]
    assert second["state_before"]["consecutive_correct"] == 2
    # 状态带过去之后，新会话的首个动作直接就是升难度
    assert second["action"] == ActionType.HARDER


def test_learner_state_endpoint(tutor_kp, tutor_env) -> None:
    kp_id = tutor_kp["kp_id"]

    fresh = client.get(f"/api/tutor/learner-state/{kp_id}").json()
    assert fresh["exists"] is False
    assert fresh["mastery"] == 0.0
    assert fresh["status"] == "new"

    session_id = start(kp_id)["session_id"]
    answer(session_id, "回答")

    after = client.get(f"/api/tutor/learner-state/{kp_id}").json()
    assert after["exists"] is True
    assert after["attempt_count"] == 1
    assert after["mastery"] > 0


def test_learner_state_unknown_kp(tutor_env) -> None:
    assert client.get("/api/tutor/learner-state/99999999").status_code == 404


# --------------------------------------------------------------------------- #
# 会话详情
# --------------------------------------------------------------------------- #
def test_session_detail_replays_decision_trail(tutor_kp, tutor_env) -> None:
    """会话详情必须能回放出完整过程：消息 + 动作序列 + 评估 + 状态。

    P4 要证明"Agent 根据学习状态改变策略"，这个接口就是证据链的出口。
    """
    kp_id = tutor_kp["kp_id"]
    session_id = start(kp_id)["session_id"]
    answer(session_id, "进程是资源分配的基本单位")
    answer(session_id, "线程是调度的基本单位")

    body = client.get(f"/api/tutor/sessions/{session_id}").json()

    assert body["session_id"] == session_id
    assert body["knowledge_point_id"] == kp_id
    assert body["action_count"] == 3  # start + 2 次作答

    # 动作序列：一眼看出教学策略的变化
    assert body["action_sequence"][0] == "explain"
    assert len(body["action_sequence"]) == 3

    # 每条助手消息都留下了决策理由与状态快照
    assistant = [m for m in body["messages"] if m["role"] == "assistant"]
    assert assistant
    for message in assistant:
        assert message["action_type"]
        assert message["reason"]
        assert message["state_snapshot"] is not None

    # 评估与用户消息一一对应
    user_messages = [m for m in body["messages"] if m["role"] == "user"]
    assert len(body["evaluations"]) == len(user_messages) == 2
    for evaluation in body["evaluations"]:
        assert "confidence" in evaluation
        assert "error_type" in evaluation

    assert body["state"] is not None
    assert body["state"]["attempt_count"] == 2


def test_session_detail_unknown(tutor_env) -> None:
    assert client.get("/api/tutor/sessions/99999999").status_code == 404


# --------------------------------------------------------------------------- #
# 达到掌握 → summarize
# --------------------------------------------------------------------------- #
def test_full_learning_loop_reaches_summarize(tutor_kp, tutor_env) -> None:
    """**完整闭环**：一路答对 → 动作逐步变化 → 达成掌握 → summarize 收束。"""
    kp_id = tutor_kp["kp_id"]
    session_id = start(kp_id)["session_id"]

    actions = ["explain"]
    final = None
    for index in range(15):
        final = answer(session_id, f"第 {index + 1} 次全对")
        actions.append(final["action"])
        if final["action"] == ActionType.SUMMARIZE:
            break

    assert final is not None and final["action"] == ActionType.SUMMARIZE
    assert final["decision"]["rule"] == policy.RuleId.MASTERED
    assert final["state_after"]["mastery"] >= policy.THRESHOLDS.mastery_threshold
    assert final["state_after"]["status"] == "mastered"
    assert final["session_finished"] is True

    # 动作序列应当是变化的，而不是一路 harder
    assert "harder" in actions
    assert len(set(actions)) >= 2

    detail = client.get(f"/api/tutor/sessions/{session_id}").json()
    assert detail["status"] == "finished"
    assert detail["state"]["status"] == "mastered"


def test_three_wrong_answers_in_a_row_go_easier(tutor_kp, tutor_env) -> None:
    """接口层验证连错三次 → 降难度。"""
    tutor_env["llm"].assessments = [ScriptedLLM.wrong() for _ in range(3)]
    session_id = start(tutor_kp["kp_id"])["session_id"]

    answer(session_id, "错 1")
    answer(session_id, "错 2")
    third = answer(session_id, "错 3")

    assert third["decision"]["rule"] == policy.RuleId.WRONG_STREAK_3
    assert third["action"] == ActionType.EASIER


# --------------------------------------------------------------------------- #
# P0–P3 的接口未被改动
# --------------------------------------------------------------------------- #
def test_existing_routes_still_registered() -> None:
    """Tutor 是新增能力入口，不该动到既有接口。

    这里真正要守的是「上面列的既有接口一个都不缺」；
    路径总数只是**防误删的粗检查**，每个阶段都会增长
    （P4 是 31，P5 加了看板与画像两个变 33），所以要随阶段更新。
    """
    paths = app.openapi()["paths"]
    for required in (
        "/api/chat",
        "/api/chat/stream",
        "/api/rag/ask",
        "/api/documents/{document_id}/graph",
        "/api/documents/{document_id}/verify",
        "/api/knowledge-points/{kp_id}/checks",
        # P7 自由学习空间。加进守护列表，将来误删也会被这条测试抓到 ——
        # 只更新总数是抓不住"删了一个旧的、加了两个新的"这种情况的。
        "/api/study/conversations",
        "/api/study/conversations/{conversation_id}/ask",
        "/api/study/capabilities",
        # Phase 3A 保存知识。同样加进守护列表，而不是只改总数。
        "/api/study/knowledge",
        # Phase 5C-2 学习主题 → 知识点。同上，加进守护列表。
        "/api/study/learn-target",
        # Phase 5D 待办清单。同上，加进守护列表。
        "/api/todos",
        "/api/todos/{todo_id}",
    ):
        assert required in paths, f"既有接口缺失：{required}"
    # P6 新增 2 个流式入口（/api/tutor/start/stream、/api/tutor/answer/stream）——
    # **只是新增**：入参与返回体都与原有同步接口一致，原有 33 个路径一个没动。
    # 这个断言的作用是别把既有接口弄丢或改名，所以数量跟着新增一起往上走。
    # P6 又新增 5 个认证接口（/api/auth/register·login·logout·me·password）。
    # 同样**只是新增**：既有 35 个路径一个没动。
    # P7 自由学习空间新增 6 个路径（对话 CRUD + 提问流 + 能力自检 + 可选资料）。
    # 同样是**只增不改**：上面那些既有接口一个没动。
    # Phase 3A 新增 1 个路径（/api/study/knowledge —— POST 保存与 GET 列出共用同一路径）。
    # 依旧是**只增不改**。
    # Phase 5C-2 新增 1 个路径（/api/study/learn-target）。依旧是**只增不改**。
    # Phase 5D 新增 2 个路径（/api/todos、/api/todos/{todo_id}）。同上。
    assert len(paths) == 50, (
        f"路径总数应为 50（P0-P6 的 40 + 自由学习 6 + 保存知识 1 + 学习主题 1 + 待办 2），"
        f"实际 {len(paths)}"
    )
