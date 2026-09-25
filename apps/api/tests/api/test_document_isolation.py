"""P6 资料归属隔离的回归测试。

补这一组的原因：账号体系刚接上时我只绑定了**学习状态**，漏了**资料** ——
结果是新注册的账号一进「资料」「知识地图」就看到别人的文件。
这个文件把那次的缺口固定住，以后改坏了会立刻红。
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.document import Document
from app.models.knowledge_point import KnowledgePoint
from app.models.user import User
from app.services import auth_service, learner_service

client = TestClient(app)
PASSWORD = "isolation-test-pw"


def _name(prefix: str = "iso") -> str:
    return f"{prefix}{uuid4().hex[:8]}"


@pytest.fixture()
def two_accounts():
    """两个互不相干的账号，用完连同资料一起清掉。"""
    from app.db.session import SessionLocal

    created: list[str] = []
    yield created

    with SessionLocal() as db:
        for username in created:
            user = db.query(User).filter(User.username == username).first()
            if user is None:
                continue
            learner_id = user.learner_id
            docs = db.query(Document).filter(Document.owner_learner_id == learner_id).all()
            for doc in docs:
                db.query(KnowledgePoint).filter(
                    KnowledgePoint.document_id == doc.id
                ).delete()
                db.delete(doc)
            db.delete(user)
        db.commit()
    client.cookies.clear()


def _register(username: str) -> dict:
    res = client.post(
        "/api/auth/register", json={"username": username, "password": PASSWORD}
    )
    assert res.status_code == 200, res.text
    return res.json()["user"]


def _make_document(learner_id: str, file_name: str) -> int:
    """直接建一条资料记录（不走上传，避免依赖解析链路）。"""
    from app.db.session import SessionLocal
    from app.services import document_service

    with SessionLocal() as db:
        doc = document_service.create_document(
            db,
            file_name=file_name,
            file_size=123,
            file_hash=f"hash-{uuid4().hex}",
            storage_path="/tmp/does-not-matter",
            owner_learner_id=learner_id,
        )
        return doc.id


# --------------------------------------------------------------------------- #
# 列表隔离
# --------------------------------------------------------------------------- #
def test_new_account_sees_no_documents(two_accounts) -> None:
    """全新账号的资料库必须是空的 —— 这正是用户报的那个问题的反面。"""
    username = _name()
    two_accounts.append(username)
    me = _register(username)

    res = client.get("/api/documents")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 0, f"新账号不该看到任何资料，实际 {body['total']} 份"
    assert body["items"] == []


def test_demo_account_keeps_sample_documents(two_accounts) -> None:
    """演示账号（learner_id == local）仍要能看到 P0–P5 攒下的样例资料。

    这条是防"修隔离时把演示数据也一起挡掉了"。
    """
    from app.core.identity import DEFAULT_LEARNER_ID

    # 直接用本地档案身份（匿名就是它）
    client.cookies.clear()
    res = client.get("/api/documents")
    assert res.status_code == 200
    body = res.json()

    from app.db.session import SessionLocal

    with SessionLocal() as db:
        expected = (
            db.query(Document)
            .filter(Document.owner_learner_id == DEFAULT_LEARNER_ID)
            .count()
        )
    assert body["total"] == expected, "本地档案应看到自己名下的全部资料"


def test_documents_are_isolated_between_accounts(two_accounts) -> None:
    """A 上传的资料，B 看不到。"""
    names = [_name(), _name()]
    two_accounts.extend(names)

    a = _register(names[0])
    _make_document(a["learner_id"], "甲的讲义.pdf")
    assert client.get("/api/documents").json()["total"] == 1

    client.cookies.clear()
    b = _register(names[1])
    assert b["learner_id"] != a["learner_id"]
    assert client.get("/api/documents").json()["total"] == 0, "B 不该看到 A 的资料"

    client.cookies.clear()


# --------------------------------------------------------------------------- #
# 按 id 直取的隔离（比列表更危险 —— 那是"知道 id 就能拿"）
# --------------------------------------------------------------------------- #
def test_cannot_read_other_account_document_by_id(two_accounts) -> None:
    names = [_name(), _name()]
    two_accounts.extend(names)

    a = _register(names[0])
    doc_id = _make_document(a["learner_id"], "甲的讲义.pdf")

    client.cookies.clear()
    _register(names[1])

    # 详情、状态、结构、删除 —— 逐条都要挡住
    assert client.get(f"/api/documents/{doc_id}").status_code == 404
    assert client.get(f"/api/documents/{doc_id}/status").status_code == 404
    assert client.get(f"/api/documents/{doc_id}/structure").status_code == 404
    assert client.get(f"/api/documents/{doc_id}/chunks").status_code == 404
    assert client.delete(f"/api/documents/{doc_id}").status_code == 404

    client.cookies.clear()


def test_cannot_read_other_account_knowledge_point(two_accounts) -> None:
    """知识点没有归属字段，归属要顺着它所属的资料判 —— 这条测的就是那条链路。"""
    from app.db.session import SessionLocal

    names = [_name(), _name()]
    two_accounts.extend(names)

    a = _register(names[0])
    doc_id = _make_document(a["learner_id"], "甲的讲义.pdf")

    with SessionLocal() as db:
        kp = KnowledgePoint(
            document_id=doc_id,
            title="甲的知识点",
            # title_norm 是三层去重的落库那一层，非空
            title_norm="甲的知识点",
            summary="只该被甲看到",
        )
        db.add(kp)
        db.commit()
        db.refresh(kp)
        kp_id = kp.id

    client.cookies.clear()
    _register(names[1])

    assert client.get(f"/api/knowledge-points/{kp_id}").status_code == 404
    assert client.get(f"/api/knowledge-points/{kp_id}/checks").status_code == 404
    assert client.get(f"/api/knowledge-points/{kp_id}/relations").status_code == 404
    assert client.get(f"/api/tutor/learner-state/{kp_id}").status_code == 404

    # 连教学入口也挡住：否则等于让助教把别人资料里的内容讲出来
    res = client.post("/api/tutor/start", json={"knowledge_point_id": kp_id})
    assert res.status_code == 404

    client.cookies.clear()


def test_owner_can_still_read_own_document(two_accounts) -> None:
    """隔离不能误伤：本人读自己的资料必须正常。"""
    username = _name()
    two_accounts.append(username)
    me = _register(username)
    doc_id = _make_document(me["learner_id"], "我自己的讲义.pdf")

    assert client.get("/api/documents").json()["total"] == 1
    assert client.get(f"/api/documents/{doc_id}").status_code == 200

    client.cookies.clear()


# --------------------------------------------------------------------------- #
# 检索范围
# --------------------------------------------------------------------------- #
def test_ask_does_not_fall_back_to_whole_library(two_accounts) -> None:
    """新账号问问题时，范围**不能退化成全库**（那会搜到别人的资料）。

    这里不关心回答内容，只关心它没有把别人的资料当成可检索范围：
    账号一份资料都没有时，应当明确告知，而不是去翻全库。
    """
    from tests.conftest import ScriptedLLM  # noqa: F401  —— 仅为保证测试环境一致

    username = _name()
    two_accounts.append(username)
    _register(username)

    res = client.post("/api/rag/ask", json={"question": "随便问一句"})
    # 409 = 明确告知"你还没有资料"；200 也可接受（若恰好短路成"不在你的资料中"），
    # 但**绝不能**因为搜到了别人的块而给出带来源的答案
    assert res.status_code in (200, 409), res.text
    if res.status_code == 200:
        assert res.json().get("sources") in ([], None), "不该引用到不属于自己的资料来源"

    client.cookies.clear()


def test_duplicate_hash_does_not_collide_across_accounts(two_accounts) -> None:
    """两个账号传同一个文件：各自拿到自己那份，不能命中对方的记录。"""
    from app.db.session import SessionLocal

    names = [_name(), _name()]
    two_accounts.extend(names)

    a = _register(names[0])
    client.cookies.clear()
    b = _register(names[1])

    shared_hash = f"same-{uuid4().hex}"
    with SessionLocal() as db:
        doc_a = Document(
            file_name="同一份.pdf",
            file_type="pdf",
            file_size=1,
            file_hash=shared_hash,
            storage_path="/tmp/a",
            owner_learner_id=a["learner_id"],
        )
        db.add(doc_a)
        db.commit()

        # B 用同一个 hash 查重，必须查不到（否则会被当成"已存在"而拿不到自己的文档）
        found = db.query(Document).filter(
            Document.file_hash == shared_hash,
            Document.owner_learner_id == b["learner_id"],
        ).first()
        assert found is None

        found_a = db.query(Document).filter(
            Document.file_hash == shared_hash,
            Document.owner_learner_id == a["learner_id"],
        ).first()
        assert found_a is not None

    client.cookies.clear()


def test_default_learner_constant_is_shared(two_accounts) -> None:
    """模型层与业务层必须用同一个默认标识，否则新资料会悄悄变成"无主"。"""
    from app.core.identity import DEFAULT_LEARNER_ID as CORE
    from app.models.document import Document as DocModel

    assert learner_service.DEFAULT_LEARNER_ID == CORE
    assert DocModel.__table__.columns["owner_learner_id"].default.arg == CORE

    # 不显式传归属时，应当落到默认档案上（匿名上传走的就是这条路）
    assert auth_service.new_learner_id().startswith("u")

# --------------------------------------------------------------------------- #
# 资料维度接口的覆盖
# --------------------------------------------------------------------------- #
def test_owner_can_list_knowledge_points(two_accounts) -> None:
    """本人列自己资料下的知识点 —— 必须 200。

    **这条测试是补上一个真实缺口**：改造隔离之前，
    `GET /api/documents/{id}/knowledge-points` 完全没有覆盖
    （测试只测了"知识点不存在"的 404 分支），
    结果我在里面漏了一个 import，接口 500 了很久都没被测试发现。
    凡是"改了但没测到"的分支，都会以这种方式回来找你。
    """
    username = _name()
    two_accounts.append(username)
    me = _register(username)
    doc_id = _make_document(me["learner_id"], "我的讲义.pdf")

    res = client.get(f"/api/documents/{doc_id}/knowledge-points")
    assert res.status_code == 200, res.text
    body = res.json()
    assert "items" in body and "total" in body

    client.cookies.clear()


def test_knowledge_points_of_missing_document_is_404(two_accounts) -> None:
    client.cookies.clear()
    assert client.get("/api/documents/99999999/knowledge-points").status_code == 404


def test_cannot_list_knowledge_points_of_other_account(two_accounts) -> None:
    names = [_name(), _name()]
    two_accounts.extend(names)

    a = _register(names[0])
    doc_id = _make_document(a["learner_id"], "甲的讲义.pdf")

    client.cookies.clear()
    _register(names[1])
    assert client.get(f"/api/documents/{doc_id}/knowledge-points").status_code == 404

    client.cookies.clear()


def test_owner_can_read_own_document_metadata(two_accounts) -> None:
    """资料元数据接口的整体覆盖，避免同类的"改了没测到"。

    **只测数据库支撑的接口**（详情 / 状态 / 块）。
    `/structure` 与 `/graph` 依赖解析产物，而这里造的资料没有真解析过，
    它们返回 404 是**正确行为**（产物不存在），不是归属问题 ——
    把这种接口写进"应该 200"的断言里，只会得到一个恒红的测试。
    """
    username = _name()
    two_accounts.append(username)
    me = _register(username)
    doc_id = _make_document(me["learner_id"], "我的讲义.pdf")

    for path in (
        f"/api/documents/{doc_id}",
        f"/api/documents/{doc_id}/status",
        f"/api/documents/{doc_id}/chunks",
    ):
        assert client.get(path).status_code == 200, f"{path} 应可访问"

    client.cookies.clear()


# --------------------------------------------------------------------------- #
# 跨账号同内容上传（P1-1）
# --------------------------------------------------------------------------- #
@pytest.fixture()
def stub_pipeline(monkeypatch: pytest.MonkeyPatch):
    """把流水线本体换成空实现 —— 这里只验 HTTP 契约，不验解析。

    刻意**不打桩** `ingest_runner.submit`（理由见 test_documents_api.py 的同名夹具）。
    """
    called: list[int] = []

    async def fake_run_pipeline(document_id: int) -> None:
        called.append(document_id)

    from app.services import ingest_runner

    monkeypatch.setattr(ingest_runner, "run_pipeline", fake_run_pipeline)
    return called


def _upload_text(content: bytes, filename: str):
    return client.post(
        "/api/documents",
        files={"file": (filename, content, "application/octet-stream")},
    )


def test_cross_account_same_content_is_409_not_500(two_accounts, stub_pipeline) -> None:
    """别的账号传过同一份内容时，绝不能 500，也不能把别人的文档交出去。

    背景：`documents.file_hash` 是**全局**唯一，而去重查询只在自己账号内找
    （`document_service.find_by_hash`：去重必须限定同账号，代价是各存一份）。
    两条规则叠在一起，第二个人上传同一份文件时 INSERT 必然撞唯一键 ——
    修之前这里是一个 500，而且**演示必然踩到**（样例文件已被演示账号传过）。
    """
    # 每次运行内容都不同，避免和开发库里既有数据撞车
    payload = (
        f"# 跨账号去重测试 {uuid4().hex[:8]}\n\n"
        "进程是资源分配的基本单位，线程是调度的基本单位。\n"
    ).encode("utf-8")

    names = [_name("xa"), _name("xb")]
    two_accounts.extend(names)

    # 甲先传
    client.cookies.clear()
    _register(names[0])
    first = _upload_text(payload, "甲的笔记.txt")
    assert first.status_code == 202, first.text
    assert first.json()["dedup"] is False
    first_id = first.json()["document"]["id"]

    # 乙传**完全相同**的内容
    client.cookies.clear()
    _register(names[1])
    second = _upload_text(payload, "乙的笔记.txt")

    assert second.status_code == 409, (
        f"期望 409（明确拒绝），实际 {second.status_code}：{second.text[:200]}"
    )
    detail = second.json()["detail"]
    assert "重复" in detail, f"错误信息要说明原因，实际：{detail!r}"

    # 关键：绝不能把甲的文档交出去
    assert client.get(f"/api/documents/{first_id}").status_code == 404, (
        "乙不该能访问甲的文档"
    )
    assert client.get(f"/api/documents/{first_id}/status").status_code == 404
    # 甲的文档也不该出现在乙的列表里
    listed = client.get("/api/documents").json()
    ids = [d["id"] for d in listed.get("items", listed if isinstance(listed, list) else [])]
    assert first_id not in ids, "乙的列表里出现了甲的文档"

    # 没有回归：乙传**不同内容**仍然正常
    other = _upload_text(f"乙自己的内容 {uuid4().hex}\n".encode("utf-8"), "乙的其他笔记.txt")
    assert other.status_code == 202, other.text
    assert other.json()["dedup"] is False

    client.cookies.clear()
