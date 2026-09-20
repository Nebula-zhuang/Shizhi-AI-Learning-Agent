"""索引服务测试。

用独立的测试集合（见 tests/rag/conftest.py），不碰生产集合。

重点验证三件事：
  1. **重建语义** —— 重复索引同一份资料不能产生重复向量；
     换 embedding 模型后必须能清掉旧向量，否则检索会混杂两种语义空间；
  2. **元数据完整** —— 缺字段就意味着答案无法溯源；
  3. **单份失败不拖垮整批** —— 与 P1 摄取、P2 校验保持一致的处理原则。
"""

from __future__ import annotations

import pytest

from app.rag.embedding import EmbeddingError, EmbeddingErrorCode
from app.services import index_service as idx


# --------------------------------------------------------------------------- #
# 基础写入
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_index_document_writes_all_chunks(
    seeded_document, mock_embedder, test_store, session
) -> None:
    document_id = seeded_document["document_id"]
    stats = await idx.index_document(
        session, document_id, provider=mock_embedder, store=test_store
    )

    assert stats.ok
    assert stats.chunk_total == seeded_document["chunk_count"]
    assert stats.indexed == seeded_document["chunk_count"]
    assert test_store.count() == seeded_document["chunk_count"]
    assert test_store.document_chunk_indexes(document_id) == {0, 1, 2}


@pytest.mark.asyncio
async def test_index_writes_traceable_metadata(
    seeded_document, mock_embedder, test_store, session
) -> None:
    """元数据是「答案能否溯源」的前提，逐字段断言。"""
    document_id = seeded_document["document_id"]
    stats = await idx.index_document(
        session, document_id, provider=mock_embedder, store=test_store
    )
    assert stats.indexed == 3

    query = await mock_embedder.embed(["线程"])
    hit = test_store.query(query.vectors[0], top_k=1, where={"document_id": document_id})[0]
    metadata = hit["metadata"]

    assert metadata["document_id"] == document_id
    assert metadata["file_name"] == "p3_index_test.txt"
    assert metadata["chunk_index"] == 1
    assert metadata["page_start"] >= 1
    assert metadata["page_end"] >= metadata["page_start"]
    assert metadata["block_type"] == "text"
    assert metadata["char_count"] > 0
    # heading_path 以 JSON 字符串存放（Chroma 的 metadata 只接受标量）
    assert "第三章 进程管理" in metadata["heading_path"]


@pytest.mark.asyncio
async def test_vector_id_is_derivable(seeded_document, mock_embedder, test_store, session) -> None:
    document_id = seeded_document["document_id"]
    await idx.index_document(session, document_id, provider=mock_embedder, store=test_store)
    assert idx.chunk_vector_id(document_id, 0) == f"doc{document_id}:chunk0"


# --------------------------------------------------------------------------- #
# 幂等与重建
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_reindex_is_idempotent(seeded_document, mock_embedder, test_store, session) -> None:
    """重复索引不产生重复向量 —— 用户手动重建索引时会依赖这一点。"""
    document_id = seeded_document["document_id"]

    first = await idx.index_document(
        session, document_id, provider=mock_embedder, store=test_store
    )
    after_first = test_store.count()
    second = await idx.index_document(
        session, document_id, provider=mock_embedder, store=test_store
    )

    assert first.indexed == second.indexed
    assert test_store.count() == after_first, "重复索引后向量数量不应翻倍"


@pytest.mark.asyncio
async def test_rebuild_keeps_count_stable(
    seeded_document, mock_embedder, test_store, session
) -> None:
    """重建后数量稳定、覆盖完整。

    如果重建不清旧向量，同一份资料会同时存在两套语义空间的向量，
    检索排序混杂两种度量 —— 而且完全不报错，只会悄悄给出差答案。
    """
    document_id = seeded_document["document_id"]
    await idx.index_document(session, document_id, provider=mock_embedder, store=test_store)
    stats = await idx.index_document(
        session, document_id, provider=mock_embedder, store=test_store
    )
    assert stats.indexed == seeded_document["chunk_count"]
    assert test_store.count() == seeded_document["chunk_count"]
    assert test_store.document_chunk_indexes(document_id) == {0, 1, 2}


@pytest.mark.asyncio
async def test_rebuild_does_not_touch_other_documents(
    seeded_document, mock_embedder, test_store, session
) -> None:
    """重建一份文档不能影响别的文档的向量。"""
    document_id = seeded_document["document_id"]
    await idx.index_document(session, document_id, provider=mock_embedder, store=test_store)

    result = await mock_embedder.embed(["别的文档的内容"])
    test_store.upsert(
        ids=["doc777:chunk0"],
        documents=["别的文档的内容"],
        embeddings=result.vectors,
        metadatas=[
            {
                "document_id": 777,
                "chunk_index": 0,
                "page_start": 1,
                "page_end": 1,
                "block_type": "text",
                "heading_path": "",
                "file_name": "other.txt",
                "char_count": 8,
            }
        ],
    )
    assert test_store.count() == 4

    await idx.index_document(session, document_id, provider=mock_embedder, store=test_store)
    assert test_store.document_chunk_indexes(777) == {0}, "其他文档的向量不应被误删"


# --------------------------------------------------------------------------- #
# 失败路径
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_missing_document_raises(mock_embedder, test_store, session) -> None:
    with pytest.raises(ValueError):
        await idx.index_document(session, 99999999, provider=mock_embedder, store=test_store)


