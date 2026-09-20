"""最小 RAG：问题 → 向量化 → Top-K 检索 → 拼接上下文 → DeepSeek 回答 → 答案 + 来源。

链路刻意保持最短，可拆开的每一段都拆开了，便于单独测试与替换：

    embed(question)  →  chroma.query(top_k)  →  过滤 + 拼装上下文  →  llm.chat()  →  RagAnswer

三个关键设计：

1. **距离门槛（`RAG_MAX_DISTANCE`）不是可选项。**
   Top-K 永远会返回 K 条结果 —— 哪怕资料里根本没有相关内容。
   没有门槛的话，问一个资料外的问题也会检索出 K 条无关片段，
   模型面对一堆噪声只能硬编答案。有了门槛，无关片段被丢掉，
   上下文为空时直接告诉用户"资料里没有"，**连模型都不用调用**。

2. **来源片段带编号。**
   上下文里每个片段前缀 `[1] [2]`，提示词要求模型在结论后标编号 ——
   于是答案里的每个说法都能回查到具体哪份资料的第几页。

3. **引用优先级沿用 P1/P2 已确立的约定**：用户资料 > 模型通识。
   资料不足时必须明说「这部分不在你的资料中」，绝不用通识把答案补完整。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.llm import LLMGateway, llm_gateway
from app.core.logging import get_logger
from app.rag.embedding import EmbeddingError, EmbeddingErrorCode, EmbeddingProvider
from app.rag.embedding import embedder as default_embedder
from app.rag.vectorstore import VectorStore, vector_store as default_store

logger = get_logger(__name__)

PROMPT_PATH = Path(__file__).resolve().parent.parent / "agent" / "prompts" / "rag_answer.md"

#: 单条片段在上下文里的截断长度。太长会把上下文预算吃光，挤掉其他片段。
SOURCE_SNIPPET_CHARS = 1200
#: 返回给前端的片段原文长度（比上下文里短，够用户核对即可）
RETURN_SNIPPET_CHARS = 300

NO_CONTEXT_ANSWER = (
    "这部分不在你的资料中。\n\n"
    "检索没有找到与该问题相关的资料片段 —— 可能是资料里确实没有涉及，"
    "也可能你的说法与资料里的表述差异较大。可以换个问法，"
    "或先把相关章节的资料上传并建立索引。"
)


def _readable_llm_error(exc: Exception) -> str:
    """把模型调用异常翻译成人能看懂的一句话。

    余额不足是会被反复问到的类别（"为什么突然不能回答了"），单独识别出来，
    否则用户看到的是一串 OpenAI SDK 的原始错误。

    **中英文关键词都要认**：底层抛的是英文（OpenAI SDK），
    但我们自己包装的异常（如 `LLMError`）里可能是中文。
    """
    text = str(exc)
    lowered = text.lower()

    def hit(*keywords: str) -> bool:
        return any(k in text or k in lowered for k in keywords)

    if hit("insufficient balance", "402", "余额不足", "余额不够"):
        return "模型账户余额不足（HTTP 402）"
    if hit("401", "invalid api key", "authentication", "unauthorized", "凭据", "密钥无效"):
        return "模型凭据无效（HTTP 401）"
    if hit("429", "rate limit", "限流", "过于频繁"):
        return "模型调用被限流（HTTP 429）"
    if hit("timeout", "timed out", "超时"):
        return "模型调用超时"
    if hit("503", "502", "service unavailable", "暂时不可用"):
        return "模型服务暂时不可用"
    return f"模型调用失败：{type(exc).__name__}"


def _generation_failed_answer(error: str, used: int) -> str:
    """生成失败时的回答正文。

    刻意**不**编造答案，而是把"资料找到了、但模型没答上来"这件事讲清楚，
    并让用户直接看下面的来源片段 —— 至少他能自己读到原文。
    """
    return (
        f"找到了 {used} 条相关资料，但生成回答时模型不可用（{error}）。\n\n"
        "下面列出了检索到的来源片段，你可以直接查看原文。"
        "模型恢复后重新提问即可拿到完整的回答。"
    )


class RagError(RuntimeError):
    """RAG 链路的可读错误。`status_code` 供路由直接使用。"""

    def __init__(self, message: str, *, status_code: int = 502, code: str = "rag_error") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


@dataclass
class RagSource:
    """一条检索到的来源片段。"""

    #: 上下文里的编号，答案中的 [1] [2] 与它对应
    index: int
    document_id: int
    file_name: str
    chunk_index: int
    page_start: int
    page_end: int
    block_type: str
    heading_path: list[str]
    distance: float
    #: 片段原文（截断），供前端展示与人工核对
    content: str
    #: 是否被真正放进上下文（受长度预算与距离门槛影响）
    used: bool = True
    #: 未使用的原因
    dropped_reason: str = ""

    @property
    def page_label(self) -> str:
        if self.page_start == self.page_end:
            return f"第 {self.page_start} 页"
        return f"第 {self.page_start}-{self.page_end} 页"

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "document_id": self.document_id,
            "file_name": self.file_name,
            "chunk_index": self.chunk_index,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "page_label": self.page_label,
            "block_type": self.block_type,
            "heading_path": self.heading_path,
            "distance": round(self.distance, 4),
            "content": self.content,
            "used": self.used,
            "dropped_reason": self.dropped_reason,
        }


@dataclass
class RagAnswer:
    """一次问答的完整结果。"""

    question: str
    #: 回答正文。先构造结果对象再逐步填充，所以给默认值。
    answer: str = ""
    sources: list[RagSource] = field(default_factory=list)
    model: str = ""
    mode: str = ""
    #: 检索到（过门槛前）的片段数
    retrieved: int = 0
    #: 实际放进上下文的片段数
    used: int = 0
    context_chars: int = 0
    timings: dict[str, int] = field(default_factory=dict)
    #: 是否因为资料里没有相关内容而没有调用模型
    short_circuited: bool = False
    #: 检索成功但生成失败（模型不可用 / 余额不足 / 超时）
    generation_failed: bool = False
    #: 失败原因（人可读）。`generation_failed` 为真时才有值。
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.answer,
            "sources": [s.as_dict() for s in self.sources],
            "model": self.model,
            "mode": self.mode,
            "retrieved": self.retrieved,
            "used": self.used,
            "context_chars": self.context_chars,
            "timings_ms": self.timings,
            "short_circuited": self.short_circuited,
            "generation_failed": self.generation_failed,
            "error": self.error,
        }


# --------------------------------------------------------------------------- #
# 提示词
# --------------------------------------------------------------------------- #
def load_rag_prompt() -> tuple[str, str]:
    """加载问答提示词，返回 (system, user_template)。"""
    text = PROMPT_PATH.read_text(encoding="utf-8")
    if "--- USER ---" not in text:
        raise RuntimeError(f"RAG 提示词缺少 '--- USER ---' 分隔符：{PROMPT_PATH}")
    system_part, user_part = text.split("--- USER ---", 1)
    return system_part.replace("--- SYSTEM ---", "", 1).strip(), user_part.strip()


def render_rag_prompt(*, question: str, context: str) -> list[dict[str, str]]:
    system, template = load_rag_prompt()
    user = template.replace("{{context}}", context).replace("{{question}}", question)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def build_context(sources: Sequence[RagSource]) -> str:
    """把片段拼成带编号的上下文。

    编号是刚需：提示词要求模型在结论后标 `[1]`，用户才能回查到具体出处。
    没有编号的话，"来源"就只是几个链接，用户无法判断哪句话出自哪里。
    """
    blocks: list[str] = []
    for source in sources:
        if not source.used:
            continue
        header = f"[{source.index}] 《{source.file_name}》{source.page_label}"
        if source.heading_path:
            header += f" · {' / '.join(source.heading_path)}"
        blocks.append(f"{header}\n{source.content}")
    return "\n\n".join(blocks)


# --------------------------------------------------------------------------- #
# 检索
# --------------------------------------------------------------------------- #
def _parse_heading(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    try:
        parsed = json.loads(str(raw))
        return [str(x) for x in parsed] if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


def _to_source(hit: dict[str, Any], index: int) -> RagSource:
    metadata = hit.get("metadata") or {}
    content = str(hit.get("document") or "")
    return RagSource(
        index=index,
        document_id=int(metadata.get("document_id") or 0),
        file_name=str(metadata.get("file_name") or ""),
        chunk_index=int(metadata.get("chunk_index") or 0),
        page_start=int(metadata.get("page_start") or 0),
        page_end=int(metadata.get("page_end") or 0),
        block_type=str(metadata.get("block_type") or "text"),
        heading_path=_parse_heading(metadata.get("heading_path")),
        distance=float(hit.get("distance") if hit.get("distance") is not None else 1.0),
        content=content[:RETURN_SNIPPET_CHARS],
    )


def retrieve(
    query_vector: list[float],
    *,
    top_k: int,
    document_ids: Sequence[int] | None = None,
    store: VectorStore | None = None,
    max_distance: float | None = None,
) -> tuple[list[RagSource], int]:
    """Top-K 检索 + 距离门槛过滤。返回 (入选片段, 过门槛前的总数)。

    过滤掉的片段也会保留在结果里（`used=False`），这样用户能看到
    "检索到了什么、为什么没用上"，而不是对着空来源发呆。
    """
    vectors = store or default_store
    threshold = settings.rag_max_distance if max_distance is None else max_distance

    where: dict[str, Any] | None = None
    if document_ids:
        ids = [int(x) for x in document_ids]
        # 单个 id 用等值，多个用 $in —— Chroma 的过滤语法要求如此
        where = {"document_id": ids[0]} if len(ids) == 1 else {"document_id": {"$in": ids}}

    hits = vectors.query(query_vector, top_k=top_k, where=where)

    sources: list[RagSource] = []
    for order, hit in enumerate(hits, start=1):
        source = _to_source(hit, order)
        if source.distance > threshold:
            source.used = False
            source.dropped_reason = f"相似度不足（距离 {source.distance:.3f} > 门槛 {threshold}）"
        sources.append(source)

    # 入选的重新编号，保证上下文编号连续（否则会出现 [1] [3] 这种断号）
    kept = 0
    for source in sources:
        if source.used:
            kept += 1
            source.index = kept
    return sources, len(hits)


def apply_context_budget(sources: list[RagSource], max_chars: int) -> int:
    """按相似度顺序填充上下文，超出预算的标记为未使用。返回使用的字符数。

    片段已按距离升序排列（Chroma 返回即有序），所以从前往后填即可 ——
    最相关的优先入选，超预算的丢掉。
    """
    used_chars = 0
    for source in sources:
        if not source.used:
            continue
        text = source.content[:SOURCE_SNIPPET_CHARS]
        if used_chars + len(text) > max_chars and used_chars > 0:
            source.used = False
            source.dropped_reason = "上下文长度预算已用尽"
            continue
        source.content = text
        used_chars += len(text)
    return used_chars


# --------------------------------------------------------------------------- #
# 主链路
# --------------------------------------------------------------------------- #
async def answer_question(
    db: Session | None,
    question: str,
    *,
    document_ids: Sequence[int] | None = None,
    top_k: int | None = None,
    provider: EmbeddingProvider | None = None,
    store: VectorStore | None = None,
    llm: LLMGateway | None = None,
) -> RagAnswer:
    """最小 RAG 主链路。

    `db` 目前不参与检索（元数据都从向量库里取），保留该参数是为了
    后续按用户/课程做过滤时不必改签名。
    """
    del db  # 显式声明当前未使用

    text = (question or "").strip()
    if not text:
        raise RagError("问题不能为空。", status_code=422, code="empty_question")

    embed = provider or default_embedder
    vectors = store or default_store
    gateway = llm or llm_gateway
    limit = int(top_k or settings.rag_top_k)

    result = RagAnswer(question=text, model=gateway.model, mode=gateway.mode)

    # ------------------------------------------------------- 1. 问题向量化
    started = time.perf_counter()
    try:
        embedded = await embed.embed([text])
    except EmbeddingError as exc:
        raise _rag_error_from_embedding(exc) from exc
    result.timings["embed"] = int((time.perf_counter() - started) * 1000)

    if not embedded.vectors:
        raise RagError("问题向量化没有返回结果。", code="embed_empty")

    # ----------------------------------------------------------- 2. Top-K
    started = time.perf_counter()
    try:
        sources, retrieved = retrieve(
            embedded.vectors[0],
            top_k=limit,
            document_ids=document_ids,
            store=vectors,
        )
    except EmbeddingError as exc:  # pragma: no cover - 检索不抛这个，防御性
        raise _rag_error_from_embedding(exc) from exc
    except Exception as exc:  # noqa: BLE001 - 向量库不可用要给出可读提示
        raise RagError(
            f"向量检索失败：{type(exc).__name__}: {exc}", status_code=503, code="retrieve_failed"
        ) from exc
    result.timings["retrieve"] = int((time.perf_counter() - started) * 1000)
    result.retrieved = retrieved

    # ------------------------------------------- 3. 上下文预算 + 空上下文短路
    result.context_chars = apply_context_budget(sources, settings.rag_max_context_chars)
    result.sources = sources
    result.used = sum(1 for s in sources if s.used)

    if result.used == 0:
        # 资料里没有相关内容 —— 直接给结论，**不调用模型**。
        # 让模型面对空上下文去回答，只会得到一段编造的内容。
        logger.info("问题「%s」没有命中任何相关资料片段，跳过模型调用", text[:30])
        result.answer = NO_CONTEXT_ANSWER
        result.short_circuited = True
        result.mode = "retrieval-only"
        return result

    # ------------------------------------------------------- 4. 生成回答
    context = build_context(sources)
    messages = render_rag_prompt(question=text, context=context)

    started = time.perf_counter()
    try:
        llm_result = await gateway.chat(
            messages,
            temperature=settings.rag_temperature,
            max_tokens=settings.rag_max_tokens,
        )
    except Exception as exc:  # noqa: BLE001 - 生成失败必须降级，不能让整次问答白费
        # 检索已经成功、资料就在手边，此时因为模型不可用而抛 500 是最差的选择：
        # 用户问了一个资料里确实有的问题，却既拿不到答案也拿不到来源。
        # 余额不足、限流、超时都是常规运维事件，应当如实告知并**把来源交出去**。
        result.timings["generate"] = int((time.perf_counter() - started) * 1000)
        result.generation_failed = True
        result.error = _readable_llm_error(exc)
        result.mode = "retrieval-only"
        result.answer = _generation_failed_answer(result.error, result.used)
        logger.warning("RAG 生成失败，已降级为只返回检索结果：%s", exc)
        return result

    result.timings["generate"] = int((time.perf_counter() - started) * 1000)
    result.answer = llm_result.content.strip()
    result.model = llm_result.model
    result.mode = llm_result.mode

    logger.info(
        "RAG 完成：命中 %d 条 / 采用 %d 条 / 上下文 %d 字 / 耗时 %s",
        result.retrieved,
        result.used,
        result.context_chars,
        result.timings,
    )
    return result


def _rag_error_from_embedding(exc: EmbeddingError) -> RagError:
    """把向量化错误翻译成对用户有意义的 HTTP 状态与提示。"""
    if exc.code == EmbeddingErrorCode.NOT_CONFIGURED:
        return RagError(
            "未配置云端 Embedding 凭据，无法执行语义检索。"
            "请在 .env 中设置 EMBEDDING_API_KEY 后重启后端。",
            status_code=409,
            code="embedding_not_configured",
        )
    if exc.code == EmbeddingErrorCode.UNSUPPORTED:
        return RagError(exc.message, status_code=409, code="embedding_unsupported")
    if exc.code == EmbeddingErrorCode.TIMEOUT:
        return RagError(f"向量化超时：{exc.message}", status_code=503, code="embedding_timeout")
    if exc.code == EmbeddingErrorCode.HTTP_ERROR:
        return RagError(f"向量化服务返回错误：{exc.message}", status_code=503, code="embedding_http_error")
    return RagError(f"向量化失败：{exc.message}", status_code=502, code=str(exc.code))


def capabilities() -> dict[str, Any]:
    """RAG 能力探测。**只暴露布尔值与参数，绝不回显 Key。**"""
    embed = default_embedder
    return {
        "embedding": embed.capabilities(),
        "llm_model": llm_gateway.model,
        "llm_mode": llm_gateway.mode,
        "top_k": settings.rag_top_k,
        "max_distance": settings.rag_max_distance,
        "max_context_chars": settings.rag_max_context_chars,
        "temperature": settings.rag_temperature,
    }


async def retrieve_knowledge(
    question: str,
    *,
    document_ids: Sequence[int] | None = None,
    top_k: int | None = None,
    provider: EmbeddingProvider | None = None,
    store: VectorStore | None = None,
    max_distance: float | None = None,
) -> list[RagSource]:
    """`question` → 向量化 → Top-K 检索，返回通过距离门槛的来源片段。

    这是 P4 教学闭环里的 `retrieve_knowledge` 工具。它**没有另写一套检索**，
    只是把 P3 已有的 `embed` + `retrieve` 组合暴露成一个入口 ——
    排序、距离门槛、元数据解析全部复用 P3 的实现。

    检索失败时抛异常（`EmbeddingError` 或向量库异常），
    由调用方（P4 的 `ToolRunner`）统一降级 —— 检索不到资料不该让整轮学习中断。
    """
    embed = provider or default_embedder
    vectors = store or default_store
    limit = int(top_k or settings.rag_top_k)

    query = (question or "").strip()
    if not query:
        return []

    embedded = await embed.embed([query])
    if not embedded.vectors:
        return []

    sources, _ = retrieve(
        embedded.vectors[0],
        top_k=limit,
        document_ids=document_ids,
        store=vectors,
        max_distance=max_distance,
    )
    # 只把真正入选的片段交给教学环节 —— 被门槛挡掉的片段不该进提示词，
    # 否则等于把"相似度不足"的内容当成资料喂给模型。
    return [source for source in sources if source.used]
