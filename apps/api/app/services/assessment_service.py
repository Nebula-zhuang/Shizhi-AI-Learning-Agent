"""作答评估与提问生成 —— 教学互动的两端。

为什么放同一个模块：**出题时就隐含了"什么样的回答算对"**，
评估就是那个隐含标准的执行。把它们放在一起，改题型时不会忘记同步改判据。

## 结构化评估输出（需求指定）

```
correct / score / confidence / missing_points / misunderstood_points / feedback
+ level（not_mastered|vague|mastered）与 error_type —— 教学动作决策要用的两个粗粒度信号
```

## 两条降级路径（都不允许把整轮学习搞挂）

1. **模型调用失败/超时** → 启发式评估（字符重合度），`engine="heuristic"`，
   并在 feedback 里如实说明"这是自动降级的结果"；
2. **模型返回的 JSON 不合法** → 同上。

`engine` 字段让"谁下的结论"永远可查 —— 与 P2 校验的 `engine` 列是同一个设计动机。

## confidence 的定位

它是**评估本身的把握**（模型对"我判得对吗"的自我评估），
会存进 `answer_evaluations.confidence`，
**绝不参与 `learner_kp_states.mastery` 的计算**（那条公式在 `policy.py`）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.core.llm import LLMGateway, llm_gateway
from app.core.logging import get_logger
from app.models.answer_evaluation import AssessmentLevel, ErrorType
from app.models.knowledge_point import KnowledgePoint
from app.models.message import ActionType

logger = get_logger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "agent" / "prompts"
ASSESSMENT_PROMPT = PROMPTS_DIR / "answer_assessment.md"
ACTIONS_PROMPT = PROMPTS_DIR / "tutor_actions.md"

#: 启发式评估认为"答对"的重合度门槛。仅用于降级路径，不参与正常判定。
HEURISTIC_CORRECT_OVERLAP = 0.30
#: 低于该重合度直接判为完全没答到点上
HEURISTIC_MIN_OVERLAP = 0.05

_WS = re.compile(r"\s+")
_PUNCT = "，。、：；！？·—…\"'（）《》,.:;!?()<>[] "


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class Assessment:
    """一次作答的评估结果。字段与 `answer_evaluations` 表一一对应。"""

    correct: bool = False
    score: float = 0.0
    confidence: float = 0.0
    level: str = AssessmentLevel.NOT_MASTERED
    error_type: str = ErrorType.NONE
    missing_points: list[str] = field(default_factory=list)
    misunderstood_points: list[str] = field(default_factory=list)
    feedback: str = ""
    engine: str = ""

    @property
    def is_fallback(self) -> bool:
        return self.engine.startswith("heuristic")

    def as_dict(self) -> dict[str, Any]:
        return {
            "correct": self.correct,
            "score": round(self.score, 3),
            "confidence": round(self.confidence, 3),
            "level": self.level,
            "error_type": self.error_type,
            "missing_points": self.missing_points,
            "misunderstood_points": self.misunderstood_points,
            "feedback": self.feedback,
            "engine": self.engine,
        }


# --------------------------------------------------------------------------- #
# 提示词
# --------------------------------------------------------------------------- #
def _load_prompt(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    if "--- USER ---" not in text:
        raise RuntimeError(f"提示词缺少 '--- USER ---' 分隔符：{path}")
    system_part, user_part = text.split("--- USER ---", 1)
    return system_part.replace("--- SYSTEM ---", "", 1).strip(), user_part.strip()


def reference_text(point: KnowledgePoint) -> str:
    """知识点的"标准答案素材"。评估与出题都以它为依据。"""
    parts = [
        f"标题：{point.title}",
        f"摘要：{point.summary or '（无）'}",
    ]
    key_points = point.key_points or []
    if key_points:
        parts.append("要点：" + "；".join(str(k) for k in key_points))
    if point.details:
        parts.append(f"讲解：{point.details[:1200]}")
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# 启发式评估（降级路径）
# --------------------------------------------------------------------------- #
def _normalize(text: str) -> str:
    return re.sub(rf"[\s{re.escape(_PUNCT)}]+", "", text or "")


def _bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return {text} if text else set()
    return {text[i : i + 2] for i in range(len(text) - 1)}


def heuristic_score(user_answer: str, reference: str) -> float:
    """字符二元组覆盖率。中文短答案上比词重叠稳，也不需要分词。

    只用于 LLM 不可用时的降级 —— 它衡量的是"像不像"，理解不了语义。
    因此降级结果会明确标注 `engine=heuristic`，让用户知道这次判定是粗略的。
    """
    answer = _normalize(user_answer)
    target = _normalize(reference)
    if not answer or not target:
        return 0.0
    if answer in target:
        return 1.0
    grams = _bigrams(answer)
    if not grams:
        return 0.0
    coverage = len(grams & _bigrams(target)) / len(grams)
    # 长度惩罚：只答两三个字不给高分，否则"进程"两个字就能拿满分
    length_factor = min(1.0, len(answer) / 20.0)
    return round(min(1.0, coverage * (0.5 + 0.5 * length_factor)), 3)


def heuristic_assessment(
    *, user_answer: str, reference: str, engine: str = "heuristic"
) -> Assessment:
    """LLM 不可用时的兜底评估。

    刻意的保守取向：**宁可判得模糊，也不假装判得准**。
    分数低、confidence 低，feedback 里直接说明是自动降级的结果。
    """
    score = heuristic_score(user_answer, reference)
    stripped = _normalize(user_answer)

    if not stripped:
        return Assessment(
            correct=False,
            score=0.0,
            confidence=0.6,
            level=AssessmentLevel.NOT_MASTERED,
            error_type=ErrorType.MEMORY_GAP,
            missing_points=["没有作答内容"],
            feedback="没有看到你的作答。试着用自己的话把要点说出来，哪怕不完整也没关系。",
            engine=engine,
        )

    if score >= HEURISTIC_CORRECT_OVERLAP:
        level = AssessmentLevel.MASTERED if score >= 0.6 else AssessmentLevel.VAGUE
        return Assessment(
            correct=True,
            score=score,
            # 降级路径的把握本来就不高，如实给低值
            confidence=0.45,
            level=level,
            error_type=ErrorType.NONE,
            missing_points=[],
            feedback=(
                f"回答命中了资料中的表述（自动比对，重合度 {score:.0%}）。"
                "这是模型不可用时的粗略判定，仅供参考。"
            ),
            engine=engine,
        )

    return Assessment(
        correct=False,
        score=score,
        confidence=0.45,
        level=AssessmentLevel.NOT_MASTERED,
        error_type=(
            ErrorType.CONCEPT_CONFUSION
            if score >= HEURISTIC_MIN_OVERLAP
            else ErrorType.MEMORY_GAP
        ),
        missing_points=["与资料中的表述重合度较低"],
        feedback=(
            f"回答与资料中的表述重合度较低（{score:.0%}）。"
            "这是模型不可用时的粗略判定，建议对照原文复核。"
        ),
        engine=engine,
    )


# --------------------------------------------------------------------------- #
# 结构化校验
# --------------------------------------------------------------------------- #
def _coerce_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, parsed))


def _coerce_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()][:8]
    return []


def parse_assessment_payload(payload: Any, *, engine: str) -> Assessment | None:
    """把模型的 JSON 输出解析成 Assessment。不合法时返回 None（调用方走降级）。

    `engine` 的取值优先看**返回体自己声明的**，其次才用传入的默认值。
    为什么不直接用 `gateway.mode == "mock"` 来推断：mode 只说"这个网关不联网"，
    并不等于"这份结果是启发式算出来的"——注入的测试替身、将来的本地模型都属于
    mode=mock 但结果并非启发式的情况。用 mode 推断会把它们误标成降级结果，
    于是 `degraded` 永远为真，真正的降级就被淹没了。
    mock 模式下的 `mock_builder` 会在返回体里带 `engine="heuristic"`，
    所以优先读返回体既准确又不需要额外约定。
    """
    if not isinstance(payload, dict):
        return None

    declared_engine = str(payload.get("engine") or "").strip() or engine

    correct = payload.get("correct")
    if not isinstance(correct, bool):
        # 允许 "true"/"false" 这类字符串，但其它值一律不认
        if isinstance(correct, str) and correct.strip().lower() in {"true", "false"}:
            correct = correct.strip().lower() == "true"
        else:
            return None

    score = _coerce_float(payload.get("score"), 0.0)
    confidence = _coerce_float(payload.get("confidence"), 0.5)

    level = str(payload.get("level") or "").strip()
    if level not in set(AssessmentLevel):
        # 推导一个合理的缺省，而不是丢弃整份结果
        level = (
            AssessmentLevel.MASTERED
            if correct and score >= 0.6
            else AssessmentLevel.VAGUE
            if correct
            else AssessmentLevel.NOT_MASTERED
        )

    error_type = str(payload.get("error_type") or "").strip()
    if error_type not in set(ErrorType):
        error_type = ErrorType.NONE if correct else ErrorType.CONCEPT_CONFUSION

    # 答对就不该有错因，保持一致
    if correct:
        error_type = ErrorType.NONE

    return Assessment(
        correct=correct,
        score=score,
        confidence=confidence,
        level=level,
        error_type=error_type,
        missing_points=_coerce_str_list(payload.get("missing_points")),
        misunderstood_points=_coerce_str_list(payload.get("misunderstood_points")),
        feedback=str(payload.get("feedback") or "").strip()[:2000],
        engine=declared_engine,
    )


# --------------------------------------------------------------------------- #
# 评估
# --------------------------------------------------------------------------- #
def render_assessment_prompt(*, point: KnowledgePoint, question: str, user_answer: str) -> list[dict[str, str]]:
    system, template = _load_prompt(ASSESSMENT_PROMPT)
    user = (
        template.replace("{{reference}}", reference_text(point))
        .replace("{{question}}", question or "（无）")
        .replace("{{answer}}", user_answer or "（空）")
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def evaluate_answer(
    *,
    point: KnowledgePoint,
    question: str,
    user_answer: str,
    llm: LLMGateway | None = None,
) -> Assessment:
    """评估一次作答。

    永不抛异常 —— 任何失败都降级成启发式评估并在 `engine` 里标明。
    一轮学习不该因为一次评估调用失败而中断。
    """
    gateway = llm or llm_gateway
    reference = reference_text(point)

    def mock_payload(_messages: list[dict[str, str]]) -> dict[str, Any]:
        # mock 模式下返回**真实的启发式结果**而不是固定假数据 ——
        # 这样离线跑通的教学闭环是有意义的（答对会真的判对），
        # 而不是"无论答什么都返回同一个结果"的假闭环。
        return heuristic_assessment(user_answer=user_answer, reference=reference).as_dict()

    try:
        payload = await gateway.chat_json(
            render_assessment_prompt(point=point, question=question, user_answer=user_answer),
            max_tokens=900,
            mock_builder=mock_payload,
        )
    except Exception as exc:  # noqa: BLE001 - 评估失败必须降级而不是中断
        logger.warning("作答评估调用失败，降级为启发式：%s", exc)
        return heuristic_assessment(
            user_answer=user_answer, reference=reference, engine="heuristic(llm_error)"
        )

    assessment = parse_assessment_payload(payload, engine=gateway.model)

    if assessment is None:
        logger.warning("作答评估返回体不合法，降级为启发式：%r", str(payload)[:200])
        return heuristic_assessment(
            user_answer=user_answer, reference=reference, engine="heuristic(bad_payload)"
        )
    return assessment


# --------------------------------------------------------------------------- #
# 提问 / 讲解生成
# --------------------------------------------------------------------------- #
def load_action_prompts() -> dict[str, tuple[str, str]]:
    """解析 `tutor_actions.md`，返回 {动作: (system, user_template)}。

    六个动作共用**一个文件**：它们共享同一段系统指令（角色、语气、禁止事项），
    拆成六个文件会让这段指令重复六遍，改一次要改六处。

    文件结构：文件头是全局 system 段，之后每个动作一个 `## <action>` 小节。
    用正则按行首的 `## xxx` 切分，而不是按字符串 `partition` ——
    后者在"第二节开始"处会丢掉 `## ` 前缀，解析出一堆无名小节。
    """
    text = ACTIONS_PROMPT.read_text(encoding="utf-8")
    pattern = re.compile(r"^##\s+([a-z_]+)\s*$", re.MULTILINE)
    matches = list(pattern.finditer(text))

    if not matches:
        raise RuntimeError(f"{ACTIONS_PROMPT.name} 里没有找到任何 '## <action>' 小节")

    head = text[: matches[0].start()]
    system = (
        head.replace("--- SYSTEM ---", "", 1).replace("--- USER ---", "", 1).strip()
    )

    sections: dict[str, tuple[str, str]] = {}
    valid = set(ActionType)
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        action = match.group(1).strip().lower()
        if action not in valid:
            continue
        sections[action] = (system, text[match.end() : end].strip())

    missing = valid - set(sections)
    if missing:
        raise RuntimeError(f"{ACTIONS_PROMPT.name} 缺少动作小节：{sorted(missing)}")
    return sections


def render_action_prompt(
    *,
    action: str,
    point: KnowledgePoint,
    state_summary: str,
    assessment_summary: str,
    context: str,
    style_instruction: str = "",
    credibility: str = "",
) -> list[dict[str, str]]:
    """为某个教学动作渲染提示词。

    六个动作共用同一个变量集合（知识点 / 学习状态 / 上次评估 / 检索资料 /
    讲法偏好 / 核查情况），各动作的差异全部写在 `tutor_actions.md` 的对应小节里 ——
    **这样"动作本身"的知识只存在于 Prompt，而"什么时候用哪个动作"只存在于 Policy。**

    **替换同时作用于系统段与用户段**：讲法偏好与核查情况都写在六节共用的系统段里，
    只替换用户段的话，那些 `{{...}}` 会原样发给模型。

    `credibility` 为空串是**常态**（大多数知识点没什么可说的）。
    这时替换成一句明确的"无需提及"，而不是留个空占位符 ——
    留空会让模型自己去猜这一节该怎么处理，而它多半会猜成"要说点什么"。
    """
    sections = load_action_prompts()
    entry = sections.get(action)
    if entry is None:
        raise ValueError(f"tutor_actions.md 里缺少动作 {action!r} 的小节")

    system, template = entry

    def fill(text: str) -> str:
        return (
            text.replace("{{reference}}", reference_text(point))
            .replace("{{state}}", state_summary or "（新学习者）")
            .replace("{{last_assessment}}", assessment_summary or "（首次接触，暂无评估）")
            .replace("{{context}}", context or "（没有检索到相关资料片段）")
            .replace("{{style_instruction}}", style_instruction or "（暂无偏好，用常规讲法）")
            .replace(
                "{{credibility}}",
                credibility or "（没有需要特别说明的核查结论，正常讲即可，不要主动提核查这件事。）",
            )
        )

    return [
        {"role": "system", "content": fill(system)},
        {"role": "user", "content": fill(template)},
    ]


def fallback_content(action: str, point: KnowledgePoint) -> str:
    """生成失败时的兜底内容。

    刻意做成"有内容但明显是模板"而不是空字符串 —— 用户至少知道该做什么，
    也能一眼看出这一轮是降级产生的。
    """
    title = point.title or "这个知识点"
    if action == ActionType.SUMMARIZE:
        return (
            f"关于「{title}」，你已经达到掌握要求了。\n\n"
            f"简明回顾：{point.summary or '（本知识点暂无摘要）'}\n\n"
            "（本轮由降级模板生成，模型当时不可用。）"
        )
    if action in {ActionType.EXPLAIN, ActionType.REPHRASE}:
        detail = (point.details or point.summary or "").strip()
        return (
            f"【{title}】\n\n{detail or '（本知识点暂无讲解内容）'}\n\n"
            "（本轮由降级模板生成，模型当时不可用。）"
        )
    # probe / harder / easier 都要给一个可以回答的问题
    return (
        f"请用自己的话说明「{title}」的核心内容"
        f"{'，并说明它与相邻概念的区别' if action == ActionType.HARDER else ''}。\n\n"
        "（本轮由降级模板生成，模型当时不可用。）"
    )


@dataclass
class GeneratedContent:
    """教学内容的生成结果。

    `degraded` 是必要的：兜底模板是在**工具内部**产生的，
    如果只返回一个字符串，外层的 ToolRunner 完全看不出这一轮降级了，
    `degraded` 标记就会漏报 —— 而"这一轮到底有没有降级"是演示与排障的关键信息。
    """

    content: str
    action: str = ""
    degraded: bool = False
    error: str = ""

    def __str__(self) -> str:
        return self.content

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "degraded": self.degraded,
            "error": self.error,
            "chars": len(self.content),
        }


async def generate_question(
    *,
    point: KnowledgePoint,
    action: str,
    state_summary: str = "",
    assessment_summary: str = "",
    context: str = "",
    style_instruction: str = "",
    credibility: str = "",
    llm: LLMGateway | None = None,
) -> GeneratedContent:
    """生成某一轮要展示给学习者的内容（讲解 / 提问 / 总结都由它产出）。

    失败时返回降级模板内容并置 `degraded=True`，绝不返回空 ——
    这一轮的学习提示仍然有意义。

    `credibility` 是核查结论的**人话版本**（见 `verify_voice.py`）。
    为空是常态，表示这条没什么需要特别说明的。
    """
    return await _generate_content(
        point=point,
        action=action,
        state_summary=state_summary,
        assessment_summary=assessment_summary,
        context=context,
        style_instruction=style_instruction,
        credibility=credibility,
        llm=llm,
    )


async def generate_question_streaming(
    *,
    point: KnowledgePoint,
    action: str,
    state_summary: str = "",
    assessment_summary: str = "",
    context: str = "",
    style_instruction: str = "",
    credibility: str = "",
    llm: LLMGateway | None = None,
    on_piece: Callable[[str], None] | None = None,
) -> GeneratedContent:
    """流式生成教学内容：边生成边把文字增量交给 `on_piece`。

    为什么要它：生成是整轮里最慢的一段（实测可到 7 秒以上 ——
    "提问必须自述清楚"要求输出更完整）。如果非要等生成完才显示，
    学习者就干等 7 秒；流式让正文**从第一个字起就开始出现**，
    他读第一句的时间和后面仍在生成的时间是重叠的。

    与 `generate_question` 的差异只有一处：要求模型**直接输出正文**、
    不要 JSON 包装 —— 要边生成边展示，就不能等 JSON 解析完才拿到第一个字。
    返回的 `GeneratedContent` 与 JSON 路径等价（`content` 都是纯正文）。

    mock 模式直接走非流式路径：mock 的流式产出是通用假文本，
    而非流式路径的 `mock_builder` 能给出**与动作对应**的降级模板，
    后者才是离线回归真正需要的行为。
    """
    gateway = llm or llm_gateway
    if gateway.mode == "mock":
        return await _generate_content(
            point=point,
            action=action,
            state_summary=state_summary,
            assessment_summary=assessment_summary,
            context=context,
            style_instruction=style_instruction,
            credibility=credibility,
            llm=gateway,
        )

    messages = render_action_prompt(
        action=action,
        point=point,
        state_summary=state_summary,
        assessment_summary=assessment_summary,
        context=context,
        style_instruction=style_instruction,
        credibility=credibility,
    )
    # 把提示词里的 JSON 要求**换掉**（不是追加一条相反的）。
    #
    # 踩过的坑：一开始只在末尾追加"直接输出正文，不要 JSON 包装"，
    # 结果模型仍然照做了前面的"输出 JSON：{...}" —— 界面上直接把
    # {"content": "这道题问的是…"} 原样显示了出来。**两条相反的指令里，
    # 先出现的那条赢**，所以必须把原来的那条删掉。
    if messages and messages[0].get("role") == "system":
        system_text = re.sub(
            r"6\.\s*输出 JSON[^\n]*\n(?:\s{2,}[^\n]*\n)?",
            "6. 直接输出正文本身，不要用 JSON 包装，不要加任何前后缀、不要写解释性的话。\n",
            messages[0]["content"],
        )
        messages[0] = {"role": "system", "content": system_text}

    raw = ""
    shown = ""
    try:
        async for piece in gateway.stream_chat(messages, max_tokens=1100):
            if not piece:
                continue
            raw += piece
            # 增量解包：模型偶尔仍会套 JSON。对累积原文反复求值，
            # 只把**新增的那一段**交给回调，这样无论套没套 JSON，
            # 界面上显示的都始终是正文。
            current = _display_of(raw)
            if current is None:
                continue  # 还判断不出来，先压住
            if len(current) > len(shown):
                delta = current[len(shown) :]
                shown = current
                if on_piece is not None:
                    on_piece(delta)
    except Exception as exc:  # noqa: BLE001
        logger.warning("教学动作 %s 的流式生成失败：%s", action, exc)
        if shown:
            # 已经吐了一半就不要换模板 —— 半截正文比"整体换成模板"更连贯
            return GeneratedContent(
                content=shown.strip(),
                action=action,
                degraded=True,
                error=f"{type(exc).__name__}: {exc}",
            )
        return GeneratedContent(
            content=fallback_content(action, point),
            action=action,
            degraded=True,
            error=f"{type(exc).__name__}: {exc}",
        )

    content = shown.strip()
    if not content:
        return GeneratedContent(
            content=fallback_content(action, point),
            action=action,
            degraded=True,
            error="模型返回了空内容",
        )
    return GeneratedContent(content=content, action=action, degraded=False)


_JSON_WRAPPER = re.compile(r'^\s*\{\s*"content"\s*:\s*"')

#: 判断"这是不是 JSON 包装"最多容忍多少个字符。
#: 包装前缀本身只有十几个字符；给到 64 字符还没判出来，就当作不是包装，原样显示。
_JSON_PROBE_LIMIT = 64


def _display_of(raw: str) -> str | None:
    """从流式累积的原文里算出"现在应该显示什么"。

    返回 `None` 表示**还判断不出来，先压住别显示**。

    为什么需要它：提示词里已经要求直接输出正文（并且把原来的 JSON 要求换掉了），
    但模型偶尔仍会套一层 `{"content": "..."}`。如果不管，流式会把
    `{"content": "` 这种碎片直接闪在屏幕上 —— 比显示得慢更难看。

    所以这里对累积原文反复求值：确认是包装就剥壳、确认不是就原样、还看不出来就压住。
    正常路径（模型老实输出正文）下，第一个字符不是 `{`，立刻原样显示，等于没有这个函数。
    """
    if not raw:
        return ""
    if not raw.lstrip().startswith("{"):
        return raw

    match = _JSON_WRAPPER.match(raw)
    if match:
        inner = raw[match.end() :]
        # 收尾的 "} 还没到时不用管，到了再剥
        inner = re.sub(r'"\s*\}\s*$', "", inner)
        return (
            inner.replace("\\n", "\n")
            .replace('\\"', '"')
            .replace("\\/", "/")
            .replace("\\\\", "\\")
        )

    # 以 { 开头但还认不出是我们的包装：可能是还没收全，也可能是别的 JSON。
    # 超过容忍长度就放弃判断，原样显示 —— 宁可显示怪东西，也不要永远不显示。
    return None if len(raw) < _JSON_PROBE_LIMIT else raw


async def _generate_content(
    *,
    point: KnowledgePoint,
    action: str,
    state_summary: str,
    assessment_summary: str,
    context: str,
    style_instruction: str,
    credibility: str = "",
    llm: LLMGateway | None,
) -> GeneratedContent:
    gateway = llm or llm_gateway

    def mock_payload(_messages: list[dict[str, str]]) -> dict[str, Any]:
        return {"content": fallback_content(action, point).replace("（本轮由降级模板生成，模型当时不可用。）", "（离线 mock 模式生成）")}

    try:
        payload = await gateway.chat_json(
            render_action_prompt(
                action=action,
                point=point,
                state_summary=state_summary,
                assessment_summary=assessment_summary,
                context=context,
                style_instruction=style_instruction,
                credibility=credibility,
            ),
            max_tokens=900,
            mock_builder=mock_payload,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("教学动作 %s 的内容生成失败，用降级模板：%s", action, exc)
        return GeneratedContent(
            content=fallback_content(action, point),
            action=action,
            degraded=True,
            error=f"{type(exc).__name__}: {exc}",
        )

    content = ""
    if isinstance(payload, dict):
        content = str(payload.get("content") or payload.get("text") or "").strip()
    elif isinstance(payload, str):
        content = payload.strip()

    if not content:
        return GeneratedContent(
            content=fallback_content(action, point),
            action=action,
            degraded=True,
            error="模型返回了空内容",
        )
    return GeneratedContent(content=content, action=action, degraded=False)


def summarize_state_for_prompt(state_summary: dict[str, Any] | None) -> str:
    """把状态快照压成一句人话，喂给 Prompt。

    只给**事实**，不给阈值判断 —— 该不该升难度是 Policy 的事，
    模型看到"连对 2 次"就够了，不需要知道"2 次算多"。
    """
    if not state_summary:
        return "（新学习者）"
    return (
        f"掌握度 {float(state_summary.get('mastery') or 0):.2f}，"
        f"累计作答 {int(state_summary.get('attempt_count') or 0)} 次，"
        f"连续答对 {int(state_summary.get('consecutive_correct') or 0)} 次，"
        f"连续答错 {int(state_summary.get('consecutive_wrong') or 0)} 次"
    )


def summarize_assessment_for_prompt(assessment: Assessment | None) -> str:
    if assessment is None:
        return ""
    missing = "、".join(assessment.missing_points) or "无"
    return (
        f"判定：{'正确' if assessment.correct else '错误'}；"
        f"得分 {assessment.score:.2f}；深度 {assessment.level}；"
        f"错因 {assessment.error_type}；遗漏点：{missing}；"
        f"评语：{assessment.feedback}"
    )


__all__ = [
    "Assessment",
    "GeneratedContent",
    "reference_text",
    "evaluate_answer",
    "fallback_content",
    "generate_question",
    "heuristic_assessment",
    "heuristic_score",
    "load_action_prompts",
    "parse_assessment_payload",
    "render_action_prompt",
    "render_assessment_prompt",
    "summarize_assessment_for_prompt",
    "summarize_state_for_prompt",
]
