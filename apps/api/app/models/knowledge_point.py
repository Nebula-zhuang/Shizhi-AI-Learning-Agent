"""知识点模型：本项目的核心实体。

三个关键设计：
1. **source_pages 由代码回填，不由模型输出** —— 模型只给 source_chunk_indexes，
   页码从 chunks 反查。与其在提示词里反复叮嘱「不要编页码」，不如让模型根本没有
   输出页码的字段。能靠结构避免的错误，不要靠提示词避免。
2. **title_norm 作为去重键** —— 归一化标题（去空格/标点/虚词、转小写），
   (document_id, title_norm) 唯一索引是去重的最后一道防线。
3. **为后续阶段预留字段** —— verify_status 给 P2，source_chunk_indexes 给 P3 做引用、
   给 P4 做讲解依据。P1 不写入这些字段的语义，但结构必须就位。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base_class import Base


class VerifyStatus(StrEnum):
    """可信度核验状态。P1 一律为 UNVERIFIED，实际校验在 P2 实现。

    ## 只有两个状态是**会实际产出**的

        UNVERIFIED  还没核对（默认）
        TRUSTED     可信 —— 找到了明确依据

    ## 为什么删掉了 SUSPECT（存疑）

    它由 `CheckVerdict.SUSPICIOUS` 汇总而来，而那个结论的产出规则是软信号。
    实测在一份 108 个知识点的资料上标出了 **27 条存疑（25%）**——
    一个每四条命中一条的标签，用户学会的是"这个标签没用"，
    于是真正有问题的少数几条也被一起无视了。**噪音会淹没信号。**

    `CONFLICT`（有出入）与 `OUTDATED`（可能过时）**保留在枚举里但当前不产出**，
    理由和 "宁可少下结论" 的老规矩一致：
      - `CONFLICT` 需要"外部证据与资料直接矛盾"这个判断。当前只能给出"没找到依据"，
        而**"找不到依据"和"有出入"是两件事** —— 拿前者冒充后者就是在冤枉知识点。
        等有了更强的比对手段再启用。
      - `OUTDATED` 同理（P2 就已说明）。

    保留枚举值而不是删掉，是为了**历史数据能原样读出来**（旧行里确实有这两个值）。
    """

    UNVERIFIED = "unverified"
    TRUSTED = "trusted"
    # 以下两个为保留位，当前不产出（见类文档）
    CONFLICT = "conflict"
    OUTDATED = "outdated"


class KnowledgePoint(Base):
    """一个可考查、可溯源的知识点。"""

    __tablename__ = "knowledge_points"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )

    # ------------------------------------------------------------ 内容主体
    title: Mapped[str] = mapped_column(String(255), nullable=False, comment="≤20 字的名词短语")
    title_norm: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="归一化标题，去重键"
    )
    summary: Mapped[str] = mapped_column(
        String(512), nullable=False, default="", comment="≤60 字，一句话讲清是什么"
    )
    details: Mapped[str] = mapped_column(
        LONGTEXT, nullable=False, default="", comment="Markdown 展开讲解"
    )
    key_points: Mapped[list | None] = mapped_column(
        JSON, nullable=True, comment="2-4 条要点"
    )

    # ------------------------------------------------------------ 标注维度
    difficulty: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=3, comment="1-5，见 SKILL 的难度标准"
    )
    importance: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=3, comment="1-5，见 SKILL 的重要度标准"
    )
    confidence: Mapped[Decimal] = mapped_column(
        Numeric(3, 2), nullable=False, default=Decimal("0.80"), comment="0-1，忠实于原文的程度"
    )
    verify_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=VerifyStatus.UNVERIFIED,
        comment="P2 使用：unverified/trusted/suspect/outdated/conflict",
    )

    # ------------------------------------------------------------ 归类与溯源
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True, comment="≤3 个")
    heading_path: Mapped[list | None] = mapped_column(JSON, nullable=True, comment="归属章节面包屑")
    source_chunk_indexes: Mapped[list | None] = mapped_column(
        JSON, nullable=True, comment="来源 chunk 序号，溯源 backlink 的一部分"
    )
    source_pages: Mapped[list | None] = mapped_column(
        JSON, nullable=True, comment="由 chunk 页码反查得到，不由模型输出"
    )

    order_index: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, comment="按首次出现位置排序"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    document: Mapped["Document"] = relationship(  # noqa: F821
        back_populates="knowledge_points"
    )

    __table_args__ = (
        UniqueConstraint("document_id", "title_norm", name="uq_kp_doc_title"),
        Index("ix_kp_doc_order", "document_id", "order_index"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<KnowledgePoint id={self.id} title={self.title!r} d={self.difficulty}>"
