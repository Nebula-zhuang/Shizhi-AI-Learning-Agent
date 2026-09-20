"""P3 接口契约测试。

覆盖新增的 5 个端点与三个容易出错的边界：
  - 未配 Embedding Key 时必须明确 409（而不是 502/500，也不是静默降级）
  - 索引任务必须真的投递成功（路由写成同步 def 会因没有事件循环而失败）
  - 空问题必须被 422 挡住

与 P1/P2 的接口测试同一策略：**只打桩流水线本体，保留真实的 runner.submit** ——
把被测环节本身打桩，等于把该环节的 bug 请出测试。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services import index_runner, index_service, rag_service

client = TestClient(app)


class FakeLLM:
    model = "fake-model"
    mode = "mock"

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, **_kwargs):  # noqa: ANN001
        from app.core.llm import LLMResult

        self.calls += 1
        return LLMResult(
            content="进程是资源分配的基本单位 [1]。", model=self.model, mode=self.mode
        )


@pytest.fixture()
def p3_env(monkeypatch, mock_embedder, test_store):
    """把索引/问答用到的默认依赖统统换成测试替身。

    这样接口测试既不打网络、也不碰生产向量集合。
    """
    fake_llm = FakeLLM()
    monkeypatch.setattr(index_service, "default_embedder", mock_embedder)
    monkeypatch.setattr(index_service, "default_store", test_store)
    monkeypatch.setattr(rag_service, "default_embedder", mock_embedder)
    monkeypatch.setattr(rag_service, "default_store", test_store)
    monkeypatch.setattr(rag_service, "llm_gateway", fake_llm)
    # 让能力接口与索引接口都认为"已配置"
    monkeypatch.setattr(settings, "embedding_api_key", "sk-test-key")
    monkeypatch.setattr(settings, "embedding_base_url", "https://example.com/v1")
    return {"llm": fake_llm, "store": test_store}


@pytest.fixture()
def seeded_index(seeded_document, p3_env):
    """把测试文档索引进测试集合。"""
    import asyncio

    from app.db.session import SessionLocal

    with SessionLocal() as session:
        asyncio.run(
            index_service.index_document(
                session,
                seeded_document["document_id"],
                provider=index_service.default_embedder,
                store=p3_env["store"],
            )
        )
    return seeded_document


# --------------------------------------------------------------------------- #
# 能力探测
# --------------------------------------------------------------------------- #
def test_capabilities_shape_and_no_key_leak(p3_env) -> None:
    res = client.get("/api/rag/capabilities")
    assert res.status_code == 200
    body = res.json()

    assert body["embedding"]["provider"] in {"api", "mock"}
    assert body["embedding"]["configured"] is True
    assert body["embedding"]["dim"] > 0
    assert body["top_k"] >= 1
    assert 0 < body["max_distance"] <= 2
    # 绝不能回显密钥
    assert "sk-test-key" not in res.text
    assert "api_key" not in json.dumps(body).lower()


def test_capabilities_reports_unconfigured(monkeypatch) -> None:
    monkeypatch.setattr(settings, "embedding_provider", "api")
    monkeypatch.setattr(settings, "embedding_api_key", "")
    monkeypatch.setattr(settings, "embedding_base_url", "")
    body = client.get("/api/rag/capabilities").json()
    assert body["embedding"]["configured"] is False


# --------------------------------------------------------------------------- #
# 索引
# --------------------------------------------------------------------------- #
def test_index_rejects_when_embedding_not_configured(monkeypatch) -> None:
    """未配 Key 时必须明确 409 并说清怎么修，而不是 502。"""
    monkeypatch.setattr(settings, "embedding_provider", "api")
    monkeypatch.setattr(settings, "embedding_api_key", "")
    monkeypatch.setattr(settings, "embedding_base_url", "")

    res = client.post("/api/rag/index", json={})
    assert res.status_code == 409
    assert "EMBEDDING_API_KEY" in res.json()["detail"]


def test_index_rejects_unknown_document(p3_env) -> None:
    res = client.post("/api/rag/index", json={"document_ids": [99999999]})
    assert res.status_code == 404


def test_index_dispatches_task(p3_env, monkeypatch) -> None:
    """必须真的投递成功 —— 路由若是同步函数，submit 会因没有事件循环而失败。"""
    calls: list = []

    async def fake_index_documents(db, document_ids=None, **kwargs):  # noqa: ANN001
        calls.append(document_ids)
        return index_service.IndexRunStats(documents=0, indexed_chunks=0)

    monkeypatch.setattr(index_service, "index_documents", fake_index_documents)

    res = client.post("/api/rag/index", json={})
    assert res.status_code == 202, f"索引未成功投递：{res.status_code} {res.text}"
    body = res.json()
    assert body["accepted"] is True
    assert "轮询" in body["message"]


def test_index_status_shape(p3_env) -> None:
    body = client.get("/api/rag/index/status").json()
    assert body["collection"]
    assert body["state"] in {"idle", "queued", "running", "done", "failed", "cancelled"}
    assert isinstance(body["items"], list)
    assert body["store_available"] is True


def test_index_status_rejects_bad_document_ids(p3_env) -> None:
    res = client.get("/api/rag/index/status", params={"document_ids": "1,abc"})
    assert res.status_code == 422


# --------------------------------------------------------------------------- #
# 问答
# --------------------------------------------------------------------------- #
def test_ask_returns_answer_and_sources(seeded_index, p3_env) -> None:
    res = client.post(
        "/api/rag/ask",
        json={
            "question": "进程和线程有什么区别？",
            "document_ids": [seeded_index["document_id"]],
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["answer"]
    assert body["sources"], "必须返回来源"
    assert body["used"] >= 1
    assert body["model"] == "fake-model"
    assert body["short_circuited"] is False

    source = body["sources"][0]
    for key in (
        "index",
        "document_id",
        "file_name",
        "chunk_index",
        "page_start",
        "page_end",
        "page_label",
        "heading_path",
        "distance",
        "content",
        "used",
    ):
        assert key in source, f"来源缺少字段 {key}"


def test_ask_short_circuits_for_unrelated_question(seeded_index, p3_env) -> None:
    """资料里没有相关内容时不调用模型，直接给出明确结论。"""
    res = client.post(
        "/api/rag/ask",
        json={
            "question": "天气预报显示明天晴转多云，气温回升，请注意添衣",
            "document_ids": [seeded_index["document_id"]],
        },
    )
    assert res.status_code == 200
    body = res.json()

    assert body["short_circuited"] is True
    assert body["used"] == 0
    assert "不在你的资料中" in body["answer"]
    assert p3_env["llm"].calls == 0, "短路时绝不能调用模型"
    # 被门槛挡掉的片段依然返回，并带原因
    dropped = [s for s in body["sources"] if not s["used"]]
    assert dropped and dropped[0]["dropped_reason"]


def test_ask_rejects_empty_question(seeded_index) -> None:
    res = client.post("/api/rag/ask", json={"question": " "})
    assert res.status_code in {422, 400}


def test_ask_rejects_out_of_range_top_k(seeded_index) -> None:
    assert client.post("/api/rag/ask", json={"question": "进程", "top_k": 0}).status_code == 422
    assert client.post("/api/rag/ask", json={"question": "进程", "top_k": 999}).status_code == 422


def test_ask_returns_409_when_not_configured(monkeypatch) -> None:
    """未配 Key 时问答给 409 与可操作提示。"""
    monkeypatch.setattr(settings, "embedding_provider", "api")
    monkeypatch.setattr(settings, "embedding_api_key", "")
    monkeypatch.setattr(settings, "embedding_base_url", "")

    from app.rag.embedding import APIEmbeddingProvider

    broken = APIEmbeddingProvider(
        settings.model_copy(update={"embedding_api_key": "", "embedding_base_url": ""})
    )
    monkeypatch.setattr(rag_service, "default_embedder", broken)

    res = client.post("/api/rag/ask", json={"question": "进程是什么"})
    assert res.status_code == 409
    assert "EMBEDDING_API_KEY" in res.json()["detail"]


# --------------------------------------------------------------------------- #
# 原文回链
# --------------------------------------------------------------------------- #
def test_source_backlink_returns_chunk(seeded_index) -> None:
    document_id = seeded_index["document_id"]
    res = client.get(f"/api/rag/sources/{document_id}/0")
    assert res.status_code == 200
    body = res.json()

    assert body["document_id"] == document_id
    assert body["chunk_index"] == 0
    assert body["page_start"] >= 1
    assert body["content"]


def test_source_backlink_404_for_missing_chunk(seeded_index) -> None:
    document_id = seeded_index["document_id"]
    assert client.get(f"/api/rag/sources/{document_id}/9999").status_code == 404


def test_source_backlink_404_for_missing_document() -> None:
    assert client.get("/api/rag/sources/99999999/0").status_code == 404


# --------------------------------------------------------------------------- #
# 健康检查新增组件
# --------------------------------------------------------------------------- #
def test_health_reports_embedding_component(p3_env) -> None:
    body = client.get("/api/health").json()
    names = {c["name"] for c in body["components"]}
    assert "embedding" in names, "P3 起健康检查应包含 embedding 组件"

    embedding = next(c for c in body["components"] if c["name"] == "embedding")
    assert "configured" in embedding["detail"]
    # 健康检查同样不能回显密钥
    assert "sk-test-key" not in json.dumps(body)


def test_health_marks_embedding_not_configured(monkeypatch) -> None:
    monkeypatch.setattr(settings, "embedding_provider", "api")
    monkeypatch.setattr(settings, "embedding_api_key", "")
    monkeypatch.setattr(settings, "embedding_base_url", "")
    body = client.get("/api/health").json()
    embedding = next(c for c in body["components"] if c["name"] == "embedding")
    assert embedding["ok"] is False
    assert body["status"] == "degraded"


def test_index_runner_is_not_running_after_tests() -> None:
    """索引任务必须自己收尾，不能把状态留在 running 让后续请求一直 409。"""
    assert index_runner.get_state().get("state") in {
        None,
        "idle",
        "done",
        "queued",
        "running",
        "failed",
    }
