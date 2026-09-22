"""保存知识（Phase 3A）的服务层契约。

## 这里守四件事

1. **集合必须与 chunks 分开** —— 否则 `retrieve_knowledge` 在全库检索时会
   把保存的内容一起捞回来，"资料检索"的语义就被改掉了（见 service 模块注释）。
2. **向量 id 可推导** —— `saved:{id}`，于是"有没有索引"看 `embedding_id` 就知道。
3. **写向量失败不让保存失败** —— 向量是派生数据，用户的意图是"存下来"。
4. **列表恒定按时间倒序、恒定带 learner_id**。

用独立的测试集合（`test_store`），不碰生产集合。
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.models.conversation import Conversation, ConversationMessage, MessageAuthor
from app.services import saved_knowledge_service as svc


@pytest.fixture(autouse=True)
def _clean_saved_collection(test_store):
    """每个用例前把**保存知识专用的那个集合**清掉。

    ⚠️ `test_store` 夹具只重建 `TEST_COLLECTION`，而这里用的是
    `{TEST_COLLECTION}_saved_knowledge` —— 另一个集合。不单独清理的话，
    用例之间会互相看到对方的向量（实测踩过：peek 出来的是上一条用例的记录）。
    """
    name = svc.saved_collection_name(test_store)
    try:
        test_store.client().delete_collection(name)
    except Exception:  # noqa: BLE001 - 集合不存在时忽略
        pass
    yield
    try:
        test_store.client().delete_collection(name)
    except Exception:  # noqa: BLE001
        pass


def _seed(db, learner_id: str, *, question="什么是 JVM？", answer="JVM 是 Java 虚拟机。") -> int:
    """造一条对话 + 一轮问答，返回助手消息 id。

    ⚠️ **必须用调用方的 session**：MySQL 默认 REPEATABLE READ，
    夹具 session 的旧快照看不见另一个 session 刚提交的行 ——
    用独立 session 插入会让 `save_from_message` 报"消息不存在"。
    """
    conv = Conversation(learner_id=learner_id, title="svc 测试")
    db.add(conv)
    db.commit()
    db.refresh(conv)
    db.add(ConversationMessage(conversation_id=conv.id, role=MessageAuthor.USER, content=question))
    db.commit()
    assistant = ConversationMessage(
        conversation_id=conv.id, role=MessageAuthor.ASSISTANT, content=answer
    )
    db.add(assistant)
    db.commit()
    db.refresh(assistant)
    return int(assistant.id)


def _learner() -> str:
    return f"svc{uuid4().hex[:10]}"


# --------------------------------------------------------------------------- #
# 一、集合隔离：这是本阶段最重要的一条设计约束
# --------------------------------------------------------------------------- #
def test_saved_collection_is_separate_from_chunks(test_store) -> None:
    """保存知识用的集合**必须**与 chunks 的集合不同名。

    同名（或复用同一个集合）会让 `retrieve_knowledge` 在 `document_ids=None`
    时把保存的内容一起返回 —— 那等于**改了它的语义**，而项目有一条硬约束：
    引用优先级是"用户资料 > 联网证据 > 模型通识"，保存的内容属于哪一档
    是需要单独定义的事，不能在 3A 悄悄混进去。
    """
    saved_name = svc.saved_collection_name(test_store)
    assert saved_name != test_store.collection_name
    assert saved_name.startswith(test_store.collection_name)
    assert svc.SAVED_COLLECTION_SUFFIX in saved_name


def test_vector_id_is_derivable() -> None:
    """向量 id 从主键就能算出来 —— 不需要额外存状态。"""
    assert svc.vector_id(42) == "saved:42"
    # 稳定：两次调用同一个值
    assert svc.vector_id(42) == svc.vector_id(42)


# --------------------------------------------------------------------------- #
# 二、送去向量化的文本
# --------------------------------------------------------------------------- #
def test_embed_text_puts_question_first() -> None:
    """问题在前 —— 它是"我以前学过 X 吗"最好的匹配信号。"""
    record = svc.SavedKnowledge(learner_id="x", question="什么是 JVM？", answer="一个虚拟机")
    text = svc.embed_text(record)
    assert text.startswith("什么是 JVM？")
    assert "一个虚拟机" in text


def test_embed_text_is_truncated() -> None:
    """长回答要被截断 —— 尾部的展开与例子会稀释向量。"""
    record = svc.SavedKnowledge(
        learner_id="x", question="问", answer="答" * (svc.EMBED_TEXT_LIMIT * 2)
    )
    assert len(svc.embed_text(record)) == svc.EMBED_TEXT_LIMIT


def test_web_urls_only_keeps_web_kind() -> None:
    """只收 `kind=web`；资料出处（document_id / page）不是 URL。"""
    citations = [
        {"kind": "web", "title": "A", "url": "https://a.example"},
        {"kind": "knowledge_base", "document_id": 1, "file_name": "a.pdf", "page": 2},
        {"kind": "web", "title": "没有 url"},
    ]
    assert svc._web_urls(citations) == [{"title": "A", "url": "https://a.example"}]
    assert svc._web_urls(None) is None
    assert svc._web_urls([]) is None
    assert svc._web_urls([{"kind": "knowledge_base", "document_id": 1}]) is None


# --------------------------------------------------------------------------- #
# 三、保存：成功路径会建索引并回填
# --------------------------------------------------------------------------- #
def test_save_indexes_and_backfills_embedding_id(session, mock_embedder, test_store) -> None:
    learner = _learner()
    message_id = _seed(session, learner)

    record = svc.save_from_message(
        session,
        learner_id=learner,
        message_id=message_id,
        provider=mock_embedder,
        store=test_store,
    )

    assert record.embedding_id == svc.vector_id(record.id), "索引成功后必须回填 embedding_id"
    # 向量真的进了集合（用测试集合，不碰生产）
    assert test_store.count(svc.saved_collection_name(test_store)) == 1


def test_save_writes_traceable_metadata(session, mock_embedder, test_store) -> None:
    """元数据里要有 learner_id 与 saved_id —— 将来跨对话检索要靠它们过滤。"""
    learner = _learner()
    message_id = _seed(session, learner)
    record = svc.save_from_message(
        session,
        learner_id=learner,
        message_id=message_id,
        provider=mock_embedder,
        store=test_store,
    )

    peek = test_store.peek(limit=1, name=svc.saved_collection_name(test_store))
    meta = peek["metadatas"][0]
    assert meta["saved_id"] == int(record.id)
    assert meta["learner_id"] == learner


def test_saved_items_are_isolated_per_learner(session, mock_embedder, test_store) -> None:
    """两个人各存一条 → 各自只查到自己的。"""
    alice = _learner()
    bob = _learner()
    a_msg = _seed(session, alice, question="Alice 的问题", answer="Alice 的回答")
    b_msg = _seed(session, bob, question="Bob 的问题", answer="Bob 的回答")

    svc.save_from_message(
        session, learner_id=alice, message_id=a_msg, provider=mock_embedder, store=test_store
    )
    svc.save_from_message(
        session, learner_id=bob, message_id=b_msg, provider=mock_embedder, store=test_store
    )

    a_items, a_total = svc.list_saved(session, learner_id=alice)
    b_items, b_total = svc.list_saved(session, learner_id=bob)

    assert a_total == 1 and b_total == 1
    assert a_items[0].question == "Alice 的问题"
    assert b_items[0].question == "Bob 的问题"


# --------------------------------------------------------------------------- #
# 四、失败行为：**保存成功、索引留空**（本阶段的关键约定）
# --------------------------------------------------------------------------- #
def test_embedding_failure_still_saves(session, test_store) -> None:
    """向量化抛异常 → 保存**照样成功**，`embedding_id` 留空，可事后回填。

    用户的意图是"存下来"，向量只是"将来能被搜到"的加速手段。
    让派生数据的失败回滚用户的主动操作，方向反了。
    """

    class _Boom:
        name = "boom"
        model = "boom"
        dim = 8

        async def embed(self, texts):  # noqa: ANN001, ANN202
            raise RuntimeError("模拟向量化服务不可用")

    learner = _learner()
    message_id = _seed(session, learner)
    record = svc.save_from_message(
        session, learner_id=learner, message_id=message_id, provider=_Boom(), store=test_store
    )

    assert record.id is not None, "保存必须成功"
    assert record.embedding_id is None, "索引失败时 embedding_id 应当留空（而不是回滚）"
    assert svc.list_saved(session, learner_id=learner)[1] == 1, "记录必须真的落库了"


def test_vectorstore_failure_still_saves(session, mock_embedder) -> None:
    """向量库写入失败（例如集合维度冲突）→ 同样只留空，不影响保存。"""

    class _BoomStore:
        collection_name = "boom_collection"

        def enforce_dimension(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
            return "ok"

        def upsert(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
            raise RuntimeError("模拟向量库不可用")

    learner = _learner()
    message_id = _seed(session, learner)
    record = svc.save_from_message(
        session,
        learner_id=learner,
        message_id=message_id,
        provider=mock_embedder,
        store=_BoomStore(),
    )

    assert record.embedding_id is None
    assert svc.list_saved(session, learner_id=learner)[1] == 1


def test_empty_body_skips_indexing_but_saves(session, mock_embedder, test_store) -> None:
    """正文为空（理论上不该发生）→ 跳过索引，但记录仍然有效。"""
    learner = _learner()
    conv = Conversation(learner_id=learner, title="空回答")
    session.add(conv)
    session.commit()
    session.refresh(conv)
    msg = ConversationMessage(
        conversation_id=conv.id, role=MessageAuthor.ASSISTANT, content=""
    )
    session.add(msg)
    session.commit()
    session.refresh(msg)
    message_id = int(msg.id)

    record = svc.save_from_message(
        session, learner_id=learner, message_id=message_id, provider=mock_embedder, store=test_store
    )
    assert record.embedding_id is None
    assert svc.list_saved(session, learner_id=learner)[1] == 1


# --------------------------------------------------------------------------- #
# 五、归属与校验
# --------------------------------------------------------------------------- #
def test_cannot_save_a_message_from_another_learner(session) -> None:
    """归属不符 → `SavedKnowledgeNotFound`（调用方负责转成 404）。"""
    owner = _learner()
    message_id = _seed(session, owner)

    with pytest.raises(svc.SavedKnowledgeNotFound):
        svc.save_from_message(session, learner_id=_learner(), message_id=message_id)


def test_cannot_save_a_user_message(session) -> None:
    learner = _learner()
    conv = Conversation(learner_id=learner, title="只有提问")
    session.add(conv)
    session.commit()
    session.refresh(conv)
    msg = ConversationMessage(
        conversation_id=conv.id, role=MessageAuthor.USER, content="只有我问的"
    )
    session.add(msg)
    session.commit()
    session.refresh(msg)
    message_id = int(msg.id)

    with pytest.raises(svc.SavedKnowledgeNotFound):
        svc.save_from_message(session, learner_id=learner, message_id=message_id)


def test_list_bounds_are_clamped(session) -> None:
    """limit 必须被夹在合理范围内 —— 不让一次请求把整库拉出来。"""
    learner = _learner()
    for n in range(3):
        msg = _seed(session, learner, question=f"q{n}", answer=f"a{n}")
        svc.save_from_message(session, learner_id=learner, message_id=msg)

    items, total = svc.list_saved(session, learner_id=learner, limit=9999, offset=-5)
    assert total == 3
    assert len(items) == 3  # 只有 3 条，夹紧后取到全部
