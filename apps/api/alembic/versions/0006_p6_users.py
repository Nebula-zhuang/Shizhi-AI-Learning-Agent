"""P6：用户账号。

Revision ID: 0006_p6_users
Revises: 0005_p5_learning_memory
Create Date: 2026-09-19

新增一张表 `users`：登录名、口令哈希、展示名、学习数据归属标识。

**纯增量**：不动任何既有表。P0–P5 的学习数据挂在 `learner_id` 这个字符串上，
所以接入账号体系只需要给每个账号分配一个自己的 `learner_id` ——
`learner_kp_states` / `learner_profiles` / `sessions` 三张表一个字都不用改。

`learner_id` 上建唯一索引：一个账号对应一份学习档案，不能两个账号指向同一份
（否则会出现"两个人共用一个掌握度"这种说不清的状态）。
`username` 同样唯一 —— 查重放在数据库层，而不是只靠应用层先查再插
（并发注册时"先查后插"会漏）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_p6_users"
down_revision: str | None = "0005_p5_learning_memory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_KW = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "username",
            sa.String(length=32),
            nullable=False,
            comment="登录名，唯一",
        ),
        sa.Column(
            "password_hash",
            sa.String(length=128),
            nullable=False,
            comment="口令哈希",
        ),
        sa.Column("display_name", sa.String(length=64), nullable=False, server_default=""),
        sa.Column(
            "learner_id",
            sa.String(length=64),
            nullable=False,
            comment="学习数据归属标识",
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username", name="uq_users_username"),
        sa.UniqueConstraint("learner_id", name="uq_users_learner_id"),
        **TABLE_KW,
    )
    # 唯一约束在 MySQL 里本身就带索引，**不再额外 create_index** ——
    # 否则同一列上会出现两条索引（一条 UNIQUE、一条普通），纯属冗余，
    # 还会让 SHOW INDEX 的结果看起来像"建重了"。


def downgrade() -> None:
    op.drop_table("users")
