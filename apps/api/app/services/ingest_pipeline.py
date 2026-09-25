"""摄取流水线（Workflow A 的 P1 切片）。

    parsing(10→40) → chunking(40→55) → extracting(55→95) → ready(100)
    任一阶段异常 → failed

设计要点：
1. **CPU 密集的解析与分块放到线程里跑**（asyncio.to_thread）。
   否则一个 50 页 PDF 的解析会卡住整个事件循环，健康检查与 SSE 都会超时。
2. **局部失败不牵连整体**：单页解析失败、单批抽取失败都只记 warning，其余照常产出。
3. **进度真实可观测**：每个阶段与每个抽取批次都回写数据库，前端不靠猜。
"""

from __future__ import annotations

import asyncio

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.llm import llm_gateway
from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.ingestion.base import ParserError
from app.ingestion.chunker import chunk_document
from app.ingestion.router import parse_stored
from app.models.chunk import Chunk
from app.models.document import Document, ParseStatus
from app.models.knowledge_point import KnowledgePoint
from app.services import document_service
from app.services.knowledge_service import ExtractionResult, extract_knowledge_points

logger = get_logger(__name__)

# 进度区间。集中定义便于调整，前端只消费 progress 数值。
_P_PARSE_START = 10
_P_PARSE_END = 40
_P_CHUNK_END = 55
_P_EXTRACT_START = 55
_P_EXTRACT_END = 95


async def run_pipeline(document_id: int) -> None:
    """对一份文档执行完整的摄取流水线。使用独立数据库会话。"""
    with SessionLocal() as db:
        document = document_service.get_document(db, document_id)
        if document is None:
            logger.warning("文档 id=%s 不存在，跳过处理", document_id)
            return

        try:
            await _run(db, document)
        except ParserError as exc:
            # 解析类错误：message 是写给用户看的
            document_service.mark_failed(
                db,
                document,
                message=exc.message,
                stage_detail="解析失败",
            )
        except Exception as exc:  # noqa: BLE001 - 兜底，避免任务静默消失
            logger.exception("文档 id=%s 处理时出现未预期错误", document_id)
            document_service.mark_failed(
                db,
                document,
                message=f"处理过程中出现未预期的错误：{type(exc).__name__}: {exc}",
                stage_detail="内部错误",
            )


