"""作答评估记录（教学闭环的燃料）。

每次用户作答都会留下一条 —— 教学动作的决策依据几乎全部来自这里
（对错影响连击、score 决定"答得浅不浅"、error_type 决定"换讲法怎么换"）。

关于 `confidence` 的定位：它**不是**掌握度，而是"本次判定本身有多可信"。
模型对一道模糊答案拿不准时会给低 confidence，此时 mastery 的更新幅度
仍按公式走，但界面上会提示"本次评估把握不大"。两者不可混淆（见 learner_kp_state 的说明）。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base_class import Base


class AssessmentLevel(StrEnum):
    """作答深度。比 score 更粗，但更稳定、更适合驱动教学动作。"""

    NOT_MASTERED = "not_mastered"  # 没答对/没答到点上
    VAGUE = "vague"  # 方向对但没答透 —— 触发 probe
    MASTERED = "mastered"  # 答得完整准确


class ErrorType(StrEnum):
    """错因分类。换讲法时要对症：概念混淆和记忆缺口需要完全不同的讲法。"""

    CONCEPT_CONFUSION = "concept_confusion"
    MEMORY_GAP = "memory_gap"
    REASONING_BREAK = "reasoning_break"
    MISREAD = "misread"
    NONE = "none"


class AnswerEvaluation(Base):
    __tablename__ = "answer_evaluations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    session_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    #: 被评估的那条用户消息
    message_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("messages.id", ondelete="CASCADE"), nullable=True
    )
    knowledge_point_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("knowledge_points.id", ondelete="CASCADE"), nullable=False
    )

    question: Mapped[str] = mapped_column(String(2048), nullable=False, default="")
    user_answer: Mapped[str] = mapped_column(String(4096), nullable=False, default="")

    # ------------------------------------------------- 结构化评估输出
    correct: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    score: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), nullable=False, default=Decimal("0.000"), comment="0~1 作答质量"
    )
    #: **评估本身的把握**，与 mastery 严格区分
    confidence: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), nullable=False, default=Decimal("0.000"), comment="0~1 判定置信度"
    )
    level: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AssessmentLevel.NOT_MASTERED
    )
    error_type: Mapped[str] = mapped_column(
        String(30), nullable=False, default=ErrorType.NONE
    )

    missing_points: Mapped[list | None] = mapped_column(JSON, nullable=True)
    misunderstood_points: Mapped[list | None] = mapped_column(JSON, nullable=True)
    feedback: Mapped[str] = mapped_column(String(2048), nullable=False, default="")

    #: 下结论的主体：模型名 / heuristic（LLM 失败时的降级路径）
    engine: Mapped[str] = mapped_column(String(32), nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    message: Mapped["Message | None"] = relationship(  # noqa: F821
        back_populates="evaluations"
    )

    __table_args__ = (
        Index("ix_eval_session", "session_id", "id"),
        Index("ix_eval_kp", "knowledge_point_id"),
    )

    def __repr__(self) -> str:
        return f"<AnswerEvaluation {self.id} correct={self.correct} score={self.score}>"
