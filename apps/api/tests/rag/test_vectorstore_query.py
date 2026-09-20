"""向量库检索能力测试。

覆盖 P3 给 `VectorStore` 新增的 `query` / `delete_document` / `document_chunk_indexes`
以及维度守卫 `enforce_dimension`。

全部写在独立测试集合里（见 conftest），不碰生产集合。
"""

from __future__ import annotations

import pytest

from app.rag.vectorstore import VectorStore

from tests.conftest import TEST_COLLECTION


async def seed(store: VectorStore, embedder, texts: list[str], document_id: int = 1) -> None:
    result = await embedder.embed(texts)
    store.upsert(
        ids=[f"doc{document_id}:chunk{i}" for i in range(len(texts))],
        documents=texts,
        embeddings=result.vectors,
        metadatas=[
            {
                "document_id": document_id,
                "chunk_index": i,
                "page_start": i + 1,
                "page_end": i + 1,
                "block_type": "text",
                "heading_path": '["第三章"]',
                "file_name": "t.txt",
                "char_count": len(text),
            }
            for i, text in enumerate(texts)
        ],
    )


# --------------------------------------------------------------------------- #
# 写入与检索
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_write_then_query_returns_hits(test_store, mock_embedder) -> None:
    texts = [
        "进程是程序的一次执行过程，是资源分配的基本单位。",
        "线程是进程内部的执行单元，是处理机调度的基本单位。",
        "死锁的产生必须同时满足互斥与循环等待等四个条件。",
    ]
    await seed(test_store, mock_embedder, texts)
    assert test_store.count() == 3

    query = await mock_embedder.embed(["线程和进程有什么区别"])
    hits = test_store.query(query.vectors[0], top_k=3)

    assert len(hits) == 3
    assert hits[0]["distance"] <= hits[-1]["distance"], "必须按距离升序"

    # 断言的是「相关性排序成立」，而不是「某一条必须第一」。
    # mock 是字符级词袋，不具备语义理解 —— 要求它区分"线程更相关还是进程更相关"
    # 是不现实的。但它必须体现真实的相关性：与查询同主题的两条排在无关的那条之前。
    assert {hits[0]["document"], hits[1]["document"]} == {texts[0], texts[1]}
    assert hits[2]["document"] == texts[2], "与查询无关的片段必须排在最后"
    assert hits[2]["distance"] > hits[1]["distance"]


@pytest.mark.asyncio
async def test_query_respects_top_k(test_store, mock_embedder) -> None:
    await seed(test_store, mock_embedder, [f"第 {i} 段关于操作系统的内容" for i in range(6)])
    query = await mock_embedder.embed(["操作系统的内容"])
    assert len(test_store.query(query.vectors[0], top_k=2)) == 2
    assert len(test_store.query(query.vectors[0], top_k=6)) == 6


@pytest.mark.asyncio
async def test_query_returns_full_metadata(test_store, mock_embedder) -> None:
    """元数据决定了答案能否溯源 —— 缺一个字段用户就少一个回查的入口。"""
    await seed(test_store, mock_embedder, ["进程是资源分配的基本单位"], document_id=7)
    query = await mock_embedder.embed(["进程"])
    hit = test_store.query(query.vectors[0], top_k=1, where={"document_id": 7})[0]

    metadata = hit["metadata"]
    for key in (
        "document_id",
        "chunk_index",
        "page_start",
        "page_end",
        "block_type",
        "heading_path",
        "file_name",
        "char_count",
    ):
        assert key in metadata, f"元数据缺少 {key}"
    assert metadata["document_id"] == 7
    # Chroma 只接受标量，heading_path 必须序列化成字符串
    assert isinstance(metadata["heading_path"], str)


@pytest.mark.asyncio
async def test_query_filters_by_document(test_store, mock_embedder) -> None:
    await seed(test_store, mock_embedder, ["文档一的进程内容"], document_id=1)
    await seed(test_store, mock_embedder, ["文档二的线程内容"], document_id=2)

    query = await mock_embedder.embed(["进程与线程"])
    only_one = test_store.query(query.vectors[0], top_k=5, where={"document_id": 1})
    assert len(only_one) == 1
    assert only_one[0]["metadata"]["document_id"] == 1

    none = test_store.query(query.vectors[0], top_k=5, where={"document_id": 999})
    assert none == []


@pytest.mark.asyncio
async def test_query_filters_by_multiple_documents(test_store, mock_embedder) -> None:
    """多文档过滤要用 $in 语法，这里验证它确实生效。"""
    for doc_id in (1, 2, 3):
        await seed(test_store, mock_embedder, [f"文档 {doc_id} 的内容"], document_id=doc_id)

    query = await mock_embedder.embed(["内容"])
    hits = test_store.query(
        query.vectors[0], top_k=10, where={"document_id": {"$in": [1, 3]}}
    )
    assert {h["metadata"]["document_id"] for h in hits} == {1, 3}


@pytest.mark.asyncio
async def test_query_with_empty_embedding_returns_nothing(test_store) -> None:
    assert test_store.query([], top_k=3) == []


