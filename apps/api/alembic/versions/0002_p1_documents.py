"""P1：新增 documents / chunks / knowledge_points 三张表。

Revision ID: 0002_p1_documents
Revises: 0001_initial
Create Date: 2026-09-17

本阶段只建摄取流水线必需的三张表。
刻意不建的表与理由见 docs/03-P1-实施方案.md §3.4：
  users               —— P1 无登录、无消费者，建了就是空表（P5 再建）
  knowledge_relations —— 关系构建属 P2
  knowledge_checks    —— 可信度校验属 P2
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0002_p1_documents"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_KW = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}


def upgrade() -> None:
    # ------------------------------------------------------------------ documents
    op.create_table(
        "documents",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("file_name", sa.String(255), nullable=False, comment="原始文件名"),
        sa.Column("file_type", sa.String(16), nullable=False, comment="pdf / txt / md / image"),
        sa.Column("file_size", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("file_hash", sa.String(64), nullable=False, comment="sha256，去重键"),
        sa.Column("storage_path", sa.String(512), nullable=False),
        sa.Column("structure_path", sa.String(512), nullable=True),
        sa.Column("page_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("char_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("image_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("chunk_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("kp_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "parse_status",
            sa.String(16),
            nullable=False,
            server_default="pending",
            comment="pending/parsing/chunking/extracting/ready/failed",
        ),
        sa.Column("progress", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("stage_detail", sa.String(128), nullable=True),
        sa.Column("parse_error", mysql.LONGTEXT(), nullable=True),
        sa.Column("warnings", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("file_hash", name="uq_documents_file_hash"),
        comment="上传的学习资料",
        **TABLE_KW,
    )
    op.create_index("ix_documents_parse_status", "documents", ["parse_status"])
    op.create_index("ix_documents_created_at", "documents", ["created_at"])

    # --------------------------------------------------------------------- chunks
    op.create_table(
        "chunks",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "document_id",
            sa.BigInteger(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.BigInteger(), nullable=False, comment="从 0 开始"),
        sa.Column("content", mysql.LONGTEXT(), nullable=False),
        sa.Column("page_start", sa.BigInteger(), nullable=False, comment="从 1 开始"),
        sa.Column("page_end", sa.BigInteger(), nullable=False, comment="从 1 开始"),
        sa.Column("block_type", sa.String(16), nullable=False, server_default="text"),
        sa.Column("heading_path", sa.JSON(), nullable=True),
        sa.Column("image_path", sa.String(512), nullable=True),
        sa.Column("char_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("token_estimate", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_chunks_doc_index"),
        comment="文档切分后的语义块",
        **TABLE_KW,
    )
    op.create_index("ix_chunks_doc_page", "chunks", ["document_id", "page_start"])

    # ---------------------------------------------------------- knowledge_points
    op.create_table(
        "knowledge_points",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "document_id",
            sa.BigInteger(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("title_norm", sa.String(255), nullable=False, comment="归一化标题，去重键"),
        sa.Column("summary", sa.String(512), nullable=False, server_default=""),
        sa.Column("details", mysql.LONGTEXT(), nullable=False),
        sa.Column("key_points", sa.JSON(), nullable=True),
        sa.Column("difficulty", sa.BigInteger(), nullable=False, server_default="3"),
        sa.Column("importance", sa.BigInteger(), nullable=False, server_default="3"),
        sa.Column("confidence", sa.Numeric(3, 2), nullable=False, server_default="0.80"),
        sa.Column(
            "verify_status",
            sa.String(16),
            nullable=False,
            server_default="unverified",
            comment="P2 使用",
        ),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("heading_path", sa.JSON(), nullable=True),
        sa.Column("source_chunk_indexes", sa.JSON(), nullable=True),
        sa.Column("source_pages", sa.JSON(), nullable=True),
        sa.Column("order_index", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("document_id", "title_norm", name="uq_kp_doc_title"),
        comment="结构化知识点",
        **TABLE_KW,
    )
    op.create_index("ix_kp_doc_order", "knowledge_points", ["document_id", "order_index"])

    # 记录本次迁移的阶段标记（复用 P0 的 app_meta 表）
    op.execute(
        "UPDATE app_meta SET value = 'P1' WHERE `key` = 'init_stage'"
    )
    op.execute(
        "INSERT INTO app_meta (`key`, `value`, `description`) "
        "VALUES ('p1_tables', 'documents,chunks,knowledge_points', "
        "'P1 新增的摄取流水线数据表') "
        "ON DUPLICATE KEY UPDATE value = VALUES(value)"
    )


def downgrade() -> None:
    op.drop_table("knowledge_points")
    op.drop_table("chunks")
    op.drop_table("documents")
