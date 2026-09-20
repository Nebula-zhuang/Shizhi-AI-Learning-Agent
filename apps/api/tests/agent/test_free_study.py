"""自由学习 Agent 的路由与诚实性测试。

阶段 2 起架构变了，这个文件也跟着重写：

    阶段 1   quick_route 直接给出能力清单（"要检索还是要联网"）
    阶段 2   quick_route **只判断"要不要进 Agent 循环"** ——
             需要工具的问题返回 `None` 交给循环去一步步决定

所以下面的断言分成两类：

  **快通道**：通用问题必须一眼定性，**不进循环**（省掉一次决策调用）
  **进循环**：指向资料 / 含时效信号 / 带附件的问题必须交给循环

第二类不能用"返回了什么能力"来断言 —— 那正是阶段 2 删掉的东西。
"""
from __future__ import annotations

import pytest

from app.agent import free_study
from app.agent.runtime_loop import LoopDecision, LoopObservation
from app.agent.tools.specs import ToolResult
from app.search.provider import MockWebSearchProvider


# =========================================================================== #
# 一、快通道：通用问题不进循环
# =========================================================================== #
@pytest.mark.parametrize(
    "question",
    [
        "什么是 JVM？",
        "帮我解释一下这个 Java 代码。",
        "什么是 TCP 三次握手？",
        "解释一下快速排序",
        "进程和线程有什么区别",
    ],
)
def test_general_questions_take_fast_path(question: str) -> None:
    """**通用问题必须一眼定性、不进循环** —— 否则每个问题都要多一次决策调用。"""
    plan = free_study.quick_route(
        question, has_attachments=False, kb_size=0, web_available=True
    )
    assert plan is not None, f"{question} 本该走快通道"
    assert plan.capabilities == ["general"]


@pytest.mark.parametrize(
    "question",
    [
        "帮我分析这个 PDF。",
        "根据这个文件给我讲讲第三章。",
        "这个知识点和我之前学的有什么关系？",
        "我上传的课件里怎么说的？",
        "根据我的资料讲讲虚拟线程，资料里没有的再联网",
    ],
)
def test_material_questions_enter_the_loop(question: str) -> None:
    """指向用户资料的问题 → **交给循环**。

    阶段 2 不再在这里返回"要检索"—— 那是循环第一步该做的决定。
    这里只负责判断"该不该进循环"。
    """
    assert (
        free_study.quick_route(
            question, has_attachments=False, kb_size=174, web_available=True
        )
        is None
    ), f"{question} 应当交给 Agent 循环"


@pytest.mark.parametrize(
    "question",
    [
        "帮我查一下 Java 25 的虚拟线程。",
        "Java 25 虚拟线程现在有什么变化？",
        "最新版本的 Spring Boot 有什么变化",
        "Python 3.13 有什么新特性",
    ],
)
def test_time_sensitive_questions_enter_the_loop(question: str) -> None:
    """含时效信号 → 进循环（由循环决定要不要真的联网）。

    ⚠️ `帮我查一下 Java 25 的虚拟线程` 是用户需求里**逐字举的例子**，
    阶段 1 曾漏判过它（裸版本号没被识别），所以留在这里当回归。
    """
    assert (
        free_study.quick_route(
            question, has_attachments=False, kb_size=0, web_available=True
        )
        is None
    ), f"{question} 应当交给 Agent 循环"


def test_attachment_always_enters_the_loop() -> None:
    """带图片 → 必须进循环：不看图无法判断还能不能答。"""
    assert (
        free_study.quick_route(
            "这张图片是什么意思？", has_attachments=True, kb_size=0, web_available=True
        )
        is None
    )


def test_empty_question_takes_fast_path() -> None:
    assert free_study.quick_route("", has_attachments=False, kb_size=0, web_available=True)


# =========================================================================== #
# 二、决策解析：无论模型返回什么都必须得到可用结果
# =========================================================================== #
def test_parse_decision_reads_tool_call() -> None:
    d = free_study._parse_decision(
        {"thought": "需要联网", "tool": "web_search", "arguments": {"query": "x"}}
    )
    assert d.wants_tool is True
    assert d.tool == "web_search"
    assert d.arguments == {"query": "x"}
    assert d.thought == "需要联网"


