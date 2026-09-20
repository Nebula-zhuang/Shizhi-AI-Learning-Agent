"""最小 RAG 链路测试。

全部用 mock embedding + 假 LLM，**不打网络、不消耗额度**。

这里守护的是 RAG 最容易出错、也最容易悄悄出错的三处：
  1. **距离门槛** —— Top-K 永远返回 K 条，没有门槛就会拿无关片段硬编答案；
  2. **上下文编号** —— 答案里的 [n] 必须能对上具体来源；
  3. **资料外问题不调用模型** —— 让模型面对空上下文回答只会得到编造内容。
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from app.core.llm import LLMResult
from app.rag.embedding import EmbeddingError, EmbeddingErrorCode
from app.services import index_service as idx
from app.services import rag_service as rag

from tests.conftest import CHUNKS


class FakeLLM:
    """假 LLM 网关。记录收到的消息，返回固定内容。"""

    model = "fake-model"
    mode = "mock"

    def __init__(self, content: str = "答案正文 [1]") -> None:
        self.content = content
        self.calls: list[list[dict[str, str]]] = []

    async def chat(self, messages, **_kwargs) -> LLMResult:
        self.calls.append(messages)
        return LLMResult(content=self.content, model=self.model, mode=self.mode, usage={})


class BrokenLLM:
    """永远抛异常的假网关，用于验证"生成失败但不丢掉检索结果"。

    现实来源：DeepSeek 账户余额不足（HTTP 402）时，
    原先的实现会让异常直接冒泡成 500 —— 用户问了一个资料里确实有的问题，
    却既拿不到答案、也拿不到已经检索到的来源。
    """

    model = "broken-model"
    mode = "live"

    def __init__(self, message: str = "LLM 服务返回错误状态 402：Insufficient Balance") -> None:
        self.message = message

    async def chat(self, messages, **_kwargs):  # noqa: ANN001, ANN202
        from app.core.llm import LLMError

        raise LLMError(self.message, status_code=502)


class BrokenEmbedder:
    """永远抛 not_configured 的向量化器，用于验证降级提示。"""

    name = "broken"
    model = "broken"
    dim = 1024

    def available(self) -> bool:
        return False

    def capabilities(self) -> dict:
        return {"provider": "broken", "configured": False}

    async def embed(self, texts):  # noqa: ANN001
        raise EmbeddingError(
            "未配置 EMBEDDING_API_KEY。", code=EmbeddingErrorCode.NOT_CONFIGURED
        )


@pytest_asyncio.fixture()
async def indexed(seeded_document, mock_embedder, test_store, session):
    """把测试文档索引好，供 RAG 检索。

    这里必须用 `pytest_asyncio.fixture` 而不是 `pytest.fixture` ——
    strict 模式下后者不会 await 协程夹具，只会留下一个从未被 await 的协程对象，
    表现为一堆莫名其妙的失败 + "coroutine was never awaited" 警告。
    """
    await idx.index_document(
        session, seeded_document["document_id"], provider=mock_embedder, store=test_store
    )
    return seeded_document


# --------------------------------------------------------------------------- #
# 主链路
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_answer_returns_answer_and_sources(indexed, mock_embedder, test_store) -> None:
    llm = FakeLLM("进程是资源分配的基本单位 [1]，线程是调度的基本单位 [2]。")
    result = await rag.answer_question(
        None,
        "进程和线程有什么区别？",
        document_ids=[indexed["document_id"]],
        provider=mock_embedder,
        store=test_store,
        llm=llm,
    )

    assert result.answer.startswith("进程是资源分配的基本单位")
    assert result.sources, "必须返回来源"
    assert result.used >= 1
    assert result.model == "fake-model"
    assert len(llm.calls) == 1
    # 计时分段齐全，便于定位是检索慢还是生成慢
    assert {"embed", "retrieve", "generate"} <= set(result.timings)


@pytest.mark.asyncio
async def test_sources_carry_traceable_metadata(indexed, mock_embedder, test_store) -> None:
    """来源必须能让用户回查到"哪份资料的第几页" —— 否则 RAG 与普通聊天没区别。"""
    llm = FakeLLM()
    result = await rag.answer_question(
        None,
        "进程和线程有什么区别？",
        document_ids=[indexed["document_id"]],
        provider=mock_embedder,
        store=test_store,
        llm=llm,
    )

    source = result.sources[0]
    assert source.document_id == indexed["document_id"]
    assert source.file_name == "p3_index_test.txt"
    assert source.page_start >= 1
    assert source.page_label.startswith("第")
    assert source.chunk_index >= 0
    assert "第三章 进程管理" in source.heading_path
    assert source.content


@pytest.mark.asyncio
async def test_sources_are_numbered_continuously(indexed, mock_embedder, test_store) -> None:
    """上下文编号必须连续（1,2,3…），否则答案里的 [n] 会对不上。"""
    result = await rag.answer_question(
        None,
        "进程和线程有什么区别？",
        document_ids=[indexed["document_id"]],
        provider=mock_embedder,
        store=test_store,
        llm=FakeLLM(),
    )
    used_indexes = [s.index for s in result.sources if s.used]
    assert used_indexes == list(range(1, len(used_indexes) + 1))


@pytest.mark.asyncio
async def test_prompt_contains_context_with_page_labels(
    indexed, mock_embedder, test_store
) -> None:
    """提示词里的片段必须带编号与页码，模型才能标引用。"""
    llm = FakeLLM()
    await rag.answer_question(
        None,
        "进程和线程有什么区别？",
        document_ids=[indexed["document_id"]],
        provider=mock_embedder,
        store=test_store,
        llm=llm,
    )

    user_message = llm.calls[0][-1]["content"]
    assert "[1]" in user_message
    assert "第 " in user_message and "页" in user_message
    assert "p3_index_test.txt" in user_message
    assert "进程和线程有什么区别？" in user_message
    # 提示词必须包含"资料之外不回答"这条硬性约束
    system_message = llm.calls[0][0]["content"]
    assert "不在你的资料中" in system_message


# --------------------------------------------------------------------------- #
# 距离门槛：RAG 最关键的一道闸门
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_irrelevant_question_short_circuits_without_calling_llm(
    indexed, mock_embedder, test_store
) -> None:
    """资料里没有相关内容时**不调用模型**。

    让模型面对一堆无关片段或空上下文回答，只会得到一段编造的内容 ——
    这是"套壳 ChatGPT"和真正 RAG 的分界线。
    """
    llm = FakeLLM()
    result = await rag.answer_question(
        None,
        "天气预报显示明天晴转多云，气温回升，请注意添衣",
        document_ids=[indexed["document_id"]],
        provider=mock_embedder,
        store=test_store,
        llm=llm,
    )

    assert result.short_circuited is True
    assert result.used == 0
    assert "不在你的资料中" in result.answer
    assert llm.calls == [], "短路时绝不能调用模型"


@pytest.mark.asyncio
async def test_dropped_sources_keep_reason(indexed, mock_embedder, test_store) -> None:
    """被门槛挡掉的片段也要返回，并写明原因 ——
    让用户看到"检索到了什么、为什么没用上"，而不是对着空来源发呆。"""
    result = await rag.answer_question(
        None,
        "天气预报显示明天晴转多云，气温回升，请注意添衣",
        document_ids=[indexed["document_id"]],
        provider=mock_embedder,
        store=test_store,
        llm=FakeLLM(),
    )

    assert result.retrieved > 0, "检索到了片段"
    dropped = [s for s in result.sources if not s.used]
    assert dropped
    assert all("相似度不足" in s.dropped_reason for s in dropped)


@pytest.mark.asyncio
async def test_distance_threshold_can_be_relaxed(
    indexed, mock_embedder, test_store, monkeypatch
) -> None:
    """放宽门槛后，原本被挡掉的片段会进入上下文 —— 证明门槛确实在起作用。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "rag_max_distance", 0.99)
    result = await rag.answer_question(
        None,
        "天气预报显示明天晴转多云，气温回升，请注意添衣",
        document_ids=[indexed["document_id"]],
        provider=mock_embedder,
        store=test_store,
        llm=FakeLLM(),
    )
    assert result.used > 0
    assert result.short_circuited is False


