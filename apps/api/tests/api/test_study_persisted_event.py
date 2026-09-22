"""`persisted` SSE 事件的**时序契约**。

## 为什么需要这条测试

Phase 3C 之前，前端**无法保存刚生成的那条回答** —— 因为 `done` 事件里
没有 `message_id`，而消息是路由在**流结束之后**才落库的（推 `done` 时 id 还不存在）。

修法是落库成功后补推一帧 `persisted`。这条修法有一个**很容易被后人破坏**的
不变量：

> **`persisted` 必须在 `append_message` 真正成功之后才推。**

它脆在哪：`persisted` 只是 `event_source` 里的一行 `yield`。
后来人很容易为了"代码整齐"把它挪到 `try` 块里（甚至在 `append_message` **之前**），
于是落库失败时前端仍会收到一个 id —— 而那个 id **在库里不存在**，
用户点保存只会得到一个让人摸不着头脑的 404。

**没有任何其他测试会拦住这个**，所以单独守一条。

## 怎么测的（不打 LLM）

把 `free_study.stream_turn` 换成只推 `delta + done` 的假实现，
再走真实的 SSE 路由 —— 于是能精确观察**帧的产生顺序**，
而落库那一段仍然是**真实代码路径**（不是 mock）。

落库失败那条用 `monkeypatch` 让 `study_service.append_message` 抛异常，
断言此时**不得**出现 `persisted`。
"""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from app.agent import free_study
from app.db.session import SessionLocal
from app.main import app
from app.models.conversation import ConversationMessage, MessageAuthor
from app.services import study_service

client = TestClient(app)
PASSWORD = "Shizhi#2026"

#: 假回答的正文 —— 用来断言落库的确实是流式推出去的那份
FAKE_ANSWER = "这是一段测试回答。"


# --------------------------------------------------------------------------- #
# 助手
# --------------------------------------------------------------------------- #
def _register(username: str) -> str:
    res = client.post("/api/auth/register", json={"username": username, "password": PASSWORD})
    assert res.status_code == 200, res.text
    return res.json()["user"]["learner_id"]


def _new_conversation() -> int:
    res = client.post("/api/study/conversations", json={"title": ""})
    assert res.status_code == 200, res.text
    return int(res.json()["id"])


def _stub_stream(monkeypatch) -> None:
    """把 Agent 循环换成"只推 delta + done"的假实现 —— 不打 LLM。"""

    async def fake_stream_turn(**kwargs):
        yield free_study.TurnEvent("delta", {"text": FAKE_ANSWER})
        yield free_study.TurnEvent(
            "done",
            {
                "capabilities": ["general"],
                "sources": [],
                "citations": [],
                "status_trace": [],
                "degraded_reason": None,
                "steps": [],
                "provider": "",
                "fell_back": False,
                "fallback_reason": "",
            },
        )

    monkeypatch.setattr(free_study, "stream_turn", fake_stream_turn)


