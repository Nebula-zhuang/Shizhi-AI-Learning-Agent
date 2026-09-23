"""FreeStudy × 长期记忆（Phase 5A）：**只读注入**。

## 这一组守什么

阶段 5 的"共享学习状态"在 5A 只做**只读**那一半：把这位学习者的**长期讲法偏好**
（与「辅导」页共用同一份 `learner_profiles`）注入自由学习的答案提示词。

四条不变量：

| 不变量 | 为什么必须守 |
|---|---|
| 偏好**真的**进到提示词里 | 否则"共享"只是嘴上说说 |
| **没有知识点**时也能用 | 自由学习讨论的话题不一定对应知识点 —— 这是常态，不是边界 |
| 记忆读失败**照常回答** | 长期记忆是**锦上添花**；它坏了不该让一轮问答开始不了 |
| **不创建新的学习状态** | 只读就是只读。写回掌握度需要"话题→知识点"映射，不在本轮范围 |

## 为什么在**路由层**读记忆

`stream_turn` 里没有 db。把 session 穿进 runtime loop，会让一次多步编排
始终占着一个连接 —— 这与 3B 给 `search_saved_knowledge` 定下的取舍一致
（工具内开短生命周期 session）。所以：**路由层读一次，传下去**。
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.agent import free_study
from app.db.session import SessionLocal
from app.main import app
from app.models.learner_kp_state import LearnerKpState
from app.services import memory_service

client = TestClient(app)
PASSWORD = "Shizhi#2026"


# --------------------------------------------------------------------------- #
# 助手
# --------------------------------------------------------------------------- #
def _register(username: str) -> str:
    res = client.post("/api/auth/register", json={"username": username, "password": PASSWORD})
    assert res.status_code == 200, res.text
    return res.json()["user"]["learner_id"]


def _set_style(learner_id: str, style: str) -> None:
    with SessionLocal() as db:
        memory_service.set_manual_style(db, style, learner_id=learner_id)


def _ask_capturing(monkeypatch: pytest.MonkeyPatch) -> dict:
    """发一轮 ask，把 `stream_turn` **真正收到**的参数抓出来。"""
    captured: dict = {}

    async def fake_stream_turn(**kwargs):
        captured.update(kwargs)
        yield free_study.TurnEvent("delta", {"text": "好的"})
        yield free_study.TurnEvent(
            "done",
            {"capabilities": ["general"], "sources": [], "citations": [],
             "status_trace": [], "degraded_reason": None, "steps": [],
             "provider": "", "fell_back": False, "fallback_reason": ""},
        )

    monkeypatch.setattr(free_study, "stream_turn", fake_stream_turn)

    conv = client.post("/api/study/conversations", json={"title": ""})
    assert conv.status_code == 200, conv.text
    conv_id = conv.json()["id"]

    res = client.post(
        f"/api/study/conversations/{conv_id}/ask",
        json={"question": "什么是 JVM？"},
    )
    assert res.status_code == 200, res.text
    assert captured, "stream_turn 没有被调用"
    return captured


def _kp_state_count() -> int:
    with SessionLocal() as db:
        return int(db.scalar(select(func.count()).select_from(LearnerKpState)) or 0)


# --------------------------------------------------------------------------- #
# 一、偏好真的进到提示词里
# --------------------------------------------------------------------------- #
def test_preferred_style_reaches_the_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """存过的讲法偏好必须**原样**送到 Agent 手上。"""
    learner = _register(f"mem{uuid4().hex[:10]}")
    _set_style(learner, "contrast")

    captured = _ask_capturing(monkeypatch)

    assert "learner_memory" in captured, "stream_turn 必须收到 learner_memory"
    assert captured["learner_memory"] == memory_service.style_instruction("contrast")
    # 与默认讲法不同 —— 证明读的是**这个人**的偏好，不是兜底值
    assert captured["learner_memory"] != memory_service.style_instruction("balanced")


def test_default_style_when_nothing_was_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """没设过偏好的人拿到的是默认讲法指令，而不是空串。"""
    _register(f"mem{uuid4().hex[:10]}")
    captured = _ask_capturing(monkeypatch)
    assert captured["learner_memory"] == memory_service.style_instruction("balanced")


def test_placeholder_is_filled_in_the_real_prompt() -> None:
    """`{{learner_memory}}` 必须落在 **USER 段**（`_fill` 只作用于那一段）。"""
    _system, template = free_study._load_prompt("free_study_answer.md")
    assert "{{learner_memory}}" in template, "占位符不在 USER 段，_fill 永远填不到它"

    filled = free_study._fill(
        template,
        {"material": "（无）", "question": "什么是 JVM", "learner_memory": "【讲法标记】"},
    )
    assert "{{learner_memory}}" not in filled, "占位符没被替换"
    assert "【讲法标记】" in filled


def test_prompt_warns_not_to_read_the_memory_aloud() -> None:
    """提示词要明确：这段是**记忆**，不是本轮对话内容，不要复述它。"""
    _system, template = free_study._load_prompt("free_study_answer.md")
    assert "长期记忆" in template
    assert "不要复述" in template


# --------------------------------------------------------------------------- #
# 二、没有知识点时也能用（自由学习的**常态**）
# --------------------------------------------------------------------------- #
def test_load_memory_works_without_a_knowledge_point() -> None:
    """`knowledge_point_id=None` 是自由学习走的那条路 —— 必须不抛、且有偏好。"""
    learner = _register(f"mem{uuid4().hex[:10]}")
    _set_style(learner, "stepwise")

    with SessionLocal() as db:
        context = memory_service.load_memory(db, learner_id=learner, knowledge_point_id=None)

    assert context.preferred_style == "stepwise", "没有知识点也要拿得到讲法偏好"
    # 没有知识点就没有知识点历史 —— 这是对的，不是缺陷
    assert context.knowledge_state is None
    assert context.is_due is False


def test_route_does_not_pass_a_knowledge_point(monkeypatch: pytest.MonkeyPatch) -> None:
    """路由必须以**无 KP** 的方式读记忆 —— 自由学习没有知识点。"""
    calls: list[dict] = []
    real = memory_service.load_memory

    def spy(db, **kwargs):  # noqa: ANN001, ANN003, ANN201
        calls.append(kwargs)
        return real(db, **kwargs)

    monkeypatch.setattr(memory_service, "load_memory", spy)
    learner = _register(f"mem{uuid4().hex[:10]}")
    _set_style(learner, "structured")
    _ask_capturing(monkeypatch)

    assert calls, "路由没有调用 load_memory"
    assert calls[-1].get("knowledge_point_id", None) is None


# --------------------------------------------------------------------------- #
# 三、记忆读失败**照常回答**
# --------------------------------------------------------------------------- #
def test_answer_continues_when_memory_read_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """长期记忆坏了，一轮问答**必须照常进行**。

    这是本阶段最重要的一条：注入记忆是**锦上添花**，
    绝不能变成"回答不出来"的理由。
    """
    _register(f"mem{uuid4().hex[:10]}")

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        raise RuntimeError("模拟记忆库不可用")

    monkeypatch.setattr(memory_service, "load_memory", boom)

    captured = _ask_capturing(monkeypatch)

    # 记忆为空 → 交给 `_make_generator` 填成中性说明（不是留空）
    assert captured["learner_memory"] == ""
    assert captured["question"] == "什么是 JVM？", "记忆失败不该影响问题本身"


@pytest.mark.asyncio
async def test_empty_memory_renders_a_neutral_sentence(monkeypatch: pytest.MonkeyPatch) -> None:
    """空字符串不能直接进提示词 —— 否则「讲法偏好」下面是个空标题。

    这里**真跑** `_make_generator` 并把实际发给模型的 user message 抓出来，
    而不是重复一遍 `or` 表达式（那样等于测试抄了一遍实现）。
    """
    sent = await _capture_generated_prompt(monkeypatch, learner_memory="")

    assert "（暂无特别偏好，用常规讲法。）" in sent, "空记忆必须落到中性说明"
    assert "{{learner_memory}}" not in sent, "占位符没被替换"
    assert "## 这位学习者的讲法偏好" in sent, "偏好那一段应当还在"


@pytest.mark.asyncio
async def test_real_memory_text_lands_in_the_generated_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**端到端**：把一句话当记忆传进去，它必须出现在真正发给模型的提示词里。"""
    marker = "【讲法标记：多用类比】"
    sent = await _capture_generated_prompt(monkeypatch, learner_memory=marker)

    assert marker in sent
    assert "什么是 JVM" in sent, "问题也应当照常在提示词里"


