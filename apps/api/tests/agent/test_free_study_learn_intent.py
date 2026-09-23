"""「想学 X」学习意图识别（Phase 5B）。

## 这一组守什么

5B **只做意图识别 + 结构化输出**：决策模型若判断出"这轮其实是想学一个主题"，
`done` 事件会**多带一个字段**：

    {"learn_suggestion": {"topic": "Java 线程", "kind": "topic"}}

三条不变量：

| 不变量 | 为什么必须守 |
|---|---|
| **普通提问不产生该字段** | 只是问了个概念却被反问"要不要系统学"，比不提议更烦人 |
| 普通提问的 `done` **键集完全不变** | 5B 不能顺手动别人的契约（不做成恒为 null 的常驻键） |
| 拿不准就**不给** | 空问题、模糊追问、字段残缺一律不猜 |

## 关于"不依赖真实 LLM 配额"

判断本身是模型做的，测不了（要配额）。所以这里测的是**5B 自己写的那段代码**：
路由把该进 Loop 的问题送进 Loop、解析把模型的输出收成合法结构、
`done` 只在有意图时多带字段。模型的判断力用**脚本化的假响应**代表。
"""

from __future__ import annotations

import pytest

from app.agent import free_study
from app.core.config import settings


# --------------------------------------------------------------------------- #
# 助手：脚本化的假 LLM（不打真实模型）
# --------------------------------------------------------------------------- #
def _stub_llm(monkeypatch: pytest.MonkeyPatch, decide_payload: dict) -> None:
    """把决策与生成都换成脚本 —— 不消耗任何配额。"""

    async def fake_chat_json(messages, **_kwargs):  # noqa: ANN001, ANN202
        return dict(decide_payload)

    async def fake_stream_chat(messages, **_kwargs):  # noqa: ANN001, ANN202
        yield "好的，我来讲讲。"

    monkeypatch.setattr(free_study.llm_gateway, "chat_json", fake_chat_json)
    monkeypatch.setattr(free_study.llm_gateway, "stream_chat", fake_stream_chat)
    # 与本组测试无关的视觉分支：关掉，免得读环境
    monkeypatch.setattr(settings, "llm_supports_vision", False, raising=False)


async def _run_turn(question: str) -> dict:
    """跑一轮，返回 `done` 事件的数据。"""
    done: dict = {}
    async for event in free_study.stream_turn(
        question=question,
        registry=free_study.build_registry(),
    ):
        if event.event == "done":
            done = dict(event.data)
    assert done, "没有收到 done 事件"
    return done


def _suggestion(topic: str, kind: str = "topic") -> dict:
    return {"learn_suggestion": {"topic": topic, "kind": kind}}


# --------------------------------------------------------------------------- #
# 一、路由：该进 Loop 的必须进（否则决策模型根本没机会看到）
# --------------------------------------------------------------------------- #
def test_learning_intent_goes_into_the_loop() -> None:
    """⚠️ 这条是 5B 的**命门**。

    `quick_route` 的兜底是"通用问题直接回答、不进 Loop"。若不特判，
    「我想学 Java 线程」会被快通道答掉，`learn_suggestion` **永远产不出来** ——
    整条功能变成死代码。
    """
    for question in [
        "我想学 Java 线程",
        "教我 Java 线程",
        "带我系统学一下 JVM",
        "Java 线程怎么学",
        "从哪开始学 Java 线程",
    ]:
        assert (
            free_study.quick_route(
                question, has_attachments=False, kb_size=0, web_available=True
            )
            is None
        ), f"「{question}」应当进 Loop"


def test_plain_concept_question_stays_on_the_fast_path() -> None:
    """普通概念提问**不进 Loop** —— 快通道是省调用用的，不能被这条规则拖累。"""
    for question in ["什么是 JVM？", "虚拟线程和平台线程有什么区别"]:
        plan = free_study.quick_route(
            question, has_attachments=False, kb_size=0, web_available=True
        )
        assert plan is not None, f"「{question}」不该被推进 Loop"


def test_empty_question_still_takes_the_fast_path() -> None:
    """空问题不进 Loop（原来就是这样，别改坏）。"""
    plan = free_study.quick_route(
        "", has_attachments=False, kb_size=0, web_available=True
    )
    assert plan is not None