# --------------------------------------------------------------------------- #
# 上下文预算
# --------------------------------------------------------------------------- #
def test_context_budget_drops_overflow() -> None:
    sources = [
        rag.RagSource(
            index=i + 1,
            document_id=1,
            file_name="t.txt",
            chunk_index=i,
            page_start=1,
            page_end=1,
            block_type="text",
            heading_path=[],
            distance=0.1 * i,
            content="甲" * 100,
        )
        for i in range(5)
    ]
    used = rag.apply_context_budget(sources, max_chars=250)

    assert used <= 250
    assert [s.used for s in sources] == [True, True, False, False, False]
    assert "预算" in sources[2].dropped_reason


def test_context_budget_keeps_first_item_even_if_oversized() -> None:
    """最相关的那条即使超预算也要保留，否则会返回空上下文去问模型。"""
    sources = [
        rag.RagSource(
            index=1,
            document_id=1,
            file_name="t.txt",
            chunk_index=0,
            page_start=1,
            page_end=1,
            block_type="text",
            heading_path=[],
            distance=0.1,
            content="甲" * 500,
        )
    ]
    assert rag.apply_context_budget(sources, max_chars=100) == 500
    assert sources[0].used is True


def test_build_context_skips_unused_sources() -> None:
    used = rag.RagSource(
        index=1,
        document_id=1,
        file_name="a.txt",
        chunk_index=0,
        page_start=3,
        page_end=3,
        block_type="text",
        heading_path=["第三章"],
        distance=0.2,
        content="正文内容",
    )
    dropped = rag.RagSource(
        index=2,
        document_id=1,
        file_name="a.txt",
        chunk_index=1,
        page_start=9,
        page_end=9,
        block_type="text",
        heading_path=[],
        distance=0.9,
        content="不该出现的内容",
        used=False,
    )
    context = rag.build_context([used, dropped])

    assert "[1]" in context
    assert "第 3 页" in context
    assert "第三章" in context
    assert "不该出现的内容" not in context


