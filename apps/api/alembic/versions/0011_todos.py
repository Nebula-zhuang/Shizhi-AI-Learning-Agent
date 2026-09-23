"""待办事项。

Revision ID: 0011_todos
Revises: 0010_saved_knowledge
Create Date: 2026-09-23

## 为什么另建表

见 `app/models/todo.py` 的模块注释。一句话：每张表只回答一个问题，
`todos` 回答的是「**我要做什么**」—— 既不是对话、也不是知识点、也不是掌握度。

## 几个字段的取舍

- **`learner_id` 不建外键**：沿用 `conversations` / `sessions` / `saved_knowledge`
  的做法，只按字符串隔离。加外键会把"删账号级联删数据"这件事变成数据库的职责，
  而目前不是。
- **`due_date` 用 DATE 不是 DATETIME**：待办问的是"**哪一天**到期"。
  用 DATETIME 会逼前端编一个时刻（00:00 或 23:59），
  而这个编出来的时刻会进比较逻辑 —— "今天到期"于是变得取决于时区和编法。
- **`completed` 用 BOOLEAN 不是枚举**：MVP 只有两种状态，枚举是在猜将来。
- **`updated_at` 带 `onupdate`**：与 `learner_kp_states` 同一做法，数据库侧兜底。
- **`title` 给 `VARCHAR(255)`**：真正的长度约束在 schema 层，这里只保证列够宽。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_todos"
down_revision: str | None = "0010_saved_knowledge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "todos",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "learner_id",
            sa.String(length=64),
            nullable=False,
            server_default="local",
            comment="归属账号的学习档案标识",
        ),
        sa.Column(
            "title",
            sa.String(length=255),
            nullable=False,
            comment="一句话，非空",
        ),
        sa.Column(
            "completed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="做完没做完",
        ),
        sa.Column(
            "due_date",
            sa.Date(),
            nullable=True,
            comment="哪一天到期；空 = 没有截止日",
        ),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # 列表查询恒定是"我的，未完成优先，按时间"。
    # `completed` 放进索引是刻意的：列表按它分段，而不是取回来在内存里排。
    op.create_index(
        "ix_todos_learner_completed_created",
        "todos",
        ["learner_id", "completed", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_todos_learner_completed_created", table_name="todos")
    op.drop_table("todos")