async def _run(db: Session, document: Document) -> None:
    file_hash = document.file_hash
    file_name = document.file_name

    # ------------------------------------------------------------ 幂等保障
    # 先清空该文档的全部派生物。这样无论从哪个入口进入流水线
    # （首次处理 / 用户点重新处理 / 重复投递的任务），
    # 都不会撞上 chunks 与 knowledge_points 的唯一约束。
    # 「重复投递」在进程内任务执行器下并非不可能（例如用户快速连点两次重跑）。
    purged_chunks, purged_points = document_service.purge_derived(db, document)
    if purged_chunks or purged_points:
        logger.info(
            "文档 id=%s 重跑：已清空 %d 个文本块、%d 个知识点",
            document.id,
            purged_chunks,
            purged_points,
        )

    # ---------------------------------------------------------------- 解析
    # 源文件在上传阶段已落盘，这里不重复读取与写入
    document_service.set_status(
        db,
        document,
        status=ParseStatus.PARSING,
        progress=_P_PARSE_START,
        stage_detail="正在解析文档结构与图片",
    )
    parsed, structure_path = await asyncio.to_thread(
        parse_stored,
        file_name=file_name,
        file_hash=file_hash,
        storage_path=document.storage_path,
    )
    document.structure_path = structure_path
    db.commit()

    # ---------------------------------------------------------------- 分块
    document_service.set_status(
        db,
        document,
        status=ParseStatus.CHUNKING,
        progress=_P_PARSE_END,
        stage_detail=f"正在切分文本（共 {parsed.page_count} 页）",
    )
    drafts = await asyncio.to_thread(
        chunk_document,
        parsed,
        target_chars=settings.chunk_target_chars,
        max_chars=settings.chunk_max_chars,
        min_chars=settings.chunk_min_chars,
        overlap_chars=settings.chunk_overlap_chars,
    )
    if not drafts:
        raise ParserError(
            "解析后没有得到任何可用的文本内容，无法抽取知识点。"
            "请确认该资料包含文字（纯图片资料在 P1 阶段无法处理）。",
            code="NO_CONTENT",
        )

    # ----------------------------------------------------- 抽取容量闸门
    #
    # **上传上限与解析能力是两个不同的限制。**
    #
    # 把文件上限提到 300MB 解决的是"传输能不能收下"，但知识点抽取是
    # **按批次调模型**的：一份 35MB 的纯文本切分后会有一万多个块、约五千批 ——
    # 即使并发跑也要几个小时，还会烧掉大量 API 配额。
    #
    # PDF 本来就有 `max_document_pages`（默认 50 页）兜着，
    # 而 docx / md / txt 没有等价闸门，所以这里统一补上。
    #
    # 放在 `_persist_chunks` **之前**：否则会先把两万行块写进库再失败，
    # 白占空间还得再清理。
    batches = (len(drafts) + settings.extract_batch_chunks - 1) // settings.extract_batch_chunks
    if batches > settings.max_extract_batches:
        raise ParserError(
            f"这份资料切分后有 {len(drafts)} 个文本块（约 {batches} 批），"
            f"超过单文档处理上限 {settings.max_extract_batches} 批。"
            "强行处理需要上万个模型请求，既慢也不划算 —— 请拆分后再上传。",
            code="TOO_MANY_CHUNKS",
        )

    _persist_chunks(db, document, drafts)
    document_service.set_status(
        db,
        document,
        status=ParseStatus.EXTRACTING,
        progress=_P_CHUNK_END,
        stage_detail=f"已切分 {len(drafts)} 个文本块，开始抽取知识点",
    )

    # ---------------------------------------------------------------- 抽取
    chunks = _load_chunks(db, document.id)
    result = await extract_knowledge_points(
        chunks,
        file_name=file_name,
        llm=llm_gateway,
        on_progress=_progress_reporter(db, document),
        base_progress=_P_EXTRACT_START,
        end_progress=_P_EXTRACT_END,
    )
    kp_count = _persist_knowledge_points(db, document, result)

    # ---------------------------------------------------------------- 完成
    warnings = list(parsed.meta.get("warnings") or []) + list(result.warnings)
    document_service.mark_ready(
        db,
        document,
        page_count=parsed.page_count,
        char_count=parsed.char_count,
        image_count=parsed.image_count,
        chunk_count=len(drafts),
        kp_count=kp_count,
        warnings=warnings,
    )
    logger.info(
        "文档 id=%s 处理完成：%d 页 / %d 块 / %d 知识点 / %d 条警告",
        document.id,
        parsed.page_count,
        len(drafts),
        kp_count,
        len(warnings),
    )

    # -------------------------------------------------------------- 自动索引
    # 「上传完就能拿资料问」是产品的核心承诺，而索引在此之前**只能**手动调
    # `POST /api/rag/index` —— 前端从不调用它，于是从界面上传的资料
    # 永远检索不到（Phase 6A 审计发现的 P0-2）。
    #
    # 这里**复用同一个 `index_runner`**，不复制任何索引逻辑 ✓
    # 它必须在事件循环里投递，而本函数正是 `async def`，由 `ingest_runner`
    # 的任务调用 ✓（`index_runner.submit` 的注释明确要求这一点）。
    #
    # ⚠️ 三处刻意的保守处理，都是为了"让索引少影响摄取"：
    #   1. **没有文本块就不投递**。索引空文档没有意义，而且会让**全局**索引状态
    #      变成 `failed`（`_guarded` 里 `stats.failed and not stats.documents`）
    #      —— 界面上会显得"资料全坏了"，比不索引更糟 ✗
    #   2. **投递失败不算摄取失败**。文档此刻已经是 READY，只是暂时没进向量库；
    #      忙的时候 `submit` 返回 False（已有索引任务在跑），用户稍后手动重试即可。
    #   3. **不让异常冒泡**。摄取任务绝不能因为"索引投递"而变成失败 ✗
    try:
        from app.services import index_runner  # 函数内导入：与 index_runner 自己的习惯一致，规避导入环

        if not document.chunk_count:
            logger.info("文档 id=%s 没有文本块，跳过索引", document.id)
        elif index_runner.submit([document.id]):
            logger.info("文档 id=%s 已自动投递索引任务", document.id)
        else:
            logger.warning(
                "文档 id=%s 未能自动索引（多半是已有索引任务在跑），"
                "可稍后手动 POST /api/rag/index 重试",
                document.id,
            )
    except Exception:  # noqa: BLE001 - 索引投递失败绝不能把摄取判成失败
        logger.exception("文档 id=%s 自动投递索引任务时出错（摄取本身已完成）", document.id)


