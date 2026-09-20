"""文本块模型：文档切分后的语义块，是「页码溯源」与「P3 向量检索」的共同载体。

设计要点：
- chunk_index 从 0 开始，文档内唯一（唯一索引兜底）
- page_start / page_end 从 1 开始，允许一个块跨页（同章节内）
- heading_path 是面包屑，为知识点提供语境，也是 P2 建关系边的依据
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base_class import Base


class BlockType(StrEnum):
    """块的类型。取值与 skills/doc-ingestion/SKILL.md 的契约一致。"""

    HEADING = "heading"
    TEXT = "text"
    TABLE = "table"
    FIGURE = "figure"
    FORMULA = "formula"


class Chunk(Base):
    """一个带页码的语义块。"""

    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )

    chunk_index: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="文档内序号，从 0 开始"
    )
    content: Mapped[str] = mapped_column(LONGTEXT, nullable=False, comment="块正文")

    page_start: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="起始页，从 1 开始"
    )
    page_end: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="结束页，从 1 开始"
    )

    block_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default=BlockType.TEXT, comment="text/heading/table/figure"
    )
    heading_path: Mapped[list | None] = mapped_column(
        JSON, nullable=True, comment='面包屑，如 ["第3章 进程管理","3.1 进程的概念"]'
    )
    image_path: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="block_type=figure 时的图片相对路径"
    )

    char_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    token_estimate: Mapped[int] = mapped_column(
        BigInteger, default=0, nullable=False, comment="粗略估算，供后续成本预估"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    document: Mapped["Document"] = relationship(back_populates="chunks")  # noqa: F821

    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_chunks_doc_index"),
        Index("ix_chunks_doc_page", "document_id", "page_start"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Chunk doc={self.document_id} idx={self.chunk_index} p{self.page_start}>"
