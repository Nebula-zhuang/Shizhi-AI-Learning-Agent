"""文档模型：一份上传的学习资料。

对应技术方案第九节的 documents 表。P1 只实现到「解析 + 抽取」，因此本表聚焦
摄取流水线的状态与统计；知识点内容在 knowledge_points 表。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, Index, String, Text, func
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.core.identity import DEFAULT_LEARNER_ID
from app.db.base_class import Base


class ParseStatus(StrEnum):
    """摄取流水线的状态。

    正常路径：PENDING → PARSING → CHUNKING → EXTRACTING → READY
    任一阶段失败：→ FAILED（此时 parse_error 记录原因，stage_detail 记录失败阶段）
    """

    PENDING = "pending"
    PARSING = "parsing"
    CHUNKING = "chunking"
    EXTRACTING = "extracting"
    READY = "ready"
    FAILED = "failed"


#: 处于中间态的状态集合。应用启动时据此清理因进程重启而中断的僵尸文档。
IN_PROGRESS_STATUSES: frozenset[str] = frozenset(
    {ParseStatus.PARSING, ParseStatus.CHUNKING, ParseStatus.EXTRACTING}
)

#: 终态集合
TERMINAL_STATUSES: frozenset[str] = frozenset({ParseStatus.READY, ParseStatus.FAILED})


class Document(Base):
    """一份学习资料。"""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    #: 归属账号的学习档案标识。
    #:
    #: **这是资料私有性的唯一开关。** 所有资料列表与按 id 的读取都必须带上它，
    #: 否则 A 上传的 PDF 会出现在 B 的资料库里。
    #: P6 之前没有账号概念，这一列是后补的；既有行统一回填成 `local`，
    #: 于是演示账号（其 learner_id 也是 `local`）仍然看得到历史样例资料。
    owner_learner_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default=DEFAULT_LEARNER_ID,
        index=True,
        comment="归属账号的学习档案标识",
    )

    file_name: Mapped[str] = mapped_column(String(255), nullable=False, comment="原始文件名")
    file_type: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="pdf / txt / md / image"
    )
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, comment="字节")
    file_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, comment="sha256，去重键"
    )
    storage_path: Mapped[str] = mapped_column(
        String(512), nullable=False, comment="源文件相对 UPLOAD_DIR 的路径"
    )
    structure_path: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="统一文档结构 JSON 的相对路径"
    )

    # ------------------------------------------------------------ 解析统计
    page_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    char_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    image_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    chunk_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    kp_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    # ------------------------------------------------------------ 状态机
    parse_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ParseStatus.PENDING,
        index=True,
        comment="pending/parsing/chunking/extracting/ready/failed",
    )
    progress: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, comment="0-100，供前端进度条"
    )
    stage_detail: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="当前阶段的可读描述"
    )
    parse_error: Mapped[str | None] = mapped_column(
        LONGTEXT, nullable=True, comment="失败原因（面向用户可读）"
    )
    warnings: Mapped[list | None] = mapped_column(
        JSON, nullable=True, comment="[{code, page_no, message}]"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # 级联删除：删文档时一并清掉切片与知识点
    chunks: Mapped[list["Chunk"]] = relationship(  # noqa: F821
        back_populates="document", cascade="all, delete-orphan", lazy="noload"
    )
    knowledge_points: Mapped[list["KnowledgePoint"]] = relationship(  # noqa: F821
        back_populates="document", cascade="all, delete-orphan", lazy="noload"
    )

    __table_args__ = (
        Index("ix_documents_created_at", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Document id={self.id} name={self.file_name!r} status={self.parse_status}>"