def test_page_label_for_multipage_chunk() -> None:
    source = rag.RagSource(
        index=1,
        document_id=1,
        file_name="t.txt",
        chunk_index=0,
        page_start=6,
        page_end=8,
        block_type="text",
        heading_path=[],
        distance=0.1,
        content="x",
    )
    assert source.page_label == "第 6-8 页"


# --------------------------------------------------------------------------- #
# 错误路径
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_embedding_not_configured_maps_to_409() -> None:
    """未配 Key 要给出 409 与"去配 Key"的提示，而不是 502 或 500。"""
    with pytest.raises(rag.RagError) as exc:
        await rag.answer_question(
            None, "任意问题", provider=BrokenEmbedder(), store=None, llm=FakeLLM()
        )
    assert exc.value.status_code == 409
    assert exc.value.code == "embedding_not_configured"
    assert "EMBEDDING_API_KEY" in exc.value.message


@pytest.mark.asyncio
async def test_empty_question_is_rejected() -> None:
    with pytest.raises(rag.RagError) as exc:
        await rag.answer_question(None, "   ")
    assert exc.value.status_code == 422
    assert exc.value.code == "empty_question"


@pytest.mark.asyncio
async def test_broken_store_maps_to_503(indexed, mock_embedder) -> None:
    """向量库不可用要给 503 与可读提示，而不是把原始异常抛给用户。"""
    from app.rag.vectorstore import VectorStore

    class BrokenStore(VectorStore):
        def collection(self, name=None):  # type: ignore[no-untyped-def]
            raise RuntimeError("chroma 挂了")

    with pytest.raises(rag.RagError) as exc:
        await rag.answer_question(
            None,
            "进程是什么",
            provider=mock_embedder,
            store=BrokenStore(),
            llm=FakeLLM(),
        )
    assert exc.value.status_code == 503
    assert exc.value.code == "retrieve_failed"


