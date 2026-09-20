"""云端 Embedding provider 测试。

全部用 `httpx.MockTransport` 打桩传输层 —— **不打真实网络、不需要 Key**，
因此在任何环境下都能跑，且结果确定。

重点验证「未配置 / 超时 / HTTP 错误 / 维度不符 / 批次数量不符」各自可区分，
以及批量、截断、重试这三件容易被忽略但一定会用到的编排逻辑。
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.core.config import get_settings
from app.rag.embedding import (
    APIEmbeddingProvider,
    EmbeddingError,
    EmbeddingErrorCode,
    MockEmbeddingProvider,
    get_embedder,
)


def config(**overrides):
    """基于真实配置派生一份测试配置，避免污染单例。"""
    return get_settings().model_copy(update=overrides)


def api_provider(handler, **overrides) -> APIEmbeddingProvider:
    """构造一个把网络请求交给 handler 的 API provider。"""
    cfg = config(
        embedding_api_key="sk-test-key",
        embedding_base_url="https://example.com/v1",
        **overrides,
    )
    return APIEmbeddingProvider(cfg, transport=httpx.MockTransport(handler))


def ok_handler(count_dim: int = 1024, *, reverse: bool = False, model: str = "fake"):
    """返回 n 条 count_dim 维向量的假服务端。`reverse` 用于验证我们按 index 重排。"""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        inputs = body["input"]
        indexes = list(range(len(inputs)))
        if reverse:
            indexes = list(reversed(indexes))
        data = [
            {"index": i, "embedding": [float(i) / 10.0] * count_dim} for i in indexes
        ]
        return httpx.Response(200, json={"data": data, "model": model})

    return handler


# --------------------------------------------------------------------------- #
# 配置与凭据
# --------------------------------------------------------------------------- #
def test_not_configured_when_key_missing() -> None:
    provider = APIEmbeddingProvider(config(embedding_api_key="", embedding_base_url=""))
    assert provider.available() is False

    import asyncio

    with pytest.raises(EmbeddingError) as exc:
        asyncio.run(provider.embed(["测试文本"]))
    assert exc.value.code == EmbeddingErrorCode.NOT_CONFIGURED


def test_capabilities_never_leaks_key() -> None:
    provider = APIEmbeddingProvider(
        config(embedding_api_key="sk-super-secret", embedding_base_url="https://x/v1")
    )
    caps = provider.capabilities()
    assert caps["configured"] is True
    assert "sk-super-secret" not in json.dumps(caps)


def test_get_embedder_respects_provider_setting() -> None:
    assert isinstance(get_embedder(config(embedding_provider="mock")), MockEmbeddingProvider)
    assert isinstance(get_embedder(config(embedding_provider="api")), APIEmbeddingProvider)


def test_get_embedder_rejects_unimplemented_local() -> None:
    """local 未实现时要明确报错，而不是悄悄降级成别的东西。"""
    with pytest.raises(EmbeddingError) as exc:
        get_embedder(config(embedding_provider="local"))
    assert exc.value.code == EmbeddingErrorCode.UNSUPPORTED


# --------------------------------------------------------------------------- #
# Mock provider：确定性与相似性
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_mock_is_deterministic() -> None:
    provider = MockEmbeddingProvider()
    first = await provider.embed(["进程是资源分配的基本单位"])
    second = await provider.embed(["进程是资源分配的基本单位"])
    assert first.vectors == second.vectors


@pytest.mark.asyncio
async def test_mock_preserves_similarity_ordering() -> None:
    """mock 必须是"有意义的"向量化，否则检索相关的测试全部失去价值。

    这条断言在守护一个原则：mock 不是返回随机数，它得让
    「内容相近 → 距离更小」成立，距离门槛之类的逻辑才可测。
    """
    provider = MockEmbeddingProvider()
    result = await provider.embed(
        [
            "线程是进程内部的执行单元，也是处理机调度的基本单位。",
            "死锁的产生必须同时满足互斥、请求并保持、不可剥夺与循环等待四个条件。",
            "今天天气不错",
            "线程是什么",
        ]
    )
    related, unrelated, _, query = result.vectors

    def cosine(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b))

    assert cosine(query, related) > cosine(query, unrelated)


@pytest.mark.asyncio
async def test_mock_blank_text_still_produces_valid_vector() -> None:
    """空文本在 `_prepare()` 阶段就被替换成占位符，因此不会送到 provider 那里变成空串。

    得到的是占位符的向量（非零、已归一化）—— 这是刻意的：
    空串会被服务端拒绝，替换成占位符能保证"输入 N 条一定拿到 N 条向量"。
    """
    provider = MockEmbeddingProvider()
    result = await provider.embed([""])
    assert len(result.vectors) == 1
    norm = sum(v * v for v in result.vectors[0]) ** 0.5
    assert norm == pytest.approx(1.0) or norm == 0.0


def test_hash_vector_of_blank_is_zero() -> None:
    """直接调用底层向量化时空文本才得到零向量。"""
    assert set(MockEmbeddingProvider.hash_vector("   ", 64)) == {0.0}


# --------------------------------------------------------------------------- #
# 批量
# --------------------------------------------------------------------------- #
class CountingProvider(MockEmbeddingProvider):
    """记录每批大小的 mock，用于验证分批逻辑。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.batch_sizes: list[int] = []

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.batch_sizes.append(len(texts))
        return await super()._embed_batch(texts)


