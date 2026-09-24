"""待办计时。

Revision ID: 0012_todo_timer
Revises: 0011_todos
Create Date: 2026-09-24

## 为什么加这三列

待办不只是"要做的事"，还是"打算花多久做的事"。三个字段各管一件事：

- `timer_mode` —— 这条任务要不要计时、怎么计：

  | 值 | 含义 |
  |---|---|
  | `none` | 不计时（默认，也是加这三列之前的全部历史数据）|
  | `countup` | 正计时：从 0 往上走，看"这条花了多久" |
  | `countdown` | 倒计时：从 `timer_minutes` 往下走，看"还剩多少" |

- `timer_minutes` —— **只有倒计时用**。正计时不给目标，给了也没意义。
- `spent_seconds` —— **累计已计时秒数**（不是某一次的时长）。

## 为什么 `spent_seconds` 要落库

不落库的话，刷新页面计时就归零 —— 而"我到底在这条上花了多久"恰恰是
计时功能唯一的产出 ✗ 所以它必须活过刷新。

代价是前端要**定期把增量写回去**（运行中每 15 秒一次 PATCH），
而不是只在前端内存里累加。这个代价是值的：一次会话中途关掉标签页，
最多只丢最后 15 秒。

## 为什么不加索引

计时相关字段**从不参与筛选或排序** —— 列表恒定按
`(learner_id, completed, created_at)` 走（见 `0011_todos` 的索引 ✓）。
给不查的列加索引只会拖慢写入。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_todo_timer"
down_revision: str | None = "0011_todos"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "todos",
        sa.Column(
            "timer_mode",
            sa.String(length=16),
            nullable=False,
            server_default="none",
            comment="none / countup / countdown",
        ),
    )
    op.add_column(
        "todos",
        sa.Column(
            "timer_minutes",
            sa.Integer(),
            nullable=True,
            comment="倒计时的目标分钟数；正计时与不计时为 NULL",
        ),
    )
    op.add_column(
        "todos",
        sa.Column(
            "spent_seconds",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="累计已计时秒数（不是单次时长）",
        ),
    )


def downgrade() -> None:
    op.drop_column("todos", "spent_seconds")
    op.drop_column("todos", "timer_minutes")
    op.drop_column("todos", "timer_mode")
