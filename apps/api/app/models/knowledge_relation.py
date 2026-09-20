"""知识点之间的关系。

设计要点（完整理由见 docs/05-P2-实施方案.md §3.1）：

1. **每条边都必须有依据**。`source` 与 `evidence` 都是非空列，数据库层面就杜绝了"随机连边"。
   `source` 说明用的是哪条规则，`evidence` 给出人类可读的证明（如"同在 3.2 节下"）。

2. **只存一个方向**。`contains`（父→子）与 `belongs_to`（子→父）是同一关系的正反视角，
   存两份会让图里出现重影。为此本表只存 `contains`，反向视角由接口层派生
   （边的 `inverse_type` 字段），前端在反向遍历时渲染为"属于"。
   `related` 无向，也只存一条。

3. **外键全部 CASCADE**。删除知识点或文档时关系自动清理，不留下指向不存在节点的悬空边。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base_class import Base


class RelationType(StrEnum):
    """关系类型。

    注意 `BELONGS_TO` **不会**被构建器直接产出 —— 它是 `CONTAINS` 的反向视角。
    列在这里是为了让接口层与前端有统一的词汇表，避免各处硬编码字符串。
    """

    PREREQUISITE = "prerequisite"  # 前置知识（有向：前置 → 后继）
    RELATED = "related"  # 相关（无向）
    CONTAINS = "contains"  # 从属（有向：父概念 → 子概念）
    BELONGS_TO = "belongs_to"  # 属于（CONTAINS 的反向视角，不落库）


#: 允许落库的关系类型。BELONGS_TO 不在其中。
STORABLE_RELATION_TYPES: frozenset[str] = frozenset(
    {RelationType.PREREQUISITE, RelationType.RELATED, RelationType.CONTAINS}
)

#: CONTAINS 的反向类型，供接口层派生
INVERSE_RELATION_TYPE: dict[str, str] = {
    RelationType.CONTAINS: RelationType.BELONGS_TO,
    RelationType.BELONGS_TO: RelationType.CONTAINS,
    RelationType.PREREQUISITE: RelationType.PREREQUISITE,  # 前置关系反向仍是"前置"语义，仅方向相反
    RelationType.RELATED: RelationType.RELATED,
}


class RelationSource(StrEnum):
    """关系的构建依据。每一种都对应 §4 表格里的一行规则。"""

    HEADING_PARENT = "heading_parent"  # heading_path 前缀包含
    TITLE_CONTAINS = "title_contains"  # 无章节层级时的标题包含
    SHARED_CHUNK = "shared_chunk"  # 共享来源块
    SAME_SECTION = "same_section"  # 同一小节
    ORDER_HEURISTIC = "order_heuristic"  # 顺序 + 章节双重信号
    LLM_CONFIRMED = "llm_confirmed"  # 规则候选经模型确认（仅能保留，不能新增）


class KnowledgeRelation(Base):
    """两个知识点之间的一条关系。"""

    __tablename__ = "knowledge_relations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    from_kp_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("knowledge_points.id", ondelete="CASCADE"), nullable=False
    )
    to_kp_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("knowledge_points.id", ondelete="CASCADE"), nullable=False
    )

    relation_type: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="prerequisite / related / contains"
    )
    confidence: Mapped[Decimal] = mapped_column(
        Numeric(3, 2), nullable=False, default=Decimal("0.60"), comment="0-1"
    )

    # ------------------------------------------------------------ 建边依据（非空）
    source: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="构建依据，见 RelationSource"
    )
    evidence: Mapped[str] = mapped_column(
        String(512), nullable=False, default="", comment="人类可读的依据说明"
    )
    detail: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, comment="结构化依据，如共享的 chunk 序号列表"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    from_point: Mapped["KnowledgePoint"] = relationship(  # noqa: F821
        foreign_keys=[from_kp_id], lazy="joined"
    )
    to_point: Mapped["KnowledgePoint"] = relationship(  # noqa: F821
        foreign_keys=[to_kp_id], lazy="joined"
    )

    __table_args__ = (
        UniqueConstraint("from_kp_id", "to_kp_id", "relation_type", name="uq_relation_triple"),
        Index("ix_relation_doc", "document_id"),
        Index("ix_relation_from", "from_kp_id"),
        Index("ix_relation_to", "to_kp_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<KnowledgeRelation {self.from_kp_id}-[{self.relation_type}]->{self.to_kp_id} "
            f"via {self.source}>"
        )
