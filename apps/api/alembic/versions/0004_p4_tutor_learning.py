"""P4：新增学习闭环所需的四张表。

Revision ID: 0004_p4_tutor_learning
Revises: 0003_p2_relations_checks
Create Date: 2026-09-18

新增：
  sessions            学习会话（过程）
  messages            会话消息 + **Agent 决策留痕**（action_type / reason / state_snapshot）
  answer_evaluations  作答评估（结构化输出，含 confidence）
  learner_kp_states   学习状态（mastery / 连击计数）—— 长期记忆的核心

设计取舍见 docs/09-P4-实施方案.md §2：
  - `learner_id` 用 VARCHAR 而不是指向 users 的外键：用户体系属 P5，
    现在建 users 表只会得到一张没有登录入口的空表。P5 加外键列即可，不返工。
  - `messages` 存 `reason` 与 `state_snapshot`：P4 要证明"Agent 根据学习状态改变策略"，
    不记当时的状态就证明不了。
  - `mastery` 用 DECIMAL(4,3) 而不是 (3,2)：增量可能只有 0.004，
    (3,2) 会把它四舍五入成 0，看起来像"状态没更新"。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_p4_tutor_learning"
down_revision: str | None = "0003_p2_relations_checks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_KW = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}


def upgrade() -> None:
    # ------------------------------------------------------------------ sessions
    op.create_table(
        "sessions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("learner_id", sa.String(64), nullable=False, server_default="local"),
        sa.Column(
            "document_id",
            sa.BigInteger(),
            sa.ForeignKey("documents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "knowledge_point_id",
            sa.BigInteger(),
            sa.ForeignKey("knowledge_points.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("title", sa.String(255), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("action_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("last_active_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        comment="学习会话（过程记录，学习状态跨会话累积）",
        **TABLE_KW,
    )
    op.create_index("ix_session_learner", "sessions", ["learner_id", "id"])
    op.create_index("ix_session_kp", "sessions", ["knowledge_point_id"])

    # ------------------------------------------------------------------ messages
    op.create_table(
        "messages",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "session_id",
            sa.BigInteger(),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(16), nullable=False, comment="user/assistant/system"),
        sa.Column("content", sa.String(4096), nullable=False, server_default=""),
        sa.Column(
            "action_type",
            sa.String(24),
            nullable=True,
            comment="probe/explain/rephrase/harder/easier/summarize",
        ),
        sa.Column("reason", sa.String(512), nullable=False, server_default=""),
        sa.Column("state_snapshot", sa.JSON(), nullable=True),
        sa.Column("kp_ids", sa.JSON(), nullable=True),
        sa.Column("refs", sa.JSON(), nullable=True),
        sa.Column("engine", sa.String(32), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        comment="会话消息 + Agent 决策留痕",
        **TABLE_KW,
    )
    op.create_index("ix_message_session", "messages", ["session_id", "id"])

    # -------------------------------------------------------- answer_evaluations
    op.create_table(
        "answer_evaluations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "session_id",
            sa.BigInteger(),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "message_id",
            sa.BigInteger(),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "knowledge_point_id",
            sa.BigInteger(),
            sa.ForeignKey("knowledge_points.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("question", sa.String(2048), nullable=False, server_default=""),
        sa.Column("user_answer", sa.String(4096), nullable=False, server_default=""),
        sa.Column("correct", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("score", sa.Numeric(4, 3), nullable=False, server_default="0.000"),
        sa.Column(
            "confidence",
            sa.Numeric(4, 3),
            nullable=False,
            server_default="0.000",
            comment="评估本身的把握，与 learner_kp_states.mastery 严格区分",
        ),
        sa.Column("level", sa.String(16), nullable=False, server_default="not_mastered"),
        sa.Column("error_type", sa.String(30), nullable=False, server_default="none"),
        sa.Column("missing_points", sa.JSON(), nullable=True),
        sa.Column("misunderstood_points", sa.JSON(), nullable=True),
        sa.Column("feedback", sa.String(2048), nullable=False, server_default=""),
        sa.Column("engine", sa.String(32), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        comment="作答评估（教学动作决策的燃料）",
        **TABLE_KW,
    )
    op.create_index("ix_eval_session", "answer_evaluations", ["session_id", "id"])
    op.create_index("ix_eval_kp", "answer_evaluations", ["knowledge_point_id"])

    # --------------------------------------------------------- learner_kp_states
    op.create_table(
        "learner_kp_states",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("learner_id", sa.String(64), nullable=False, server_default="local"),
        sa.Column(
            "knowledge_point_id",
            sa.BigInteger(),
            sa.ForeignKey("knowledge_points.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "mastery",
            sa.Numeric(4, 3),
            nullable=False,
            server_default="0.000",
            comment="0~1 掌握度，由 Policy 增量更新",
        ),
        sa.Column("attempt_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("correct_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("consecutive_correct", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("consecutive_wrong", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_error_type", sa.String(30), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="new"),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("learner_id", "knowledge_point_id", name="uq_learner_kp"),
        comment="学习状态（长期记忆的核心），跨会话累积",
        **TABLE_KW,
    )
    op.create_index("ix_lks_learner", "learner_kp_states", ["learner_id"])


def downgrade() -> None:
    op.drop_table("learner_kp_states")
    op.drop_table("answer_evaluations")
    op.drop_table("messages")
    op.drop_table("sessions")