# --------------------------------------------------------------------------- #
# 二、解析：把模型输出收成合法结构，**拿不准就不猜**
# --------------------------------------------------------------------------- #
def test_parses_a_valid_suggestion() -> None:
    assert free_study._parse_learn_suggestion(_suggestion("Java 线程")) == {
        "topic": "Java 线程",
        "kind": "topic",
    }
    assert free_study._parse_learn_suggestion(_suggestion("第三章", "material")) == {
        "topic": "第三章",
        "kind": "material",
    }


def test_absent_field_is_not_a_suggestion() -> None:
    """普通提问：字段根本不存在 → 不能凭空造一个出来。"""
    for raw in [{"thought": "直接回答", "final": True}, {}, None, "不是字典", []]:
        assert free_study._parse_learn_suggestion(raw) is None, f"{raw!r} 不该产生提议"


def test_broken_suggestion_is_dropped() -> None:
    """字段存在但残缺 → 整条丢掉，不要半条。"""
    for raw in [
        {"learn_suggestion": None},
        {"learn_suggestion": "Java 线程"},  # 不是字典
        {"learn_suggestion": {}},  # 没有 topic
        {"learn_suggestion": {"topic": ""}},
        {"learn_suggestion": {"topic": "   "}},
        {"learn_suggestion": {"topic": None}},
    ]:
        assert free_study._parse_learn_suggestion(raw) is None, f"{raw!r} 不该产生提议"


def test_topic_is_trimmed_and_truncated() -> None:
    assert free_study._parse_learn_suggestion(_suggestion("  Java 线程  ")) == {
        "topic": "Java 线程",
        "kind": "topic",
    }
    long = "很" * (free_study.LEARN_TOPIC_MAX_CHARS + 20)
    parsed = free_study._parse_learn_suggestion(_suggestion(long))
    assert parsed is not None
    assert len(parsed["topic"]) == free_study.LEARN_TOPIC_MAX_CHARS


def test_kind_falls_back_to_topic() -> None:
    """`kind` 缺失或写了别的值时归一成 `topic` —— 不因为一个附属字段作废整条。"""
    for kind in [None, "", "乱写", "TOPIC", "Topic"]:
        parsed = free_study._parse_learn_suggestion(
            {"learn_suggestion": {"topic": "Java 线程", "kind": kind}}
        )
        assert parsed is not None
        assert parsed["kind"] == "topic", f"kind={kind!r} 应当归一成 topic"


def test_kind_is_trimmed_before_checking() -> None:
    """带空格的合法值应当**被救回来**，而不是当无效丢掉。"""
    parsed = free_study._parse_learn_suggestion(
        {"learn_suggestion": {"topic": "第三章", "kind": "  material  "}}
    )
    assert parsed is not None
    assert parsed["kind"] == "material"


# --------------------------------------------------------------------------- #
# 三、四类输入端到端：只有"想学主题"和"材料学习"才带字段
# --------------------------------------------------------------------------- #
#: 四类输入 × 脚本化的模型判断 → 期望的 done 字段
SCENARIOS = [
    (
        "明确想学主题",
        "我想学 Java 线程",
        _suggestion("Java 线程"),
        {"topic": "Java 线程", "kind": "topic"},
    ),
    (
        "普通知识提问",
        "什么是 JVM？",
        {"thought": "通用概念，直接回答", "final": True},
        None,
    ),
    (
        "材料学习请求",
        "帮我照我上传的资料把第三章讲一遍",
        _suggestion("第三章", "material"),
        {"topic": "第三章", "kind": "material"},
    ),
    (
        "空/模糊请求",
        "嗯，再讲讲",
        {"thought": "没说要学什么，直接回答", "final": True},
        None,
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "question", "decide_payload", "expected"),
    SCENARIOS,
    ids=[s[0] for s in SCENARIOS],
)
async def test_scenarios(
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    question: str,
    decide_payload: dict,
    expected: dict | None,
) -> None:
    """四类输入各跑一轮，看 `done` 里到底带不带 `learn_suggestion`。"""
    _stub_llm(monkeypatch, decide_payload)

    done = await _run_turn(question)

    if expected is None:
        assert "learn_suggestion" not in done, f"[{label}] 不该产生学习提议：{done.keys()}"
    else:
        assert done.get("learn_suggestion") == expected, f"[{label}] 提议不对"