async def _capture_generated_prompt(
    monkeypatch: pytest.MonkeyPatch, *, learner_memory: str
) -> str:
    """跑一次 `_make_generator`，返回它真正发给模型的最后一条 user message。"""
    calls: list[list] = []

    async def fake_stream_chat(messages, **_kwargs):  # noqa: ANN001, ANN003, ANN202
        calls.append(list(messages))
        yield "好的"

    # `free_study` 里 `llm_gateway` 是模块级引用；monkeypatch 会在用例结束后还原
    monkeypatch.setattr(free_study.llm_gateway, "stream_chat", fake_stream_chat)

    observation = free_study.LoopObservation(question="什么是 JVM？")
    generator = free_study._make_generator(history=[], learner_memory=learner_memory)
    async for _ in generator(observation):
        pass

    assert calls, "没有向模型发出任何消息"
    return str(calls[0][-1]["content"])


# --------------------------------------------------------------------------- #
# 四、**不得创建新的学习状态**
# --------------------------------------------------------------------------- #
def test_asking_does_not_write_any_mastery_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """问一轮自由学习，`learner_kp_states` **一行都不该动**。

    写回掌握度需要"话题→知识点"映射（自由学习没有知识点），
    5A 是**只读**注入 —— 一旦有人在这里写状态，这条会红。
    """
    _register(f"mem{uuid4().hex[:10]}")
    before = _kp_state_count()

    _ask_capturing(monkeypatch)

    assert _kp_state_count() == before, "自由学习不该写任何掌握度状态"


def test_free_study_module_does_not_touch_state_models() -> None:
    """静态守卫：FreeStudy 的模块**不许**真的导入学习状态模型或记忆服务。

    一旦 `free_study.py` 开始 `import LearnerKpState` / `memory_service`，
    说明有人把"只读注入"扩成了"自己管状态" —— 那是另一个阶段的事，
    而且会绕过 `memory_service` 这个唯一入口。

    ⚠️ 用 **AST 查 import**，不是 `in source` 字符串匹配 ——
    后者会被注释里提到的名字误伤（本模块的注释里就写着 `memory_service`）。
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(free_study.__file__).read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[-1] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.update(alias.name for alias in node.names)
            if node.module:
                imported.add(node.module.split(".")[-1])

    for forbidden in ("LearnerKpState", "LearnerProfile", "memory_service"):
        assert forbidden not in imported, f"free_study.py 不该 import {forbidden}"
