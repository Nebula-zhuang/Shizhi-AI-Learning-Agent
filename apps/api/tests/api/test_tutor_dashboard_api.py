"""P5 接口测试：学习看板与讲法画像。

两个新端点是"跨会话记得住"的对外出口 —— 用户关掉浏览器再回来，
首屏看到的就是上次留下的状态。
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.knowledge_point import KnowledgePoint
from app.models.learner_kp_state import LearnerKpState
from app.models.learner_profile import ExplanationStyle, LearnerProfile, StyleSource
from app.services import memory_service as mem

from tests.conftest import ScriptedLLM

client = TestClient(app)


@pytest.fixture()
def learner_id() -> str:
    """每个用例一个独立学习者，互不干扰，也不动真实数据。

    P6 之前，隔离是靠把 `learner_id` 放进请求体和查询串实现的 ——
    服务端直接采信那个值。引入账号体系后那个口子被封掉了
    （否则改一下请求体就能读写别人的学习记录），身份只能来自会话。

    所以这个夹具改成走**真实路径**：建一个绑定该 learner_id 的账号，
    登录一次，让客户端持有会话 Cookie。既不依赖后门，也顺带覆盖了认证链路。
    """
    value = f"test-api-{uuid4().hex[:10]}"
    username = f"t{uuid4().hex[:10]}"
    password = "fixture-password"

    from app.db.session import SessionLocal
    from app.models.session import Session as TutorSession
    from app.models.user import User
    from app.services import auth_service

    with SessionLocal() as db:
        auth_service.create_user(db, username=username, password=password, learner_id=value)

    res = client.post("/api/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200, f"夹具登录失败：{res.text}"
    # 断言身份真的落在这个 learner_id 上，避免以后认证改坏了夹具却静默继续跑
    assert res.json()["user"]["learner_id"] == value

    yield value

    client.cookies.clear()
    with SessionLocal() as db:
        db.query(LearnerKpState).filter(LearnerKpState.learner_id == value).delete()
        db.query(LearnerProfile).filter(LearnerProfile.learner_id == value).delete()
        for item in db.query(TutorSession).filter(TutorSession.learner_id == value).all():
            db.delete(item)
        db.query(User).filter(User.learner_id == value).delete()
        db.commit()


@pytest.fixture()
def owned_kp(learner_id, tutor_kp):
    """把夹具资料的知识点归属改到测试账号名下，然后返回它。

    为什么要这一步：`/api/tutor/start` 现在会校验知识点归属 ——
    否则只要知道一个 id，就能让助教把**别人资料里的内容**讲出来。
    而 `tutor_kp` 建的资料默认归 `local`，与本用例的隔离账号对不上。

    这里改的是夹具自己刚重建出来的那条资料（`tutor_kp` 每次都会按 hash
    删掉旧的重建），所以不会污染其它用例。
    """
    from app.db.session import SessionLocal
    from app.models.document import Document

    with SessionLocal() as db:
        document = db.get(Document, tutor_kp["document_id"])
        if document is not None:
            document.owner_learner_id = learner_id
            db.commit()
    return tutor_kp


@pytest.fixture()
def env(monkeypatch, mock_embedder, test_store):
    from app.agent import runtime as runtime_module
    from app.services import rag_service

    llm = ScriptedLLM()
    monkeypatch.setattr(runtime_module, "llm_gateway", llm)
    monkeypatch.setattr(rag_service, "default_embedder", mock_embedder)
    monkeypatch.setattr(rag_service, "default_store", test_store)
    return {"llm": llm}


# --------------------------------------------------------------------------- #
# 看板
# --------------------------------------------------------------------------- #
def test_dashboard_empty_learner_returns_zero_values(learner_id, env) -> None:
    """全新学习者：不能 404，要给一份零值看板 —— 前端不必为空数据写额外分支。"""
    res = client.get("/api/tutor/dashboard", params={"learner_id": learner_id})
    assert res.status_code == 200
    body = res.json()

    assert body["learner_id"] == learner_id
    assert body["overview"]["tracked"] == 0
    assert body["overview"]["mastered"] == 0
    assert body["due_reviews"] == []
    assert body["weak_points"] == []
    assert body["profile"]["preferred_style"] == ExplanationStyle.BALANCED
    assert body["profile"]["style_source"] == StyleSource.DEFAULT


def test_dashboard_shape(learner_id, env) -> None:
    body = client.get("/api/tutor/dashboard", params={"learner_id": learner_id}).json()
    for key in (
        "learner_id",
        "generated_at",
        "overview",
        "profile",
        "due_reviews",
        "weak_points",
        "review_curve",
    ):
        assert key in body, f"看板缺少 {key}"
    # 遗忘曲线的参数要露出来 —— 让用户看得到"复习计划是怎么排的"
    assert body["review_curve"]["tiers"]
    assert body["review_curve"]["min_seconds"] > 0


def test_dashboard_reflects_progress(learner_id, env, owned_kp) -> None:
    """学过之后看板必须反映出来。"""
    kp_id = owned_kp["kp_id"]

    res = client.post(
        "/api/tutor/start", json={"knowledge_point_id": kp_id, "learner_id": learner_id}
    )
    assert res.status_code == 200
    session_id = res.json()["session_id"]

    env["llm"].assessments = [ScriptedLLM.wrong(), ScriptedLLM.wrong()]
    for index in range(2):
        assert (
            client.post(
                "/api/tutor/answer",
                json={
                    "session_id": session_id,
                    "answer": f"第 {index + 1} 次答错",
                    "learner_id": learner_id,
                },
            ).status_code
            == 200
        )

    body = client.get("/api/tutor/dashboard", params={"learner_id": learner_id}).json()
    assert body["overview"]["tracked"] == 1
    assert body["overview"]["weak"] == 1
    assert any(item["knowledge_point_id"] == kp_id for item in body["weak_points"])

    weak = next(item for item in body["weak_points"] if item["knowledge_point_id"] == kp_id)
    assert weak["consecutive_wrong"] == 2
    assert weak["urgency"] > 0
    assert weak["last_error_type"] == "concept_confusion"


def test_dashboard_due_reviews_after_schedule(learner_id, env, owned_kp) -> None:
    """到期项要出现在待复习里。"""
    from datetime import datetime, timedelta

    from app.db.session import SessionLocal
    from app.services import learner_service

    kp_id = owned_kp["kp_id"]
    res = client.post(
        "/api/tutor/start", json={"knowledge_point_id": kp_id, "learner_id": learner_id}
    )
    session_id = res.json()["session_id"]
    client.post(
        "/api/tutor/answer",
        json={"session_id": session_id, "answer": "答错", "learner_id": learner_id},
    )

    # 未到期时不该出现在待复习里
    before = client.get("/api/tutor/dashboard", params={"learner_id": learner_id}).json()
    assert before["due_reviews"] == []

    # 把事情拨到过去
    with SessionLocal() as db:
        state = learner_service.get_learner_state(db, kp_id, learner_id=learner_id)
        state.next_review_at = datetime.now() - timedelta(hours=2)
        db.commit()

    after = client.get("/api/tutor/dashboard", params={"learner_id": learner_id}).json()
    assert any(item["knowledge_point_id"] == kp_id for item in after["due_reviews"])
    assert after["due_reviews"][0]["due"] is True


def test_dashboard_limits_are_validated(learner_id, env) -> None:
    assert (
        client.get(
            "/api/tutor/dashboard", params={"learner_id": learner_id, "weak_limit": 0}
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/tutor/dashboard", params={"learner_id": learner_id, "due_limit": 999}
        ).status_code
        == 422
    )


# --------------------------------------------------------------------------- #
# 讲法画像
# --------------------------------------------------------------------------- #
def test_profile_update_sets_manual_style(learner_id, env) -> None:
    res = client.put(
        "/api/tutor/profile",
        json={"preferred_style": ExplanationStyle.CONTRAST, "learner_id": learner_id},
    )
    assert res.status_code == 200
    body = res.json()

    assert body["preferred_style"] == ExplanationStyle.CONTRAST
    assert body["style_source"] == StyleSource.MANUAL
    assert body["style_label"] == "对比式讲法"
    assert "概念混淆" in body["style_instruction"], "指令要具体，不能是空话"

    # 看板要反映出来
    dashboard = client.get("/api/tutor/dashboard", params={"learner_id": learner_id}).json()
    assert dashboard["profile"]["preferred_style"] == ExplanationStyle.CONTRAST
    assert dashboard["profile"]["style_source"] == StyleSource.MANUAL


def test_profile_update_rejects_unknown_style(learner_id, env) -> None:
    res = client.put(
        "/api/tutor/profile",
        json={"preferred_style": "随便编的", "learner_id": learner_id},
    )
    assert res.status_code == 422


def test_profile_update_rejects_missing_field(learner_id, env) -> None:
    assert client.put("/api/tutor/profile", json={"learner_id": learner_id}).status_code == 422


def test_manual_style_survives_auto_refresh(learner_id, env) -> None:
    """手动设定后，即便积累了相反的错因证据也不会被改掉。"""
    from app.db.session import SessionLocal
    from app.models.answer_evaluation import (
        AnswerEvaluation,
        AssessmentLevel,
        ErrorType,
    )
    from app.models.session import Session as TutorSession

    client.put(
        "/api/tutor/profile",
        json={"preferred_style": ExplanationStyle.CLARIFY, "learner_id": learner_id},
    )

    # 造出一批"概念混淆"证据（会推导成 contrast）
    with SessionLocal() as db:
        session = TutorSession(learner_id=learner_id, title="证据会话")
        db.add(session)
        db.flush()
        kp = db.query(KnowledgePoint).first()
        for _ in range(6):
            db.add(
                AnswerEvaluation(
                    session_id=session.id,
                    knowledge_point_id=kp.id,
                    question="q",
                    user_answer="a",
                    correct=False,
                    score=0.2,
                    confidence=0.8,
                    level=AssessmentLevel.NOT_MASTERED,
                    error_type=ErrorType.CONCEPT_CONFUSION,
                    feedback="f",
                    engine="test",
                )
            )
        db.commit()
        mem.refresh_profile(db, learner_id=learner_id)

    body = client.get("/api/tutor/dashboard", params={"learner_id": learner_id}).json()
    assert body["profile"]["preferred_style"] == ExplanationStyle.CLARIFY, "手动设定不该被覆盖"
    assert body["profile"]["style_source"] == StyleSource.MANUAL


def test_dashboard_style_instruction_matches_injection(learner_id, env, owned_kp) -> None:
    """**看板返回的讲法指令必须与注入 Prompt 的逐字一致** —— 前后端不能是两套说法。"""
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        mem.set_manual_style(db, ExplanationStyle.STRUCTURED, learner_id=learner_id)

    dashboard = client.get("/api/tutor/dashboard", params={"learner_id": learner_id}).json()
    instruction = dashboard["profile"]["style_instruction"]
    assert instruction == mem.style_instruction(ExplanationStyle.STRUCTURED)

    env["llm"].messages.clear()
    env["llm"].calls.clear()
    res = client.post(
        "/api/tutor/start",
        json={"knowledge_point_id": owned_kp["kp_id"], "learner_id": learner_id},
    )
    assert res.status_code == 200
    turn = res.json()
    assert turn["memory"]["style_instruction"] == instruction

    prompts = env["llm"].prompts_of("decision")
    assert prompts and any(instruction in p for p in prompts), "指令必须真的进了 Prompt"


# --------------------------------------------------------------------------- #
# 跨会话验收（HTTP 层）
# --------------------------------------------------------------------------- #
def test_second_session_recalls_weak_point_over_http(learner_id, env, owned_kp) -> None:
    """**验收标准在 HTTP 层的复现**：
    关掉浏览器（开新会话）再回来，Tutor 会主动提到上次的薄弱点。"""
    from datetime import datetime, timedelta

    from app.db.session import SessionLocal
    from app.services import learner_service

    kp_id = owned_kp["kp_id"]

    # 会话一：连错两次
    first = client.post(
        "/api/tutor/start", json={"knowledge_point_id": kp_id, "learner_id": learner_id}
    ).json()
    env["llm"].assessments = [ScriptedLLM.wrong(), ScriptedLLM.wrong()]
    for index in range(2):
        client.post(
            "/api/tutor/answer",
            json={
                "session_id": first["session_id"],
                "answer": f"第 {index + 1} 次答错",
                "learner_id": learner_id,
            },
        )

    # 模拟"隔了一段时间再回来"
    with SessionLocal() as db:
        state = learner_service.get_learner_state(db, kp_id, learner_id=learner_id)
        state.next_review_at = datetime.now() - timedelta(hours=1)
        db.commit()

    # 会话二：新会话首轮
    second = client.post(
        "/api/tutor/start", json={"knowledge_point_id": kp_id, "learner_id": learner_id}
    ).json()

    assert second["session_id"] != first["session_id"]
    assert second["memory"]["recalled"] is True
    assert "连续答错 2 次" in second["memory"]["recall_note"]
    assert second["memory"]["recall_note"] in second["content"]
    assert second["memory"]["is_due"] is True

    # 会话详情里也能查到这次回顾
    detail = client.get(f"/api/tutor/sessions/{second['session_id']}").json()
    assistant = [m for m in detail["messages"] if m["role"] == "assistant"]
    assert "连续答错" in assistant[0]["content"]


def test_existing_routes_untouched() -> None:
    """P5 只新增 2 个路径，P0–P4 的 31 个一个不动。"""
    paths = app.openapi()["paths"]
    for required in (
        "/api/chat",
        "/api/rag/ask",
        "/api/tutor/start",
        "/api/tutor/answer",
        "/api/tutor/sessions/{session_id}",
        "/api/tutor/learner-state/{knowledge_point_id}",
        "/api/tutor/capabilities",
        # P7 自由学习空间。同上，加进守护列表而不是只改总数。
        "/api/study/conversations",
        "/api/study/conversations/{conversation_id}/ask",
        "/api/study/capabilities",
        # Phase 3A 保存知识，见 test_tutor_api.py 的同类断言说明。
        "/api/study/knowledge",
    ):
        assert required in paths, f"既有接口缺失：{required}"
    # P6 新增 2 个流式入口（见 test_tutor_api.py 的同类断言说明）
    # P6 新增 2 个流式入口 + 5 个认证接口，见 test_tutor_api.py 的同类断言说明
    # P7 自由学习空间新增 6 个路径（对话 CRUD + 提问流 + 能力自检 + 可选资料）。
    # 同样是**只增不改**：上面那些既有接口一个没动。
    # Phase 3A 新增 1 个路径（/api/study/knowledge，POST + GET 共用）。
    assert len(paths) == 47, f"路径总数应为 47，实际 {len(paths)}"