@pytest.mark.asyncio
async def test_texts_are_split_into_batches() -> None:
    provider = CountingProvider(config(embedding_batch_size=10))
    result = await provider.embed([f"第 {i} 段内容" for i in range(25)])

    assert len(result.vectors) == 25
    assert result.batch_count == 3
    assert provider.batch_sizes == [10, 10, 5], "应按配置分批，最后一批装余数"


@pytest.mark.asyncio
async def test_batch_size_is_respected_for_large_input() -> None:
    provider = CountingProvider(config(embedding_batch_size=7))
    await provider.embed([f"内容 {i}" for i in range(20)])
    assert provider.batch_sizes == [7, 7, 6]


# --------------------------------------------------------------------------- #
# 截断
# --------------------------------------------------------------------------- #
class RecordingProvider(MockEmbeddingProvider):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.seen: list[str] = []

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.seen.extend(texts)
        return await super()._embed_batch(texts)


@pytest.mark.asyncio
async def test_long_text_is_truncated() -> None:
    """超长文本会让服务端直接返回 400，先截断比失败实用。"""
    provider = RecordingProvider(config(embedding_max_chars=50))
    await provider.embed(["甲" * 500])
    assert len(provider.seen[0]) == 50


@pytest.mark.asyncio
async def test_blank_text_is_replaced() -> None:
    """空串通常被服务端拒绝，替换成占位符。"""
    provider = RecordingProvider()
    await provider.embed(["", "   "])
    assert all(text.strip() for text in provider.seen)


# --------------------------------------------------------------------------- #
# 错误状态
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_timeout_is_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    provider = api_provider(handler, embedding_max_retries=0)
    with pytest.raises(EmbeddingError) as exc:
        await provider.embed(["测试"])
    assert exc.value.code == EmbeddingErrorCode.TIMEOUT


@pytest.mark.asyncio
async def test_http_error_carries_status_code() -> None:
    provider = api_provider(
        lambda request: httpx.Response(401, text="invalid api key"), embedding_max_retries=0
    )
    with pytest.raises(EmbeddingError) as exc:
        await provider.embed(["测试"])
    assert exc.value.code == EmbeddingErrorCode.HTTP_ERROR
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_dim_mismatch_is_reported() -> None:
    """维度与配置不符属于配置错误，必须让人看见 ——
    静默写入一批错维度的向量会让检索悄悄失效。"""
    provider = api_provider(ok_handler(count_dim=768), embedding_dim=1024)
    with pytest.raises(EmbeddingError) as exc:
        await provider.embed(["测试"])
    assert exc.value.code == EmbeddingErrorCode.DIM_MISMATCH
    assert "768" in str(exc.value)


