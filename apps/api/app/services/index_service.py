"""向量索引服务：把 P1 分好的文本块向量化并写入 Chroma。

三条设计约定：

1. **重建语义**：索引一份文档前先删掉它已有的向量。
   换 embedding 模型后必须重建索引，若不先删，同一份资料会同时存在新旧两套向量，
   检索结果混杂两种语义空间，排序完全不可信 —— 而且这种错误不会报错，只会悄悄给出差答案。

2. **幂等**：id 用 `doc{document_id}:chunk{chunk_index}`，可推导、稳定。
   重复索引同一份资料不会产生重复向量。

3. **索引状态以 Chroma 为准**，不新增数据表。
   用「Chroma 里该文档的块数」对比「数据库里该文档的块数」即可判断完整性 ——
   这样不存在"库里的状态与向量库不一致"这种经典问题。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.chunk import Chunk
from app.models.document import Document
from app.rag.embedding import EmbeddingProvider, embedder as default_embedder
from app.rag.vectorstore import VectorStore, vector_store as default_store

logger = get_logger(__name__)


def chunk_vector_id(document_id: int, chunk_index: int) -> str:
    """向量 id。可推导、稳定、幂等 —— 重复索引不会产生重复向量。"""
    return f"doc{document_id}:chunk{chunk_index}"


def embedding_fingerprint(provider: EmbeddingProvider) -> str:
    """向量化的"身份"：provider:model:dim。

    写进集合 metadata，用于识别"集合是用另一套向量化产生的"。
    光看维度不够 —— 换模型（或从 mock 换成真实模型）时维度可能完全相同，
    而集合里新旧两套语义空间混杂，检索距离不可比且**不会报任何错**。
    """
    return f"{provider.name}:{provider.model}:{provider.dim}"


@dataclass
class IndexStats:
    """一次索引的结果。"""

    document_id: int
    file_name: str = ""
    chunk_total: int = 0
    indexed: int = 0
    removed: int = 0
    batch_count: int = 0
    elapsed_ms: int = 0
    skipped_reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.skipped_reason

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "file_name": self.file_name,
            "chunk_total": self.chunk_total,
            "indexed": self.indexed,
            "removed": self.removed,
            "batch_count": self.batch_count,
            "elapsed_ms": self.elapsed_ms,
            "skipped_reason": self.skipped_reason,
        }


@dataclass
class IndexRunStats:
    """一次批量索引（可能覆盖多份文档）的结果。"""

    documents: int = 0
    indexed_chunks: int = 0
    failed: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "documents": self.documents,
            "indexed_chunks": self.indexed_chunks,
            "failed": self.failed,
            "elapsed_ms": self.elapsed_ms,
            "details": self.details,
        }


def _heading_path_json(value: Any) -> str:
    """Chroma 的 metadata 只接受标量，list 必须序列化。

    直接塞 list 会抛异常 —— 这是必踩的坑，所以单独抽一个函数并注明。
    """
    if not value:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(list(value), ensure_ascii=False)
    except (TypeError, ValueError):
        return ""


def _metadata(document: Document, chunk: Chunk) -> dict[str, Any]:
    """构造检索结果需要的全部元数据。

    这些字段决定了用户能否看到「这句话出自哪份资料的第几页哪个小节」——
    没有它们，RAG 的回答就无法溯源，和普通聊天没有区别。
    """
    return {
        "document_id": int(document.id),
        "chunk_index": int(chunk.chunk_index),
        "page_start": int(chunk.page_start),
        "page_end": int(chunk.page_end),
        "block_type": str(chunk.block_type or "text"),
        "heading_path": _heading_path_json(chunk.heading_path),
        "file_name": str(document.file_name or ""),
        "char_count": int(chunk.char_count or 0),
    }


def load_chunks(db: Session, document_id: int) -> list[Chunk]:
    return list(
        db.execute(
            select(Chunk)
            .where(Chunk.document_id == document_id)
            .order_by(Chunk.chunk_index)
        )
        .scalars()
        .all()
    )


def _upsert_with_dimension_rebuild(
    vectors: VectorStore,
    *,
    ids: list[str],
    documents: list[str],
    embeddings: list[list[float]],
    metadatas: list[dict[str, Any]],
    dim: int,
    fingerprint: str = "",
) -> int:
    """写入向量。若因集合维度被历史数据钉死而失败，则重建集合后重试一次。

    这是 `enforce_dimension()` 覆盖不到的一类情况：集合里有数据、但没有记录维度
    （P0 时期创建的旧集合就是这种，且维度一旦写入就被 Chroma 永久固定）。
    预先无法判断，只能在写入报错后兜底。

    重建是安全的：向量完全可以从 chunks 重新生成，不存在数据丢失。
    """
    try:
        return vectors.upsert(
            ids=ids, documents=documents, embeddings=embeddings, metadatas=metadatas
        )
    except Exception as exc:  # noqa: BLE001
        if "dimension" not in str(exc).lower():
            raise
        logger.warning("向量集合维度与当前 embedding 不符，重建集合后重试：%s", exc)
        vectors.reset()
        vectors.enforce_dimension(dim, fingerprint=fingerprint)
        return vectors.upsert(
            ids=ids, documents=documents, embeddings=embeddings, metadatas=metadatas
        )


async def index_document(
    db: Session,
    document_id: int,
    *,
    provider: EmbeddingProvider | None = None,
    store: VectorStore | None = None,
) -> IndexStats:
    """向量化一份文档的全部文本块并写入向量库（重建语义）。"""
    embed = provider or default_embedder
    vectors = store or default_store

    document = db.get(Document, document_id)
    if document is None:
        raise ValueError(f"文档 {document_id} 不存在。")

    chunks = load_chunks(db, document_id)
    stats = IndexStats(document_id=document_id, file_name=document.file_name)

    if not chunks:
        stats.skipped_reason = "该文档还没有文本块，请先完成资料解析。"
        return stats

    stats.chunk_total = len(chunks)

    # ------------------------------------------- 维度守卫（集合维度不可更改）
    vectors.enforce_dimension(embed.dim, fingerprint=embedding_fingerprint(embed))

    # ---------------------------------------------------------- 先删后写
    vectors.delete_document(document_id)

    texts = [chunk.content or "" for chunk in chunks]
    ids = [chunk_vector_id(document_id, chunk.chunk_index) for chunk in chunks]
    metadatas = [_metadata(document, chunk) for chunk in chunks]

    result = await embed.embed(texts)
    stats.batch_count = result.batch_count
    stats.elapsed_ms = result.elapsed_ms

    stats.indexed = _upsert_with_dimension_rebuild(
        vectors,
        ids=ids,
        documents=texts,
        embeddings=result.vectors,
        metadatas=metadatas,
        dim=embed.dim,
        fingerprint=embedding_fingerprint(embed),
    )
    stats.removed = 0  # 重建前删除的是旧向量，数量不影响本次结果

    logger.info(
        "文档 id=%s（%s）索引完成：%d 块 / %d 批 / %d ms",
        document_id,
        document.file_name,
        stats.indexed,
        stats.batch_count,
        stats.elapsed_ms,
    )
    return stats


async def index_documents(
    db: Session,
    document_ids: Sequence[int] | None = None,
    *,
    provider: EmbeddingProvider | None = None,
    store: VectorStore | None = None,
    on_progress: Any | None = None,
) -> IndexRunStats:
    """批量索引。`document_ids` 省略时索引全部已解析完成的文档。

    单份文档失败不中断整批 —— 与 P1 摄取、P2 校验保持一致的处理原则。
    """
    run = IndexRunStats()

    if document_ids is None:
        rows = db.execute(
            select(Document.id).where(Document.parse_status == "ready").order_by(Document.id)
        ).all()
        targets = [int(r[0]) for r in rows]
    else:
        targets = [int(x) for x in document_ids]

    for order, document_id in enumerate(targets, start=1):
        try:
            stats = await index_document(
                db, document_id, provider=provider, store=store
            )
            run.documents += 1
            run.indexed_chunks += stats.indexed
            run.details.append(stats.as_dict())
        except Exception as exc:  # noqa: BLE001 - 单份失败不拖垮整批
            logger.warning("文档 id=%s 索引失败：%s", document_id, exc)
            run.failed.append(f"{document_id}: {type(exc).__name__}: {exc}")
            run.details.append(
                {"document_id": document_id, "ok": False, "error": str(exc)[:200]}
            )
        if on_progress is not None:
            on_progress(
                int(order / max(len(targets), 1) * 100),
                f"已索引 {order}/{len(targets)} 份资料",
            )

    return run


# --------------------------------------------------------------------------- #
# 状态查询
# --------------------------------------------------------------------------- #
def document_index_state(
    db: Session, document_id: int, *, store: VectorStore | None = None
) -> dict[str, Any]:
    """单份文档的索引状态。"""
    vectors = store or default_store
    document = db.get(Document, document_id)
    if document is None:
        return {"document_id": document_id, "exists": False}

    chunk_total = int(
        db.execute(
            select(func.count()).select_from(Chunk).where(Chunk.document_id == document_id)
        ).scalar_one()
    )

    try:
        indexed = len(vectors.document_chunk_indexes(document_id))
    except Exception as exc:  # noqa: BLE001 - 向量库不可用时不能拖垮状态接口
        logger.warning("读取文档 %s 的向量状态失败：%s", document_id, exc)
        return {
            "document_id": document_id,
            "exists": True,
            "file_name": document.file_name,
            "chunk_total": chunk_total,
            "indexed_chunks": 0,
            "complete": False,
            "error": f"向量库不可用：{type(exc).__name__}",
        }

    return {
        "document_id": document_id,
        "exists": True,
        "file_name": document.file_name,
        "parse_status": document.parse_status,
        "kp_count": int(document.kp_count or 0),
        "chunk_total": chunk_total,
        "indexed_chunks": indexed,
        "complete": chunk_total > 0 and indexed >= chunk_total,
    }


def index_overview(
    db: Session,
    document_ids: Sequence[int] | None = None,
    *,
    store: VectorStore | None = None,
) -> dict[str, Any]:
    """全局索引状态：集合总量 + 每份文档的覆盖情况。"""
    vectors = store or default_store

    if document_ids is None:
        rows = db.execute(
            select(Document.id).where(Document.parse_status == "ready").order_by(Document.id)
        ).all()
        targets = [int(r[0]) for r in rows]
    else:
        targets = [int(x) for x in document_ids]

    items = [document_index_state(db, document_id, store=vectors) for document_id in targets]

    try:
        collection_count = vectors.count()
        collection_name = vectors.collection_name
        store_available = True
    except Exception as exc:  # noqa: BLE001
        collection_count = 0
        collection_name = vectors.collection_name
        store_available = False
        logger.warning("读取向量集合状态失败：%s", exc)

    return {
        "collection": collection_name,
        "collection_count": collection_count,
        "store_available": store_available,
        "document_total": len(targets),
        "document_indexed": sum(1 for item in items if item.get("complete")),
        "items": items,
    }


__all__ = [
    "IndexRunStats",
    "IndexStats",
    "chunk_vector_id",
    "document_index_state",
    "index_document",
    "index_documents",
    "index_overview",
    "load_chunks",
]
