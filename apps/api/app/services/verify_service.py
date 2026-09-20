"""知识点可信度校验（三级）。

对应技术方案的 L1 → L2 → L3，层层过滤，越往后越贵：

  L1 规则校验   确定性、零成本，**所有**知识点都跑
  L2 模型自评   LLM，只对 importance 高或 L1 报警的知识点跑
  L3 联网核验   Tavily，闸门最严，只对 L2 未通过者跑

**三条不可动摇的约定**（见 docs/05-P2-实施方案.md §5）：

1. **绝不覆盖 P1 的抽取结果。** 本模块只写 `knowledge_points.verify_status` 这一个字段 ——
   它是 P1 明确为本阶段预留的位。title/summary/details/key_points/标注/溯源一律只读。
   所有校验细节写进 `knowledge_checks` 表（append-only），历史永不覆盖。

2. **三层结果不混。** rule / model / web 各自成行，不合并成一行"综合结论"。
   用户需要能分辨「程序按规则查的」「模型自己说的」「网上查到的」——
   三者的可信度和失效方式完全不同。

3. **引用优先级：用户资料 > 联网证据 > 模型通识。** 联网证据只能作为补充记录，
   不得覆盖用户资料；与用户资料冲突时只保留双方证据，不改写知识点内容。

**未配置 Tavily Key 时**：L3 整层记 `skipped` 并说明原因，L1/L2 照常跑。
这不是故障，是设计上的降级 —— 与「配了 Key 但调用失败」（记 `error`）是两种状态。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.llm import LLMGateway, llm_gateway
from app.core.logging import get_logger
from app.models.chunk import Chunk
from app.models.knowledge_check import CheckType, CheckVerdict, KnowledgeCheck
from app.models.knowledge_point import KnowledgePoint, VerifyStatus
from app.models.knowledge_relation import KnowledgeRelation
from app.search.tavily_client import SearchErrorCode, TavilyClient, TavilyResult, tavily_client

logger = get_logger(__name__)

PROMPT_PATH = Path(__file__).resolve().parent.parent / "agent" / "prompts" / "verify_knowledge_point.md"

#: 下结论的主体标识，写进 knowledge_checks.engine，便于追溯"当时是谁判的"
ENGINE_RULE = "rule-v1"


# --------------------------------------------------------------------------- #
# L1：规则校验
# --------------------------------------------------------------------------- #
#: 通识外溢的引导词。这些词常常是模型把自己知道的东西"顺口"写进讲解的信号。
_LEAK_MARKERS = (
    "一般来说",
    "通常情况下",
    "众所周知",
    "需要注意的是",
    "值得一提的是",
    "此外",
    "另外",
    "常见的做法",
    "实际应用中",
    "补充一点",
)

_SENTENCE_SPLIT = re.compile(r"[。！？；\n]+")
_PUNCT_STRIP = "，。、：；！？·—…\"'（）《》,.:;!?()<>[] "

#: 判定「这句话在原文里有依据」的字符二元组覆盖率阈值。
#:
#: 用二元组覆盖率而不是「整句包含」，是因为模型转述时几乎必然会改动连接词与标点：
#: 原文「动态性、并发性」被写成「动态性和并发性」是正常概述，不是编造。
#: 整句包含会把这类正常转述全部误报成"混入通识"，让告警失去价值。
#: 实测：正常转述覆盖率约 0.9，真正编造的内容约 0.2，0.6 有足够的安全间距。
_BIGRAM_RATIO_THRESHOLD = 0.6

#: 待判定的句子核心短于该长度时无法可靠判断，一律视为有依据
_MIN_CORE_CHARS = 6

#: 标题里不该出现的句子标点
_TITLE_BAD_CHARS = "。！？，；"


@dataclass
class RuleItem:
    """L1 的单项检查结果。"""

    code: str
    ok: bool
    detail: str
    #: error 级失败会让整体判 unsupported；warn 级仅提示
    severity: str = "warn"

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "ok": self.ok, "detail": self.detail, "severity": self.severity}


@dataclass
class CheckDraft:
    """一条待落库的校验记录。"""

    kp_id: int
    check_type: str
    verdict: str
    reason: str
    confidence: float
    engine: str
    evidence: dict[str, Any] = field(default_factory=dict)
    source_urls: list[str] = field(default_factory=list)


@dataclass
class VerifyStats:
    """一次校验运行的整体统计。"""

    total_points: int = 0
    rule_done: int = 0
    model_done: int = 0
    model_skipped: int = 0
    web_done: int = 0
    web_skipped: int = 0
    web_quota_used: int = 0
    errors: int = 0
    by_status: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_points": self.total_points,
            "rule_done": self.rule_done,
            "model_done": self.model_done,
            "model_skipped": self.model_skipped,
            "web_done": self.web_done,
            "web_skipped": self.web_skipped,
            "web_quota_used": self.web_quota_used,
            "errors": self.errors,
            "by_status": self.by_status,
        }


def _normalize_for_match(text: str) -> str:
    """归一化：去掉空白与标点，只留内容字符，用于跨行/跨标点的包含判断。"""
    return re.sub(r"[\s" + re.escape(_PUNCT_STRIP) + r"]+", "", text or "")


def _bigrams(text: str) -> set[str]:
    """字符二元组集合。用于衡量两段文本的内容重合度。"""
    if len(text) < 2:
        return set()
    return {text[i : i + 2] for i in range(len(text) - 1)}


def _is_supported(core: str, haystack_norm: str) -> bool:
    """判断句子核心在原文中是否有依据。

    先试整句包含（快路径），再用二元组覆盖率兜住正常的转述差异。
    """
    core_norm = _normalize_for_match(core)
    if len(core_norm) < _MIN_CORE_CHARS:
        # 太短，无法可靠判断 —— 宁可放过也不误报
        return True
    if core_norm in haystack_norm:
        return True

    grams = _bigrams(core_norm)
    if not grams:
        return True
    coverage = len(grams & _bigrams(haystack_norm)) / len(grams)
    return coverage >= _BIGRAM_RATIO_THRESHOLD


def find_unsupported_sentences(details: str, source_text: str) -> list[str]:
    """找出讲解里"带通识引导词、但在原文中找不到依据"的句子。

    这是个启发式，不是判定器 —— 它只用来表示**「原文里没找到这句话的依据」**。

    ⚠️ 它**刻意不下「编造」这种指控**。把正常概述误报成编造，会让用户对整个
    校验功能失去信任 —— 这正是「存疑」这个结论被删掉的原因：
    一个每四条就命中一条的标签，用户学会的是无视它，而不是去查那四条。
    """
    if not details or not source_text:
        return []
    haystack = _normalize_for_match(source_text)
    hits: list[str] = []
    for raw in _SENTENCE_SPLIT.split(details):
        sentence = raw.strip()
        if len(sentence) < 8:
            continue
        if not any(marker in sentence for marker in _LEAK_MARKERS):
            continue
        core = sentence
        for marker in _LEAK_MARKERS:
            core = core.replace(marker, "")
        if len(_normalize_for_match(core)) < 8:
            continue
        if not _is_supported(core, haystack):
            hits.append(sentence[:80])
    return hits


def rule_check_point(
    point: KnowledgePoint,
    *,
    chunk_indexes: set[int],
    page_count: int,
    source_text: str,
) -> tuple[list[RuleItem], str, float]:
    """对单个知识点跑 L1 规则校验。返回 (逐项结果, 汇总结论, 置信度)。"""
    items: list[RuleItem] = []

    # ------------------------------------------------------- 1. 溯源完整性
    raw_indexes = point.source_chunk_indexes or []
    try:
        indexes = {int(x) for x in raw_indexes}
    except (TypeError, ValueError):
        indexes = set()
    missing = sorted(indexes - chunk_indexes)
    if not indexes:
        items.append(RuleItem("source_backlink", False, "没有记录来源块，无法回溯原文", "error"))
    elif missing:
        items.append(
            RuleItem("source_backlink", False, f"引用了不存在的块 {missing}", "error")
        )
    else:
        items.append(RuleItem("source_backlink", True, f"来源块 {sorted(indexes)} 均存在"))

    # ------------------------------------------------------------ 2. 页码
    pages = [int(p) for p in (point.source_pages or []) if isinstance(p, (int, float, str)) and str(p).isdigit()]
    if not pages:
        items.append(RuleItem("page_range", False, "没有来源页码", "error"))
    elif page_count > 0 and (min(pages) < 1 or max(pages) > page_count):
        items.append(
            RuleItem("page_range", False, f"页码 {pages} 超出 1-{page_count} 范围", "error")
        )
    else:
        items.append(RuleItem("page_range", True, f"页码 {pages} 有效"))

    # -------------------------------------------------------- 3. 标题规范
    title = (point.title or "").strip()
    if not title:
        items.append(RuleItem("title_quality", False, "标题为空", "error"))
    elif len(title) > 20:
        items.append(RuleItem("title_quality", False, f"标题 {len(title)} 字，超过 20", "warn"))
    elif any(ch in title for ch in _TITLE_BAD_CHARS):
        items.append(RuleItem("title_quality", False, "标题含句子标点，不像名词短语", "warn"))
    else:
        items.append(RuleItem("title_quality", True, f"标题规范（{len(title)} 字）"))

    # -------------------------------------------------------- 4. 摘要长度
    summary = (point.summary or "").strip()
    if not summary:
        items.append(RuleItem("summary_present", False, "摘要为空", "warn"))
    elif len(summary) > 60:
        items.append(RuleItem("summary_present", False, f"摘要 {len(summary)} 字，超过 60", "warn"))
    else:
        items.append(RuleItem("summary_present", True, f"摘要 {len(summary)} 字"))

    # ------------------------------------------------------ 5. 要点条数
    key_points = point.key_points or []
    if not isinstance(key_points, list) or not key_points:
        items.append(RuleItem("key_points", False, "没有要点", "warn"))
    elif not (2 <= len(key_points) <= 4):
        items.append(RuleItem("key_points", False, f"要点 {len(key_points)} 条，建议 2-4 条", "warn"))
    else:
        items.append(RuleItem("key_points", True, f"要点 {len(key_points)} 条"))

    # -------------------------------------------------- 6. 通识外溢启发式
    leaks = find_unsupported_sentences(point.details or "", source_text)
    if leaks:
        items.append(
            RuleItem(
                "common_knowledge_leak",
                False,
                f"{len(leaks)} 处说法带通识引导词且在原文中找不到依据，疑似混入模型通识",
            )
        )
    else:
        items.append(RuleItem("common_knowledge_leak", True, "未发现无法溯源的表述"))

    # ------------------------------------------------------------ 汇总
    failed_error = [i for i in items if not i.ok and i.severity == "error"]
    failed_warn = [i for i in items if not i.ok and i.severity != "error"]
    if failed_error:
        # **error 级记 `ERROR` 而不是 `UNSUPPORTED`** —— 这两件事必须分开：
        #
        #   ERROR        核对**没能进行**（拿不到来源块 / 没有页码）→ 我们不知道
        #   UNSUPPORTED  核对进行了，但**没找到依据** → 我们知道结果是"没依据"
        #
        # 混成一个的后果很实际：一个连原文都取不到的知识点，
        # 只要模型层（看不到原文的情况下）说一句"忠实"，就会被判成"可信" ——
        # 上面所有结论都建立在"资料能读到"这个前提上，前提不成立时它们都不算数。
        verdict = CheckVerdict.ERROR
        failed_codes = "、".join(i.code for i in failed_error)
        reason = f"无法完成核对：缺少必要依据（{failed_codes}）"
        confidence = 0.35
    elif failed_warn:
        # 曾经判 `suspicious`（"需人工复核"）。删掉该结论后并入 `unsupported` ——
        # 语义上也更准：规范未达标说明**没找到依据**，而不是"有问题"。
        verdict = CheckVerdict.UNSUPPORTED
        failed_codes = "、".join(i.code for i in failed_warn)
        reason = f"有 {len(failed_warn)} 项规范未达标：{failed_codes}"
        confidence = 0.55
    else:
        verdict = CheckVerdict.PASSED
        reason = "全部规则检查通过"
        confidence = 0.85
    return items, verdict, confidence


# --------------------------------------------------------------------------- #
# L2：模型自评
# --------------------------------------------------------------------------- #
def load_verify_prompt() -> tuple[str, str]:
    """加载校验提示词，返回 (system, user_template)。"""
    text = PROMPT_PATH.read_text(encoding="utf-8")
    if "--- USER ---" not in text:
        raise RuntimeError(f"校验提示词缺少 '--- USER ---' 分隔符：{PROMPT_PATH}")
    system_part, user_part = text.split("--- USER ---", 1)
    return system_part.replace("--- SYSTEM ---", "", 1).strip(), user_part.strip()


def render_verify_prompt(
    *,
    point: KnowledgePoint,
    source_chunks: Sequence[Chunk],
    system: str,
    template: str,
) -> list[dict[str, str]]:
    sources_text = "\n\n".join(
        f"<<<page={c.page_start} | chunk_index={c.chunk_index}>>>\n{c.content}"
        for c in source_chunks
    )
    key_points = point.key_points or []
    key_text = "\n".join(f"- {k}" for k in key_points) if key_points else "（无）"
    user = (
        template.replace("{{title}}", point.title or "")
        .replace("{{summary}}", point.summary or "")
        .replace("{{details}}", (point.details or "（无）")[:2000])
        .replace("{{key_points}}", key_text)
        .replace("{{sources}}", sources_text[:4000] or "（未找到原文）")
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


_MODEL_VERDICT_MAP = {
    "faithful": CheckVerdict.PASSED,
    # "说得过头"并入 unsupported：模型的这类自评误报率很高
    # （实测一份 108 知识点的资料被标出 27 条"存疑"），不足以支撑一个用户可见的结论。
    "overstated": CheckVerdict.UNSUPPORTED,
    "unsupported": CheckVerdict.UNSUPPORTED,
}


def build_model_mock_payload(messages: list[dict[str, str]]) -> dict[str, Any]:
    """mock 模式下的确定性返回，使整条校验链路可离线回归、零 API 消耗。"""
    return {
        "verdict": "faithful",
        "confidence": 0.9,
        "reason": "[MOCK] 讲解与原文一致（离线模拟结果，未调用模型）。",
        "unsupported_claims": [],
    }


async def model_check_point(
    point: KnowledgePoint,
    *,
    source_chunks: Sequence[Chunk],
    llm: LLMGateway | None = None,
) -> CheckDraft:
    """L2：让模型只回答一个问题 —— 这份讲解是否忠实于原文。"""
    gateway = llm or llm_gateway
    system, template = load_verify_prompt()
    messages = render_verify_prompt(
        point=point, source_chunks=source_chunks, system=system, template=template
    )

    try:
        payload = await gateway.chat_json(
            messages,
            max_tokens=settings.verify_llm_max_tokens,
            mock_builder=build_model_mock_payload,
        )
    except Exception as exc:  # noqa: BLE001 - 单点失败不影响其余知识点
        logger.warning("知识点 id=%s 的模型自评失败：%s", point.id, exc)
        return CheckDraft(
            kp_id=point.id,
            check_type=CheckType.MODEL,
            verdict=CheckVerdict.ERROR,
            reason=f"模型自评调用失败：{type(exc).__name__}",
            confidence=0.0,
            engine=gateway.model,
        )

    raw_verdict = str((payload or {}).get("verdict") or "").strip().lower()
    verdict = _MODEL_VERDICT_MAP.get(raw_verdict)
    if verdict is None:
        return CheckDraft(
            kp_id=point.id,
            check_type=CheckType.MODEL,
            verdict=CheckVerdict.ERROR,
            reason=f"模型返回了无法识别的结论：{raw_verdict!r}",
            confidence=0.0,
            engine=gateway.model,
        )

    try:
        confidence = float((payload or {}).get("confidence") or 0.7)
    except (TypeError, ValueError):
        confidence = 0.7

    return CheckDraft(
        kp_id=point.id,
        check_type=CheckType.MODEL,
        verdict=verdict,
        reason=str((payload or {}).get("reason") or "")[:480],
        confidence=max(0.0, min(1.0, confidence)),
        engine=gateway.model,
        evidence={
            "raw_verdict": raw_verdict,
            "unsupported_claims": (payload or {}).get("unsupported_claims") or [],
        },
    )


# --------------------------------------------------------------------------- #
# L3：联网核验
# --------------------------------------------------------------------------- #
def build_search_query(point: KnowledgePoint) -> str:
    """构造检索词。刻意保守：只用标题 + 摘要前段，避免把模型的话当事实去搜。"""
    title = (point.title or "").strip()
    summary = (point.summary or "").strip()
    if summary:
        return f"{title} {summary[:40]}"
    return title


def should_verify_online(
    point: KnowledgePoint,
    *,
    l1_verdict: str,
    l2_verdict: str | None,
) -> tuple[bool, str]:
    """联网核验的闸门。返回 (是否核验, 不核验的原因)。

    闸门严格是刻意的 —— 搜索有配额成本，而且对"常识性稳定知识点"联网搜索
    不但浪费，还容易引入噪声污染原本正确的结论。
    """
    if not settings.effective_web_verify:
        if not settings.verify_web_enabled:
            return False, "联网核验已被配置关闭"
        return False, "未配置 TAVILY_API_KEY"

    # 常识性稳定知识点：难度低、重要度不高、且前两层都没报警 —— 一律跳过
    if (
        point.difficulty <= 2
        and point.importance <= 3
        and l1_verdict == CheckVerdict.PASSED
        and l2_verdict in (None, CheckVerdict.PASSED, CheckVerdict.SKIPPED)
    ):
        return False, "常识性稳定知识点，无联网核验必要"

    # 只有前两层真的存疑才值得花钱搜
    if l1_verdict == CheckVerdict.PASSED and l2_verdict in (None, CheckVerdict.PASSED):
        return False, "规则与模型自评均通过，无需联网核验"

    return True, ""


def judge_web_results(point: KnowledgePoint, results: Sequence[TavilyResult]) -> tuple[str, float, str]:
    """把检索结果映射成结论。

    需要注意的第一个原则：**联网证据不能覆盖用户资料**。这里只判断
    "网上是否找到了与这个知识点明确一致的说法"，绝不据此改写知识点内容。

    第二个原则：**找不到证据 ≠ 知识点是错的。**
    所以这种情况判 `unsupported`（"没找到依据"），而它在汇总时**不会**升级成
    "有出入" —— 忠实反映"我们没查到"，不宣称知识点有问题。
    """
    title = (point.title or "").strip()
    if title and any(title in (r.title + r.content) for r in results):
        return CheckVerdict.PASSED, 0.75, f"联网结果中找到与「{title}」一致的表述"
    return (
        CheckVerdict.UNSUPPORTED,
        0.45,
        "联网结果中未找到与该知识点明确一致的表述（找不到证据不等于结论错误）",
    )


async def web_check_point(
    point: KnowledgePoint,
    *,
    search: TavilyClient | None = None,
) -> CheckDraft:
    """L3：Tavily 联网核验。"""
    client = search or tavily_client
    query = build_search_query(point)
    outcome = await client.search_safe(query)

    if not outcome.ok:
        # 「没配 Key」是主动跳过，不是故障；其余（超时/HTTP 错误/无结果）如实记录
        if outcome.code == SearchErrorCode.NOT_CONFIGURED:
            verdict, reason, confidence = (
                CheckVerdict.SKIPPED,
                f"未配置 TAVILY_API_KEY，跳过联网核验：{outcome.message}",
                0.0,
            )
        elif outcome.code == SearchErrorCode.EMPTY:
            verdict, reason, confidence = (
                CheckVerdict.UNSUPPORTED,
                "联网检索没有返回任何结果，无法交叉验证",
                0.4,
            )
        else:
            verdict, reason, confidence = (
                CheckVerdict.ERROR,
                f"联网核验执行失败（{outcome.code}）：{outcome.message}",
                0.0,
            )
        return CheckDraft(
            kp_id=point.id,
            check_type=CheckType.WEB,
            verdict=verdict,
            reason=reason[:480],
            confidence=confidence,
            engine="tavily",
            evidence={"query": query, "error_code": str(outcome.code) if outcome.code else None},
        )

    verdict, confidence, reason = judge_web_results(point, outcome.results)
    return CheckDraft(
        kp_id=point.id,
        check_type=CheckType.WEB,
        verdict=verdict,
        reason=reason,
        confidence=confidence,
        engine="tavily",
        evidence={
            "query": query,
            "elapsed_ms": outcome.elapsed_ms,
            "hits": [
                {"title": r.title, "url": r.url, "content": r.content, "score": r.score}
                for r in outcome.results
            ],
        },
        source_urls=[r.url for r in outcome.results if r.url],
    )


# --------------------------------------------------------------------------- #
# 汇总：写回 verify_status（唯一允许写的字段）
# --------------------------------------------------------------------------- #
def aggregate_status(drafts: Iterable[CheckDraft], *, fallback: str = VerifyStatus.UNVERIFIED) -> str:
    """把三层结论汇总成一个 verify_status。

    现在只有**两种产出**：`trusted`（找到了依据）或 `unverified`（没找到）。

    ## 为什么不把 `unsupported` 升级成 `conflict`（有出入）

    老实现是 `unsupported → conflict`，而 `unsupported` 的覆盖面很宽
    （规范未达标、模型觉得说得过头、联网没查到一致表述…）。
    这些情况的共同点是"**我们没查到**"，但它们被翻译成了"**这条有出入**"——
    那是把一个关于**我们**的事实，说成了关于**知识点**的事实。

    结果就是信噪比崩掉：一份 108 个知识点的资料会冒出二十多条"存疑/有出入"，
    而用户很快学会无视这个标签，**连带真正有问题的少数几条也一起无视**。

    所以：`unsupported` 就如实反映成"还没核对出结果"。
    要下"有出入"必须拿出"外部证据与资料**直接矛盾**"的判断 ——
    现在还没有这个手段，那就先不说（`VerifyStatus.CONFLICT` 保留为保留位）。

    ## 为什么"通过"要求至少有一层非规则校验

    只有 L1 规则通过说明不了什么 —— 规则查的是格式与引用完整性，
    不是内容对不对。必须有模型或联网层背书才算数。
    """
    drafts = list(drafts)

    # **先看前提成不成立：规则层如果连原文都取不到（ERROR），后面所有"通过"都不算数。**
    #
    # 这一条守的是一个很隐蔽的漏洞：模型层是在**看不到原文**的情况下被调用时，
    # 它仍可能返回"忠实"。若只看"有没有非规则层通过"，这种知识点就会被评为"可信" ——
    # 而它其实连来源块都没有。**前提不成立，结论就不成立。**
    if any(
        d.check_type == CheckType.RULE and d.verdict == CheckVerdict.ERROR for d in drafts
    ):
        return fallback

    # **必须是非规则层给出的通过**，才算 trusted。
    #
    # ⚠️ 这里同时修掉了一个隐藏的错误：原实现写的是
    #     `PASSED in verdicts and any(check_type != RULE)`
    # 它只要求"存在一个通过"和"存在一个非规则层" —— **两件事可以来自不同的层**。
    # 于是 `[规则层通过, 联网层说没找到依据]` 会被判成"可信"：
    # 通过是规则层给的，而规则层查的是格式，根本没法背书内容对不对。
    # 两个条件必须落在**同一条** draft 上。
    if any(
        d.verdict == CheckVerdict.PASSED and d.check_type != CheckType.RULE
        for d in drafts
    ):
        return VerifyStatus.TRUSTED
    return fallback


# --------------------------------------------------------------------------- #
# 编排
# --------------------------------------------------------------------------- #
def load_source_chunks(db: Session, point: KnowledgePoint) -> list[Chunk]:
    indexes = []
    for item in point.source_chunk_indexes or []:
        try:
            indexes.append(int(item))
        except (TypeError, ValueError):
            continue
    if not indexes:
        return []
    return list(
        db.execute(
            select(Chunk)
            .where(Chunk.document_id == point.document_id, Chunk.chunk_index.in_(indexes))
            .order_by(Chunk.chunk_index)
        )
        .scalars()
        .all()
    )


async def verify_document(
    db: Session,
    document_id: int,
    *,
    page_count: int,
    llm: LLMGateway | None = None,
    search: TavilyClient | None = None,
    on_progress: Callable[[int, str], None] | None = None,
) -> VerifyStats:
    """对一份文档的全部知识点跑三级校验。

    知识点之间互不影响：单个知识点任意一层失败都只记一条 error 记录，不中断整批。
    """
    stats = VerifyStats()

    points = list(
        db.execute(
            select(KnowledgePoint)
            .where(KnowledgePoint.document_id == document_id)
            .order_by(KnowledgePoint.order_index, KnowledgePoint.id)
        )
        .scalars()
        .all()
    )
    stats.total_points = len(points)
    if not points:
        return stats

    chunk_rows = db.execute(
        select(Chunk.chunk_index, Chunk.content).where(Chunk.document_id == document_id)
    ).all()
    chunk_indexes = {int(r[0]) for r in chunk_rows}
    chunk_text_by_index = {int(r[0]): (r[1] or "") for r in chunk_rows}

    # 关系依据完整性也纳入校验：没有依据的边不该存在
    relations = list(
        db.execute(
            select(KnowledgeRelation).where(KnowledgeRelation.document_id == document_id)
        )
        .scalars()
        .all()
    )
    relations_without_evidence = sum(1 for r in relations if not (r.source and r.evidence))

    status_counter: dict[str, int] = {}

    for order, point in enumerate(points, start=1):
        drafts: list[CheckDraft] = []

        # ------------------------------------------------------------- L1
        source_chunks = load_source_chunks(db, point)
        source_text = "\n".join(c.content or "" for c in source_chunks)
        items, l1_verdict, l1_conf = rule_check_point(
            point,
            chunk_indexes=chunk_indexes,
            page_count=page_count,
            source_text=source_text,
        )
        drafts.append(
            CheckDraft(
                kp_id=point.id,
                check_type=CheckType.RULE,
                verdict=l1_verdict,
                reason="全部规则检查通过" if l1_verdict == CheckVerdict.PASSED else _rule_reason(items),
                confidence=l1_conf,
                engine=ENGINE_RULE,
                evidence={"items": [i.as_dict() for i in items]},
            )
        )
        stats.rule_done += 1

        # ------------------------------------------------------------- L2
        l2_verdict: str | None = None
        l2_gate = (
            point.importance >= settings.verify_model_importance_threshold
            or l1_verdict != CheckVerdict.PASSED
        )
        if l2_gate:
            draft = await model_check_point(point, source_chunks=source_chunks, llm=llm)
            drafts.append(draft)
            l2_verdict = draft.verdict
            stats.model_done += 1
            if draft.verdict == CheckVerdict.ERROR:
                stats.errors += 1
        else:
            stats.model_skipped += 1
            drafts.append(
                CheckDraft(
                    kp_id=point.id,
                    check_type=CheckType.MODEL,
                    verdict=CheckVerdict.SKIPPED,
                    reason=(
                        f"重要度 {point.importance} 低于阈值 "
                        f"{settings.verify_model_importance_threshold} 且规则检查通过，无需模型自评"
                    ),
                    confidence=0.0,
                    engine="gate",
                )
            )

        # ------------------------------------------------------------- L3
        allow, blocked_reason = should_verify_online(
            point, l1_verdict=l1_verdict, l2_verdict=l2_verdict
        )
        if allow and stats.web_quota_used >= settings.verify_web_max_per_document:
            allow, blocked_reason = (
                False,
                f"已达单文档联网核验上限（{settings.verify_web_max_per_document} 次）",
            )

        if allow:
            draft = await web_check_point(point, search=search)
            drafts.append(draft)
            stats.web_quota_used += 1
            if draft.verdict == CheckVerdict.SKIPPED:
                stats.web_skipped += 1
            elif draft.verdict == CheckVerdict.ERROR:
                stats.errors += 1
            else:
                stats.web_done += 1
        else:
            stats.web_skipped += 1
            drafts.append(
                CheckDraft(
                    kp_id=point.id,
                    check_type=CheckType.WEB,
                    verdict=CheckVerdict.SKIPPED,
                    reason=blocked_reason,
                    confidence=0.0,
                    engine="gate",
                )
            )

        # ------------------------------------------- 落库 + 只写 verify_status
        status = aggregate_status(drafts, fallback=point.verify_status)
        for draft in drafts:
            db.add(
                KnowledgeCheck(
                    kp_id=draft.kp_id,
                    document_id=document_id,
                    check_type=draft.check_type,
                    verdict=draft.verdict,
                    confidence=Decimal(str(round(draft.confidence, 2))),
                    reason=draft.reason[:500],
                    evidence=draft.evidence or None,
                    source_urls=draft.source_urls or None,
                    engine=draft.engine,
                )
            )
        # 唯一允许写入 knowledge_points 的字段
        point.verify_status = status
        db.commit()

        status_counter[status] = status_counter.get(status, 0) + 1

        if on_progress:
            on_progress(
                int(order / len(points) * 100),
                f"已校验 {order}/{len(points)} 个知识点",
            )

    stats.by_status = status_counter
    if relations_without_evidence:
        stats.errors += 1
        logger.error(
            "文档 id=%s 有 %d 条关系缺少构建依据，数据异常", document_id, relations_without_evidence
        )

    logger.info(
        "文档 id=%s 校验完成：%d 个知识点，状态分布 %s，联网核验 %d 次",
        document_id,
        stats.total_points,
        stats.by_status,
        stats.web_quota_used,
    )
    return stats


def _rule_reason(items: Sequence[RuleItem]) -> str:
    failed = [i for i in items if not i.ok]
    if not failed:
        return "全部规则检查通过"
    return "；".join(f"{i.code}: {i.detail}" for i in failed)[:480]


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
def list_checks(db: Session, kp_id: int) -> list[KnowledgeCheck]:
    return list(
        db.execute(
            select(KnowledgeCheck)
            .where(KnowledgeCheck.kp_id == kp_id)
            .order_by(KnowledgeCheck.check_type, KnowledgeCheck.id.desc())
        )
        .scalars()
        .all()
    )


def count_checks(db: Session, document_id: int) -> int:
    return int(
        db.execute(
            select(func.count())
            .select_from(KnowledgeCheck)
            .where(KnowledgeCheck.document_id == document_id)
        ).scalar_one()
    )


def check_status_breakdown(db: Session, document_id: int) -> dict[str, int]:
    """按 verify_status 统计知识点数量，供进度接口兜底显示。"""
    rows = db.execute(
        select(KnowledgePoint.verify_status, func.count())
        .where(KnowledgePoint.document_id == document_id)
        .group_by(KnowledgePoint.verify_status)
    ).all()
    return {str(r[0]): int(r[1]) for r in rows}
