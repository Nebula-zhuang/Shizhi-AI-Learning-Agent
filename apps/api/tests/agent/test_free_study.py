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
from app.core.config import settings
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
        # 3B 新增：在**用户主动保存的知识**里跨对话检索。
        # 与 retrieve_knowledge 是两件事（上传的资料 vs 主动保存的内容），刻意分开。
        "search_saved_knowledge",
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
    # 3B 从 5 个增到 6 个（多了 search_saved_knowledge）
    assert len(registry.names()) == 6
    assert len(registry.available_names()) <= 6


# =========================================================================== #
# 五、DECIDE 的输出预算：别忘了这里曾经写死 400
#
# 背景（2B1 回归暴露的）：
#   这里原本是 `max_tokens=400`，而 `_parse_decision` 用 `thought[:300]` ——
#   中文 300 字本身就要 200~400 tokens，再加 JSON 外壳，400 根本不够。
#   两个数字本来就矛盾，只是模型一直没写那么长。
#   **2B1 把预算告诉模型后，它开始逐条权衡剩余次数/时间，thought 涨到 300+ 字**，
#   于是输出在 thought 中途被硬截断 → 没有闭合 `}` → 解析失败 →
#   重试参数一模一样、再次同样截断 → 降级为直接回答，**丢掉多步工具编排**。
#
# 这一组守的就是"别再让上限和允许的 thought 长度互相打架"。
# =========================================================================== #
@pytest.mark.asyncio
async def test_decide_call_uses_the_project_max_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """DECIDE 的输出上限必须取 `settings.llm_max_tokens`，**不许再写死数字**。

    刻意断言"等于设置里的值"而不是"等于 1024"：
    1024 是一个可被 `.env` 覆盖的配置，不是业务常量。
    写死它会让将来调配置的人莫名其妙地踩到这条测试。
    """
    captured: dict = {}

    async def fake_chat_json(messages, **kwargs):
        captured.update(kwargs)
        captured["messages"] = messages
        return {"thought": "够了", "final": True}

    monkeypatch.setattr(free_study.llm_gateway, "chat_json", fake_chat_json)

    decider = free_study._make_decider(images=(), document_ids=())
    decision = await decider(LoopObservation(question="什么是 JVM？"), [])

    assert decision.final is True
    assert "max_tokens" in captured, "决策调用没有传 max_tokens"
    assert captured["max_tokens"] == settings.llm_max_tokens, (
        f"决策的输出上限应取 settings.llm_max_tokens="
        f"{settings.llm_max_tokens}，实际 {captured['max_tokens']}"
    )


def test_loop_decide_prompt_bounds_the_thought_length() -> None:
    """提示词里必须给 `thought` 一个明确的字数上限。

    为什么必须有：**输出长度是生成属性，运行时无从拦截** ——
    代码能限制工具、
    能限制步数，唯独没法阻止模型把理由写长。提示词是唯一的杠杆。

    为什么是 60 字：Tutor 的 `tutor_decision.md` 对 `reason` 用的就是这个数，
    而 Tutor 同样是 `max_tokens` 有限的决策调用、却从没出现过截断。
    照抄一个**已被生产验证过**的约束，比重新拍一个数字稳。
    """
    import re
    from pathlib import Path

    prompt = Path(free_study.__file__).parent / "prompts" / "loop_decide.md"
    text = prompt.read_text(encoding="utf-8")

    match = re.search(r"`?thought`?[^\n]{0,30}?不超过\s*(\d+)\s*字", text)
    assert match, "输出格式里必须给 thought 一个明确的字数上限（形如「thought 不超过 N 字」）"

    limit = int(match.group(1))
    assert limit <= 100, f"上限 {limit} 字太大，起不到约束作用"
    # 与 Tutor 对齐 —— 两边不一致时，读代码的人会不知道哪个才是规矩
    assert limit == 60, f"应当与 Tutor 的 reason 约束一致（60 字），实际 {limit}"


def test_extract_json_cannot_recover_a_truncated_decision() -> None:
    """**记录能力边界**：`extract_json` 救不回被截断的 JSON。

    这条不是在测 bug，而是在**把事实钉下来**，免得将来有人以为
    "反正解析器有兜底" 就敢把输出上限调小 —— 它的兜底是
    "找第一个 `{` 到**最后一个** `}`"，而截断的输出**根本没有闭合 `}`**。

    真正的防线是**不让它被截断**（上限够大 + 提示词约束 thought 长度），
    而不是指望事后补救。**本轮刻意没有改解析器。**
    """
    from app.core.llm import extract_json

    truncated = (
        '{"thought": "用户要求三件事：①用资料解释进程与线程区别 → 已有充分资料'
        "（片段[1][2]已覆盖定义、资源分配/调度单位、共享/开销/通信/同步等核心区别），可直接回答，无需调工具；②联网查"
    )
    with pytest.raises(ValueError):
        extract_json(truncated)

    # 另一种截断形态：字段值都完整、只是最后的括号没闭上 —— 同样救不回来
    half_closed = '{"thought": "够了", "tool": "web_search", "arguments": {"query": "x"'
    with pytest.raises(ValueError):
        extract_json(half_closed)

    # 对照：**格式污染**（而非截断）是能救的 —— 兜底正是为它而写
    assert extract_json('```json\n{"thought": "够了", "final": true}\n```') == {
        "thought": "够了",
        "final": True,
    }
    assert extract_json('好的，我的判断是：{"thought": "够了", "final": true} 以上。') == {
        "thought": "够了",
        "final": True,
    }


