"""文档与知识点 API 契约测试。

这里验证的是**接口层的行为约定**，不是流水线本身（流水线由 scripts/p1_smoke.py 端到端覆盖）：
  1. 非法输入被拒绝，且错误信息可读
  2. 内容去重命中时不重复建记录
  3. 状态/列表/详情/删除的状态码与结构
  4. 知识点回链能取到来源原文
  5. 删除后相关资源不可再访问

后台任务被打桩：`ingest_runner.submit` 返回 True 但不真正调度。
理由 —— 本文件的目的是锁住 HTTP 契约，不应依赖流水线耗时与 LLM 行为。
真的跑流水线由 p1_smoke.py 负责。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.db.session import SessionLocal
from app.main import app
from app.services import document_service, ingest_runner

client = TestClient(app)


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture()
def stub_pipeline(monkeypatch: pytest.MonkeyPatch):
    """把「流水线本体」换成无副作用的空实现，但**保留真实的 submit**。

    这里刻意不打桩 `ingest_runner.submit`。踩过一次坑：reprocess 路由被写成同步函数
    (`def`)，FastAPI 把它丢进线程池执行，那里没有运行中的事件循环，
    `asyncio.create_task` 抛错、submit 返回 False，接口静默降级为 503。
    当时因为把 submit 打桩成「永远返回 True」，测试全绿却掩盖了故障。
    保留真实的 submit，这类问题就会立刻暴露成失败用例。
    """
    called: list[int] = []

    async def fake_run_pipeline(document_id: int) -> None:
        called.append(document_id)

    monkeypatch.setattr(ingest_runner, "run_pipeline", fake_run_pipeline)
    return called


@pytest.fixture()
def cleanup_documents():
    """测试结束后清掉本次创建的文档，避免污染开发库。"""
    created: list[int] = []
    yield created
    with SessionLocal() as db:
        for document_id in created:
            document = document_service.get_document(db, document_id)
            if document is not None:
                document_service.delete_document(db, document)


def _upload(content: bytes, filename: str):
    return client.post(
        "/api/documents",
        files={"file": (filename, content, "application/octet-stream")},
    )


def _unique_text() -> str:
    """每次运行内容都不同，避免与库里既有数据发生 hash 去重。"""
    return (
        f"# 测试文档 {uuid.uuid4().hex[:8]}\n\n"
        "进程是程序的一次执行过程，是系统进行资源分配的基本单位。\n\n"
        "线程是 CPU 调度的基本单位，同一进程内的线程共享地址空间。\n"
    )


# --------------------------------------------------------------------------- #
# 上传校验
# --------------------------------------------------------------------------- #
def test_unsupported_extension_is_rejected() -> None:
    res = _upload(b"whatever", "恶意可执行文件.exe")
    assert res.status_code == 400
    # 错误信息要能指导用户，而不是一句 internal error
    assert "支持" in res.json()["detail"]


def test_empty_file_is_rejected() -> None:
    res = _upload(b"", "empty.txt")
    assert res.status_code == 400


def test_valid_upload_returns_202_and_schedules(stub_pipeline, cleanup_documents) -> None:
    res = _upload(_unique_text().encode("utf-8"), "p1_api_test.txt")
    assert res.status_code == 202

    body = res.json()
    assert body["dedup"] is False
    assert body["document"]["file_name"] == "p1_api_test.txt"
    # 注意：类型按解析器归类，.txt/.md 统一归为 text（见 ingestion/base.py 的 SUPPORTED_EXTENSIONS）
    assert body["document"]["file_type"] == "text"

    document_id = body["document"]["id"]
    cleanup_documents.append(document_id)


def test_same_content_hits_dedup(stub_pipeline, cleanup_documents) -> None:
    """同一份内容反复上传是常见行为，必须复用而不是重新解析。"""
    payload = _unique_text().encode("utf-8")

    first = _upload(payload, "重复资料.txt")
    assert first.status_code == 202
    assert first.json()["dedup"] is False
    cleanup_documents.append(first.json()["document"]["id"])

    second = _upload(payload, "重复资料-改名版.txt")
    assert second.status_code == 202
    body = second.json()
    assert body["dedup"] is True
    assert body["document"]["id"] == first.json()["document"]["id"]


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
def test_list_contains_uploaded_document(stub_pipeline, cleanup_documents) -> None:
    uploaded = _upload(_unique_text().encode("utf-8"), "列表测试.txt").json()["document"]
    cleanup_documents.append(uploaded["id"])

    res = client.get("/api/documents", params={"limit": 50})
    assert res.status_code == 200
    body = res.json()
    assert body["total"] >= 1
    assert any(item["id"] == uploaded["id"] for item in body["items"])


def test_status_endpoint_shape(stub_pipeline, cleanup_documents) -> None:
    document_id = _upload(_unique_text().encode("utf-8"), "状态测试.txt").json()["document"]["id"]
    cleanup_documents.append(document_id)

    res = client.get(f"/api/documents/{document_id}/status")
    assert res.status_code == 200
    body = res.json()
    for key in ("parse_status", "progress", "stage_detail", "chunk_count", "kp_count"):
        assert key in body


def test_missing_document_returns_404() -> None:
    assert client.get("/api/documents/99999999").status_code == 404
    assert client.get("/api/documents/99999999/status").status_code == 404
    assert client.get("/api/knowledge-points/99999999").status_code == 404


def test_structure_before_parse_returns_404(stub_pipeline, cleanup_documents) -> None:
    """还没解析出结构就请求结构，应给出清晰提示而不是 500。"""
    document_id = _upload(_unique_text().encode("utf-8"), "结构测试.txt").json()["document"]["id"]
    cleanup_documents.append(document_id)

    res = client.get(f"/api/documents/{document_id}/structure")
    assert res.status_code == 404
    assert "结构" in res.json()["detail"]


def test_chunks_of_unparsed_document_is_empty(cleanup_documents) -> None:
    # 注意：本用例不做上传，直接断言一个不存在的文档会 404
    assert client.get("/api/documents/99999999/chunks").status_code == 404


# --------------------------------------------------------------------------- #
# 图片路径安全
# --------------------------------------------------------------------------- #
def test_image_endpoint_rejects_path_traversal(stub_pipeline, cleanup_documents) -> None:
    """图片接口必须杜绝目录穿越。"""
    document_id = _upload(_unique_text().encode("utf-8"), "穿越测试.txt").json()["document"]["id"]
    cleanup_documents.append(document_id)

    res = client.get(f"/api/documents/{document_id}/images/..%2F..%2Fsource.txt")
    # 要么被路径校验拒绝(400)，要么找不到(404)；绝不能 200 把源码吐出来
    assert res.status_code in (400, 404)


# --------------------------------------------------------------------------- #
# 删除
# --------------------------------------------------------------------------- #
def test_delete_then_gone(stub_pipeline, cleanup_documents) -> None:
    document_id = _upload(_unique_text().encode("utf-8"), "删除测试.txt").json()["document"]["id"]

    res = client.delete(f"/api/documents/{document_id}")
    assert res.status_code == 200
    assert res.json()["ok"] is True

    assert client.get(f"/api/documents/{document_id}").status_code == 404


def test_reprocess_actually_dispatches_task(stub_pipeline, cleanup_documents) -> None:
    """回归用例：重跑必须真的投递成功。

    曾经的故障：reprocess 路由写成同步 `def`，被 FastAPI 放进线程池执行，
    那里没有事件循环 → `asyncio.create_task` 失败 → submit 返回 False →
    文档被标记为 failed，错误信息是含糊的「服务当前无法接收处理任务」。

    因此这里断言两件事：
      1. 不能返回 503（投递失败的信号）
      2. 文档不能立刻变成 failed
    依赖真实的 submit（夹具只替换流水线本体），否则这条用例失去意义。
    """
    payload = _unique_text().encode("utf-8")
    document_id = _upload(payload, "重跑测试.txt").json()["document"]["id"]
    cleanup_documents.append(document_id)

    res = client.post(f"/api/documents/{document_id}/reprocess")
    assert res.status_code == 202, f"重跑未成功投递：{res.status_code} {res.text}"

    detail = client.get(f"/api/documents/{document_id}").json()
    assert detail["parse_status"] != "failed", f"重跑后立刻失败：{detail.get('parse_error')}"


def test_reprocess_is_guarded_while_processing(stub_pipeline, cleanup_documents) -> None:
    """正在处理中的文档不允许重跑，避免两个任务同时写同一份数据。"""
    document_id = _upload(_unique_text().encode("utf-8"), "并发重跑.txt").json()["document"]["id"]
    cleanup_documents.append(document_id)

    # 直接改成处理中，模拟任务仍在跑
    with SessionLocal() as db:
        document = document_service.get_document(db, document_id)
        document.parse_status = "parsing"
        db.commit()

    res = client.post(f"/api/documents/{document_id}/reprocess")
    assert res.status_code == 409