@pytest.mark.asyncio
async def test_embedding_not_configured_fails_loudly(
    seeded_document, test_store, session
) -> None:
    """未配 Key 时必须明确失败，不能静默写入零向量 ——
    那会让用户以为索引建好了，实际检索毫无意义。"""
    from app.core.config import get_settings
    from app.rag.embedding import APIEmbeddingProvider

    broken = APIEmbeddingProvider(
        get_settings().model_copy(update={"embedding_api_key": "", "embedding_base_url": ""})
    )
    with pytest.raises(EmbeddingError) as exc:
        await idx.index_document(
            session, seeded_document["document_id"], provider=broken, store=test_store
        )
    assert exc.value.code == EmbeddingErrorCode.NOT_CONFIGURED


@pytest.mark.asyncio
async def test_document_without_chunks_is_skipped_cleanly(
    mock_embedder, test_store, session
) -> None:
    """没有文本块的文档返回明确的跳过原因，而不是抛异常。"""
    from app.db.session import SessionLocal
    from app.models.document import Document

    with SessionLocal() as other:
        document = Document(
            file_name="p3_empty.txt",
            file_type="text",
            file_size=0,
            file_hash="p3empty" + "0" * 57,
            storage_path="x",
            parse_status="ready",
        )
        other.add(document)
        other.commit()
        document_id = document.id

    try:
        stats = await idx.index_document(
            session, document_id, provider=mock_embedder, store=test_store
        )
        assert stats.ok is False
        assert "文本块" in stats.skipped_reason
        assert stats.indexed == 0
    finally:
        with SessionLocal() as other:
            doc = other.get(Document, document_id)
            if doc is not None:
                other.delete(doc)
                other.commit()


# --------------------------------------------------------------------------- #
# 状态查询
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_document_index_state_reports_coverage(
    seeded_document, mock_embedder, test_store, session
) -> None:
    document_id = seeded_document["document_id"]

    before = idx.document_index_state(session, document_id, store=test_store)
    assert before["exists"] is True
    assert before["chunk_total"] == 3
    assert before["indexed_chunks"] == 0
    assert before["complete"] is False

    await idx.index_document(session, document_id, provider=mock_embedder, store=test_store)

    after = idx.document_index_state(session, document_id, store=test_store)
    assert after["indexed_chunks"] == 3
    assert after["complete"] is True
    assert after["file_name"] == "p3_index_test.txt"


def test_document_index_state_of_missing_document(test_store, session) -> None:
    state = idx.document_index_state(session, 99999999, store=test_store)
    assert state["exists"] is False


@pytest.mark.asyncio
async def test_index_overview(seeded_document, mock_embedder, test_store, session) -> None:
    document_id = seeded_document["document_id"]
    await idx.index_document(session, document_id, provider=mock_embedder, store=test_store)

    overview = idx.index_overview(session, [document_id], store=test_store)
    assert overview["store_available"] is True
    assert overview["collection"] == test_store.collection_name
    assert overview["collection_count"] == 3
    assert overview["document_total"] == 1
    assert overview["document_indexed"] == 1


@pytest.mark.asyncio
async def test_index_overview_handles_broken_store(session) -> None:
    """向量库不可用时，状态接口要能返回而不是崩掉。"""
    from app.rag.vectorstore import VectorStore

    class BrokenStore(VectorStore):
        def collection(self, name=None):  # type: ignore[no-untyped-def]
            raise RuntimeError("chroma 挂了")

    broken = BrokenStore()
    overview = idx.index_overview(session, [], store=broken)
    assert overview["store_available"] is False


# --------------------------------------------------------------------------- #
# 批量
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_batch_continues_after_single_failure(
    seeded_document, mock_embedder, test_store, session
) -> None:
    """一份文档失败不能中断整批 —— 与 P1 摄取、P2 校验的处理原则一致。"""
    good_id = seeded_document["document_id"]

    run = await idx.index_documents(
        session, [99999999, good_id], provider=mock_embedder, store=test_store
    )

    assert run.documents == 1, "好的那份应当正常完成"
    assert len(run.failed) == 1
    assert "99999999" in run.failed[0]
    assert run.indexed_chunks == 3


@pytest.mark.asyncio
async def test_batch_reports_progress(
    seeded_document, mock_embedder, test_store, session
) -> None:
    progress: list[tuple[int, str]] = []
    await idx.index_documents(
        session,
        [seeded_document["document_id"]],
        provider=mock_embedder,
        store=test_store,
        on_progress=lambda pct, detail: progress.append((pct, detail)),
    )
    assert progress and progress[-1][0] == 100


# --------------------------------------------------------------------------- #
# 维度守卫
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_dimension_guard_runs_during_indexing(
    seeded_document, mock_embedder, test_store, session
) -> None:
    """索引时必须记录集合的向量维度 —— 这是后续判断能否换模型写入的依据。"""
    from app.rag.vectorstore import VectorStore

    await idx.index_document(
        session, seeded_document["document_id"], provider=mock_embedder, store=test_store
    )
    assert int(test_store.collection().metadata[VectorStore.DIM_KEY]) == mock_embedder.dim


@pytest.mark.asyncio
async def test_index_recovers_from_poisoned_collection(
    seeded_document, mock_embedder, test_store, session
) -> None:
    """集合维度被历史数据钉死时，索引应当能自动恢复。

    现实来源：P0 的冒烟脚本往集合里写了 4 维假向量，
    而 Chroma 的集合维度一旦写入就永久固定 —— 不重建就再也写不进 1024 维向量。
    """
    test_store.enforce_dimension(4)  # 模拟被 4 维数据污染的集合
    stats = await idx.index_document(
        session, seeded_document["document_id"], provider=mock_embedder, store=test_store
    )
    assert stats.indexed == seeded_document["chunk_count"]
    assert test_store.count() == seeded_document["chunk_count"]
