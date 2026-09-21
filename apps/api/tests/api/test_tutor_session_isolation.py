"""Tutor 会话归属越权的回归测试（V1 / V2）。

守住 2026-09-21 安全审计发现的两个**真实可利用**漏洞：

  **V1 读越权** —— `GET /api/tutor/sessions/{id}` 过去只依赖 `get_db`，
      按主键直取会话。任何人（**包括未登录**）枚举自增 id 就能读到别人的
      完整对话内容、学习者作答与评分。
  **V2 写越权** —— `TutorRuntime.start` / `submit_answer` 加载会话时
      不比对 `learner_id`（`self.learner_id` 就在同一个对象上），
      已登录用户可以把自己的作答**写进别人的会话**，污染他人对话记录。

两者的修法相同：加载后校验归属，不匹配一律 404。

## 为什么必须 404 而不是 403

403 等于确认"这个 id 确实存在" —— 那就成了一个用来**枚举他人会话**的接口。
下面有一条用例专门守这件事：别人的 id 与不存在的 id 必须返回**完全一样**的响应。

## 为什么要自带知识点夹具

`/api/tutor/start` 会校验**知识点归属**（`require_knowledge_point` 沿
`knowledge_points.document_id → documents.owner_learner_id` 判）。
conftest 里的 `tutor_kp` 没设 owner（默认落到 `local`），
所以注册用户访问它会 404。

因此本文件对注册用户**自建一份归属于他的文档 + 知识点**（`_own_kp`）——
这样测的是"会话归属"，而不是被前置的知识点归属挡住。

## 为什么还要测匿名 / local

既有契约是"未登录也能用 Tutor"：匿名落到 `local`，而演示账号的
`learner_id` **也是 `local`**。给这类接口加归属校验最容易的失误，
就是顺手把匿名路径一起堵死 —— 那会破坏功能而不是修漏洞。
所以两条都要测：**匿名读不到别人的**，**但也读得到自己的**。
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.agent import runtime as runtime_module
from app.agent.runtime import TutorError, TutorRuntime
from app.db.session import SessionLocal
from app.main import app
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.knowledge_point import KnowledgePoint
from app.models.learner_kp_state import LearnerKpState
from app.models.learner_profile import LearnerProfile
from app.models.message import Message
from app.models.session import Session as TutorSession
from app.models.session import SessionStatus
from app.models.user import User
from app.services import rag_service

from tests.conftest import ScriptedLLM

client = TestClient(app)

PASSWORD = "audit-password-123"
DEFAULT_LEARNER = "local"


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture()
def tutor_env(monkeypatch, mock_embedder, test_store):
    """把 Tutor 链路的默认依赖换成测试替身。

    与 `test_tutor_api.py` 同一套做法：**不调真实 LLM、不碰生产向量集合**。
    """
    llm = ScriptedLLM()
    monkeypatch.setattr(runtime_module, "llm_gateway", llm)
    monkeypatch.setattr(rag_service, "default_embedder", mock_embedder)
    monkeypatch.setattr(rag_service, "default_store", test_store)
    return {"llm": llm}


@pytest.fixture()
def cleanup():
    """记录本用例造出来的账号 / 会话 / 知识点，结束时清干净。

    删除顺序要照顾外键：学习状态与消息 → 会话 → 知识点 → 文本块 → 文档 → 账号。
    """
    state = {"users": [], "sessions": [], "kps": [], "docs": []}
    yield state

    with SessionLocal() as db:
        for session_id in state["sessions"]:
            db.query(Message).filter(Message.session_id == session_id).delete()
            db.query(TutorSession).filter(TutorSession.id == session_id).delete()

        for username in state["users"]:
            user = db.query(User).filter(User.username == username).first()
            if user is None:
                continue
            learner_id = user.learner_id
            db.query(LearnerKpState).filter(LearnerKpState.learner_id == learner_id).delete()
            db.query(LearnerProfile).filter(LearnerProfile.learner_id == learner_id).delete()
            for item in db.query(TutorSession).filter(TutorSession.learner_id == learner_id).all():
                db.query(Message).filter(Message.session_id == item.id).delete()
                db.delete(item)
            db.delete(user)

        for kp_id in state["kps"]:
            db.query(LearnerKpState).filter(LearnerKpState.knowledge_point_id == kp_id).delete()
            for item in db.query(TutorSession).filter(TutorSession.knowledge_point_id == kp_id).all():
                db.query(Message).filter(Message.session_id == item.id).delete()
                db.delete(item)
            db.query(KnowledgePoint).filter(KnowledgePoint.id == kp_id).delete()

        for doc_id in state["docs"]:
            db.query(Chunk).filter(Chunk.document_id == doc_id).delete()
            db.query(Document).filter(Document.id == doc_id).delete()

        db.commit()
    client.cookies.clear()


# --------------------------------------------------------------------------- #
# 助手
# --------------------------------------------------------------------------- #
def _name(prefix: str = "sec") -> str:
    return f"{prefix}{uuid4().hex[:10]}"


def _register(username: str) -> str:
    """注册并保持登录，返回该账号的 learner_id。"""
    res = client.post("/api/auth/register", json={"username": username, "password": PASSWORD})
    assert res.status_code == 200, res.text
    return res.json()["user"]["learner_id"]


def _logout() -> None:
    client.cookies.clear()


def _own_kp(learner_id: str, cleanup_state: dict) -> int:
    """给指定 learner 造一份**归属于他**的文档 + 知识点。

    `owner_learner_id` 必须显式设置 —— 不设的话会落到默认的 `local`，
    于是注册用户访问 `/api/tutor/start` 会因知识点归属校验拿 404，
    测到的就不是会话归属了。
    """
    with SessionLocal() as db:
        document = Document(
            owner_learner_id=learner_id,
            file_name="session_isolation_fixture.txt",
            file_type="text",
            file_size=200,
            file_hash=uuid4().hex + uuid4().hex,
            storage_path="test/session_isolation_fixture.txt",
            page_count=1,
            char_count=200,
            chunk_count=1,
            kp_count=1,
            parse_status="ready",
            progress=100,
        )
        db.add(document)
        db.flush()

        db.add(
            Chunk(
                document_id=document.id,
                chunk_index=0,
                content="进程是程序的一次执行过程，是资源分配和调度的基本单位。",
                page_start=1,
                page_end=1,
                block_type="text",
                heading_path=["第三章 进程管理"],
                char_count=28,
            )
        )

        point = KnowledgePoint(
            document_id=document.id,
            title="进程与线程的区别",
            title_norm="进程与线程的区别",
            summary="进程是资源分配的基本单位，线程是处理机调度的基本单位。",
            details="进程是资源分配的基本单位；线程是处理机调度的基本单位。",
            key_points=["进程是资源分配的基本单位"],
            difficulty=3,
            importance=5,
            confidence=Decimal("0.90"),
            verify_status="unverified",
            heading_path=["第三章 进程管理"],
            source_chunk_indexes=[0],
            source_pages=[1],
            order_index=0,
        )
        db.add(point)
        db.commit()

        cleanup_state["docs"].append(document.id)
        cleanup_state["kps"].append(point.id)
        return int(point.id)


def _make_session(learner_id: str, kp_id: int, *, title: str = "审计夹具会话") -> int:
    """直接在库里造一条属于指定 learner 的会话。

    不走 `/api/tutor/start` 是因为要构造"**别人**的会话"这个前置条件 ——
    接口本身不允许你给别人建会话。
    """
    with SessionLocal() as db:
        session = TutorSession(
            learner_id=learner_id,
            knowledge_point_id=kp_id,
            title=title,
            status=SessionStatus.ACTIVE,
        )
        db.add(session)
        db.commit()
        return int(session.id)


def _message_count(session_id: int) -> int:
    with SessionLocal() as db:
        return db.query(Message).filter(Message.session_id == session_id).count()


# =========================================================================== #
# 一、V1：匿名场景
# =========================================================================== #
def test_anonymous_can_still_read_local_session(tutor_kp, tutor_env, cleanup) -> None:
    """⚠️ **既有契约不能误伤**：匿名请求解析为 `local`，
    而匿名走 `/api/tutor/start` 建出来的会话 learner_id 也是 `local`，
    所以它必须读得到自己的会话。

    这条是"加归属校验"最容易踩坏的用例 —— 修完 V1 之后如果它红了，
    说明把匿名路径一并堵死了，那是破坏功能而不是修漏洞。
    """
    _logout()
    created = client.post(
        "/api/tutor/start", json={"knowledge_point_id": tutor_kp["kp_id"]}
    )
    assert created.status_code == 200, created.text
    session_id = created.json()["session_id"]
    cleanup["sessions"].append(session_id)

    res = client.get(f"/api/tutor/sessions/{session_id}")
    assert res.status_code == 200, res.text
    assert res.json()["session_id"] == session_id
    assert res.json()["learner_id"] == DEFAULT_LEARNER


def test_anonymous_cannot_read_other_learners_session(tutor_kp, tutor_env, cleanup) -> None:
    """**V1 的核心用例**：未登录访客不能读到真实用户的会话。

    这正是审计里被实证利用的那条路径 —— 曾经返回 200，
    并在响应体里带着完整对话与作答。
    """
    _logout()
    foreign = _make_session("u" + uuid4().hex[:16], tutor_kp["kp_id"])
    cleanup["sessions"].append(foreign)

    res = client.get(f"/api/tutor/sessions/{foreign}")
    assert res.status_code == 404, res.text
    assert "messages" not in res.text and "evaluations" not in res.text


# =========================================================================== #
# 二、V1：两个真实用户之间
# =========================================================================== #
def test_user_a_cannot_read_user_b_session(tutor_env, cleanup) -> None:
    """用户 A 登录后读不到用户 B 的会话；B 自己读得到（不误伤）。"""
    name_a, name_b = _name("sea"), _name("seb")
    cleanup["users"] += [name_a, name_b]

    learner_a = _register(name_a)
    kp_a = _own_kp(learner_a, cleanup)
    _logout()
    learner_b = _register(name_b)
    kp_b = _own_kp(learner_b, cleanup)

    # B 建会话、自己能读
    created = client.post("/api/tutor/start", json={"knowledge_point_id": kp_b})
    assert created.status_code == 200, created.text
    session_b = created.json()["session_id"]
    cleanup["sessions"].append(session_b)
    assert client.get(f"/api/tutor/sessions/{session_b}").status_code == 200

    session_a = _make_session(learner_a, kp_a)
    cleanup["sessions"].append(session_a)

    # 换成 A
    _logout()
    assert (
        client.post("/api/auth/login", json={"username": name_a, "password": PASSWORD}).status_code
        == 200
    )

    forbidden = client.get(f"/api/tutor/sessions/{session_b}")
    assert forbidden.status_code == 404, f"A 读到了 B 的会话：{forbidden.text[:200]}"
    # A 自己的照常可读
    assert client.get(f"/api/tutor/sessions/{session_a}").status_code == 200

    # 反向也验一遍，避免"只是 A 恰好被拒"
    _logout()
    client.post("/api/auth/login", json={"username": name_b, "password": PASSWORD})
    assert client.get(f"/api/tutor/sessions/{session_b}").status_code == 200
    assert client.get(f"/api/tutor/sessions/{session_a}").status_code == 404
    _logout()


def test_foreign_and_missing_session_are_indistinguishable(tutor_kp, tutor_env, cleanup) -> None:
    """**不能靠状态码或响应体区分「不存在」与「存在但不属于你」。**

    否则攻击者可以拿它当探测器，逐个 id 试出哪些会话真实存在。

    ⚠️ 比对时要把**被回显的 id 归一化掉** —— 错误信息里带 `会话 {id} 不存在。`
    是常规写法，那个 id 本来就是请求方自己传的，回显它不泄漏任何东西。
    真正要守的是：**除了这个回显，两种情况的响应完全一致**。
    """
    _logout()
    foreign = _make_session("u" + uuid4().hex[:16], tutor_kp["kp_id"])
    cleanup["sessions"].append(foreign)

    got_foreign = client.get(f"/api/tutor/sessions/{foreign}")
    got_missing = client.get("/api/tutor/sessions/99999999")

    assert got_foreign.status_code == got_missing.status_code == 404

    def normalized(res, own_id: int) -> str:
        return res.text.replace(str(own_id), "<id>")

    assert normalized(got_foreign, foreign) == normalized(got_missing, 99999999), (
        "除了回显的 id，两种情况响应必须一致，否则可被用来探测会话是否存在"
    )
    # 响应体里不能带任何会话内容
    for res in (got_foreign, got_missing):
        assert "messages" not in res.text and "evaluations" not in res.text


# =========================================================================== #
# 三、V2：写入越权（Runtime 层）
# =========================================================================== #
@pytest.mark.asyncio
async def test_runtime_start_rejects_foreign_session(tutor_kp, tutor_env, cleanup) -> None:
    """`start(session_id=...)` 不能续跑别人的会话。"""
    foreign = _make_session("u" + uuid4().hex[:16], tutor_kp["kp_id"])
    cleanup["sessions"].append(foreign)

    with SessionLocal() as db:
        runtime = TutorRuntime(db, learner_id=DEFAULT_LEARNER)
        with pytest.raises(TutorError) as exc:
            await runtime.start(knowledge_point_id=tutor_kp["kp_id"], session_id=foreign)

    assert exc.value.status_code == 404
    assert exc.value.code == "session_not_found"


@pytest.mark.asyncio
async def test_runtime_submit_answer_rejects_foreign_session(tutor_kp, tutor_env, cleanup) -> None:
    """`submit_answer(session_id=...)` 不能往别人的会话里写。"""
    foreign = _make_session("u" + uuid4().hex[:16], tutor_kp["kp_id"])
    cleanup["sessions"].append(foreign)

    with SessionLocal() as db:
        runtime = TutorRuntime(db, learner_id=DEFAULT_LEARNER)
        with pytest.raises(TutorError) as exc:
            await runtime.submit_answer(session_id=foreign, user_answer="越权写入尝试")

    assert exc.value.status_code == 404
    assert exc.value.code == "session_not_found"


@pytest.mark.asyncio
async def test_rejected_write_leaves_foreign_session_untouched(
    tutor_kp, tutor_env, cleanup
) -> None:
    """被拒之后，**别人的会话里一条消息都不该多出来**。

    只断言"抛了异常"是不够的 —— 要证明**没有副作用**。
    """
    foreign = _make_session("u" + uuid4().hex[:16], tutor_kp["kp_id"])
    cleanup["sessions"].append(foreign)
    before = _message_count(foreign)

    with SessionLocal() as db:
        runtime = TutorRuntime(db, learner_id=DEFAULT_LEARNER)
        with pytest.raises(TutorError):
            await runtime.start(knowledge_point_id=tutor_kp["kp_id"], session_id=foreign)
        with pytest.raises(TutorError):
            await runtime.submit_answer(session_id=foreign, user_answer="污染尝试")

    assert _message_count(foreign) == before, "越权被拒后仍在他人会话里留下了消息"
    assert before == 0


@pytest.mark.asyncio
async def test_runtime_accepts_own_session(tutor_kp, tutor_env, cleanup) -> None:
    """**不能误伤**：自己的会话必须照常能续、能作答。"""
    own = _make_session(DEFAULT_LEARNER, tutor_kp["kp_id"])
    cleanup["sessions"].append(own)

    with SessionLocal() as db:
        runtime = TutorRuntime(db, learner_id=DEFAULT_LEARNER)
        turn = await runtime.start(knowledge_point_id=tutor_kp["kp_id"], session_id=own)
        assert turn.session_id == own

        answered = await runtime.submit_answer(session_id=own, user_answer="进程是资源分配单位")
        assert answered.session_id == own

    assert _message_count(own) > 0


# =========================================================================== #
# 四、V2：接口层端到端
# =========================================================================== #
def test_user_a_cannot_write_into_user_b_session(tutor_env, cleanup) -> None:
    """**端到端**：A 拿 B 的 session_id 调 `/api/tutor/answer` 与
    `/api/tutor/start` 都必须 404，且 B 的会话里不会多出任何消息。

    这是审计里 V2 的完整利用路径 —— 攻击者只需要知道（或枚举到）对方的会话 id。
    """
    name_a, name_b = _name("swa"), _name("swb")
    cleanup["users"] += [name_a, name_b]

    learner_a = _register(name_a)
    kp_a = _own_kp(learner_a, cleanup)
    _logout()
    learner_b = _register(name_b)
    kp_b = _own_kp(learner_b, cleanup)

    created = client.post("/api/tutor/start", json={"knowledge_point_id": kp_b})
    assert created.status_code == 200, created.text
    session_b = created.json()["session_id"]
    cleanup["sessions"].append(session_b)
    before = _message_count(session_b)

    # 换成 A，尝试往 B 的会话里写
    _logout()
    client.post("/api/auth/login", json={"username": name_a, "password": PASSWORD})

    res = client.post(
        "/api/tutor/answer", json={"session_id": session_b, "answer": "我是 A，越权写入"}
    )
    assert res.status_code == 404, f"A 写进了 B 的会话：{res.text[:200]}"

    res2 = client.post(
        "/api/tutor/start", json={"knowledge_point_id": kp_a, "session_id": session_b}
    )
    assert res2.status_code == 404, f"A 续跑了 B 的会话：{res2.text[:200]}"

    assert _message_count(session_b) == before, "A 的越权尝试在 B 的会话里留下了消息"
    _logout()


def test_user_a_can_still_answer_own_session(tutor_env, cleanup) -> None:
    """**不误伤**：A 对自己的会话作答照常成功。"""
    name_a = _name("sown")
    cleanup["users"].append(name_a)
    learner_a = _register(name_a)
    kp_a = _own_kp(learner_a, cleanup)

    created = client.post("/api/tutor/start", json={"knowledge_point_id": kp_a})
    assert created.status_code == 200, created.text
    session_id = created.json()["session_id"]
    cleanup["sessions"].append(session_id)

    res = client.post(
        "/api/tutor/answer", json={"session_id": session_id, "answer": "进程是资源分配单位"}
    )
    assert res.status_code == 200, res.text
    assert res.json()["session_id"] == session_id
    _logout()