def _ask(conversation_id: int) -> list[tuple[str, dict]]:
    """发一轮，按到达顺序收回全部 SSE 帧 `[(event, data), ...]`。"""
    frames: list[tuple[str, dict]] = []
    with client.stream(
        "POST",
        f"/api/study/conversations/{conversation_id}/ask",
        json={"question": "测试一下"},
        headers={"Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200
        event = ""
        for raw in response.iter_lines():
            line = raw if isinstance(raw, str) else raw.decode("utf-8", "ignore")
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                frames.append((event, _parse(line[5:].strip())))
    return frames


def _parse(raw: str) -> dict:
    import json

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:  # pragma: no cover - 不该发生
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _names(frames: list[tuple[str, dict]]) -> list[str]:
    return [name for name, _ in frames]


def _first(frames: list[tuple[str, dict]], name: str) -> dict | None:
    for event, data in frames:
        if event == name:
            return data
    return None


# --------------------------------------------------------------------------- #
# 一、正常路径：persisted 在 done 之后，且 id 与库里的记录一致
# --------------------------------------------------------------------------- #
def test_persisted_arrives_after_done_with_the_real_db_id(monkeypatch) -> None:
    """`persisted` 必须晚于 `done` 到达，且它带的 id 就是**库里那条**。"""
    _register(f"pst{uuid4().hex[:10]}")
    conversation_id = _new_conversation()
    _stub_stream(monkeypatch)

    frames = _ask(conversation_id)
    names = _names(frames)

    assert "done" in names, "正常一轮应当有 done"
    assert "persisted" in names, "落库成功后必须推 persisted —— 否则前端保存不了"

    # 顺序：persisted 不能在 done 之前（那说明它在落库前就被推了）
    assert names.index("done") < names.index("persisted"), (
        f"帧序不对：{names} —— persisted 必须是 done 之后的第一件事"
    )

    payload = _first(frames, "persisted")
    assert payload is not None
    message_id = payload.get("message_id")
    assert isinstance(message_id, int) and message_id > 0, f"id 不合法：{message_id!r}"

    # id 必须真的指向库里的那条记录
    with SessionLocal() as db:
        row = db.get(ConversationMessage, message_id)
        assert row is not None, f"persisted 给的 id={message_id} 在库里不存在"
        assert row.role == MessageAuthor.ASSISTANT
        assert int(row.conversation_id) == conversation_id, "归属的对话不对"
        assert row.content.strip() == FAKE_ANSWER, "落库的正文与流式推出去的不一致"


def test_persisted_is_sent_exactly_once(monkeypatch) -> None:
    """一轮只推一次 —— 重复推会让前端以为存了两条。"""
    _register(f"once{uuid4().hex[:10]}")
    conversation_id = _new_conversation()
    _stub_stream(monkeypatch)

    names = _names(_ask(conversation_id))
    assert names.count("persisted") == 1, f"persisted 出现了 {names.count('persisted')} 次"


# --------------------------------------------------------------------------- #
# 二、失败路径：**落库失败时不得产生 persisted**
# --------------------------------------------------------------------------- #
def test_no_persisted_when_append_fails(monkeypatch) -> None:
    """落库炸了就不能推 `persisted`。

    否则前端会拿着一个**库里不存在的 id** 去保存，报一个查不出原因的 404。
    宁可让它收不到 id（界面上不出现保存按钮），也不要给一个假的。

    ⚠️ 打桩必须**只拦助手那条**：`ask` 端点里 `append_message` 会被调用**两次** ——
    先在流式之前落**用户**的提问（那一处没有 try/except，拦它整个请求就 500 了），
    再在 `event_source` 里落**助手**的回答（我们要拦的是这一处）。
    """
    _register(f"fail{uuid4().hex[:10]}")
    conversation_id = _new_conversation()
    _stub_stream(monkeypatch)

    real_append = study_service.append_message

    def fail_only_for_assistant(*args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        if kwargs.get("role") == MessageAuthor.ASSISTANT:
            raise RuntimeError("模拟落库失败")
        return real_append(*args, **kwargs)

    monkeypatch.setattr(study_service, "append_message", fail_only_for_assistant)

    frames = _ask(conversation_id)
    names = _names(frames)

    assert "persisted" not in names, f"落库失败却推了 persisted：{names}"

    # 但回答本身**照常推给了用户** —— 落库失败不该让人看不到已经生成的回答
    assert "done" in names, "落库失败不该影响回答的推送"
    answer = "".join(str(d.get("text", "")) for n, d in frames if n == "delta")
    assert FAKE_ANSWER in answer, "正文应当照常流式推给用户"


def test_no_persisted_when_the_answer_is_empty(monkeypatch) -> None:
    """空回答会提前 return，根本走不到落库 —— 自然也不该有 persisted。"""
    _register(f"empty{uuid4().hex[:10]}")
    conversation_id = _new_conversation()

    async def empty_stream_turn(**kwargs):
        yield free_study.TurnEvent(
            "done", {"capabilities": [], "sources": [], "citations": [],
                     "status_trace": [], "degraded_reason": None, "steps": [],
                     "provider": "", "fell_back": False, "fallback_reason": ""}
        )

    monkeypatch.setattr(free_study, "stream_turn", empty_stream_turn)

    names = _names(_ask(conversation_id))
    assert "persisted" not in names, f"没有正文却推了 persisted：{names}"