def _progress_reporter(db: Session, document: Document):
    """返回一个把抽取进度写回数据库的回调。"""

    def report(progress: int, detail: str) -> None:
        try:
            document_service.set_status(
                db,
                document,
                status=ParseStatus.EXTRACTING,
                progress=progress,
                stage_detail=detail,
            )
        except Exception as exc:  # noqa: BLE001 - 进度写失败不该中断流水线
            logger.debug("写入进度失败（忽略）：%s", exc)

    return report


def _persist_chunks(db: Session, document: Document, drafts) -> None:
    """写入文本块。重跑时已由 purge_derived 清理过，这里直接插入。"""
    db.add_all(
        [
            Chunk(
                document_id=document.id,
                chunk_index=draft.chunk_index,
                content=draft.content,
                page_start=draft.page_start,
                page_end=draft.page_end,
                block_type=draft.block_type,
                heading_path=draft.heading_path or None,
                image_path=draft.image_path,
                char_count=draft.char_count,
                token_estimate=draft.token_estimate,
            )
            for draft in drafts
        ]
    )
    document.chunk_count = len(drafts)
    db.commit()


def _load_chunks(db: Session, document_id: int) -> list[Chunk]:
    from sqlalchemy import select

    return list(
        db.execute(
            select(Chunk)
            .where(Chunk.document_id == document_id)
            .order_by(Chunk.chunk_index)
        )
        .scalars()
        .all()
    )


def _persist_knowledge_points(db: Session, document: Document, result: ExtractionResult) -> int:
    """写入知识点。

    先整体提交（快），遇到唯一约束冲突再逐条降级插入（稳）。
    归一化去重已在抽取阶段做过，冲突属于异常情况，但仍不能让它导致整份文档失败。
    """
    if not result.records:
        document.kp_count = 0
        db.commit()
        return 0

    def build(record) -> KnowledgePoint:
        return KnowledgePoint(
            document_id=document.id,
            title=record.title,
            title_norm=record.title_norm,
            summary=record.summary,
            details=record.details,
            key_points=record.key_points or None,
            difficulty=record.difficulty,
            importance=record.importance,
            confidence=record.confidence,
            tags=record.tags or None,
            heading_path=record.heading_path or None,
            source_chunk_indexes=record.source_chunk_indexes or None,
            source_pages=record.source_pages or None,
            order_index=record.order_index,
        )

    from sqlalchemy.exc import IntegrityError

    objects = [build(record) for record in result.records]
    db.add_all(objects)
    try:
        db.commit()
        saved = len(objects)
    except IntegrityError as exc:
        db.rollback()
        logger.warning("知识点批量写入冲突，降级为逐条写入：%s", exc)
        saved = 0
        for obj in objects:
            db.add(obj)
            try:
                db.commit()
                saved += 1
            except IntegrityError:
                db.rollback()
                logger.debug("跳过重复知识点：%s", obj.title)

    document.kp_count = saved
    db.commit()
    return saved


__all__ = ["run_pipeline"]