def test_parse_decision_reads_final() -> None:
    d = free_study._parse_decision({"thought": "够了", "final": True})
    assert d.final is True
    assert d.wants_tool is False


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "not a dict",
        {},
        {"tool": 123},          # 工具名不是字符串
        {"final": "yes"},        # final 不是 True
        {"tool": ""},           # 空工具名
        {"tool": "x", "arguments": "not a dict"},
    ],
)
def test_parse_decision_never_breaks(raw: object) -> None:
    """**模型输出什么形状都可能出现，这里崩掉会让整轮对话挂掉。**

    注意 `{"tool": 123}` 与 `{"tool": ""}` 都应当被当成"收尾"而不是
    "调一个空名字的工具" —— 后者只会浪费一次循环。
    """
    d = free_study._parse_decision(raw)
    assert isinstance(d, LoopDecision)
    assert isinstance(d.arguments, dict)
    # 要么调一个非空工具，要么收尾 —— 不能是"有 tool 字段但为空"
    assert d.final or (d.tool and d.tool.strip())


# =========================================================================== #
# 三、素材与禁令：没联网就不许说查过
# =========================================================================== #
def _obs_with(results: list[tuple[str, ToolResult]]) -> LoopObservation:
    obs = LoopObservation(question="q")
    for name, result in results:
        obs.record(name, result)
    return obs


def test_no_web_material_contains_explicit_ban() -> None:
    """**最重要的一条。**

    没有联网结果时，素材段必须是一段**明确的禁令**，而不是空白。
    留空会让模型自己脑补"大概是没有资料，我自由发挥吧"，
    而它发挥出来的恰恰是"根据我查到的情况…"。
    """
    material = free_study._render_material(_obs_with([]))
    assert "没有联网" in material
    for phrase in ("我上网查了", "网上资料显示", "据报道"):
        assert phrase in material, f"没有明确禁止「{phrase}」"


def test_ban_also_applies_when_kb_was_used_but_web_not() -> None:
    """⚠️ **只检索过资料、没联网时也要给禁令。**

    这种情况更隐蔽：模型手上有资料片段，很容易顺着写一句
    "结合网上的最新说法…"。禁令不能只在"搜了但失败"时才给。
    """
    obs = _obs_with(
        [
            (
                "retrieve_knowledge",
                ToolResult(ok=True, content="资料片段", display={"kind": "knowledge_base"}),
            )
        ]
    )
    material = free_study._render_material(obs)
    assert "资料片段" in material
    assert "没有联网" in material, "用过资料但没联网时也必须禁掉'我查过'"


def test_ban_lifted_when_web_result_present() -> None:
    """真的联网拿到结果了，就不该再禁止 —— 那会等于否认自己刚查过。"""
    obs = _obs_with(
        [
            (
                "web_search",
                ToolResult(ok=True, content="联网结果", display={"kind": "web_search"}),
            )
        ]
    )
    material = free_study._render_material(obs)
    assert "联网结果" in material
    assert "没有联网" not in material


def test_failed_tool_result_is_visible_to_model() -> None:
    """失败的调用也要写进素材 —— 模型需要知道"这条路走不通了"，
    否则它会一再重试同一个工具。"""
    obs = _obs_with(
        [("web_search", ToolResult(ok=False, error="search_timeout", content="超时了"))]
    )
    material = free_study._render_material(obs)
    assert "没成功" in material or "search_timeout" in material


# =========================================================================== #
# 四、工具注册与状态文案
# =========================================================================== #
def test_registry_has_the_expected_tools() -> None:
    """阶段的工具集合。**每加一个工具都要在这里补一笔** ——
    它同时是一份"我们到底提供了什么能力"的清单。"""
    registry = free_study.build_registry()
    assert set(registry.names()) == {
        "retrieve_knowledge",
        "web_search",
        "document_analysis",
        "image_analysis",
        # 2.1 新增：时钟问题不能再靠模型记忆
        "current_time",
    }


