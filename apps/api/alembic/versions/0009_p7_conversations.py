"""自由学习空间：对话与消息两张表。

Revision ID: 0009_p7_conversations
Revises: 0008_drop_verify_suspect
Create Date: 2026-09-20

## 为什么另建表而不复用 `sessions`

`sessions` 绑死了"一次围绕**某一个知识点**的学习"（`knowledge_point_id` 非空语义），
而自由学习的对话没有唯一知识点：一轮里可能同时涉及三个知识点、一份刚上传的文件、
一次联网搜索，下一轮又换话题。

硬塞进去要么让那两个字段全填 NULL（所有查询都要判空来区分类型），
要么"一个问题开一条 session"（那就不叫对话了）。

## 几个字段的取舍

- **`last_message_at` 而不是拿 `updated_at` 排序**：改标题会刷新 updated_at，
  于是"改个名字"就把对话顶到列表最前，与"最近聊过"的直觉不符。
- **`message_count` 冗余存**：列表页要显示聊了多少轮。存计数比每次聚合便宜，
  这个字段只有"加一条消息"一个写入点，不存在不一致风险。
- **`sources` / `citations` / `attachments` / `status_trace` 用 JSON**：
  它们都是"这一轮发生了什么"的**摘要**（给右侧面板展示用），
  形状会随能力增减而变，拆成四张关联表不值当。
  ⚠️ 但**工具原始入参出参与模型原始响应不进这里** —— 那些进日志。
  对话表是给人看的。
- **不建外键到 learners**：沿用 `sessions` 的做法（那里也注释了同样的理由）——
  用户体系与 learner_id 的对应关系已由 P6 建立，这里只按字符串隔离数据。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_p7_conversations"
down_revision: str | None = "0008_drop_verify_suspect"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "learner_id",
            sa.String(length=64),
            nullable=False,
            server_default="local",
            comment="归属账号的学习档案标识",
        ),
        sa.Column("title", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "last_message_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # 列表查询恒定是"我的对话，按最近聊过倒序"
    op.create_index(
        "ix_conversation_learner_recent",
        "conversations",
        ["learner_id", "last_message_at"],
        unique=False,
    )

    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "role", sa.String(length=16), nullable=False, comment="user / assistant"
        ),
        # ⚠️ 必须 Text 不是 String(N)：utf8mb4 下 VARCHAR 单列上限 16383，
        # 写 20000 会报 (1074, "Column length too big")。
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sources", sa.JSON(), nullable=True, comment="本轮用到的外部能力摘要"),
        sa.Column("citations", sa.JSON(), nullable=True, comment="引用来源"),
        sa.Column("attachments", sa.JSON(), nullable=True, comment="本轮带的附件"),
        sa.Column("status_trace", sa.JSON(), nullable=True, comment="自然语言状态轨迹"),
        sa.Column("degraded_reason", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_conversation_message_conv",
        "conversation_messages",
        ["conversation_id", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_conversation_message_conv", table_name="conversation_messages")
    op.drop_table("conversation_messages")
    op.drop_index("ix_conversation_learner_recent", table_name="conversations")
    op.drop_table("conversations")
