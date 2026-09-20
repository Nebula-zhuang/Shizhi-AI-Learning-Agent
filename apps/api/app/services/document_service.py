"""文档服务：文档的增删查改与状态流转。

刻意把「状态流转」集中在这里：摄取流水线只调用 set_status/mark_ready/mark_failed，
不直接改字段。这样状态机的合法性只有一个地方需要维护。
"""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.identity import DEFAULT_LEARNER_ID
from app.core.logging import get_logger
from app.ingestion import storage
from app.ingestion.base import SUPPORTED_EXTENSIONS
from app.models.chunk import Chunk
from app.models.document import IN_PROGRESS_STATUSES, Document, ParseStatus
from app.models.knowledge_point import KnowledgePoint

logger = get_logger(__name__)


def detect_file_type(file_name: str) -> str:
    """把扩展名映射为库里的 file_type 取值。"""
    ext = storage.extension_of(file_name)
    return SUPPORTED_EXTENSIONS.get(ext, "unknown")


def find_by_hash(db: Session, file_hash: str, *, learner_id: str) -> Document | None:
    """按内容 hash 查**这个账号自己**已有的文档（去重键）。

    去重必须限定在同一账号内。否则会出现两种错：
      · B 传了一个和 A 相同的文件 → 直接命中 A 的记录，B 什么都没拿到（甚至可能看到 A 的资料）；
      · 上传接口返回"已存在"，但那份根本不属于他。
    代价是同一个文件被不同账号各存一份、各解析一次 —— 对本地演示可以接受，
    而且这正是隐私隔离该付的成本。
    """
    return db.execute(
        select(Document).where(
            Document.file_hash == file_hash,
            Document.owner_learner_id == learner_id,
        )
    ).scalar_one_or_none()


def create_document(
    db: Session,
    *,
    file_name: str,
    file_size: int,
    file_hash: str,
    storage_path: str,
    owner_learner_id: str = DEFAULT_LEARNER_ID,
) -> Document:
    """创建一条 pending 状态的文档记录。

    `owner_learner_id` 默认 `local` —— 匿名上传（本机调试、接口文档试用、
    自动化测试）都落到这个档案上，不会因为"没有身份"而存不进去。
    """
    document = Document(
        file_name=file_name,
        file_type=detect_file_type(file_name),
        file_size=file_size,
        file_hash=file_hash,
        storage_path=storage_path,
        owner_learner_id=owner_learner_id,
        parse_status=ParseStatus.PENDING,
        progress=0,
        stage_detail="排队等待处理",
        warnings=[],
    )
    db.add(document)
    db.commit()
    db.refresh(document)
    logger.info("已创建文档记录 id=%s name=%s", document.id, document.file_name)
    return document


def get_document(
    db: Session, document_id: int, *, learner_id: str | None = None
) -> Document | None:
    """按 id 取文档。

    传了 `learner_id` 就**同时校验归属**：不属于这个账号的一律当作不存在（返回 None）。
    选择"当作不存在"而不是"抛出无权访问"，是为了不泄露"这个 id 确实存在"。
    """
    document = db.get(Document, document_id)
    if document is None:
        return None
    if learner_id is not None and document.owner_learner_id != learner_id:
        return None
    return document


def list_documents(
    db: Session, *, learner_id: str, limit: int = 20, offset: int = 0
) -> tuple[list[Document], int]:
    """按创建时间倒序分页，**只返回该账号自己的资料**。"""
    owned = Document.owner_learner_id == learner_id
    total = db.execute(select(func.count()).select_from(Document).where(owned)).scalar_one()
    rows = (
        db.execute(
            select(Document)
            .where(owned)
            .order_by(Document.created_at.desc(), Document.id.desc())
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    return list(rows), int(total)


def set_status(
    db: Session,
    document: Document,
    *,
    status: str,
    progress: int,
    stage_detail: str | None = None,
) -> None:
    """更新状态与进度。"""
    document.parse_status = status
    document.progress = max(0, min(100, progress))
    if stage_detail is not None:
        document.stage_detail = stage_detail
    db.commit()


def mark_ready(
    db: Session,
    document: Document,
    *,
    page_count: int,
    char_count: int,
    image_count: int,
    chunk_count: int,
    kp_count: int,
    warnings: list[dict] | None = None,
) -> None:
    """标记为完成，并回填统计。"""
    document.parse_status = ParseStatus.READY
    document.progress = 100
    document.stage_detail = f"处理完成，共 {kp_count} 个知识点"
    document.parse_error = None
    document.page_count = page_count
    document.char_count = char_count
    document.image_count = image_count
    document.chunk_count = chunk_count
    document.kp_count = kp_count
    document.warnings = warnings or []
    db.commit()


def mark_failed(
    db: Session,
    document: Document,
    *,
    message: str,
    stage_detail: str | None = None,
    warnings: list[dict] | None = None,
) -> None:
    """标记为失败。message 会直接展示给用户，必须是可读的自然语言。"""
    document.parse_status = ParseStatus.FAILED
    document.parse_error = message
    document.stage_detail = stage_detail or "处理失败"
    if warnings:
        document.warnings = warnings
    db.commit()
    logger.warning("文档 id=%s 处理失败：%s", document.id, message)


def purge_derived(db: Session, document: Document) -> tuple[int, int]:
    """清空该文档的切片与知识点（重跑前调用）。

    显式 DELETE 而不依赖级联：级联行为在 lazy="noload" 下依赖数据库外键，
    显式删除的语义更明确，也便于返回清理数量。
    """
    chunk_result = db.execute(delete(Chunk).where(Chunk.document_id == document.id))
    kp_result = db.execute(
        delete(KnowledgePoint).where(KnowledgePoint.document_id == document.id)
    )
    document.chunk_count = 0
    document.kp_count = 0
    db.commit()
    return int(chunk_result.rowcount or 0), int(kp_result.rowcount or 0)


def delete_document(db: Session, document: Document) -> bool:
    """删除文档及其全部派生物与磁盘文件。"""
    file_hash = document.file_hash
    document_id = document.id

    purge_derived(db, document)
    db.delete(document)
    db.commit()

    removed = storage.delete_document_files(file_hash)
    logger.info("已删除文档 id=%s（磁盘文件已清理=%s）", document_id, removed)
    return removed


def cleanup_stale_documents() -> int:
    """应用启动时清理因进程重启而中断的文档。

    进程内后台任务在重启时会丢失，若不清理，文档会永久停留在 parsing/chunking/extracting，
    前端一直转圈。这里把它们统一标记为失败并给出可操作提示，用户可一键重跑。

    使用独立会话；数据库不可用时只记日志，不阻断应用启动。
    """
    from app.db.session import SessionLocal

    try:
        with SessionLocal() as db:
            rows = (
                db.execute(
                    select(Document).where(Document.parse_status.in_(list(IN_PROGRESS_STATUSES)))
                )
                .scalars()
                .all()
            )
            for document in rows:
                document.parse_status = ParseStatus.FAILED
                document.parse_error = (
                    "服务在处理过程中重启，本次处理已中断。"
                    "点击「重新处理」可以重新开始。"
                )
                document.stage_detail = "因服务重启中断"
            if rows:
                db.commit()
                logger.warning("已清理 %d 个因重启中断的文档", len(rows))
            return len(rows)
    except Exception as exc:  # noqa: BLE001 - 不能阻断启动
        logger.warning("清理中断文档失败（不影响启动）：%s", exc)
        return 0
