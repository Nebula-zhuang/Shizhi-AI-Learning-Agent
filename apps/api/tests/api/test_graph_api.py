"""P2 接口契约测试。

覆盖新增的 7 个端点，以及两个容易出错的边界：
  - 校验总开关关闭时必须明确拒绝（409），而不是静默什么都不做
  - 校验任务投递必须是真投递（路由写成同步 def 会因线程池没有事件循环而失败）

校验任务被打桩的是**流水线本体**（`verify_service.verify_document`），
`verify_runner.submit` 保持真实 —— 与 P1 的 API 测试同一策略：
把被测环节本身打桩，等于把该环节的 bug 请出测试。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.db.base  # noqa: F401 - 触发全部模型注册
from app.core.config import settings
from app.db.session import SessionLocal
from app.main import app
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.knowledge_point import KnowledgePoint
from app.services import verify_service

client = TestClient(app)

#: 夹具用的固定 hash。用固定值而非随机值，是为了让测试数据可辨认、便于人工排查。
#: 代价是必须处理上一次异常退出留下的同 hash 记录（见夹具里的清理逻辑），
#: 否则会撞 documents.file_hash 的唯一约束，表现为偶发失败。
FIXTURE_HASH = "p2graph" + "1" * 57


@pytest.fixture(autouse=True)
def force_mock_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """强制 mock 模式。

    接口契约测试不应依赖真实模型：既慢，又会因网络抖动产生与代码无关的随机失败。
    有一个用例会真的跑一遍 `verify_service.verify_document`（验证校验收据确实被写入），
    那是必须的；但它的断言只关心"记录写没写、字段对不对"，与模型说了什么无关。
    """
    monkeypatch.setattr(settings, "llm_api_key", "")


@pytest.fixture()
def graph_doc():
    """一份带章节层级的知识点文档，测试后整份删除。"""
    with SessionLocal() as session:
        # 防御：清掉可能残留的同 hash 文档，保证用例可重复运行
        for stale in session.execute(
            select(Document).where(Document.file_hash == FIXTURE_HASH)
        ).scalars().all():
            session.delete(stale)
        session.commit()

        document = Document(
            file_name="p2_graph_test.txt",
            file_type="text",
            file_size=100,
            file_hash=FIXTURE_HASH,
            storage_path="test/p2_graph_test.txt",
            page_count=3,
            char_count=300,
            chunk_count=3,
            kp_count=4,
            parse_status="ready",
            progress=100,
        )
        session.add(document)
        session.flush()
        doc_id = document.id

        for idx in range(3):
            session.add(
                Chunk(
                    document_id=doc_id,
                    chunk_index=idx,
                    content=f"第 {idx + 1} 段的原文内容，用于溯源验证。",
                    page_start=idx + 1,
                    page_end=idx + 1,
                    block_type="text",
                    char_count=20,
                )
            )

        specs = [
            ("3.1 进程的概念", ["3.1 进程的概念"], 0, 5),
            # 刻意造一个真正的子节，才能验证 contains 及其反向视角 belongs_to
            ("进程的四个基本特征", ["3.1 进程的概念", "3.1.1 基本特征"], 1, 4),
            ("3.2 进程的状态", ["3.2 进程的状态"], 2, 5),
            ("3.3 进程控制块", ["3.3 进程控制块"], 3, 3),
        ]
        kp_ids = []
        for order, (title, heading, chunk, importance) in enumerate(specs):
            point = KnowledgePoint(
                document_id=doc_id,
                title=title,
                title_norm=title.lower(),
                summary=f"{title}的摘要",
                details=f"{title}的展开讲解，忠实于原文。",
                key_points=["要点一", "要点二"],
                difficulty=2,
                importance=importance,
                confidence=0.9,
                verify_status="unverified",
                heading_path=heading,
                source_chunk_indexes=[chunk],
                source_pages=[chunk + 1],
                order_index=order,
            )
            session.add(point)
            session.flush()
            kp_ids.append(point.id)
        session.commit()

    yield {"document_id": doc_id, "kp_ids": kp_ids}

    with SessionLocal() as session:
        doc = session.get(Document, doc_id)
        if doc is not None:
            session.delete(doc)
            session.commit()


@pytest.fixture()
def stub_verify_pipeline(monkeypatch: pytest.MonkeyPatch):
    """替换校验流水线本体，保留真实的 submit。"""
    calls: list[int] = []

    async def fake_verify_document(db, document_id, **kwargs):
        calls.append(document_id)
        return verify_service.VerifyStats(total_points=0)

    monkeypatch.setattr(verify_service, "verify_document", fake_verify_document)
    return calls


# --------------------------------------------------------------------------- #
# 图谱
# --------------------------------------------------------------------------- #
def test_graph_of_missing_document_returns_404() -> None:
    assert client.get("/api/documents/99999999/graph").status_code == 404


def test_graph_without_relations_returns_isolated_nodes(graph_doc) -> None:
    """还没构建关系时，图谱应返回全部节点与 0 条边，而不是报错。"""
    res = client.get(f"/api/documents/{graph_doc['document_id']}/graph")
    assert res.status_code == 200
    body = res.json()

    assert body["document_id"] == graph_doc["document_id"]
    assert len(body["nodes"]) == 4
    assert body["edges"] == []
    assert body["stats"]["node_count"] == 4
    assert body["stats"]["edge_count"] == 0
    assert body["stats"]["isolated_nodes"] == 4

    node = body["nodes"][0]
    assert {"id", "title", "difficulty", "importance", "verify_status", "depth"} <= set(node)


def test_graph_depth_reflects_heading_hierarchy(graph_doc) -> None:
    body = client.get(f"/api/documents/{graph_doc['document_id']}/graph").json()
    depths = {n["title"]: n["depth"] for n in body["nodes"]}
    # 单级章节的深度为 1，子节为 2 —— 前端据此分层排布
    assert depths["3.1 进程的概念"] == 1
    assert depths["进程的四个基本特征"] == 2
    assert depths["3.2 进程的状态"] == 1


# --------------------------------------------------------------------------- #
# 关系构建
# --------------------------------------------------------------------------- #
def test_build_relations_creates_edges_with_evidence(graph_doc) -> None:
    doc_id = graph_doc["document_id"]
    res = client.post(f"/api/documents/{doc_id}/relations")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["stats"]["created"] > 0

    graph = client.get(f"/api/documents/{doc_id}/graph").json()
    assert graph["edges"], "构建后应当有边"
    for edge in graph["edges"]:
        # 每条边都必须能说明依据 —— 这是 P2 的硬约束
        assert edge["source"], f"边 {edge['id']} 缺少构建依据"
        assert edge["evidence"], f"边 {edge['id']} 缺少可读证据"
        assert edge["relation_type"] in {"prerequisite", "related", "contains"}
        assert 0 < edge["confidence"] <= 1
        assert edge["inverse_type"]


def test_build_relations_is_idempotent(graph_doc) -> None:
    doc_id = graph_doc["document_id"]
    first = client.post(f"/api/documents/{doc_id}/relations").json()["stats"]["created"]
    second = client.post(f"/api/documents/{doc_id}/relations").json()["stats"]["created"]
    assert first == second
    graph = client.get(f"/api/documents/{doc_id}/graph").json()
    assert graph["stats"]["edge_count"] == first


def test_contains_edge_exposes_belongs_to_inverse(graph_doc) -> None:
    """contains 的反向视角由接口派生，不落库（避免图里出现重影边）。"""
    doc_id = graph_doc["document_id"]
    client.post(f"/api/documents/{doc_id}/relations")
    edges = client.get(f"/api/documents/{doc_id}/graph").json()["edges"]
    contains = [e for e in edges if e["relation_type"] == "contains"]
    assert contains, "这组数据应当产生从属关系"
    assert all(e["inverse_type"] == "belongs_to" for e in contains)


# --------------------------------------------------------------------------- #
# 能力探测
# --------------------------------------------------------------------------- #
def test_capabilities_endpoint() -> None:
    body = client.get("/api/verify/capabilities").json()
    assert body["web_provider"] == "tavily"
    assert isinstance(body["tavily_configured"], bool)
    # 未配置 Key 时必须明确报告联网核验未生效
    assert body["web_verify_effective"] == (
        body["web_verify_enabled"] and body["tavily_configured"]
    )
    # 绝不回显 Key 本身
    assert "api_key" not in body


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def test_verify_missing_document_returns_404() -> None:
    assert client.post("/api/documents/99999999/verify").status_code == 404


def test_verify_disabled_returns_409(monkeypatch: pytest.MonkeyPatch, graph_doc) -> None:
    monkeypatch.setattr(settings, "verify_enabled", False)
    res = client.post(f"/api/documents/{graph_doc['document_id']}/verify")
    assert res.status_code == 409
    assert "关闭" in res.json()["detail"]


def test_verify_dispatches_task(graph_doc, stub_verify_pipeline) -> None:
    """必须真的投递成功 —— 路由若是同步函数，submit 会因没有事件循环而失败。"""
    res = client.post(f"/api/documents/{graph_doc['document_id']}/verify")
    assert res.status_code == 202, f"校验未成功投递：{res.status_code} {res.text}"
    body = res.json()
    assert body["accepted"] is True
    assert "轮询" in body["message"]


def test_verify_status_reports_progress(graph_doc) -> None:
    body = client.get(f"/api/documents/{graph_doc['document_id']}/verify/status").json()
    assert body["document_id"] == graph_doc["document_id"]
    assert body["state"] in {"idle", "queued", "running", "done", "failed", "cancelled"}
    assert body["total_points"] == 4
    assert body["checked_points"] == 0  # 还没校验过
    assert "by_status" in body


def test_verify_status_of_missing_document_returns_404() -> None:
    assert client.get("/api/documents/99999999/verify/status").status_code == 404


# --------------------------------------------------------------------------- #
# 校验收据
# --------------------------------------------------------------------------- #
def test_checks_of_missing_point_returns_404() -> None:
    assert client.get("/api/knowledge-points/99999999/checks").status_code == 404


def test_checks_endpoint_always_returns_three_groups(graph_doc) -> None:
    """三层必须分开返回，即使还没有任何记录 —— 界面结构才是稳定的。"""
    kp_id = graph_doc["kp_ids"][0]
    res = client.get(f"/api/knowledge-points/{kp_id}/checks")
    assert res.status_code == 200
    body = res.json()

    assert body["kp_id"] == kp_id
    assert body["verify_status"] == "unverified"
    assert [g["check_type"] for g in body["groups"]] == ["rule", "model", "web"]
    assert all(g["latest"] is None for g in body["groups"])
    assert body["total"] == 0


@pytest.mark.asyncio
async def test_checks_endpoint_returns_records_after_verification(graph_doc) -> None:
    kp_id = graph_doc["kp_ids"][0]
    with SessionLocal() as session:
        await verify_service.verify_document(
            session, graph_doc["document_id"], page_count=3
        )

    body = client.get(f"/api/knowledge-points/{kp_id}/checks").json()
    assert body["total"] > 0
    groups = {g["check_type"]: g for g in body["groups"]}
    assert groups["rule"]["latest"] is not None
    assert groups["rule"]["latest"]["engine"] == "rule-v1"
    # 规则层的逐项明细放在 evidence 里
    items = groups["rule"]["latest"]["evidence"]["items"]
    assert any(i["code"] == "source_backlink" for i in items)
    assert body["verify_status"] != "unverified"


# --------------------------------------------------------------------------- #
# 单点关系视角
# --------------------------------------------------------------------------- #
def test_point_relations_view(graph_doc) -> None:
    doc_id = graph_doc["document_id"]
    client.post(f"/api/documents/{doc_id}/relations")
    kp_id = graph_doc["kp_ids"][0]

    body = client.get(f"/api/knowledge-points/{kp_id}/relations").json()
    assert body["kp_id"] == kp_id
    for item in body["items"]:
        assert item["direction"] in {"in", "out"}
        assert item["peer_id"] != kp_id
        assert item["peer_title"]
        assert item["relation_type"] in {
            "prerequisite",
            "related",
            "contains",
            "belongs_to",
        }


def test_point_relations_of_missing_point_returns_404() -> None:
    assert client.get("/api/knowledge-points/99999999/relations").status_code == 404
