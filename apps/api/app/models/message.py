"""学习会话中的一条消息。

与普通聊天记录的关键区别：**每条助手消息都记录了 Agent 的决策结果**——
`action_type`（当时选了哪个教学动作）、`reason`（为什么选它）、
`state_snapshot`（决策时的学习状态快照）。

为什么必须记这三样：P4 要证明的是「Agent 会根据学习状态改变教学策略」。
只存一句"讲了一句话"是证明不了的 —— 必须能回放出**当时状态是什么、为什么选了这个动作**。
`state_snapshot` 让演示与测试可以直接断言"这个动作是在 mastery=0.31、连错 2 次的条件下做出的"。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base_class import Base


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ActionType(StrEnum):
    """六个固定的教学动作。

    定义在这里而不是 policy 里，是因为它会被持久化到 `messages.action_type` ——
    枚举的自然归属是"它被存到哪里"。policy 反过来导入它。
    """

    PROBE = "probe"  # 追问：把没答透的地方问深
    EXPLAIN = "explain"  # 讲解：重新讲清概念
    REPHRASE = "rephrase"  # 换讲法：换个角度/换种表述再讲一遍
    HARDER = "harder"  # 升难度：给更难的题或更深的追问
    EASIER = "easier"  # 降难度：回到更基础的表述与题目
    SUMMARIZE = "summarize"  # 总结：收束本知识点


#: 全部动作的规范顺序。前端展示、Prompt 生成、校验都基于它，避免各处硬编码。
ALL_ACTIONS: tuple[str, ...] = tuple(a.value for a in ActionType)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False, comment="user/assistant/system")
    content: Mapped[str] = mapped_column(String(4096), nullable=False, default="")

    # --------------------------------------------------- Agent 决策留痕
    action_type: Mapped[str | None] = mapped_column(
        String(24), nullable=True, comment="probe/explain/rephrase/harder/easier/summarize"
    )
    reason: Mapped[str] = mapped_column(
        String(512), nullable=False, default="", comment="Agent 选择该动作的理由"
    )
    #: 决策时的学习状态快照。演示与测试靠它断言"动作确实由状态驱动"
    state_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    #: 本轮涉及的（RAG 检索到的）知识点 id。Chroma metadata 只接受标量，故用 JSON 存
    kp_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    #: RAG 来源片段（文件/页码/距离），用于前端展示"依据"
    refs: Mapped[list | None] = mapped_column(JSON, nullable=True)
    #: 生成该消息的主体：模型名 / template（降级时）/ policy（强制动作）
    engine: Mapped[str] = mapped_column(String(32), nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    session: Mapped["Session"] = relationship(back_populates="messages")  # noqa: F821
    evaluations: Mapped[list["AnswerEvaluation"]] = relationship(  # noqa: F821
        back_populates="message", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_message_session", "session_id", "id"),)

    def __repr__(self) -> str:
        return f"<Message {self.id} session={self.session_id} {self.role} {self.action_type}>"