# --------------------------------------------------------------------------- #
# 生成失败也要把检索结果交出去
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_generation_failure_keeps_sources(indexed, mock_embedder, test_store) -> None:
    """模型不可用时，**检索到的来源必须照样返回**。

    这是真实踩到的场景：DeepSeek 余额不足（402），`LLMError` 直接冒泡成 500，
    用户问了一个资料里确实有的问题却什么都拿不到 —— 而片段明明已经检索出来了。
    模型不可用、限流、超时都是常规运维事件，不该让整次问答白费。
    """
    result = await rag.answer_question(
        None,
        "进程和线程有什么区别？",
        document_ids=[indexed["document_id"]],
        provider=mock_embedder,
        store=test_store,
        llm=BrokenLLM(),
    )

    assert result.generation_failed is True
    assert result.sources, "来源必须保留"
    assert result.used >= 1
    assert "模型不可用" in result.answer
    assert "余额不足" in result.error
    assert result.mode == "retrieval-only"
    # 失败原因要能对上具体类别，而不是一串 SDK 原始错误
    assert "402" in result.error


@pytest.mark.asyncio
async def test_generation_failure_does_not_fabricate(
    indexed, mock_embedder, test_store
) -> None:
    """生成失败时**不能编造答案** —— 只说"找到了资料但答不上来"。"""
    result = await rag.answer_question(
        None,
        "进程和线程有什么区别？",
        document_ids=[indexed["document_id"]],
        provider=mock_embedder,
        store=test_store,
        llm=BrokenLLM("连接超时"),
    )
    assert "找不到" not in result.answer
    assert result.error == "模型调用超时"
    # 正文里明确说明模型不可用，引导用户看来源
    assert "来源片段" in result.answer


def test_readable_llm_error_mapping() -> None:
    """错误翻译要覆盖常见的几类，而不是把 SDK 原文丢给用户。

    中英文都要认：底层 SDK 抛英文，我们自己包装的异常里可能有中文。
    """
    from app.core.llm import LLMError

    cases = {
        "Error code: 402 - {'message': 'Insufficient Balance'}": "余额不足",
        "Error code: 401 - invalid api key": "凭据无效",
        "Error code: 429 - rate limit exceeded": "限流",
        "Request timed out": "超时",
        "连接超时": "超时",  # 中文关键词同样要识别
        "Error code: 503 - service unavailable": "暂时不可用",
    }
    for raw, expected in cases.items():
        assert expected in rag._readable_llm_error(LLMError(raw)), raw

    # 认不出来的类别要退化成"调用失败 + 异常类型"，而不是空字符串
    assert "RuntimeError" in rag._readable_llm_error(RuntimeError("莫名其妙"))


# --------------------------------------------------------------------------- #
# 能力探测
# --------------------------------------------------------------------------- #
def test_capabilities_never_leaks_key() -> None:
    import json

    caps = rag.capabilities()
    assert "embedding" in caps
    assert "configured" in caps["embedding"]
    assert "api_key" not in json.dumps(caps).lower()


# --------------------------------------------------------------------------- #
# 多文档过滤
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_document_filter_excludes_other_documents(
    indexed, mock_embedder, test_store
) -> None:
    """指定 document_ids 后，别的文档的片段不能进入答案。"""
    other = await mock_embedder.embed(["某商超蔬菜类商品的动态定价与补货决策"])
    test_store.upsert(
        ids=["doc888:chunk0"],
        documents=["某商超蔬菜类商品的动态定价与补货决策"],
        embeddings=other.vectors,
        metadatas=[
            {
                "document_id": 888,
                "chunk_index": 0,
                "page_start": 1,
                "page_end": 1,
                "block_type": "text",
                "heading_path": "",
                "file_name": "other.txt",
                "char_count": 20,
            }
        ],
    )

    result = await rag.answer_question(
        None,
        "进程和线程有什么区别？",
        document_ids=[indexed["document_id"]],
        provider=mock_embedder,
        store=test_store,
        llm=FakeLLM(),
    )
    assert all(s.document_id == indexed["document_id"] for s in result.sources)


@pytest.mark.asyncio
async def test_seeded_chunks_are_the_expected_ones(indexed) -> None:
    """夹具内容与断言的一致性检查，避免测试与数据脱节。"""
    assert len(CHUNKS) == 3
    assert indexed["chunk_count"] == 3
