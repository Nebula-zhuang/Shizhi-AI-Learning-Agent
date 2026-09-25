"""文档路由：上传、列表、详情、状态轮询、重跑、删除、结构、切片、图片。

上传是「接收即返回」模式：校验通过后立刻返回 202，真正的解析与抽取在后台进行，
前端通过 /status 轮询进度。这样即使是一份 50 页 PDF 也不会让请求超时。

**上传走流式落盘**（见 `_upload_chunks` 与 `storage.stream_to_temp`）：
文件上限提到 300MB 之后，再一次性读进内存会直接变成内存问题，
所以改成边收边写、边算哈希，全程内存占用固定在 1MB 量级。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import get_db
from app.ingestion import storage
from app.ingestion.base import SUPPORTED_FORMAT_HINT, ParserError
from app.ingestion.router import validate_upload
from app.models.document import IN_PROGRESS_STATUSES, Document, ParseStatus
from app.schemas.document import (
    ChunkItem,
    ChunkListResponse,
    DocumentDetail,
    DocumentListResponse,
    DocumentStatus,
    DocumentSummary,
    UploadResponse,
)
from app.api.deps import current_learner_id
from app.services import document_service, ingest_runner

logger = get_logger(__name__)

router = APIRouter(prefix="/documents", tags=["documents"])


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _get_or_404(db: Session, document_id: int, learner_id: str) -> Document:
    """取文档，**同时校验归属**。

    不属于当前账号时一律按"不存在"处理（404），而不是"无权访问"（403）——
    403 会暴露"这个 id 确实存在"，等于给了一个探测别人资料数量的接口。
    """
    document = document_service.get_document(db, document_id, learner_id=learner_id)
    if document is None:
        raise HTTPException(status_code=404, detail=f"文档 {document_id} 不存在。")
    return document


# --------------------------------------------------------------------------- #
# 上传
# --------------------------------------------------------------------------- #
async def _upload_chunks(file: UploadFile) -> AsyncIterator[bytes]:
    """把 `UploadFile` 变成一个按块产出的异步流。

    单独抽出来是为了让 `storage` 层不必知道 FastAPI 的存在 ——
    它只要求"给我一个 async 迭代器"，测试里塞一个假迭代器就能验超限逻辑。
    """
    while True:
        chunk = await file.read(storage.UPLOAD_CHUNK)
        if not chunk:
            return
        yield chunk


@router.post(
    "",
    status_code=202,
    response_model=UploadResponse,
    summary="上传学习资料（异步处理）",
)
async def upload_document(
    file: UploadFile = File(..., description=f"支持 {SUPPORTED_FORMAT_HINT}"),
    db: Session = Depends(get_db),
    learner_id: str = Depends(current_learner_id),
) -> UploadResponse:
    """接收文件并投递后台处理。

    命中内容去重时不会重复解析，直接返回既有记录 —— 同一份资料反复上传是常见行为，
    每次都重跑一遍 LLM 抽取既慢又费钱。

    **文件是边收边写盘的，不整份进内存。** 300MB 上限下这一点是硬要求：
    整份读进来时，一次上传就要常驻 300MB+（读一份、算哈希一份、写盘时又一份），
    并发几个直接 OOM。流式之后内存占用固定在 1MB 量级。

    哈希在写入过程中顺带算出，因此去重判断不需要再把文件读第二遍。
    """
    file_name = storage.sanitize_filename(file.filename or "untitled")
    max_bytes = settings.max_upload_mb * 1024 * 1024

    # 临时文件必须在所有失败路径上被清掉，所以用 try/finally 兜住整个流程。
    # `temp_path` 在"已归位"或"已丢弃"之后会被置空，避免 finally 误删正式文件。
    temp_path: Path | None = None
    try:
        try:
            temp_path, size, file_hash = await storage.stream_to_temp(
                _upload_chunks(file), max_bytes=max_bytes
            )
        except storage.UploadTooLarge as exc:
            # 413 比 400 准确：请求本身格式没错，是实体太大
            raise HTTPException(
                status_code=413,
                detail=(
                    f"文件超过 {settings.max_upload_mb}MB 上限"
                    f"（已收到 {exc.received / 1024 / 1024:.0f}MB，接收已中断）。"
                    "请压缩或拆分后再上传。"
                ),
            ) from exc

        # 快速校验：类型 / 大小 / PDF 页数与加密状态。
        # 这里从**路径**读，而不是把 300MB 再取回内存。
        try:
            validate_upload(temp_path, file_name=file_name, size=size)
        except ParserError as exc:
            raise HTTPException(status_code=400, detail=exc.message) from exc

        existing = document_service.find_by_hash(db, file_hash, learner_id=learner_id)
        if existing is not None:
            storage.discard_temp(temp_path)
            temp_path = None
            created = existing.created_at.strftime("%Y-%m-%d %H:%M")
            logger.info("命中去重：%s -> document_id=%s", file_name, existing.id)
            return UploadResponse(
                document=DocumentSummary.model_validate(existing),
                dedup=True,
                message=f"该文件已于 {created} 处理过，直接复用已有结果，未重复解析。",
            )

        # 原子改名归位。走完这一步文件就属于正式的存储目录了，
        # 因此立刻把 temp_path 置空 —— 后面任何异常都不该再删它。
        storage_path = storage.finalize_source(temp_path, file_hash, file_name)
        temp_path = None

        try:
            document = document_service.create_document(
                db,
                owner_learner_id=learner_id,
                file_name=file_name,
                file_size=size,
                file_hash=file_hash,
                storage_path=storage_path,
            )
        except IntegrityError as exc:
            # `documents.file_hash` 是**全局**唯一，而去重查询只在自己账号内找
            # （`document_service.find_by_hash` 的 docstring 写明了：
            #  去重**必须**限定同账号，代价是同一文件各存一份，这是隐私隔离的成本）。
            # 两条规则叠在一起，别的账号传过同一份内容时，这里的 INSERT 必然撞键 ✗
            #
            # 处理原则 —— **绝不跨账号复用**：
            #   · 复用会把别人的 document_id 交给当前用户，而所有查询都按归属过滤，
            #     他既打不开（404），又可能从提示语里推断出"别人传过什么" ✗
            #   · 所以跨账号一律 409，把原因说清楚，而不是 500
            db.rollback()
            mine = document_service.find_by_hash(db, file_hash, learner_id=learner_id)
            if mine is not None:
                # 自己账号内的**并发**重复投递（两次上传同时走到这里）
                created_at = mine.created_at.strftime("%Y-%m-%d %H:%M")
                logger.info("并发去重命中：%s -> document_id=%s", file_name, mine.id)
                return UploadResponse(
                    document=DocumentSummary.model_validate(mine),
                    dedup=True,
                    message=f"该文件已于 {created_at} 处理过，直接复用已有结果，未重复解析。",
                )
            logger.info("跨账号重复内容被拒：%s（file_hash 已属于其它账号）", file_name)
            raise HTTPException(
                status_code=409,
                detail=(
                    "这份文件的内容和已有资料完全重复，但它归另一个账号所有，"
                    "本账号不能复用。请对文件稍作修改（例如加一行自己的笔记）后再上传。"
                ),
            ) from exc

        if not ingest_runner.submit(document.id):
            document_service.mark_failed(
                db,
                document,
                message="服务当前无法接收处理任务，请稍后重试。",
                stage_detail="任务投递失败",
            )
            raise HTTPException(status_code=503, detail="服务当前无法接收处理任务，请稍后重试。")

        return UploadResponse(
            document=DocumentSummary.model_validate(document),
            dedup=False,
            message="已接收，正在后台解析与抽取知识点。",
        )
    finally:
        storage.discard_temp(temp_path)


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
@router.get("", response_model=DocumentListResponse, summary="文档列表")
def list_documents(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    learner_id: str = Depends(current_learner_id),
) -> DocumentListResponse:
    documents, total = document_service.list_documents(
        db, learner_id=learner_id, limit=limit, offset=offset
    )
    return DocumentListResponse(
        items=[DocumentSummary.model_validate(d) for d in documents], total=total
    )


@router.get("/{document_id}", response_model=DocumentDetail, summary="文档详情")
def get_document(document_id: int, db: Session = Depends(get_db),
    learner_id: str = Depends(current_learner_id),
) -> DocumentDetail:
    document = _get_or_404(db, document_id, learner_id)
    return DocumentDetail.model_validate(document)


@router.get(
    "/{document_id}/status",
    response_model=DocumentStatus,
    summary="处理进度（供前端轮询）",
)
def get_status(document_id: int, db: Session = Depends(get_db),
    learner_id: str = Depends(current_learner_id),
) -> DocumentStatus:
    document = _get_or_404(db, document_id, learner_id)
    return DocumentStatus(
        document_id=document.id,
        parse_status=document.parse_status,  # type: ignore[arg-type]
        progress=document.progress,
        stage_detail=document.stage_detail,
        chunk_count=document.chunk_count,
        kp_count=document.kp_count,
        parse_error=document.parse_error,
        warning_count=len(document.warnings or []),
    )


# --------------------------------------------------------------------------- #
# 操作
# --------------------------------------------------------------------------- #
@router.post(
    "/{document_id}/reprocess",
    status_code=202,
    response_model=UploadResponse,
    summary="重新处理（清空派生数据后重跑）",
)
async def reprocess_document(document_id: int, db: Session = Depends(get_db),
    learner_id: str = Depends(current_learner_id),
) -> UploadResponse:
    """重新跑一遍解析与抽取。

    用途：调整分块参数或抽取提示词后验证效果；处理因服务重启而中断的文档。

    注意：本路由必须是 async def。`ingest_runner.submit` 内部用
    `asyncio.create_task` 投递任务，而 FastAPI 会把同步路由放进线程池执行 ——
    那里没有运行中的事件循环，投递会失败。上传路由同为 async def 的原因也在此。
    """
    document = _get_or_404(db, document_id, learner_id)

    if document.parse_status in IN_PROGRESS_STATUSES:
        raise HTTPException(
            status_code=409, detail="该文档正在处理中，请等待当前处理结束再重跑。"
        )

    chunks, points = document_service.purge_derived(db, document)
    document.parse_status = ParseStatus.PENDING
    document.progress = 0
    document.stage_detail = "排队等待处理"
    document.parse_error = None
    db.commit()

    if not ingest_runner.submit(document.id):
        document_service.mark_failed(
            db, document, message="服务当前无法接收处理任务，请稍后重试。"
        )
        raise HTTPException(status_code=503, detail="服务当前无法接收处理任务，请稍后重试。")

    logger.info(
        "文档 id=%s 已重跑（清空 %d 个切片、%d 个知识点）", document.id, chunks, points
    )
    return UploadResponse(
        document=DocumentSummary.model_validate(document),
        dedup=False,
        message=f"已重新投递处理（清空了 {chunks} 个文本块与 {points} 个知识点）。",
    )


@router.delete("/{document_id}", summary="删除文档")
def delete_document(document_id: int, db: Session = Depends(get_db),
    learner_id: str = Depends(current_learner_id),
) -> dict:
    document = _get_or_404(db, document_id, learner_id)
    if document.parse_status in IN_PROGRESS_STATUSES:
        raise HTTPException(
            status_code=409, detail="该文档正在处理中，请等待处理结束后再删除。"
        )

    name = document.file_name
    removed = document_service.delete_document(db, document)
    return {
        "ok": True,
        "message": f"已删除《{name}》及其全部知识点。",
        "files_removed": removed,
    }


# --------------------------------------------------------------------------- #
# 内容
# --------------------------------------------------------------------------- #
@router.get("/{document_id}/structure", summary="统一文档结构")
def get_structure(document_id: int, db: Session = Depends(get_db),
    learner_id: str = Depends(current_learner_id),
) -> dict:
    """返回解析后的统一文档结构（含图片资源路径）。

    这是解析层的原始产物，前端用它做原文预览；内容较大，按需请求。
    """
    document = _get_or_404(db, document_id, learner_id)
    structure = storage.read_structure(document.file_hash)
    if structure is None:
        raise HTTPException(
            status_code=404,
            detail="该文档尚未生成解析结构（可能仍在处理中或处理失败）。",
        )
    return structure


@router.get("/{document_id}/chunks", response_model=ChunkListResponse, summary="文本块列表")
def list_chunks(
    document_id: int,
    page: int | None = Query(None, ge=1, description="只取包含该页码的块"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    learner_id: str = Depends(current_learner_id),
) -> ChunkListResponse:
    from sqlalchemy import func, select

    from app.models.chunk import Chunk

    _get_or_404(db, document_id, learner_id)

    conditions = [Chunk.document_id == document_id]
    if page is not None:
        conditions.append(Chunk.page_start <= page)
        conditions.append(Chunk.page_end >= page)

    total = db.execute(
        select(func.count()).select_from(Chunk).where(*conditions)
    ).scalar_one()

    rows = (
        db.execute(
            select(Chunk)
            .where(*conditions)
            .order_by(Chunk.chunk_index)
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    return ChunkListResponse(
        items=[ChunkItem.model_validate(c) for c in rows], total=int(total)
    )


@router.get("/{document_id}/images/{file_name}", summary="读取提取出的图片")
def get_image(document_id: int, file_name: str, db: Session = Depends(get_db),
    learner_id: str = Depends(current_learner_id),
) -> FileResponse:
    """读取 PDF 中提取出的内嵌图片。

    file_name 只允许是纯文件名（不含路径分隔符），杜绝目录穿越。
    """
    document = _get_or_404(db, document_id, learner_id)

    if Path(file_name).name != file_name or not file_name:
        raise HTTPException(status_code=400, detail="非法的文件名。")

    try:
        absolute = storage.resolve(f"{document.file_hash}/images/{file_name}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="非法的文件路径。") from exc

    if not absolute.is_file():
        raise HTTPException(status_code=404, detail="图片不存在。")

    return FileResponse(absolute)
