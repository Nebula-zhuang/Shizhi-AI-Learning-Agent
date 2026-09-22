"""跨对话检索（Phase 3B）的 service 层契约。

## 这一组的核心是**两道隔离**

1. **向量层** `where={"learner_id": ...}` —— 只在自己的向量里找
2. **DB 层** 拿到 `saved_id` 后再按 `learner_id` 查一遍 —— 查不到就丢弃

只测第 1 道是不够的：向量 metadata 是**写入时**的快照，写错了越权就是静默的。
所以下面有一条**专门伪造 metadata** 的用例，逼出第 2 道防线。

全部用 `MockEmbeddingProvider` + 独立测试集合，**不打阿里云**。
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.models.conversation import Conversation, ConversationMessage, MessageAuthor
from app.services import saved_knowledge_service as svc


@pytest.fixture(autouse=True)
def _clean_saved_collection(test_store):
    """清掉保存知识专用的集合（`test_store` 夹具只管 TEST_COLLECTION 那个）。"""
    name = svc.saved_collection_name(test_store)
    for action in ("before",):
        del action
    try:
        test_store.client().delete_collection(name)
    except Exception:  # noqa: BLE001
        pass
    yield
    try:
        test_store.client().delete_collection(name)
    except Exception:  # noqa: BLE001
        pass


def _seed_and_save(
    db,
    learner_id: str,
    *,
    question: str,
    answer: str,
    provider,
    store,
):
    """造一轮问答并保存，返回保存记录。"""
    conv = Conversation(learner_id=learner_id, title="检索测试")
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
    return svc.save_from_message(
        db,
        learner_id=learner_id,
        message_id=int(assistant.id),
        provider=provider,
        store=store,
    )


def _learner() -> str:
    return f"sch{uuid4().hex[:10]}"


# --------------------------------------------------------------------------- #
# 一、跨对话：这正是本阶段要的能力
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_finds_saved_knowledge_across_conversations(
    session, mock_embedder, test_store
) -> None:
    """在**新的对话**里检索，能找到**旧对话**里保存的内容。

    这就是设计文档那句「我以前学过 JVM 吗」——
    检索与"当前在哪个对话"完全无关。
    """
    learner = _learner()
    _seed_and_save(
        session,
        learner,
        question="什么是 JVM？",
        answer="JVM 是 Java 虚拟机，负责把字节码翻译成机器码执行。",
        provider=mock_embedder,
        store=test_store,
    )

    hits = await svc.search_saved(
        session, learner_id=learner, query="JVM 是什么", provider=mock_embedder, store=test_store
    )

    assert hits, "跨对话检索应当能找到旧对话里保存的内容"
    assert "JVM" in hits[0].question
    assert hits[0].saved_id > 0


@pytest.mark.asyncio
async def test_hit_carries_the_full_turn(session, mock_embedder, test_store) -> None:
    """命中项要带回完整的一轮（问题 + 回答），模型才有东西可用。"""
    learner = _learner()
    _seed_and_save(
        session,
        learner,
        question="虚拟线程和平台线程的区别？",
        answer="虚拟线程由 JVM 调度，平台线程由操作系统调度。",
        provider=mock_embedder,
        store=test_store,
    )

    hits = await svc.search_saved(
        session,
        learner_id=learner,
        query="虚拟线程",
        provider=mock_embedder,
        store=test_store,
    )

    assert hits
    assert "虚拟线程" in hits[0].answer
    assert hits[0].created_at is not None
    assert hits[0].distance >= 0


@pytest.mark.asyncio
async def test_results_are_sorted_by_distance(session, mock_embedder, test_store) -> None:
    learner = _learner()
    _seed_and_save(
        session, learner, question="什么是 JVM？", answer="Java 虚拟机。",
        provider=mock_embedder, store=test_store,
    )
    _seed_and_save(
        session, learner, question="什么是三次握手？", answer="TCP 建连的三步。",
        provider=mock_embedder, store=test_store,
    )

    hits = await svc.search_saved(
        session, learner_id=learner, query="Java 虚拟机", provider=mock_embedder, store=test_store
    )
    distances = [h.distance for h in hits]
    assert distances == sorted(distances), "结果必须按距离升序（越近越靠前）"


# --------------------------------------------------------------------------- #
# 二、隔离：两层都要生效
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_does_not_return_another_learners_items(
    session, mock_embedder, test_store
) -> None:
    """**第 1 道**：向量 where 过滤 —— 别人的内容根本不进候选。"""
    owner = _learner()
    _seed_and_save(
        session, owner, question="我的密码存在哪？", answer="别人的隐私内容。",
        provider=mock_embedder, store=test_store,
    )

    attacker = _learner()
    hits = await svc.search_saved(
        session,
        learner_id=attacker,
        query="密码 存在哪",
        provider=mock_embedder,
        store=test_store,
    )
    assert hits == []


@pytest.mark.asyncio
async def test_db_layer_drops_a_spoofed_vector_metadata(
    session, mock_embedder, test_store
) -> None:
    """**第 2 道防线**：向量 metadata 撒谎时，DB 复核必须把它拦下。

    故意构造一条 `learner_id` 与检索者一致、但 `saved_id` 指向**别人**记录的向量。
    第 1 道（where 过滤）会放行它 —— 只有 DB 层的归属复核能救回来。
    """
    victim = _learner()
    victim_record = _seed_and_save(
        session,
        victim,
        question="受害者的私密笔记标题",
        answer="这是别人保存的内容，绝不该被检索到。",
        provider=mock_embedder,
        store=test_store,
    )

    attacker = _learner()
    # 伪造：learner_id 写成 attacker，saved_id 指向受害者的记录
    vector = (await mock_embedder.embed(["受害者的私密笔记标题"])) .vectors[0]
    test_store.upsert(
        ids=[svc.vector_id(int(victim_record.id))],
        documents=["受害者的私密笔记标题"],
        embeddings=[vector],
        metadatas=[
            {
                "saved_id": int(victim_record.id),
                "learner_id": attacker,  # ← 撒谎
                "question": "受害者的私密笔记标题",
                "created_at": "",
            }
        ],
        name=svc.saved_collection_name(test_store),
    )

    hits = await svc.search_saved(
        session,
        learner_id=attacker,
        query="受害者的私密笔记标题",
        provider=mock_embedder,
        store=test_store,
    )

    assert hits == [], "DB 归属复核没拦住伪造 metadata —— 越权了"


# --------------------------------------------------------------------------- #
# 三、门槛与参数
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_distance_threshold_filters_out_far_hits(
    session, mock_embedder, test_store
) -> None:
    """距离超过门槛的不该进提示词 —— 与资料检索用同一个门槛概念。"""
    learner = _learner()
    _seed_and_save(
        session, learner, question="什么是 JVM？", answer="Java 虚拟机。",
        provider=mock_embedder, store=test_store,
    )

    # 门槛设成 0：任何正距离都会被挡掉
    strict = await svc.search_saved(
        session,
        learner_id=learner,
        query="JVM",
        provider=mock_embedder,
        store=test_store,
        max_distance=0.0,
    )
    assert strict == [], "门槛为 0 时不该有任何命中"

    # 门槛放到最大：应当能命中
    loose = await svc.search_saved(
        session,
        learner_id=learner,
        query="JVM",
        provider=mock_embedder,
        store=test_store,
        max_distance=10.0,
    )
    assert loose, "门槛放宽后应当命中"


@pytest.mark.asyncio
async def test_top_k_limits_the_result_count(session, mock_embedder, test_store) -> None:
    learner = _learner()
    for n in range(4):
        _seed_and_save(
            session, learner, question=f"Java 问题 {n}", answer=f"Java 回答 {n}",
            provider=mock_embedder, store=test_store,
        )

    hits = await svc.search_saved(
        session,
        learner_id=learner,
        query="Java",
        top_k=2,
        provider=mock_embedder,
        store=test_store,
        max_distance=10.0,
    )
    assert len(hits) <= 2


@pytest.mark.asyncio
async def test_empty_query_returns_nothing(session, mock_embedder, test_store) -> None:
    learner = _learner()
    assert await svc.search_saved(
        session, learner_id=learner, query="   ", provider=mock_embedder, store=test_store
    ) == []


@pytest.mark.asyncio
async def test_search_on_empty_store_is_empty(session, mock_embedder, test_store) -> None:
    assert await svc.search_saved(
        session, learner_id=_learner(), query="随便什么", provider=mock_embedder, store=test_store
    ) == []


# --------------------------------------------------------------------------- #
# 四、事后回填：兑现 3A 那句"可事后回填"
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_reindex_missing_makes_unindexed_records_searchable(
    session, mock_embedder, test_store
) -> None:
    """保存时写向量失败 → 记录在库里但搜不到；回填之后就能搜到了。"""
    learner = _learner()
    record = _seed_and_save(
        session,
        learner,
        question="同步与异步的区别？",
        answer="同步阻塞等待，异步不阻塞。",
        provider=mock_embedder,
        store=test_store,
    )
    # 模拟"上次写向量失败"：把索引标记清掉，并确保集合里没有它
    record.embedding_id = None
    session.commit()
    try:
        test_store.client().delete_collection(svc.saved_collection_name(test_store))
    except Exception:  # noqa: BLE001
        pass

    before = await svc.search_saved(
        session, learner_id=learner, query="同步 异步", provider=mock_embedder, store=test_store
    )
    assert before == [], "未建索引时不应当被搜到"

    fixed = svc.reindex_missing(
        session, learner_id=learner, provider=mock_embedder, store=test_store
    )
    assert fixed == 1

    after = await svc.search_saved(
        session,
        learner_id=learner,
        query="同步 异步",
        provider=mock_embedder,
        store=test_store,
        max_distance=10.0,
    )
    assert after, "回填之后应当能搜到"


def test_reindex_missing_ignores_already_indexed(session, mock_embedder, test_store) -> None:
    """已经建过索引的不该被重复处理。"""
    learner = _learner()
    _seed_and_save(
        session, learner, question="已索引的问题", answer="已索引的回答",
        provider=mock_embedder, store=test_store,
    )
    assert (
        svc.reindex_missing(
            session, learner_id=learner, provider=mock_embedder, store=test_store
        )
        == 0
    )
