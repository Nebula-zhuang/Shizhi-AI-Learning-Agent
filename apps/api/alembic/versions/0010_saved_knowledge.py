"""用户主动保存的知识。

Revision ID: 0010_saved_knowledge
Revises: 0009_p7_conversations
Create Date: 2026-09-22

## 为什么另建表

见 `app/models/saved_knowledge.py` 的模块注释（设计依据是
`docs/25a-自由学习空间设计.md` §3 的「不要把 Conversation、Knowledge、Memory 混成一张表」）。
一句话：前三张表是**系统的副产物**，这一张是**用户的决定**。

## 几个字段的取舍

- **`question` / `answer` 自己存一份**而不是靠 `source_message_id` 去 join：
  "保存"的语义就是**"即使对话没了它也还在"**。只存外键的话，
  清空对话会把用户特意留下的东西一起带走。
- **`source_message_id` 不设外键**：同理 —— 加外键会级联删除。
  它只做回溯，允许为空、允许指向已删除的消息。
- **`embedding_id` 可为空**：向量是派生数据，写向量失败不该让保存失败。
- **不建外键到 learners**：沿用 `conversations` / `sessions` 的做法，
  只按 `learner_id` 字符串隔离数据。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_saved_knowledge"
down_revision: str | None = "0009_p7_conversations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "saved_knowledge",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "learner_id",
            sa.String(length=64),
            nullable=False,
            server_default="local",
            comment="归属账号的学习档案标识",
        ),
        # 快照正文。用 Text：MySQL utf8mb4 下 VARCHAR 上限只有 16383 字符，
        # 而助教的长回答本来就可能超过它（见 ConversationMessage.content 的注释）。
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column(
            "source_message_id",
            sa.BigInteger(),
            nullable=True,
            comment="回溯到 conversation_messages.id，可为空、无外键",
        ),
        sa.Column("source_urls", sa.JSON(), nullable=True),
        sa.Column("kp_ids", sa.JSON(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column(
            "embedding_id",
            sa.String(length=128),
            nullable=True,
            comment="向量库 id，空 = 尚未建立索引（可事后回填）",
        ),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # 列表查询恒定是"我保存的，按时间倒序"
    op.create_index(
        "ix_saved_knowledge_learner_recent",
        "saved_knowledge",
        ["learner_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_saved_knowledge_learner_recent", table_name="saved_knowledge")
    op.drop_table("saved_knowledge")