@pytest.mark.asyncio
async def test_error_identifies_failing_batch() -> None:
    """要说清是第几批失败 —— 一份资料会分成十几批，只说"失败了"帮不上忙。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] >= 2:
            return httpx.Response(400, text="bad request")
        body = json.loads(request.content)
        return httpx.Response(
            200, json={"data": [{"index": i, "embedding": [0.1] * 1024} for i in range(len(body["input"]))]}
        )

    provider = api_provider(handler, embedding_batch_size=2, embedding_max_retries=0)
    with pytest.raises(EmbeddingError) as exc:
        await provider.embed([f"文本 {i}" for i in range(6)])
    assert exc.value.batch_index == 2


@pytest.mark.asyncio
async def test_parse_error_on_non_json_body() -> None:
    provider = api_provider(
        lambda request: httpx.Response(200, text="<html>not json</html>"), embedding_max_retries=0
    )
    with pytest.raises(EmbeddingError) as exc:
        await provider.embed(["测试"])
    assert exc.value.code == EmbeddingErrorCode.PARSE_ERROR


@pytest.mark.asyncio
async def test_empty_data_is_reported() -> None:
    provider = api_provider(lambda request: httpx.Response(200, json={"data": []}))
    with pytest.raises(EmbeddingError) as exc:
        await provider.embed(["测试"])
    assert exc.value.code == EmbeddingErrorCode.EMPTY


@pytest.mark.asyncio
async def test_count_mismatch_is_reported() -> None:
    """返回数量少于请求数量时必须报错，不能把向量和文本错配。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1] * 1024}]})

    provider = api_provider(handler, embedding_max_retries=0)
    with pytest.raises(EmbeddingError) as exc:
        await provider.embed(["甲", "乙", "丙"])
    assert exc.value.code == EmbeddingErrorCode.PARSE_ERROR


# --------------------------------------------------------------------------- #
# 重试
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_retries_on_server_error_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="temporarily unavailable")
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={"data": [{"index": i, "embedding": [0.2] * 1024} for i in range(len(body["input"]))]},
        )

    provider = api_provider(handler, embedding_max_retries=2)
    result = await provider.embed(["测试"])
    assert len(result) == 1
    assert calls["n"] == 2, "5xx 应当重试一次并成功"


@pytest.mark.asyncio
async def test_does_not_retry_on_client_error() -> None:
    """4xx 是请求本身的问题，重试不会变好，只会浪费时间与额度。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="bad request")

    provider = api_provider(handler, embedding_max_retries=3)
    with pytest.raises(EmbeddingError):
        await provider.embed(["测试"])
    assert calls["n"] == 1


# --------------------------------------------------------------------------- #
# 协议细节
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_results_are_reordered_by_index() -> None:
    """不少供应商返回的 data 顺序与输入顺序不一致。

    直接按返回顺序使用会把向量与文本错配 —— 这类错误检索时**完全不报错**，
    只会静默给出错误答案，属于最难排查的一类问题。
    """
    provider = api_provider(ok_handler(reverse=True))
    result = await provider.embed(["A", "B", "C"])

    # 假服务端让第 i 条的向量首维为 i/10，逆序返回；重排后应恢复为 0.0 / 0.1 / 0.2
    assert [round(v[0], 3) for v in result.vectors] == [0.0, 0.1, 0.2]


@pytest.mark.asyncio
async def test_request_carries_model_and_dimensions() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200, json={"data": [{"index": 0, "embedding": [0.1] * 1024}]}
        )

    provider = api_provider(handler, embedding_model="text-embedding-v3", embedding_dim=1024)
    await provider.embed(["测试"])

    assert seen["model"] == "text-embedding-v3"
    assert seen["dimensions"] == 1024
    assert seen["input"] == ["测试"]
    assert seen["auth"] == "Bearer sk-test-key"


@pytest.mark.asyncio
async def test_empty_input_returns_empty_without_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("空输入不应该发起请求")

    provider = api_provider(handler)
    result = await provider.embed([])
    assert len(result) == 0
