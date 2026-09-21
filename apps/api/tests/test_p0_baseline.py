"""P0 基础接口测试。

运行（在 apps/api 目录下）：
    pytest -v

说明：这些测试不需要 MySQL / Chroma 可用 —— 健康检查在设计上就会
把不可用的依赖报为 degraded 而非报错，因此可以无依赖运行。

> **例外**：三个 `/api/chat` 契约用例（`test_chat_*`）从 2026-09-21 起
> 需要登录才能调（该接口此前是未鉴权的 LLM 代理），因此它们需要 MySQL。
> 同文件里其它用例仍可在无 MySQL 时运行。
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


@pytest.fixture()
def logged_in():
    """注册一个临时账号并保持登录，用完清掉。

    ⚠️ **`/api/chat` 与 `/api/chat/stream` 从 2026-09-21 起要求登录**
    （审计发现它们原本是完全无鉴权的 LLM 代理）。
    因此这三个原本匿名的契约用例改为**先登录再测** ——
    仍然是同一套断言（路由通不通、帧序列对不对），只是多了一个前置身份。

    副作用：这三个用例从此需要 MySQL（注册要落库）。
    本文件其余用例仍然可在无 MySQL 时运行。
    """
    from uuid import uuid4

    from app.db.session import SessionLocal
    from app.models.user import User

    username = f"p0{uuid4().hex[:10]}"
    password = "p0-baseline-pass"
    res = client.post("/api/auth/register", json={"username": username, "password": password})
    assert res.status_code == 200, res.text

    yield

    client.cookies.clear()
    with SessionLocal() as db:
        user = db.query(User).filter(User.username == username).first()
        if user is not None:
            db.delete(user)
            db.commit()


def test_chat_requires_login() -> None:
    """**未登录必须 401。** 这两个接口曾在无鉴权状态下可被任何人调用。"""
    client.cookies.clear()
    payload = {"messages": [{"role": "user", "content": "你好"}]}

    assert client.post("/api/chat", json=payload).status_code == 401
    # 流式入口同样要拦 —— 只保护普通 POST 是不够的
    with client.stream("POST", "/api/chat/stream", json=payload) as res:
        assert res.status_code == 401
        assert not res.headers.get("content-type", "").startswith("text/event-stream")


def test_chat_non_stream(logged_in) -> None:
    res = client.post(
        "/api/chat",
        json={"messages": [{"role": "user", "content": "你好"}]},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["content"].strip()
    assert body["mode"] in {"live", "mock"}


def test_chat_rejects_empty_messages(logged_in) -> None:
    res = client.post("/api/chat", json={"messages": []})
    assert res.status_code == 422


def test_chat_stream_emits_sse_frames(logged_in) -> None:
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
