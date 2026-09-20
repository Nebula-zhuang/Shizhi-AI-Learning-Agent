"""P5：学习状态与长期记忆。

Revision ID: 0005_p5_learning_memory
Revises: 0004_p4_tutor_learning
Create Date: 2026-09-19

新增：
  1. `learner_kp_states` 加两列 —— `next_review_at`（带索引）、`review_count`
     支撑"待复习"队列与简化遗忘曲线。
  2. 新表 `learner_profiles` —— 学习者画像（偏好讲法），
     让讲解风格跨会话保持一致。

**纯增量**：不改 P4 的任何已有列，新增列可空或带默认值，对既有数据向后兼容。

关于 `last_reviewed_at`：技术方案的 ER 里有这一列，但 P4 已经落了语义等价的
`last_attempt_at`（上次作答即上次复习）。不新增同义列 —— 两个字段各写一半
比缺一个字段更糟。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_p5_learning_memory"
down_revision: str | None = "0004_p4_tutor_learning"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_KW = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}


def upgrade() -> None:
    # ------------------------------------------- learner_kp_states 补两列
    op.add_column(
        "learner_kp_states",
        sa.Column(
            "next_review_at",
            sa.DateTime(),
            nullable=True,
            comment="下次复习时间，由简化遗忘曲线计算",
        ),
    )
    op.add_column(
        "learner_kp_states",
        sa.Column(
            "review_count",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
            comment="累计复习次数",
        ),
    )
    # 待复习要按它排序取到期项，必须建索引
    op.create_index("ix_lks_next_review", "learner_kp_states", ["next_review_at"])

    # ------------------------------------------------------ learner_profiles
    op.create_table(
        "learner_profiles",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "learner_id",
            sa.String(64),
            nullable=False,
            unique=True,
            comment="学习者标识，与 learner_kp_states 同口径",
        ),
        sa.Column(
            "preferred_style",
            sa.String(24),
            nullable=False,
            server_default="balanced",
            comment="balanced/contrast/structured/stepwise/clarify",
        ),
        sa.Column(
            "style_source",
            sa.String(16),
            nullable=False,
            server_default="default",
            comment="derived（自动推导）/ manual（用户设定，不被覆盖）/ default",
        ),
        sa.Column("style_evidence", sa.JSON(), nullable=True),
        sa.Column("stats", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        comment="学习者画像：偏好讲法与汇总统计",
        **TABLE_KW,
    )


def downgrade() -> None:
    op.drop_table("learner_profiles")
    op.drop_index("ix_lks_next_review", table_name="learner_kp_states")
    op.drop_column("learner_kp_states", "review_count")
    op.drop_column("learner_kp_states", "next_review_at")
