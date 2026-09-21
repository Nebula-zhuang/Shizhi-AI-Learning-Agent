"""图片文档**不该被当成"可检索资料"**递给 Agent。

## 背景（2B2）

上传的图片在库里就是一条 `Document`，于是会被 `scoped` 顺带带进
`document_ids` 喂给 Agent。实测后果是提示词里同时出现：

    用户这一轮附了 1 张图片 —— ...
    用户指定了 1 份资料（document_ids=[10142]）。

而**没有任何字段说明那份"资料"就是这张图** —— Agent 于是以为除了图之外
还有别的材料，转而去调 `retrieve_knowledge`。可图片文档的 chunk 里
**只有文件名**（实测 `code.png` 这种），检索它拿不回任何图片内容 ——
那次调用是纯浪费。

## 这一组守什么

**图片只走 `images=` 那条路（绑进 `image_analysis`），不进 `document_ids`。**
真实文档（PDF / DOCX / PPTX / TXT / MD）照旧保留。

## 怎么测的

不强跑 Agent（那要打 LLM）。这里把 `free_study.stream_turn` 换成一个
只记录参数、然后立刻收尾的假实现 —— 于是能**精确断言路由到底把什么交给了 Agent**，
而这正是这一层的契约。
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.agent import free_study
from app.db.session import SessionLocal
from app.main import app
from app.models.document import Document, ParseStatus

client = TestClient(app)
PASSWORD = "Shizhi#2026"


# --------------------------------------------------------------------------- #
# 助手
# --------------------------------------------------------------------------- #
def _register(username: str) -> str:
    """注册并保持登录，返回该账号的 learner_id。"""
    res = client.post("/api/auth/register", json={"username": username, "password": PASSWORD})
    assert res.status_code == 200, res.text
    return res.json()["user"]["learner_id"]


def _make_doc(learner_id: str, file_name: str) -> int:
    """给指定 learner 造一条资料。文件名决定它是"图片"还是"文档"。"""
    is_image = file_name.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"))
    with SessionLocal() as db:
        doc = Document(
            owner_learner_id=learner_id,
            file_name=file_name,
            file_type="image" if is_image else "text",
            file_size=128,
            file_hash=uuid4().hex + uuid4().hex,
            storage_path=f"test/{uuid4().hex[:8]}_{file_name}",
            page_count=1,
            char_count=128,
            chunk_count=1,
            parse_status=ParseStatus.READY,
        )
        db.add(doc)
        db.commit()
        db.refresh(doc)
        return int(doc.id)


def _ask_capturing(monkeypatch: pytest.MonkeyPatch, document_ids: list[int]) -> dict:
    """发一轮 ask，把 `stream_turn` **真正收到**的参数抓出来。"""
    captured: dict = {}

    async def fake_stream_turn(**kwargs):
        captured.update(kwargs)
        yield free_study.TurnEvent("delta", {"text": "好的"})
        yield free_study.TurnEvent("done", {"answer": "好的", "steps": []})

    monkeypatch.setattr(free_study, "stream_turn", fake_stream_turn)

    conv = client.post("/api/study/conversations", json={"title": ""})
    assert conv.status_code == 200, conv.text
    conv_id = conv.json()["id"]

    res = client.post(
        f"/api/study/conversations/{conv_id}/ask",
        json={"question": "解释一下这张图", "document_ids": document_ids},
    )
    assert res.status_code == 200, res.text
    assert captured, "stream_turn 没有被调用"
    return captured


# --------------------------------------------------------------------------- #
# a. 只带图片 → document_ids 里不该有它
# --------------------------------------------------------------------------- #
def test_image_only_is_excluded_from_material_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    learner = _register(f"img{uuid4().hex[:10]}")
    image_id = _make_doc(learner, "only_image.png")

    captured = _ask_capturing(monkeypatch, [image_id])

    assert captured["document_ids"] in (None, []), (
        f"只带图片时不该把图片当资料递给 Agent，实际传了 {captured['document_ids']}"
    )
    # 图片本身仍要进多模态通道 —— 摘掉的只是它的"资料"身份
    assert captured["images"], "图片必须仍然交给 image_analysis"
    assert captured["has_attachments"] is True


# --------------------------------------------------------------------------- #
# b. 图片 + 真实文档 → 只留真实文档
# --------------------------------------------------------------------------- #
def test_real_document_survives_but_image_does_not(monkeypatch: pytest.MonkeyPatch) -> None:
    learner = _register(f"mix{uuid4().hex[:10]}")
    image_id = _make_doc(learner, "mixed_case.png")
    doc_id = _make_doc(learner, "mixed_case.pdf")

    captured = _ask_capturing(monkeypatch, [image_id, doc_id])

    assert captured["document_ids"] == [doc_id], (
        f"应当只保留真实文档 {doc_id}，实际 {captured['document_ids']}"
    )
    assert image_id not in (captured["document_ids"] or [])


# --------------------------------------------------------------------------- #
# c. 显式 ids 里混入图片 → 图片仍被排除
# --------------------------------------------------------------------------- #
def test_image_is_excluded_even_when_explicitly_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    learner = _register(f"exp{uuid4().hex[:10]}")
    image_id = _make_doc(learner, "explicit.png")
    doc_id = _make_doc(learner, "explicit.md")

    # 用户**显式**把图片 id 也列进了 document_ids —— 仍然要摘掉
    captured = _ask_capturing(monkeypatch, [image_id, doc_id])

    assert captured["document_ids"] == [doc_id]
    assert image_id not in (captured["document_ids"] or [])


# --------------------------------------------------------------------------- #
# 多个真实文档照常保留（防"顺手改宽了"）
# --------------------------------------------------------------------------- #
def test_multiple_real_documents_are_all_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    learner = _register(f"multi{uuid4().hex[:10]}")
    image_id = _make_doc(learner, "a.png")
    first = _make_doc(learner, "a.txt")
    second = _make_doc(learner, "b.pptx")

    captured = _ask_capturing(monkeypatch, [image_id, first, second])

    assert sorted(captured["document_ids"] or []) == sorted([first, second])


# --------------------------------------------------------------------------- #
# 未显式传 ids（默认全量路径）也照样排除图片
# --------------------------------------------------------------------------- #
def test_default_path_also_excludes_images(monkeypatch: pytest.MonkeyPatch) -> None:
    """不传 document_ids 时走的是 `scoped = 全部资料` 那条路，同样要摘掉图片。"""
    learner = _register(f"dflt{uuid4().hex[:10]}")
    image_id = _make_doc(learner, "default_path.png")
    doc_id = _make_doc(learner, "default_path.docx")

    captured: dict = {}

    async def fake_stream_turn(**kwargs):
        captured.update(kwargs)
        yield free_study.TurnEvent("done", {"answer": "好", "steps": []})

    monkeypatch.setattr(free_study, "stream_turn", fake_stream_turn)

    conv = client.post("/api/study/conversations", json={"title": ""})
    conv_id = conv.json()["id"]
    res = client.post(
        f"/api/study/conversations/{conv_id}/ask",
        json={"question": "关于我的资料"},  # 不带 document_ids → 默认全量
    )
    assert res.status_code == 200, res.text

    assert image_id not in (captured.get("document_ids") or []), (
        "默认全量路径下图片仍被当成资料递给了 Agent"
    )
