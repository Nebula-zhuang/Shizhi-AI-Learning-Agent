"""学习状态（长期记忆的核心）。

**这张表是 P4 全部价值的落点。** 教学动作能不能"因人而异"、能不能"越学越难"，
完全取决于这里累积的数据 —— 没有它，Tutor 只是一个每轮独立生成内容的聊天机器人。

两个必须讲清的概念区分（需求特别强调）：

| | `mastery` | `confidence` |
|---|---|---|
| 是什么 | **学习状态**：这个人对这个知识点掌握到什么程度 | **评估质量**：本次"判对/判错"这个结论本身有多可信 |
| 谁来写 | Policy 的增量公式（`policy.py`） | 评估模型的结构化输出 |
| 存在哪 | **本表 `mastery`** | `answer_evaluations.confidence` |
| 用途 | 决定教学动作、判断是否达成掌握 | 把握不大时提示人工复核 |
| 生命周期 | 跨会话长期累积 | 一次评估一条，用完即弃 |

**两者不共用任何变量名，也不互相赋值。** 有测试断言 confidence 不参与 mastery 计算。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import Base


class LearnerStatus(StrEnum):
    NEW = "new"  # 从未作答
    LEARNING = "learning"  # 学习中
    WEAK = "weak"  # 持续出错，基础薄弱
    MASTERED = "mastered"  # 达成掌握阈值


class LearnerKpState(Base):
    """某个学习者对某个知识点的学习状态。"""

    __tablename__ = "learner_kp_states"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    learner_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="local", comment="学习者标识"
    )
    knowledge_point_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("knowledge_points.id", ondelete="CASCADE"), nullable=False
    )

    #: 掌握度 0~1。用 DECIMAL(4,3) 而不是 (3,2)：
    #: 增量可能只有 0.004，(3,2) 会把它四舍五入成 0，看起来像"状态没更新"。
    mastery: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), nullable=False, default=Decimal("0.000"), comment="0~1 掌握度"
    )

    attempt_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    correct_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: 连续答对次数 —— 驱动 harder 的直接依据
    consecutive_correct: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: 连续答错次数 —— 驱动 rephrase / easier 的直接依据
    consecutive_wrong: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    #: 上次错因。供"换讲法"对症下药：概念混淆和记忆缺口需要完全不同的讲法。
    last_error_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=LearnerStatus.NEW, comment="new/learning/weak/mastered"
    )

    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: 下次该复习的时间（P5 的简化遗忘曲线算出来的）。
    #: 加索引是因为"待复习"要按它排序取到期项。
    next_review_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True, comment="下次复习时间，由简化遗忘曲线计算"
    )
    #: 累计复习次数。语义上独立于 attempt_count：将来若引入"不看答案的纯复习"，
    #: 两者会分开统计。
    review_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    knowledge_point: Mapped["KnowledgePoint"] = relationship(lazy="joined")  # noqa: F821

    __table_args__ = (
        UniqueConstraint("learner_id", "knowledge_point_id", name="uq_learner_kp"),
        Index("ix_lks_learner", "learner_id"),
    )

    # ------------------------------------------------------------------ 派生
    @property
    def is_mastered(self) -> bool:
        return self.status == LearnerStatus.MASTERED

    def snapshot(self) -> dict:
        """给 messages.state_snapshot 用的状态快照。

        演示与测试靠它断言"这个教学动作是在什么状态下做出的"——
        没有快照就只能看到"Agent 讲了一句话"，证明不了它是**根据状态**讲的。
        """
        return {
            "learner_id": self.learner_id,
            "knowledge_point_id": self.knowledge_point_id,
            "mastery": float(self.mastery or 0),
            "attempt_count": self.attempt_count,
            "correct_count": self.correct_count,
            "consecutive_correct": self.consecutive_correct,
            "consecutive_wrong": self.consecutive_wrong,
            "status": self.status,
            "last_error_type": self.last_error_type,
            "next_review_at": self.next_review_at.isoformat() if self.next_review_at else None,
            "review_count": int(self.review_count or 0),
        }

    def __repr__(self) -> str:
        return (
            f"<LearnerKpState learner={self.learner_id} kp={self.knowledge_point_id} "
            f"mastery={self.mastery} streak=+{self.consecutive_correct}/-{self.consecutive_wrong}>"
        )
