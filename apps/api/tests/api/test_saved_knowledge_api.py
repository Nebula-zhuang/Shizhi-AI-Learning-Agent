"""保存知识（Phase 3A）的 API 契约。

## 这一组最要紧的是**归属隔离**

"保存"是一个**写**入口，而且写进去的东西将来会被检索、被引用。
所以两条线都必须堵死：

- 不能保存**别人的**消息（否则等于把别人的对话内容搬进自己的知识库）
- 列表只能看到**自己的**

两条都刻意返回 404 而不是 403 —— 同 `study_service` 的理由：
403 等于确认"这个 id 是存在的"，会变成枚举工具。
"""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from app.db.session import SessionLocal
from app.main import app
from app.models.conversation import Conversation, ConversationMessage, MessageAuthor

client = TestClient(app)
PASSWORD = "Shizhi#2026"


# --------------------------------------------------------------------------- #
# 助手
# --------------------------------------------------------------------------- #
def _register(username: str) -> str:
    """注册并保持登录，返回该账号的 learner_id。"""
    res = client.post("/api/auth/register", json={"username": username, "password": PASSWORD})
    assert res.status_code == 200, res.text
    return res.json()["user"]["learner_id"]


def _seed_turn(
    learner_id: str,
    *,
    question: str = "什么是 JVM？",
    answer: str = "JVM 是 Java 虚拟机。",
    citations: list | None = None,
) -> tuple[int, int, int]:
    """造一条对话 + 一轮问答，返回 `(conversation_id, user_msg_id, assistant_msg_id)`。"""
    with SessionLocal() as db:
        conv = Conversation(learner_id=learner_id, title="保存测试")
        db.add(conv)
        db.commit()
        db.refresh(conv)

        user_msg = ConversationMessage(
            conversation_id=conv.id, role=MessageAuthor.USER, content=question
        )
        db.add(user_msg)
        db.commit()
        db.refresh(user_msg)

        assistant_msg = ConversationMessage(
            conversation_id=conv.id,
            role=MessageAuthor.ASSISTANT,
            content=answer,
            citations=citations,
        )
        db.add(assistant_msg)
        db.commit()
        db.refresh(assistant_msg)
        return int(conv.id), int(user_msg.id), int(assistant_msg.id)


def _save(message_id: int, tags: list[str] | None = None):
    body: dict = {"message_id": message_id}
    if tags is not None:
        body["tags"] = tags
    return client.post("/api/study/knowledge", json=body)


def _list(limit: int = 20, offset: int = 0):
    return client.get("/api/study/knowledge", params={"limit": limit, "offset": offset})


# --------------------------------------------------------------------------- #
# 一、保存：正文来自真实消息
# --------------------------------------------------------------------------- #
def test_save_copies_the_real_turn() -> None:
    """保存后 question/answer 应当**正是那一轮**的原文，并记录回溯 id。"""
    learner = _register(f"saved{uuid4().hex[:10]}")
    _conv, _user_id, assistant_id = _seed_turn(
        learner, question="什么是 JVM？", answer="JVM 是 Java 虚拟机。"
    )

    res = _save(assistant_id, tags=["JVM", "基础"])
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["question"] == "什么是 JVM？"
    assert body["answer"] == "JVM 是 Java 虚拟机。"
    assert body["source_message_id"] == assistant_id
    assert body["tags"] == ["JVM", "基础"]
    # Phase 3A 不做自动推荐 → 这两个字段此刻应当是空的
    assert body["kp_ids"] is None
    # 未传向量依赖时走默认 provider，这里不断言 embedding_id（见 service 测试）


def test_save_extracts_only_web_urls() -> None:
    """`source_urls` 只收 `kind=web` 的引用。

    资料出处（document_id / page）不是 URL —— 混进来会让字段名撒谎。
    """
    learner = _register(f"url{uuid4().hex[:10]}")
    _conv, _user_id, assistant_id = _seed_turn(
        learner,
        citations=[
            {"kind": "web", "title": "Java 25 发布说明", "url": "https://example.com/java25"},
            {"kind": "knowledge_base", "document_id": 69, "file_name": "os.pdf", "page": 5},
        ],
    )

    body = _save(assistant_id).json()
    assert body["source_urls"] == [
        {"title": "Java 25 发布说明", "url": "https://example.com/java25"}
    ]


