"""`search_saved_knowledge` 工具的契约（Phase 3B）。

## 这个工具最容易犯的两个错，都要用测试钉住

1. **和 `retrieve_knowledge` 混为一谈。**
   前者找"用户上传的资料"，后者找"用户主动保存的问答"。
   项目的引用优先级规则要求这两类来源**各自可辨** ——
   所以下面既断言文案里分得清，也断言 `display.kind` / `citations.kind` 不同。

2. **身份由模型提供。**
   `learner_id` 必须在**注册时**绑定（`partial`），
   模型给不出、也不该给 —— 否则一次构造的调用就能读别人的知识库。
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.agent.tool_specs import search_saved_knowledge
from app.agent.free_study import build_registry
from app.models.conversation import Conversation, ConversationMessage, MessageAuthor
from app.services import saved_knowledge_service as svc


@pytest.fixture(autouse=True)
def _clean_saved_collection(test_store):
    name = svc.saved_collection_name(test_store)
    try:
        test_store.client().delete_collection(name)
    except Exception:  # noqa: BLE001
        pass
    yield
    try:
        test_store.client().delete_collection(name)
    except Exception:  # noqa: BLE001
        pass


def _save_one(session, learner_id: str, *, question: str, answer: str, provider, store):
    conv = Conversation(learner_id=learner_id, title="工具测试")
    session.add(conv)
    session.commit()
    session.refresh(conv)
    session.add(
        ConversationMessage(conversation_id=conv.id, role=MessageAuthor.USER, content=question)
    )
    session.commit()
    assistant = ConversationMessage(
        conversation_id=conv.id, role=MessageAuthor.ASSISTANT, content=answer
    )
    session.add(assistant)
    session.commit()
    session.refresh(assistant)
    return svc.save_from_message(
        session,
        learner_id=learner_id,
        message_id=int(assistant.id),
        provider=provider,
        store=store,
    )


def _learner() -> str:
    return f"tool{uuid4().hex[:10]}"


def _session_factory(session):
    """把测试 session 包成"可 with 的工厂"，喂给工具。"""

    class _Ctx:
        def __enter__(self):
            return session

        def __exit__(self, *exc):
            return False

    return lambda: _Ctx()


# --------------------------------------------------------------------------- #
# 一、命中
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_tool_returns_saved_content(session, mock_embedder, test_store) -> None:
    learner = _learner()
    _save_one(
        session,
        learner,
        question="什么是 JVM？",
        answer="JVM 是 Java 虚拟机。",
        provider=mock_embedder,
        store=test_store,
    )

    result = await search_saved_knowledge(
        "JVM",
        learner_id=learner,
        session_factory=_session_factory(session),
        provider=mock_embedder,
        store=test_store,
        top_k=5,
    )

    assert result.ok is True
    assert "JVM" in result.content
    # 文案必须把它和"资料"区分开：既要点明来源，也要**明确禁止**说成资料
    assert "你之前保存过" in result.content
    assert "不要说成" in result.content, "必须显式禁止把保存内容说成'资料里写着'"
    assert result.display["kind"] == "saved_knowledge"
    assert result.display["count"] >= 1


@pytest.mark.asyncio
async def test_tool_citations_are_distinguishable(session, mock_embedder, test_store) -> None:
    """citation 的 kind 必须是 `saved_knowledge`，不能混成 `knowledge_base`。

    否则前端与提示词都无法区分"来自他保存的内容"还是"来自他上传的资料"。
    """
    learner = _learner()
    _save_one(
        session, learner, question="虚拟线程是什么", answer="JVM 层面的轻量线程。",
        provider=mock_embedder, store=test_store,
    )

    result = await search_saved_knowledge(
        "虚拟线程",
        learner_id=learner,
        session_factory=_session_factory(session),
        provider=mock_embedder,
        store=test_store,
    )

    assert result.citations
    assert all(c["kind"] == "saved_knowledge" for c in result.citations)
    assert "saved_id" in result.citations[0]


# --------------------------------------------------------------------------- #
# 二、空结果：措辞不能冤枉资料
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_tool_empty_result_does_not_blame_the_materials(
    session, mock_embedder, test_store
) -> None:
    """"他保存的知识里没有" ≠ "他的资料里没有"。

    实测踩过这类混用：把"找不到依据"和"有出入"说成一回事会冤枉资料。
    这里断言文案**主动提醒模型别混**。
    """
    learner = _learner()
    result = await search_saved_knowledge(
        "随便问问",
        learner_id=learner,
        session_factory=_session_factory(session),
        provider=mock_embedder,
        store=test_store,
    )

    assert result.ok is True, "空结果不是错误 —— 模型需要据此如实说明"
    assert "没有找到" in result.content
    assert "不等于" in result.content, "必须提醒：这不等于资料里没有"
    assert result.display["count"] == 0


@pytest.mark.asyncio
async def test_tool_does_not_see_another_learners_content(
    session, mock_embedder, test_store
) -> None:
    """工具层也要隔离 —— 拿别人的 learner_id 搜不到。"""
    owner = _learner()
    _save_one(
        session, owner, question="别人的问题", answer="别人的回答",
        provider=mock_embedder, store=test_store,
    )

    result = await search_saved_knowledge(
        "别人的问题",
        learner_id=_learner(),
        session_factory=_session_factory(session),
        provider=mock_embedder,
        store=test_store,
    )
    assert result.display["count"] == 0


# --------------------------------------------------------------------------- #
# 三、错误与边界
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_tool_rejects_an_empty_query(session, mock_embedder, test_store) -> None:
    result = await search_saved_knowledge(
        "   ",
        learner_id=_learner(),
        session_factory=_session_factory(session),
        provider=mock_embedder,
        store=test_store,
    )
    assert result.ok is False
    assert result.error == "empty_query"


@pytest.mark.asyncio
async def test_tool_reports_failure_truthfully(session, mock_embedder) -> None:
    """检索炸了要**如实告诉模型**（并标记可重试），不能假装"没找到"。"""

    class _BoomStore:
        collection_name = "boom"

        def query(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
            raise RuntimeError("模拟向量库不可用")

    result = await search_saved_knowledge(
        "任意查询",
        learner_id=_learner(),
        session_factory=_session_factory(session),
        provider=mock_embedder,
        store=_BoomStore(),
    )

    assert result.ok is False
    assert result.retryable is True
    assert "出错" in result.content
    assert "没有找到" not in result.content, "失败不能伪装成'没找到'"


# --------------------------------------------------------------------------- #
# 四、注册与身份绑定
# --------------------------------------------------------------------------- #
def test_tool_is_registered_with_bound_learner_id() -> None:
    """`learner_id` 必须在注册时绑定 —— 它不是模型的参数。"""
    registry = build_registry(learner_id="learner-abc")
    spec = registry.get("search_saved_knowledge")
    assert spec is not None, "新工具必须被注册"
    assert spec.counted is True, "它调向量库+可能embedding，应当占配额"

    bound = getattr(spec.handler, "keywords", {})
    assert bound.get("learner_id") == "learner-abc"

    # 暴露给模型的参数里**只有 query** —— 没有 learner_id
    assert set(spec.parameters["properties"]) == {"query"}
    assert spec.parameters["required"] == ["query"]


def test_registry_still_has_all_six_tools() -> None:
    """新增工具不能把旧的挤掉。"""
    names = set(build_registry().names())
    assert names == {
        "retrieve_knowledge",
        "web_search",
        "document_analysis",
        "image_analysis",
        "current_time",
        "search_saved_knowledge",
    }


def test_tool_description_tells_it_apart_from_retrieve_knowledge() -> None:
    """工具描述里必须写清"我不是资料检索" —— 模型只能靠这段话判断用哪个。"""
    spec = build_registry().get("search_saved_knowledge")
    desc = spec.description
    assert "保存" in desc
    assert "retrieve_knowledge" in desc, "要显式点名那个工具，告诉模型别用错"
    assert "资料" in desc
