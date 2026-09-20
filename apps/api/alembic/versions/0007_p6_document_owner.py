"""P6：资料归属账号。

Revision ID: 0007_p6_document_owner
Revises: 0006_p6_users
Create Date: 2026-09-19

给 `documents` 加 `owner_learner_id`，让**资料库按账号隔离**。

## 为什么之前没有这一列

P0–P5 没有账号概念，资料天然是"这一个用户的"，所以没必要标归属。
P6 接入账号时我只绑定了**学习状态**（`learner_kp_states` / `learner_profiles` /
`sessions` 那三张表），漏了资料 —— 结果是新注册的账号一进「资料」「知识地图」
就看到别人的文件。这是一个真实的隐私缺口，本迁移补上。

## 为什么用 server_default='local' 而不是可空

两个原因：

1. **既有行要有归属**：回填成 `local`，于是演示账号（它的 learner_id 也是 `local`）
   仍能看到 P0–P5 攒下的样例资料，演示不受影响。
2. **不许出现"无主资料"**：如果允许 NULL，将来任何一处忘了带 owner 的查询
   都会把无主资料当成"谁都能看"，等于留了个后门。非空 + 默认值让这个状态不存在。

索引建在 `owner_learner_id` 上：所有列表查询都会带上它。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_p6_document_owner"
down_revision: str | None = "0006_p6_users"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column(
            "owner_learner_id",
            sa.String(length=64),
            nullable=False,
            server_default="local",
            comment="归属账号的学习档案标识",
        ),
    )
    # server_default 会把既有行一并写成 'local'，不需要单独 UPDATE。
    op.create_index(
        "ix_documents_owner_learner_id", "documents", ["owner_learner_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_documents_owner_learner_id", table_name="documents")
    op.drop_column("documents", "owner_learner_id")