# --------------------------------------------------------------------------- #
# 删除与覆盖统计
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_delete_document_only_removes_that_document(test_store, mock_embedder) -> None:
    """重建索引前必须能精确删掉某一份文档 —— 否则换模型后新旧向量会混杂。"""
    await seed(test_store, mock_embedder, ["甲文档的内容"], document_id=1)
    await seed(test_store, mock_embedder, ["乙文档的内容"], document_id=2)
    assert test_store.count() == 2

    test_store.delete_document(1)
    assert test_store.count() == 1
    assert test_store.document_chunk_indexes(2) == {0}
    assert test_store.document_chunk_indexes(1) == set()


@pytest.mark.asyncio
async def test_document_chunk_indexes_reports_coverage(test_store, mock_embedder) -> None:
    await seed(test_store, mock_embedder, ["一", "二", "三"], document_id=5)
    assert test_store.document_chunk_indexes(5) == {0, 1, 2}
    assert test_store.document_chunk_indexes(6) == set()


@pytest.mark.asyncio
async def test_upsert_is_idempotent(test_store, mock_embedder) -> None:
    """同一批 id 重复写入不应产生重复记录。"""
    texts = ["进程是资源分配的基本单位", "线程是调度的基本单位"]
    await seed(test_store, mock_embedder, texts)
    first = test_store.count()
    await seed(test_store, mock_embedder, texts)
    assert test_store.count() == first


# --------------------------------------------------------------------------- #
# 维度守卫
# --------------------------------------------------------------------------- #
def test_enforce_dimension_initializes_metadata(test_store) -> None:
    """空集合且未记录维度 → 重建并记录，返回 initialized。"""
    assert test_store.collection().metadata.get(VectorStore.DIM_KEY) is None
    assert test_store.enforce_dimension(1024) == "initialized"
    assert int(test_store.collection().metadata[VectorStore.DIM_KEY]) == 1024


def test_enforce_dimension_is_noop_when_matching(test_store) -> None:
    test_store.enforce_dimension(1024)
    assert test_store.enforce_dimension(1024) == "ok"


@pytest.mark.asyncio
async def test_enforce_dimension_resets_on_mismatch(test_store, mock_embedder) -> None:
    """维度不符时重建集合。

    这个能力的现实来源：P0 的冒烟脚本往生产集合写了 4 维假向量，
    而 Chroma 的集合维度一旦写入就永久固定，不重建就再也写不进 1024 维的向量。
    """
    test_store.enforce_dimension(4)
    await seed(test_store, mock_embedder, ["旧模型的向量"])
    assert test_store.count() == 1

    assert test_store.enforce_dimension(1024) == "reset"
    assert test_store.count() == 0, "重建后旧向量应被清空"
    assert int(test_store.collection().metadata[VectorStore.DIM_KEY]) == 1024


@pytest.mark.asyncio
async def test_fingerprint_change_forces_reset(test_store, mock_embedder) -> None:
    """**维度相同但向量化身份不同时，也必须重建集合。**

    这是维度检查抓不到的一类污染：切换 embedding 模型（或从 mock 换成真实模型）时，
    维度可能都是 1024，维度检查放行，而集合里新旧两套语义空间混杂 ——
    检索时距离完全不可比、排序毫无意义，**而且不会报任何错**。
    指纹（provider:model:dim）比对就是为了识别这种"看起来正常"的污染。
    """
    from app.services.index_service import embedding_fingerprint

    fingerprint = embedding_fingerprint(mock_embedder)
    assert test_store.enforce_dimension(1024, fingerprint=fingerprint) == "initialized"
    await seed(test_store, mock_embedder, ["mock 模型的向量"])
    assert test_store.count() == 1

    # 维度完全一样，只有身份变了
    other = "api:text-embedding-v3:1024"
    assert test_store.enforce_dimension(1024, fingerprint=other) == "reset"
    assert test_store.count() == 0, "换了向量化身份后旧向量必须清空"
    assert test_store.collection().metadata[VectorStore.FINGERPRINT_KEY] == other


def test_same_fingerprint_is_noop(test_store, mock_embedder) -> None:
    from app.services.index_service import embedding_fingerprint

    fingerprint = embedding_fingerprint(mock_embedder)
    test_store.enforce_dimension(1024, fingerprint=fingerprint)
    assert test_store.enforce_dimension(1024, fingerprint=fingerprint) == "ok"


def test_fingerprint_is_recorded_on_write(test_store, mock_embedder) -> None:
    """索引时必须把身份写进集合 metadata，否则下次无从判断。"""
    from app.services.index_service import embedding_fingerprint

    test_store.enforce_dimension(1024, fingerprint=embedding_fingerprint(mock_embedder))
    assert test_store.collection().metadata.get(VectorStore.FINGERPRINT_KEY)


@pytest.mark.asyncio
async def test_query_after_dimension_reset_works(test_store, mock_embedder) -> None:
    """维度守卫之后必须真的能写入并检索 —— 否则守卫只是看起来正确。"""
    test_store.enforce_dimension(1024)
    await seed(test_store, mock_embedder, ["线程是处理机调度的基本单位"])
    query = await mock_embedder.embed(["线程"])
    assert len(test_store.query(query.vectors[0], top_k=1)) == 1


def test_collection_name_is_not_polluted(test_store) -> None:
    """确认测试用的是独立集合，绝不是生产集合。"""
    from app.core.config import get_settings

    assert TEST_COLLECTION != get_settings().chroma_collection
    assert test_store.collection_name == TEST_COLLECTION