# --------------------------------------------------------------------------- #
# 四、**普通回答行为完全不变**
# --------------------------------------------------------------------------- #
#: ⚠️ 两条路径的 `done` **形状本来就不同**（5B 之前就是这样）：
#: 快通道 6 个键（没有 provider/fell_back/fallback_reason —— 它不联网），
#: Loop 路径 9 个键。所以下面分开守，不要合并。
FAST_PATH_KEYS = {
    "capabilities",
    "sources",
    "citations",
    "status_trace",
    "degraded_reason",
    "steps",
}
LOOP_PATH_KEYS = FAST_PATH_KEYS | {"provider", "fell_back", "fallback_reason"}


@pytest.mark.asyncio
async def test_fast_path_payload_keys_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """快通道（普通概念提问走的正是它）的 `done` 键集不变，且不带提议。"""
    _stub_llm(monkeypatch, {"thought": "直接回答", "final": True})

    done = await _run_turn("什么是 JVM？")

    assert set(done) == FAST_PATH_KEYS, f"键集变了：{sorted(done)}"
    assert "learn_suggestion" not in done


@pytest.mark.asyncio
async def test_loop_path_payload_keys_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """进了 Loop 但**没有学习意图**时，键集也必须与 5B 之前一模一样。

    5B 最容易犯的错就是顺手加一个恒为 null 的常驻键 ——
    那样前端拿到的每一条消息都多一个字段，"字段在不在"这个更强的信号也就没了。
    """
    _stub_llm(monkeypatch, {"thought": "直接回答", "final": True})

    # 时效类问题会进 Loop（`quick_route` 对时效信号返回 None），但不含学习意图
    done = await _run_turn("现在 Java 最新版本是多少？")

    assert set(done) == LOOP_PATH_KEYS, f"键集变了：{sorted(done)}"
    assert "learn_suggestion" not in done


@pytest.mark.asyncio
async def test_with_suggestion_only_one_key_is_added(monkeypatch: pytest.MonkeyPatch) -> None:
    """有意图时**只多一个键**，其余原样。"""
    _stub_llm(monkeypatch, _suggestion("Java 线程"))

    done = await _run_turn("我想学 Java 线程")

    assert set(done) - {"learn_suggestion"} == LOOP_PATH_KEYS


# --------------------------------------------------------------------------- #
# 五、收集箱的取舍
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_only_the_first_suggestion_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """多步决策里第一条第意图生效。

    意图是从**问题本身**读出来的；后面几步看到的是工具结果，
    容易被"资料里提到了 X"带偏成另一个主题。
    """
    payloads = [
        _suggestion("Java 线程"),
        # 第二步模型改主意了 —— 不该覆盖第一步
        {"tool": "retrieve_knowledge", "arguments": {"query": "Java 线程"}},
        _suggestion("JVM 内存模型"),
    ]
    calls = {"n": 0}

    async def fake_chat_json(messages, **_kwargs):  # noqa: ANN001, ANN202
        index = min(calls["n"], len(payloads) - 1)
        calls["n"] += 1
        return dict(payloads[index])

    async def fake_stream_chat(messages, **_kwargs):  # noqa: ANN001, ANN202
        yield "好的"

    monkeypatch.setattr(free_study.llm_gateway, "chat_json", fake_chat_json)
    monkeypatch.setattr(free_study.llm_gateway, "stream_chat", fake_stream_chat)
    monkeypatch.setattr(settings, "llm_supports_vision", False, raising=False)

    done = await _run_turn("我想学 Java 线程")

    assert done.get("learn_suggestion") == {"topic": "Java 线程", "kind": "topic"}


def test_quick_path_never_produces_a_suggestion() -> None:
    """快通道没有决策这一步 —— 所以它天然不会产生提议（普通问题走的正是它）。"""
    source = free_study._make_decider.__doc__ or ""
    assert "learn_sink" in source, "decider 应当说明收集箱的用途"
    # 快通道那个 done 的构造里不允许出现 learn_suggestion
    import inspect

    body = inspect.getsource(free_study.stream_turn)
    quick_block = body.split("if quick is not None:")[1].split("# ── 进 Loop")[0]
    assert "learn_suggestion" not in quick_block, "快通道不该挂学习意图"


def test_prompt_documents_the_field_and_its_absence() -> None:
    """提示词必须同时写清"什么时候给"和"绝大多数时候不给"。"""
    import pathlib

    text = (
        pathlib.Path(free_study.__file__).parent / "prompts" / "loop_decide.md"
    ).read_text(encoding="utf-8")

    assert "learn_suggestion" in text
    assert "不给" in text, "必须写明什么时候**不要**给"
    assert "什么是 JVM？" in text, "要有「问概念不给」的反例"
