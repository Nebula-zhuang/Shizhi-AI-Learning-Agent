"""P2：新增 knowledge_relations / knowledge_checks 两张表。

Revision ID: 0003_p2_relations_checks
Revises: 0002_p1_documents
Create Date: 2026-09-17

0002 的文档里写明「knowledge_relations / knowledge_checks 属 P2」，
本迁移正是补上这两张表，与当时的规划一致。

设计取舍见 docs/05-P2-实施方案.md §3：
  - 关系表只存一个方向（contains / prerequisite / related），
    belongs_to 是 contains 的反向视角，由接口层派生，不落库 —— 避免图里出现重影
  - 校验表是 append-only 的**事件表**而非 knowledge_points 上的列，
    这样同一知识点被反复校验时历史不会被覆盖，"可追溯"才成立
  - 两张表的 source / evidence 均为非空，数据库层面杜绝"无依据连边"
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_p2_relations_checks"
down_revision: str | None = "0002_p1_documents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_KW = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}


def upgrade() -> None:
    # -------------------------------------------------------- knowledge_relations
    op.create_table(
        "knowledge_relations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "document_id",
            sa.BigInteger(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "from_kp_id",
            sa.BigInteger(),
            sa.ForeignKey("knowledge_points.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "to_kp_id",
            sa.BigInteger(),
            sa.ForeignKey("knowledge_points.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "relation_type",
            sa.String(24),
            nullable=False,
            comment="prerequisite / related / contains",
        ),
        sa.Column("confidence", sa.Numeric(3, 2), nullable=False, server_default="0.60"),
        sa.Column(
            "source",
            sa.String(24),
            nullable=False,
            comment="构建依据：heading_parent / shared_chunk / same_section / ...",
        ),
        sa.Column("evidence", sa.String(512), nullable=False, server_default=""),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("from_kp_id", "to_kp_id", "relation_type", name="uq_relation_triple"),
        comment="知识点之间的关系，每条边都必须带构建依据",
        **TABLE_KW,
    )
    op.create_index("ix_relation_doc", "knowledge_relations", ["document_id"])
    op.create_index("ix_relation_from", "knowledge_relations", ["from_kp_id"])
    op.create_index("ix_relation_to", "knowledge_relations", ["to_kp_id"])

    # --------------------------------------------------------- knowledge_checks
    op.create_table(
        "knowledge_checks",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "kp_id",
            sa.BigInteger(),
            sa.ForeignKey("knowledge_points.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "document_id",
            sa.BigInteger(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("check_type", sa.String(16), nullable=False, comment="rule / model / web"),
        sa.Column(
            "verdict",
            sa.String(16),
            nullable=False,
            comment="passed / suspicious / unsupported / skipped / error",
        ),
        sa.Column("confidence", sa.Numeric(3, 2), nullable=False, server_default="0.80"),
        sa.Column("reason", sa.String(512), nullable=False, server_default=""),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("source_urls", sa.JSON(), nullable=True),
        sa.Column(
            "engine",
            sa.String(32),
            nullable=False,
            server_default="",
            comment="下结论的主体：rule-v1 / 模型名 / tavily",
        ),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        comment="知识点可信度校验记录，append-only，不覆盖 P1 抽取结果",
        **TABLE_KW,
    )
    op.create_index("ix_check_kp_type", "knowledge_checks", ["kp_id", "check_type"])
    op.create_index("ix_check_doc", "knowledge_checks", ["document_id"])


def downgrade() -> None:
    op.drop_table("knowledge_checks")
    op.drop_table("knowledge_relations")
