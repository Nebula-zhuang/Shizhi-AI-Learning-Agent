"""知识点抽取服务。

实现 skills/knowledge-extraction/SKILL.md 中定义的规则。核心设计：

1. **页码不由模型输出** —— 模型只给 source_chunk_indexes，source_pages 由代码从 chunk
   反查回填。与其在提示词里反复叮嘱「不要编页码」，不如让模型根本没有输出页码的字段。
   能靠结构避免的错误，不要靠提示词避免。
2. **三层去重** —— 提示词内传 existing_titles（第一层）→ 产出后归一化精确匹配（第二层）
   → 数据库唯一索引 (document_id, title_norm)（第三层）。
   包含关系的判定需要 LLM 且极易过度合并（「进程」会被并进「进程与线程」），P1 不做。
3. **局部失败不牵连整体** —— 单批抽取失败只记 warning，其余批次照常产出。
4. **mock 可回归** —— mock 模式返回由输入派生的合法 JSON，整条流水线可离线测试、零 API 消耗。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.llm import LLMGateway, llm_gateway
from app.core.logging import get_logger
from app.models.chunk import BlockType, Chunk
from app.models.document import Document, ParseStatus
from app.models.knowledge_point import KnowledgePoint

logger = get_logger(__name__)

#: 提示词模板位置
PROMPT_PATH = Path(__file__).resolve().parents[1] / "agent" / "prompts" / "extract_knowledge_points.md"

#: 传给模型的「已存在标题」上限，防止提示词无限膨胀
MAX_EXISTING_TITLES = 60

#: 标题硬上限（提示词要求 ≤20，代码兜住病态值）
MAX_TITLE_CHARS = 40
#: 摘要硬上限
MAX_SUMMARY_CHARS = 200
#: 要点条数上限
MAX_KEY_POINTS = 4
#: 标签个数上限
MAX_TAGS = 3
#: 参与抽取的块最小长度，过短的块只会产出噪声
MIN_CHUNK_CHARS = 30

#: 质量闸门：单批知识点数量的合理区间
BATCH_KP_MIN = 1
BATCH_KP_MAX = 8

WARN_EMPTY_BATCH = "EMPTY_BATCH"
WARN_TOO_FINE_GRAINED = "TOO_FINE_GRAINED"
WARN_LLM_PARSE_FAILED = "LLM_PARSE_FAILED"
WARN_INVALID_SOURCE = "INVALID_SOURCE"
WARN_NO_EXTRACTABLE_CHUNK = "NO_EXTRACTABLE_CHUNK"
WARN_NO_KNOWLEDGE_POINT = "NO_KNOWLEDGE_POINT"


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
class KPExtract(BaseModel):
    """模型输出的单条知识点。注意：**没有页码字段**。"""

    title: str
    summary: str = ""
    details: str = ""
    key_points: list[str] = Field(default_factory=list)
    difficulty: int = 3
    importance: int = 3
    confidence: float = 0.8
    tags: list[str] = Field(default_factory=list)
    source_chunk_indexes: list[int] = Field(default_factory=list)


class KPResponse(BaseModel):
    """模型输出的整体结构。"""

    knowledge_points: list[KPExtract] = Field(default_factory=list)


@dataclass(slots=True)
class KPRecord:
    """落库前的知识点。比 KPExtract 多了归一化标题与回填的页码。"""

    title: str
    title_norm: str
    summary: str
    details: str
    key_points: list[str]
    difficulty: int
    importance: int
    confidence: float
    tags: list[str]
    heading_path: list[str]
    source_chunk_indexes: list[int]
    source_pages: list[int]
    order_index: int = 0


@dataclass(slots=True)
class ExtractionResult:
    """抽取结果。"""

    records: list[KPRecord] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    batch_count: int = 0
    failed_batches: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.records) or self.batch_count == 0


# --------------------------------------------------------------------------- #
# 标题归一化与去重
# --------------------------------------------------------------------------- #
_PUNCT = re.compile(r"[\s\-_—·、，,。.：:；;！!？?（）()【】\[\]《》<>\"'“”‘’/\\|]+")
#: 虚词，去掉后「进程与线程的区别」与「进程线程区别」可归一到同一键
_STOPWORDS = re.compile(r"[的与和及或之]")


def normalize_title(title: str, *, max_len: int = 200) -> str:
    """标题归一化，作为去重键。

    去掉标点与空白、转小写、去除常见虚词。目的是让同一考点的不同表述能撞在一起。
    """
    text = (title or "").strip().lower()
    text = _PUNCT.sub("", text)
    text = _STOPWORDS.sub("", text)
    return text[:max_len]


# --------------------------------------------------------------------------- #
# 学习主题 → 知识点（5C-2：自由学习里"想学 X"跳去辅导）
# --------------------------------------------------------------------------- #
#: 太短的主题不做包含匹配（一两个字几乎能命中任何东西）。
CONTAIN_TOPIC_MIN_CHARS = 3

#: 太短的标题也不作为包含匹配的目标（一个字的"知识点"不是可以去学的东西）。
CONTAIN_TITLE_MIN_CHARS = 2


@dataclass(frozen=True)
class LearnTarget:
    """把"想学 X"解析到具体知识点的结果。"""

    #: 匹配到的知识点 id；没匹配上就是 None（**绝不编一个出来**）
    kp_id: int | None
    title: str
    document_id: int | None
    #: 机器码，给前端决定文案用；**不是给用户看的**（`exact`/`normalized`/`contained`/`none`/`ambiguous`）
    reason: str

    @property
    def matched(self) -> bool:
        return self.kp_id is not None


def _learn_target_candidates(db: Session, *, learner_id: str) -> list[KnowledgePoint]:
    """这位学习者**自己资料里**的全部知识点。

    归属沿 `knowledge_points.document_id → documents.owner_learner_id` 判定 ——
    与 `deps.require_knowledge_point` 同一套规则。**不加这一层，匹配会从别人的
    资料里挑出知识点**，而下一步就是拿它去开一轮教学（最严重的一类越权）。
    """
    owned = select(Document.id).where(
        Document.owner_learner_id == learner_id,
        Document.parse_status == ParseStatus.READY,
    )
    stmt = select(KnowledgePoint).where(KnowledgePoint.document_id.in_(owned))
    return list(db.scalars(stmt).all())


def find_learn_target(db: Session, *, topic: str, learner_id: str) -> LearnTarget:
    """把学习主题解析成一个已有知识点。**匹配不到就如实说匹配不到。**

    三级，逐级放宽，**每级都必须唯一**：

    | 级别 | 判据 | 理由 |
    |---|---|---|
    | `exact` | 标题**原样**相等 | 最可靠，用户就是照着标题说的 |
    | `normalized` | `normalize_title` 后相等 | 复用入库时的同一套归一化（标点/大小写/虚词）|
    | `contained` | 标题被主题**包含**（主题更具体） | 兜底，**方向本身即为护栏**（见下）|

    ⚠️ **每一级都要求候选唯一**：同名/多候选一律返回 `ambiguous`，让用户自己去挑。
    跳错知识点比不跳更糟 —— 用户会在一门完全没想学的课里被问第一个问题。

    ## 包含匹配为什么**只认一个方向**

    允许：`标题 ⊆ 主题`（主题更具体）。例：「Java 线程」→ 标题「线程」，
    多出来的「Java」是个限定词，落到「线程」是对的。

    拒绝：`主题 ⊂ 标题`（主题更短）。例：「进程」→ 标题「进程与线程」✗
    —— **这正是文件开头那条 P1 结论点名的形状**（「包含关系极易过度合并」）。
    那条结论针对的是入库时合并（破坏性）；这里是跳转时定位（非破坏性），
    但危险的形状是同一种，所以照挡。

    → **方向就是护栏**，不需要再叠一层长度比：反向的情形一律不匹配。
    """
    text = (topic or "").strip()
    if not text:
        return LearnTarget(None, "", None, "none")

    candidates = _learn_target_candidates(db, learner_id=learner_id)

    # ① 原样相等（**同样要求唯一** —— 两条同名时不能任选一条）
    exact = [p for p in candidates if p.title == text]
    if len(exact) == 1:
        point = exact[0]
        return LearnTarget(point.id, point.title, point.document_id, "exact")
    if len(exact) > 1:
        return LearnTarget(None, "", None, "ambiguous")

    # ② 归一化后相等（复用入库同款 `normalize_title`）
    wanted = normalize_title(text)
    if wanted:
        hits = [p for p in candidates if p.title_norm == wanted]
        if len(hits) == 1:
            point = hits[0]
            return LearnTarget(point.id, point.title, point.document_id, "normalized")
        if len(hits) > 1:
            # 不同资料里存在同名的点 —— 让用户自己去选，别替他挑
            return LearnTarget(None, "", None, "ambiguous")

    # ③ 包含：**只认「标题 ⊆ 主题」**（主题更具体），且候选唯一
    if len(wanted) >= CONTAIN_TOPIC_MIN_CHARS:
        contained = [
            p
            for p in candidates
            if p.title_norm
            and len(p.title_norm) >= CONTAIN_TITLE_MIN_CHARS
            and p.title_norm in wanted
        ]
        if len(contained) == 1:
            point = contained[0]
            return LearnTarget(point.id, point.title, point.document_id, "contained")
        if len(contained) > 1:
            return LearnTarget(None, "", None, "ambiguous")

    return LearnTarget(None, "", None, "none")



def merge_duplicates(records: Sequence[KPRecord]) -> list[KPRecord]:
    """第二层去重：归一化标题精确匹配则合并。

    合并时的字段取舍（见 SKILL §9）：
    details/key_points/tags/source 取并集，difficulty/importance 取较大者，confidence 取较小者。
    """
    merged: dict[str, KPRecord] = {}
    order: list[str] = []

    for record in records:
        key = record.title_norm
        existing = merged.get(key)
        if existing is None:
            merged[key] = record
            order.append(key)
            continue

        # details：去重后拼接
        if record.details and record.details not in existing.details:
            existing.details = f"{existing.details}\n\n{record.details}".strip()

        existing.key_points = _unique(existing.key_points + record.key_points)[:MAX_KEY_POINTS]
        existing.tags = _unique(existing.tags + record.tags)[:MAX_TAGS]
        existing.source_chunk_indexes = sorted(
            set(existing.source_chunk_indexes) | set(record.source_chunk_indexes)
        )
        existing.source_pages = sorted(set(existing.source_pages) | set(record.source_pages))
        existing.difficulty = max(existing.difficulty, record.difficulty)
        existing.importance = max(existing.importance, record.importance)
        existing.confidence = min(existing.confidence, record.confidence)
        if not existing.summary and record.summary:
            existing.summary = record.summary

    result = [merged[k] for k in order]
    for index, record in enumerate(result):
        record.order_index = index
    return result


def _unique(items: Iterable[str]) -> list[str]:
    """保序去重。"""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = (item or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


# --------------------------------------------------------------------------- #
# 批次组装
# --------------------------------------------------------------------------- #
def is_extractable(chunk: Chunk) -> bool:
    """该块是否值得送去抽取。

    图片块的 content 只是图注或占位路径，单独送抽取只会产出噪声；
    过短的块同理。
    """
    if chunk.block_type == str(BlockType.FIGURE):
        return False
    return len((chunk.content or "").strip()) >= MIN_CHUNK_CHARS


def build_batches(
    chunks: Sequence[Chunk],
    *,
    batch_chunks: int | None = None,
    batch_max_chars: int | None = None,
) -> list[list[Chunk]]:
    """按章节聚合成批次。

    同一 heading_path 的相邻块优先合到一批，这样抽出的知识点在该章节内自洽。
    超出块数或字符数上限时从块边界切分（不切块本身）。
    """
    limit_chunks = batch_chunks or settings.extract_batch_chunks
    limit_chars = batch_max_chars or settings.extract_batch_max_chars

    batches: list[list[Chunk]] = []
    current: list[Chunk] = []
    current_chars = 0
    current_key: tuple[str, ...] | None = None

    for chunk in chunks:
        if not is_extractable(chunk):
            continue

        key = tuple(chunk.heading_path or [])
        will_overflow = (
            current
            and (
                key != current_key
                or len(current) >= limit_chunks
                or current_chars + len(chunk.content) > limit_chars
            )
        )
        if will_overflow:
            batches.append(current)
            current = []
            current_chars = 0

        current.append(chunk)
        current_chars += len(chunk.content)
        current_key = key

    if current:
        batches.append(current)
    return batches


# --------------------------------------------------------------------------- #
# 提示词
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def load_prompt_template() -> tuple[str, str]:
    """加载并缓存提示词模板，返回 (system, user_template)。"""
    text = PROMPT_PATH.read_text(encoding="utf-8")
    if "--- USER ---" not in text:
        raise RuntimeError(f"提示词模板缺少 '--- USER ---' 分隔符：{PROMPT_PATH}")

    system_part, user_part = text.split("--- USER ---", 1)
    system = system_part.replace("--- SYSTEM ---", "", 1).strip()
    return system, user_part.strip()


def render_prompt(
    *,
    file_name: str,
    heading_path: list[str],
    existing_titles: list[str],
    batch: Sequence[Chunk],
) -> list[dict[str, str]]:
    """渲染提示词，返回 messages。"""
    system, user_template = load_prompt_template()

    titles = existing_titles[-MAX_EXISTING_TITLES:]
    titles_text = "\n".join(f"- {t}" for t in titles) if titles else "（暂无）"

    blocks = "\n\n".join(
        f"<<<chunk_index={c.chunk_index} | page={c.page_start}>>>\n{c.content.strip()}"
        for c in batch
    )

    user = (
        user_template.replace("{{file_name}}", file_name or "未命名文档")
        .replace("{{heading_path}}", " / ".join(heading_path) if heading_path else "（未识别章节）")
        .replace("{{existing_titles}}", titles_text)
        .replace("{{chunks}}", blocks)
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


# --------------------------------------------------------------------------- #
# 校验与回填
# --------------------------------------------------------------------------- #
def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def validate_and_backfill(
    items: Sequence[KPExtract],
    *,
    batch: Sequence[Chunk],
    heading_path: list[str],
    warnings: list[dict[str, Any]],
) -> list[KPRecord]:
    """校验模型输出并回填页码。

    回填是关键：source_pages 完全由 batch 里 chunk 的页码推导，模型没有机会编造。
    """
    by_index = {c.chunk_index: c for c in batch}
    records: list[KPRecord] = []
    dropped_source = 0

    for item in items:
        title = (item.title or "").strip()
        if not title:
            continue
        if len(title) > MAX_TITLE_CHARS:
            title = title[:MAX_TITLE_CHARS]

        # --- 溯源校验：必须引用本批次的块，越界一律丢弃 ---
        valid_indexes = sorted({i for i in item.source_chunk_indexes if i in by_index})
        if not valid_indexes:
            dropped_source += 1
            continue

        pages = sorted(
            {
                page
                for index in valid_indexes
                for page in range(
                    by_index[index].page_start, by_index[index].page_end + 1
                )
            }
        )

        records.append(
            KPRecord(
                title=title,
                title_norm=normalize_title(title),
                summary=(item.summary or "").strip()[:MAX_SUMMARY_CHARS],
                details=(item.details or "").strip(),
                key_points=_unique(item.key_points)[:MAX_KEY_POINTS],
                difficulty=clamp(int(item.difficulty or 3), 1, 5),
                importance=clamp(int(item.importance or 3), 1, 5),
                confidence=round(min(max(float(item.confidence or 0.8), 0.0), 1.0), 2),
                tags=_unique(item.tags)[:MAX_TAGS],
                heading_path=list(heading_path),
                source_chunk_indexes=valid_indexes,
                source_pages=pages,
            )
        )

    if dropped_source:
        warnings.append(
            {
                "code": WARN_INVALID_SOURCE,
                "message": f"有 {dropped_source} 条知识点因来源块越界被丢弃。",
            }
        )
    return records


# --------------------------------------------------------------------------- #
# mock 支持
# --------------------------------------------------------------------------- #
def build_mock_payload(batch: Sequence[Chunk], heading_path: list[str]) -> dict[str, Any]:
    """mock 模式下派生出的合法 JSON。

    直接从块内容生成结构合理的知识点，使整条流水线（校验 → 回填 → 去重 → 落库 → 前端渲染）
    都能在零 API 消耗下走通。这不是"假装成功"，而是给测试一个稳定的输入契约。
    """
    items: list[dict[str, Any]] = []
    for chunk in batch[: settings.extract_max_kp_per_batch]:
        first_line = next(
            (ln.strip() for ln in chunk.content.splitlines() if ln.strip()), ""
        )
        title = first_line[:20] or f"知识点 {chunk.chunk_index}"
        items.append(
            {
                # 前缀用于区分 mock 产出，避免与真实数据混淆
                "title": f"[MOCK] {title}",
                "summary": first_line[:60] or "（模拟摘要）",
                "details": f"## 模拟讲解\n\n{chunk.content[:200]}\n\n**要点**：这是 mock 模式生成的内容。",
                "key_points": ["模拟要点一", "模拟要点二"],
                # 用块序号派生，保证分布不是常数，便于验证标注维度确实被使用
                "difficulty": (chunk.chunk_index % 5) + 1,
                "importance": min(((chunk.chunk_index // 2) % 5) + 1, 5),
                "confidence": round(0.7 + (chunk.chunk_index % 3) * 0.1, 2),
                "tags": ["模拟数据"],
                "source_chunk_indexes": [chunk.chunk_index],
            }
        )
    return {"knowledge_points": items}


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def parse_response(payload: Any) -> list[KPExtract]:
    """解析模型返回的 JSON，兼容顶层直接是列表的情况。"""
    if isinstance(payload, list):
        payload = {"knowledge_points": payload}
    if not isinstance(payload, dict):
        raise ValueError(f"期望 JSON 对象，实际得到 {type(payload).__name__}")

    raw_items = payload.get("knowledge_points")
    if raw_items is None:
        raise ValueError("返回的 JSON 缺少 knowledge_points 字段")
    if not isinstance(raw_items, list):
        raise ValueError("knowledge_points 必须是数组")

    items: list[KPExtract] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        try:
            items.append(KPExtract.model_validate(raw))
        except ValidationError as exc:
            logger.debug("跳过校验失败的知识点：%s", exc)
    return items


async def extract_knowledge_points(
    chunks: Sequence[Chunk],
    *,
    file_name: str,
    llm: LLMGateway | None = None,
    on_progress: Callable[[int, str], None] | None = None,
    base_progress: int = 55,
    end_progress: int = 95,
) -> ExtractionResult:
    """从文本块中抽取知识点。

    on_progress(progress, detail) 在每个批次结束后回调，供流水线更新数据库状态。
    """
    gateway = llm or llm_gateway
    result = ExtractionResult()

    batches = build_batches(chunks)
    result.batch_count = len(batches)

    if not batches:
        result.warnings.append(
            {
                "code": WARN_NO_EXTRACTABLE_CHUNK,
                "message": "没有可用于抽取的文本块（可能整份资料都是图片或内容过短）。",
            }
        )
        return result

    existing_titles: list[str] = []
    collected: list[KPRecord] = []

    for index, batch in enumerate(batches, start=1):
        heading_path = list(batch[0].heading_path or [])
        messages = render_prompt(
            file_name=file_name,
            heading_path=heading_path,
            existing_titles=existing_titles,
            batch=batch,
        )

        try:
            payload = await gateway.chat_json(
                messages,
                max_tokens=settings.extract_llm_max_tokens,
                mock_builder=lambda _msgs, _b=batch, _h=heading_path: build_mock_payload(_b, _h),
            )
            items = parse_response(payload)
        except Exception as exc:  # noqa: BLE001 - 单批失败不牵连整体
            logger.warning("第 %d/%d 批抽取失败：%s", index, len(batches), exc)
            result.failed_batches += 1
            result.warnings.append(
                {
                    "code": WARN_LLM_PARSE_FAILED,
                    "message": f"第 {index}/{len(batches)} 批知识点抽取失败，已跳过：{exc}",
                }
            )
            _report(on_progress, base_progress, end_progress, index, len(batches))
            continue

        # ------------------------------------------------------ 质量闸门
        if not items:
            result.warnings.append(
                {
                    "code": WARN_EMPTY_BATCH,
                    "message": f"第 {index}/{len(batches)} 批未产出知识点。",
                }
            )
        elif len(items) > BATCH_KP_MAX:
            result.warnings.append(
                {
                    "code": WARN_TOO_FINE_GRAINED,
                    "message": (
                        f"第 {index}/{len(batches)} 批产出 {len(items)} 条知识点，"
                        f"超过 {BATCH_KP_MAX} 条，粒度可能过细。"
                    ),
                }
            )

        records = validate_and_backfill(
            items, batch=batch, heading_path=heading_path, warnings=result.warnings
        )
        collected.extend(records)
        existing_titles.extend(r.title for r in records)

        logger.info(
            "第 %d/%d 批完成：产出 %d 条（累计 %d 条）",
            index,
            len(batches),
            len(records),
            len(collected),
        )
        _report(on_progress, base_progress, end_progress, index, len(batches))

    # ------------------------------------------------------------ 第二层去重
    before = len(collected)
    result.records = merge_duplicates(collected)
    if before != len(result.records):
        logger.info("归一化去重合并了 %d 条重复知识点", before - len(result.records))

    # 整篇零产出时给出解释。否则用户只能看到一个空列表，无从判断是自己资料的问题
    # 还是系统的问题 —— 这条提示把"什么都没抽到"变成可排查的现象。
    if not result.records and batches:
        extractable = sum(1 for c in chunks if is_extractable(c))
        result.warnings.append(
            {
                "code": WARN_NO_KNOWLEDGE_POINT,
                "message": (
                    f"整份资料共 {len(batches)} 批 / {extractable} 个可用文本块，"
                    "但没有产出任何知识点。常见原因：文本层损坏导致内容不可读、"
                    "内容几乎全是图表或公式、或材料过于零碎。"
                ),
            }
        )

    return result


def _report(
    callback: Callable[[int, str], None] | None,
    base: int,
    end: int,
    index: int,
    total: int,
) -> None:
    if callback is None or total <= 0:
        return
    span = max(end - base, 0)
    progress = base + int(span * index / total)
    callback(progress, f"正在抽取知识点（第 {index}/{total} 批）")
