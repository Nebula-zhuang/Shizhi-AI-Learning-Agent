"""初始迁移：创建基础设施表 app_meta。

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-16

本阶段（P0）刻意不创建任何业务表 —— 业务表按技术方案第九节在 P1 起逐步加入。
app_meta 用于承载系统级元信息，同时作为 ORM → Alembic → MySQL 链路的验证载体。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "app_meta"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column(
            "id",
            sa.BigInteger(),
            primary_key=True,
            autoincrement=True,
            comment="主键",
        ),
        sa.Column("key", sa.String(64), nullable=False, comment="配置键"),
        sa.Column("value", sa.Text(), nullable=False, comment="配置值"),
        sa.Column("description", sa.String(255), nullable=True, comment="用途说明"),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
            comment="更新时间",
        ),
        sa.UniqueConstraint("key", name="uq_app_meta_key"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
        comment="系统元信息键值表（非业务表）",
    )

    # 写入初始化标记。显式指定 id 以保证在 SQLite（无 rowid 别名）下也能执行
    op.bulk_insert(
        sa.table(
            TABLE,
            sa.column("id", sa.BigInteger()),
            sa.column("key", sa.String(64)),
            sa.column("value", sa.Text()),
            sa.column("description", sa.String(255)),
        ),
        [
            {
                "id": 1,
                "key": "schema_version",
                "value": "0.1.0",
                "description": "数据库结构版本，与后端 APP_VERSION 同步演进",
            },
            {
                "id": 2,
                "key": "init_stage",
                "value": "P0",
                "description": "记录当前所处的开发阶段",
            },
            {
                "id": 3,
                "key": "initialized_at",
                "value": "2026-09-16",
                "description": "首次迁移执行日期",
            },
        ],
    )


def downgrade() -> None:
    op.drop_table(TABLE)
