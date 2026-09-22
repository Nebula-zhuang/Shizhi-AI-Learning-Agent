"""保存知识（Phase 3A）—— 用户主动留下的一轮问答。

设计依据 `docs/25a-自由学习空间设计.md` §3。职责边界只有一句话：

> **这里只管"用户特意保存了什么"，不管"资料里有什么"（KnowledgePoint）、
> 也不管"我学得怎么样"（LearnerKpState）。**

## 三条贯穿全文的规矩

1. **正文只能来自真实消息。** 客户端给的是 `message_id`，不是文本 ——
   见 `SavedKnowledgeCreate` 的注释。保存的东西将来会被检索、被引用，
   不能允许任意文本混进来。
2. **所有查询都带 `learner_id`。** 沿用 `study_service` 的做法：
   不提供"按 id 单独查"的方法，从签名上堵死越权。
   归属不符与不存在**都返回 404**（区分开等于告诉对方这个 id 存在）。
3. **写向量失败不让保存失败。** 向量是**派生数据**，用户的意图是"存下来"；
   存不下索引只是"暂时搜不到"，可以事后回填（`embedding_id` 留 NULL）。

## 为什么用**独立集合**而不是往 chunks 那个集合里加

`retrieve_knowledge` 在 `document_ids=None` 时是**全库检索** ——
保存的内容若写进同一个集合，就会被它捞回来，
于是"资料检索"变成了"资料 + 我保存的东西"，**引用优先级规则随之失真**
（项目硬约束：用户资料 > 联网证据 > 模型通识；保存的内容属于哪一档是需要单独定义的事）。

Phase 3A 刻意**不动 `retrieve_knowledge` 的语义**，所以这里用独立集合，
跨对话检索留给后续阶段用新工具接。
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.conversation import Conversation, ConversationMessage, MessageAuthor
from app.models.saved_knowledge import SavedKnowledge
from app.rag.embedding import EmbeddingError, EmbeddingProvider
from app.rag.embedding import embedder as default_embedder
from app.rag.vectorstore import VectorStore, vector_store as default_store

logger = get_logger(__name__)

#: 向量集合名后缀。与 chunks 的集合（`settings.chroma_collection`）**刻意分开**，
#: 理由见模块开头。用后缀而不是新配置项：不引入需要同步 `.env` 的开关。
SAVED_COLLECTION_SUFFIX = "_saved_knowledge"

#: 送去向量化的正文长度上限。
#: 长回答里的大部分内容是"解释"，会稀释向量；截断到前若干字符既保住主题，
#: 又不至于让一次保存的 embedding 成本失控。
EMBED_TEXT_LIMIT = 2000

#: 列表默认页大小
DEFAULT_LIMIT = 20
MAX_LIMIT = 100


class SavedKnowledgeNotFound(LookupError):
    """消息不存在，**或者**它所属的对话不属于当前用户。

    刻意不区分这两种情况 —— 同 `study_service.ConversationNotFound`。
    """


def saved_collection_name(store: VectorStore | None = None) -> str:
    """保存知识用的向量集合名。

    ⚠️ 从**传进来的 store** 派生，而不是直接读全局 settings ——
    否则测试里换了 store（`test_store` 指向独立的测试集合）而名字仍指向
    生产派生的集合，就会往不该写的地方写。
    """
    return f"{(store or default_store).collection_name}{SAVED_COLLECTION_SUFFIX}"


def vector_id(saved_id: int) -> str:
    """向量库里的 id。

    与 chunk 的 `doc{N}:chunk{M}` 同一套路：**可推导、稳定**，
    于是"这条记录有没有索引"看 `embedding_id` 就知道，不需要额外状态。
    """
    return f"saved:{saved_id}"


# --------------------------------------------------------------------------- #
# 保存
# --------------------------------------------------------------------------- #
def _load_owned_assistant_message(
    db: Session, *, learner_id: str, message_id: int
) -> ConversationMessage:
    """取一条**属于当前用户**的助手消息，顺带校验对话归属。

    一次 join 同时完成三件事：消息存在、它的对话属于我、它是助手消息。
    """
    stmt = (
        select(ConversationMessage)
        .join(Conversation, Conversation.id == ConversationMessage.conversation_id)
        .where(
            ConversationMessage.id == message_id,
            Conversation.learner_id == learner_id,
        )
    )
    message = db.scalars(stmt).one_or_none()
    if message is None:
        raise SavedKnowledgeNotFound(f"消息 {message_id} 不存在")
    if message.role != MessageAuthor.ASSISTANT:
        # 只能保存"回答"。保存用户自己的提问没有意义 ——
        # 那样的记录在检索时会以用户的口吻被引用出来。
        raise SavedKnowledgeNotFound(f"消息 {message_id} 不是回答")
    return message


def _find_question(db: Session, *, conversation_id: int, before_id: int) -> str:
    """找这条回答**紧邻之前**的那个提问。找不到就返回空串。"""
    stmt = (
        select(ConversationMessage.content)
        .where(
            ConversationMessage.conversation_id == conversation_id,
            ConversationMessage.id < before_id,
            ConversationMessage.role == MessageAuthor.USER,
        )
        .order_by(ConversationMessage.id.desc())
        .limit(1)
    )
    return str(db.scalars(stmt).one_or_none() or "")


def _web_urls(citations: Sequence[dict] | None) -> list[dict] | None:
    """从引用的 citation 里挑出**联网来源**。

    只取 `kind == "web"` 的：`knowledge_base` 那类是资料出处
    （document_id / page），不是 URL，混进来会让字段名撒谎。
    """
    if not citations:
        return None
    urls = [
        {"title": str(item.get("title") or ""), "url": str(item.get("url") or "")}
        for item in citations
        if isinstance(item, dict) and item.get("kind") == "web" and item.get("url")
    ]
    return urls or None


def save_from_message(
    db: Session,
    *,
    learner_id: str,
    message_id: int,
    tags: Sequence[str] | None = None,
    provider: EmbeddingProvider | None = None,
    store: VectorStore | None = None,
) -> SavedKnowledge:
    """把一条助手消息保存进知识库。

    **先落库、再写向量**：DB 写入是用户的意图本身，向量只是加速手段。
    因此写向量失败只会让 `embedding_id` 留空，不影响这次保存成功。
    """
    message = _load_owned_assistant_message(db, learner_id=learner_id, message_id=message_id)

    record = SavedKnowledge(
        learner_id=learner_id,
        question=_find_question(
            db, conversation_id=int(message.conversation_id), before_id=int(message.id)
        ),
        # 正文是**快照**：消息以后被删，保存的内容也还在（见模型注释）。
        answer=str(message.content or ""),
        source_message_id=int(message.id),
        source_urls=_web_urls(message.citations),
        # kp_ids 在 Phase 3A 刻意留空 —— 它需要"相关知识点推荐"，
        # 那是 3.5 的事（本阶段只做数据层与 API）。
        kp_ids=None,
        tags=[str(t) for t in tags] if tags else None,
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    _try_index(db, record, provider=provider, store=store)
    return record


# --------------------------------------------------------------------------- #
# 向量索引
# --------------------------------------------------------------------------- #
def embed_text(record: SavedKnowledge) -> str:
    """拼出送去向量化的文本。

    问题在前、回答在后。问题短且指向明确，是"我以前学过 X 吗"最好的匹配信号；
    回答补上术语与细节，但截断 —— 长回答的尾部多是展开与例子，会稀释向量。
    """
    combined = f"{record.question}\n\n{record.answer}".strip()
    return combined[:EMBED_TEXT_LIMIT]


def _try_index(
    db: Session,
    record: SavedKnowledge,
    *,
    provider: EmbeddingProvider | None,
    store: VectorStore | None,
) -> bool:
    """尝试建立向量索引并回填 `embedding_id`。**任何失败都只记日志。**

    返回是否索引成功，供测试与后续回填使用。
    """
    embed = provider or default_embedder
    vectors = store or default_store
    name = saved_collection_name(vectors)
    text = embed_text(record)
    if not text:
        logger.warning("保存 %s 的正文为空，跳过向量索引。", record.id)
        return False

    try:
        result = _run_embedding(embed, [text])
    except Exception as exc:  # noqa: BLE001 - 向量是派生数据，失败不该让保存失败
        logger.warning("保存 %s 的向量化失败（可事后回填）：%s", record.id, exc)
        return False

    if not result or not result.vectors:
        logger.warning("保存 %s 的向量化返回空结果，跳过索引。", record.id)
        return False

    try:
        vectors.enforce_dimension(result.dim, name=name)
        vectors.upsert(
            ids=[vector_id(record.id)],
            documents=[text],
            embeddings=result.vectors,
            metadatas=[
                {
                    "saved_id": int(record.id),
                    "learner_id": record.learner_id,
                    "question": record.question[:200],
                    "created_at": record.created_at.isoformat() if record.created_at else "",
                }
            ],
            name=name,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("保存 %s 的向量写入失败（可事后回填）：%s", record.id, exc)
        return False

    record.embedding_id = vector_id(record.id)
    db.commit()
    db.refresh(record)
    return True


def _run_embedding(embed: EmbeddingProvider, texts: list[str]):
    """在同步上下文里驱动一次异步 embedding。"""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(embed.embed(texts))

    # 已经有事件循环在跑（比如从异步路由里调过来）→ 另起一个线程执行，
    # 否则 `asyncio.run` 会抛 "cannot be called from a running event loop"。
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(embed.embed(texts))).result()


# --------------------------------------------------------------------------- #
# 列表
# --------------------------------------------------------------------------- #
def list_saved(
    db: Session,
    *,
    learner_id: str,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> tuple[list[SavedKnowledge], int]:
    """列出我保存的知识，按时间倒序。返回 `(当页条目, 总数)`。

    **恒定带 `learner_id`** —— 不提供不带它的重载。
    """
    size = max(1, min(int(limit), MAX_LIMIT))
    start = max(0, int(offset))

    total = int(
        db.scalar(
            select(func.count())
            .select_from(SavedKnowledge)
            .where(SavedKnowledge.learner_id == learner_id)
        )
        or 0
    )
    stmt = (
        select(SavedKnowledge)
        .where(SavedKnowledge.learner_id == learner_id)
        .order_by(SavedKnowledge.created_at.desc(), SavedKnowledge.id.desc())
        .limit(size)
        .offset(start)
    )
    return list(db.scalars(stmt).all()), total