def test_save_without_citations_leaves_urls_empty() -> None:
    learner = _register(f"nourl{uuid4().hex[:10]}")
    _conv, _user_id, assistant_id = _seed_turn(learner, citations=None)

    assert _save(assistant_id).json()["source_urls"] is None


def test_save_without_question_still_works() -> None:
    """回答前面没有提问时（比如对话被裁剪过），question 允许为空 —— 不该报错。"""
    learner = _register(f"noq{uuid4().hex[:10]}")
    with SessionLocal() as db:
        conv = Conversation(learner_id=learner, title="孤零零的回答")
        db.add(conv)
        db.commit()
        db.refresh(conv)
        msg = ConversationMessage(
            conversation_id=conv.id, role=MessageAuthor.ASSISTANT, content="凭空一句回答。"
        )
        db.add(msg)
        db.commit()
        db.refresh(msg)
        msg_id = int(msg.id)

    body = _save(msg_id).json()
    assert body["question"] == ""
    assert body["answer"] == "凭空一句回答。"


# --------------------------------------------------------------------------- #
# 二、不能保存不该保存的东西
# --------------------------------------------------------------------------- #
def test_cannot_save_a_user_message() -> None:
    """只能保存**回答**。保存提问没有意义 —— 检索时会以用户口吻被引用出来。"""
    learner = _register(f"usermsg{uuid4().hex[:10]}")
    _conv, user_id, _assistant_id = _seed_turn(learner)

    assert _save(user_id).status_code == 404


def test_cannot_save_someone_elses_message() -> None:
    """**归属隔离**：拿别人的消息 id 保存 → 404。"""
    owner = _register(f"owner{uuid4().hex[:10]}")
    _conv, _user_id, assistant_id = _seed_turn(owner, answer="这是别人的回答。")

    # 换一个人登录
    attacker = _register(f"attacker{uuid4().hex[:10]}")
    assert attacker != owner

    res = _save(assistant_id)
    assert res.status_code == 404, res.text
    assert "不存在" in res.json()["detail"]


def test_saving_a_missing_message_is_404() -> None:
    _register(f"missing{uuid4().hex[:10]}")
    assert _save(99_999_999).status_code == 404


def test_save_requires_login() -> None:
    client.cookies.clear()
    try:
        assert _save(1).status_code == 401
    finally:
        pass


# --------------------------------------------------------------------------- #
# 三、列表：只看得见自己的
# --------------------------------------------------------------------------- #
def test_list_returns_my_saved_items_newest_first() -> None:
    learner = _register(f"list{uuid4().hex[:10]}")
    _c1, _u1, a1 = _seed_turn(learner, question="第一个问题", answer="第一个回答")
    _c2, _u2, a2 = _seed_turn(learner, question="第二个问题", answer="第二个回答")

    assert _save(a1).status_code == 200
    assert _save(a2).status_code == 200

    body = _list().json()
    assert body["total"] == 2
    # 倒序：刚保存的（第二个）在最前
    assert [i["question"] for i in body["items"]] == ["第二个问题", "第一个问题"]


def test_list_only_returns_my_own() -> None:
    """**归属隔离**：另一个账号看不到我保存的东西。"""
    mine = _register(f"mine{uuid4().hex[:10]}")
    _c, _u, a = _seed_turn(mine, question="我的秘密问题")
    assert _save(a).status_code == 200

    other = _register(f"other{uuid4().hex[:10]}")
    assert other != mine

    body = _list().json()
    assert body["total"] == 0
    assert body["items"] == []


def test_list_pagination() -> None:
    learner = _register(f"page{uuid4().hex[:10]}")
    ids = []
    for n in range(3):
        _c, _u, a = _seed_turn(learner, question=f"问题{n}", answer=f"回答{n}")
        assert _save(a).status_code == 200
        ids.append(a)

    first = _list(limit=2, offset=0).json()
    assert first["total"] == 3, "total 是**总数**，不随分页变"
    assert len(first["items"]) == 2

    second = _list(limit=2, offset=2).json()
    assert second["total"] == 3
    assert len(second["items"]) == 1

    # 两页不重叠
    got = [i["id"] for i in first["items"]] + [i["id"] for i in second["items"]]
    assert len(set(got)) == 3


def test_list_requires_login() -> None:
    client.cookies.clear()
    assert _list().status_code == 401