def test_web_search_hidden_when_unavailable() -> None:
    """联网不可用时，`web_search` **不该进给模型的清单** ——
    让模型看见一个调不通的工具，只会浪费它一次决策。"""
    from app.agent.tools.specs import ToolRegistry, ToolSpec

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="web_search",
            description="d",
            parameters={},
            handler=lambda **_: None,  # type: ignore[arg-type]
            available=lambda: (False, "没配 Key"),
        )
    )
    assert registry.names() == ["web_search"]
    assert registry.available_names() == []
    assert registry.describe_for_llm() == []


def test_registry_lookup_tolerates_dash_and_case() -> None:
    """查表容忍连字符/下划线与大写 —— 外部服务命名不统一，
    在这一层归一化比让每个调用点都记得转换安全。"""
    registry = free_study.build_registry()
    assert registry.get("WEB-SEARCH") is not None
    assert registry.get("web_search") is not None


@pytest.mark.parametrize(
    "tool",
    ["retrieve_knowledge", "web_search", "image_analysis", "document_analysis", "未知工具"],
)
def test_status_text_never_leaks_internal_terms(tool: str) -> None:
    """状态文案是直接给用户看的。

    出现 Tool / RAG / Embedding 这类词，既让人困惑，也暴露了实现细节。
    """
    text = free_study._tool_status_text(tool)
    for banned in ("Tool", "tool", "RAG", "embedding", "Embedding", "retrieve", "MCP", "mcp"):
        assert banned not in text, f"状态文案暴露了内部术语「{banned}」：{text}"
    assert text, "未知工具也要给一句人话，不能是空串"


# =========================================================================== #
# 五、快通道下不联网也要诚实（整体行为）
# =========================================================================== #
@pytest.mark.asyncio
async def test_fast_path_turn_produces_events() -> None:
    """跑一轮通用问题，确认事件序列完整且没有工具调用。"""
    events = []
    async for event in free_study.stream_turn(
        question="什么是进程？",
        registry=free_study.build_registry(),
    ):
        events.append(event)

    kinds = [e.event for e in events]
    assert "status" in kinds
    assert "delta" in kinds
    assert "done" in kinds
    assert "tool_start" not in kinds, "通用问题不该调用任何工具"


def test_capabilities_lists_tools() -> None:
    caps = free_study.describe_capabilities(provider=MockWebSearchProvider())
    assert caps["free_study"] is True
    assert "retrieve_knowledge" in caps["tools"]
    assert caps["max_capabilities_per_turn"] >= 1


# =========================================================================== #
# 六、能力闸门：不支持的能力不许进清单
# =========================================================================== #
def test_vision_tool_hidden_when_model_cannot_see(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ **模型不支持视觉时，`image_analysis` 不该出现在给模型的清单里。**

    实测踩过：图片按 OpenAI 多模态格式发给一个不支持视觉的模型，
    接口**不报错**，但模型回一句"我无法查看或分析图片"。
    工具于是返回 ok=True，Agent 拿到一个没用的结果还白转了 4 轮。

    **能力必须显式声明，不能靠"发过去看行不行"。**
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "llm_supports_vision", False)
    assert "image_analysis" not in free_study.build_registry().available_names()

    monkeypatch.setattr(settings, "llm_supports_vision", True)
    assert "image_analysis" in free_study.build_registry().available_names()


@pytest.mark.asyncio
async def test_image_analysis_refuses_when_vision_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """闸门直接拒绝，并给模型一句可执行的话 —— 而不是让它去猜图里有什么。"""
    from app.agent.tool_specs import image_analysis
    from app.core.config import settings

    monkeypatch.setattr(settings, "llm_supports_vision", False)
    result = await image_analysis("这是什么", image_path="data:image/png;base64,AAAA")

    assert result.ok is False
    assert result.error == "vision_not_supported"
    assert "不要猜测" in result.content


def test_registry_lists_all_tools_regardless_of_availability() -> None:
    """`names()` 列全部，`available_names()` 只列当下可用的 —— 两者刻意分开。

    分开的理由：联网没配 Key、模型不支持视觉时，那些工具不该出现在
    给模型的清单里（让它看见一个调不通的工具只会浪费一次决策），
    但它们仍然是"我们注册过的能力"，该在自检接口里如实列出。
    """
    registry = free_study.build_registry()
    assert len(registry.names()) == 5
    assert len(registry.available_names()) <= 5
