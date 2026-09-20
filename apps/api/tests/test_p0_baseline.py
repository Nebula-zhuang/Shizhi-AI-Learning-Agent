"""P0 基础接口测试。

运行（在 apps/api 目录下）：
    pytest -v

说明：这些测试不需要 MySQL / Chroma 可用 —— 健康检查在设计上就会
把不可用的依赖报为 degraded 而非报错，因此可以无依赖运行。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.routes.health import APP_VERSION
from app.core.config import settings
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def force_mock_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """强制 mock 模式，让 P0 基线不依赖外部模型。

    这个文件里的对话测试原本会打真实 LLM（.env 配了 Key 时），
    于是模型限流或抖动会让 `status_code == 200` 变成 502 —— 一次与代码无关的随机红灯。
    实测踩到过：连着跑了几轮真实 LLM 的端到端演示之后，这两个用例就红了。

    这些用例的**意图是验证接口契约**（路由通不通、响应字段对不对），
    从 `assert body["mode"] in {"live", "mock"}` 就能看出作者本来就预期两种模式都可能。
    强制 mock 后契约照样被验证，而结果变得确定。真实模型路径由
    `scripts/smoke_test.py`（P0）与 `scripts/p3_smoke.py --live` / `p4_smoke.py --live` 覆盖。
    """
    monkeypatch.setattr(settings, "llm_api_key", "")


def test_root_returns_service_info() -> None:
    res = client.get("/")
    assert res.status_code == 200
    body = res.json()
    assert "app" in body
    assert body["version"] == APP_VERSION
    assert body["llm_mode"] in {"live", "mock"}


def test_liveness_probe() -> None:
    res = client.get("/api/health/live")
    assert res.status_code == 200
    assert res.json()["status"] == "alive"


def test_health_reports_all_components() -> None:
    res = client.get("/api/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] in {"ok", "degraded"}
    names = {c["name"] for c in body["components"]}
    # 断言方向是「P0 的三个组件必须都在」，而不是「集合恰好等于这三个」。
    # 原写法用的是 ==，健康检查每加一个依赖（P3 加了 embedding）都会让它失败 ——
    # 而健康检查本来就是要随依赖扩展的，那种断言把"允许新增"和"组件缺失"混为一谈。
    assert {"llm", "mysql", "chroma"} <= names
    # 健康检查绝不能把密钥回显出来
    assert "api_key" not in res.text.lower() or "***" in res.text


def test_chat_non_stream() -> None:
    res = client.post(
        "/api/chat",
        json={"messages": [{"role": "user", "content": "你好"}]},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["content"].strip()
    assert body["mode"] in {"live", "mock"}


def test_chat_rejects_empty_messages() -> None:
    res = client.post("/api/chat", json={"messages": []})
    assert res.status_code == 422


def test_chat_stream_emits_sse_frames() -> None:
    """验证 SSE 帧序列为 meta → delta+ → done。"""
    with client.stream(
        "POST",
        "/api/chat/stream",
        json={"messages": [{"role": "user", "content": "测试流式"}]},
    ) as res:
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/event-stream")
        body = "".join(res.iter_text())

    assert "event: meta" in body
    assert "event: delta" in body
    assert "event: done" in body
    assert body.index("event: meta") < body.index("event: done")