# =========================================================================== #
# 六、图片规则：从"无条件第一步"收敛为"按需优先 + 失败可放弃"
#
# 背景（2B2）：原来的措辞是「有图片时**第一步必须**调用看图工具」。
# 它带来两个问题（都实测过）：
#   ① 用户只是顺带传了图、问题跟图无关时，也被迫先花十几秒看图；
#   ② `image_analysis` 失败后，模型为了满足这条"必须"而无条件重试，
#      把预算烧在同一个坑里（它和"失败过不要原样重试"冲突，且"必须"会赢）。
#
# 收敛后要保住的是**当初那条规则存在的真实理由**：
# 模型曾经自己发明 `extract_text_from_image` 这个不存在的工具名，
# 所以"用哪个名字"必须写死；但"第几步"不该被规定。
#
# ⚠️ 断言打在**真实渲染出的提示词**上，而不是 grep 源码 ——
# 源码里有一段注释在讲"为什么删掉'必须第一步'"，grep 会误伤。
# =========================================================================== #
def _prompt_file() -> str:
    from pathlib import Path

    return (Path(free_study.__file__).parent / "prompts" / "loop_decide.md").read_text(
        encoding="utf-8"
    )


async def _render_decide_prompt(
    monkeypatch: pytest.MonkeyPatch,
    *,
    has_image: bool = True,
    failed_tools: tuple[str, ...] = (),
) -> str:
    """跑一次 decider，把真正发给模型的提示词抓出来。"""
    captured: dict = {}

    async def fake_chat_json(messages, **kwargs):
        captured["messages"] = messages
        return {"thought": "够了", "final": True}

    monkeypatch.setattr(free_study.llm_gateway, "chat_json", fake_chat_json)
    # 让"支持视觉"这件事确定下来（否则 .env 一变，提示词就走另一个分支）
    monkeypatch.setattr(settings, "llm_supports_vision", True, raising=False)

    decider = free_study._make_decider(
        images=("fake.png",) if has_image else (), document_ids=()
    )
    observation = LoopObservation(question="解释一下这张图")
    observation.failed_tools.extend(failed_tools)
    await decider(observation, [])

    assert captured.get("messages"), "decider 没有调用模型"
    return "\n".join(str(m.get("content") or "") for m in captured["messages"])


@pytest.mark.asyncio
async def test_decide_prompt_drops_the_unconditional_first_step_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """d. 提示词不得再出现"第一步必须看图"这类**无条件排序**措辞。"""
    prompt = await _render_decide_prompt(monkeypatch)

    for banned in ("第一步必须", "必须第一步", "必须先看"):
        assert banned not in prompt, f"仍有无条件排序措辞：{banned!r}"

    # 静态模板也要干净（它不含那种"解释为什么删掉"的注释，可以直接查）
    assert "第一步必须" not in _prompt_file(), "loop_decide.md 里还留着'第一步必须'"


@pytest.mark.asyncio
async def test_decide_prompt_keeps_the_exact_tool_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """e. 合法工具名必须**明确出现** —— 这是原规则真正要防的事。

    实测踩过：只写"附了 1 张图片"时模型会自己发明
    `extract_text_from_image`，调用失败后回一句"我看不到图"。
    """
    prompt = await _render_decide_prompt(monkeypatch)

    assert "image_analysis" in prompt, "必须点名 image_analysis，否则模型会自己编工具名"
    assert "不要自己编" in prompt, "要点明不许自造工具名"
    assert "image_analysis" in _prompt_file()


@pytest.mark.asyncio
async def test_decide_prompt_lets_the_model_give_up_after_image_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """f. `image_analysis` 失败后必须给出**退路**，而不是让它无脑重试。"""
    prompt = await _render_decide_prompt(monkeypatch, failed_tools=("image_analysis",))

    assert "已经失败过" in prompt
    assert "原样" in prompt, "要明确说'不要原样再调一次'"
    assert "看不了" in prompt, "要给出'如实说看不了'这条退路"

    # 静态模板里同样要有这条冲突求解（与 free_study 的动态注入配套）
    template = _prompt_file()
    assert "失败之后" in template and "原样" in template


@pytest.mark.asyncio
async def test_image_prompt_only_fires_when_there_really_is_an_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没带图时不该出现**动态的"附了图片"段** —— 否则又成了一条"无条件规则"。

    ⚠️ 只断言**动态段特有的措辞**：静态模板里本来就一直有规则 ①②
    以及"已经失败过的工具不要原样重试"（那讲的是通用情况），
    拿这些词去断言必然误伤。
    """
    prompt = await _render_decide_prompt(monkeypatch, has_image=False)

    assert "附了" not in prompt, "没带图却出现了'附了 N 张图片'"
    assert "不要为了满足" not in prompt, "没带图却出现了图片专用的失败退路段"

    # 对照：带图时这两段都必须在
    with_image = await _render_decide_prompt(monkeypatch)
    assert "附了" in with_image and "image_analysis" in with_image
    with_failure = await _render_decide_prompt(monkeypatch, failed_tools=("image_analysis",))
    assert "不要为了满足" in with_failure


def test_quick_route_still_sends_attachments_into_the_loop() -> None:
    """g. `quick_route(has_attachments=True)` 的行为**不变** —— 图片仍必须进循环。

    这是 2B2 的边界：摘掉的是图片的"资料"身份，
    不是它的"附件"身份 —— 看图能力一点都不能少。
    """
    assert (
        free_study.quick_route(
            "这张图片是什么意思？", has_attachments=True, kb_size=0, web_available=True
        )
        is None
    )
    # 对照：没有附件、也没有资料指向时，仍走快通道
    assert free_study.quick_route(
        "什么是 JVM？", has_attachments=False, kb_size=0, web_available=True
    )
