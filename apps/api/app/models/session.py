"""学习会话。

一次"围绕某个知识点学一学"的过程就是一条 session 记录。
学习状态（`learner_kp_states`）**跨会话累积**，会话只记录过程 ——
这是两者最重要的分工：状态是长期的，会话是临时的。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import Base


class SessionStatus(StrEnum):
    ACTIVE = "active"
    FINISHED = "finished"  # 达成掌握（summarize）后收束


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    #: 学习者标识。**刻意不是外键** —— 用户体系属 P5，
    #: 现在建 users 表只会得到一张没有登录入口的空表。
    #: 默认 "local"；P5 引入用户时加外键列并迁移即可，不返工。
    learner_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="local", comment="学习者标识，P5 接用户体系"
    )

    document_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )
    #: 本会话聚焦的知识点。会话围绕一个知识点展开，教学动作才有明确的针对对象。
    knowledge_point_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("knowledge_points.id", ondelete="SET NULL"), nullable=True
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SessionStatus.ACTIVE, comment="active / finished"
    )
    #: 已产生的教学动作轮次。用直观计数而不是从 messages 聚合，便于列表页直接用。
    action_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    messages: Mapped[list["Message"]] = relationship(  # noqa: F821
        back_populates="session", cascade="all, delete-orphan", order_by="Message.id"
    )

    __table_args__ = (
        Index("ix_session_learner", "learner_id", "id"),
        Index("ix_session_kp", "knowledge_point_id"),
    )

    def __repr__(self) -> str:
        return f"<Session {self.id} learner={self.learner_id} kp={self.knowledge_point_id}>"
