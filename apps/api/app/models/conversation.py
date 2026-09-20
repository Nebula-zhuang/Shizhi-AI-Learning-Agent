"""自由学习空间：对话与消息。

## 为什么另建一张表，而不是复用 `sessions`

`sessions` 是 **Tutor 专用**的，它的字段绑死了"一次围绕**某一个知识点**的学习"：

    sessions: learner_id, document_id, knowledge_point_id, title, status, action_count
                                  ^^^^^^^^^^^^  ^^^^^^^^^^^^^^^^^^

自由学习的对话**没有"唯一知识点"** —— 一轮里可能同时用到三个知识点、
一份刚上传的文件、和一次联网搜索；下一轮又完全换一话题。

硬塞进去只有两条路，都不好：
  · 把 `document_id` / `knowledge_point_id` 全填 NULL —— 字段失去意义，
    而且所有查询都要到处判空来区分"这是 Tutor 会话还是自由对话"；
  · 一个问题开一条 session —— 那就不叫对话了。

所以：**两张表各管一件事**。这也呼应"不要把 Conversation / Knowledge / Memory 混成一张表"——
`conversations` 存聊天过程，`learner_kp_states` 存长期学习状态，两者通过 `kp_ids` 弱关联。

## 哪些东西**不**进这张表

- embedding：属于向量库
- 工具的原始入参出参、模型原始响应：属于日志

对话表是**给人看的**。把调试信息塞进来，只会让"导出一次对话"变成导出噪音。
右侧辅助面板需要的信息（用了什么来源、引用哪些网页）用结构化字段单独存，见下面 `citations`。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base_class import Base


class MessageAuthor(StrEnum):
    """这条消息是谁说的。

    刻意不复用 Tutor 的 `MessageRole` —— 那个枚举的取值（user/assistant/system）
    与 Tutor 的教学语义绑定，而复用会让两处的演进互相牵制。
    两张表各自定义，代价只是一个 5 行的枚举。
    """

    USER = "user"
    ASSISTANT = "assistant"


class Conversation(Base):
    """一次自由学习对话。"""

    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    #: 归属账号。所有查询都必须带上它 —— 这是对话的隐私边界。
    learner_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="local", comment="归属账号的学习档案标识"
    )

    #: 标题。首轮之后由问题摘要生成，用户可改名。
    #: 不设 unique —— 两个对话叫同一个名字是完全合理的。
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    #: 消息条数。列表页要显示"这个对话聊了多少轮"，
    #: 存计数比每次 COUNT(*) 聚合便宜（列表页有 N 条对话时差别明显）。
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    #: 列表按它倒序。**刻意不用 updated_at** —— 每次改标题都会刷新 updated_at，
    #: 于是改个名字就把对话顶到列表最前面，与"最近聊过"的直觉不符。
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    messages: Mapped[list["ConversationMessage"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="ConversationMessage.id",
    )

    __table_args__ = (
        # 列表查询恒定是"我的对话，按最近聊过排序"
        Index("ix_conversation_learner_recent", "learner_id", "last_message_at"),
    )

    def __repr__(self) -> str:
        return f"<Conversation {self.id} learner={self.learner_id} {self.title[:16]!r}>"


class ConversationMessage(Base):
    """对话里的一条消息。"""

    __tablename__ = "conversation_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )

    role: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="user / assistant，见 MessageAuthor"
    )
    #: 正文。**必须用 Text 而不是 String(N)。**
    #: MySQL 的 VARCHAR 在 utf8mb4 下每字符占 4 字节，单列上限 65535 字节，
    #: 于是 VARCHAR 最大只能到 16383 —— 写 `String(20000)` 会直接报 1074。
    #: 而且助教的长回答本来就可能超过 16K 字符，Text 才是对的类型。
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")

    #: 本轮用到的**外部能力**，形如：
    #:   [{"kind": "knowledge_base", "count": 4},
    #:    {"kind": "web_search", "query": "Java 25 虚拟线程"},
    #:    {"kind": "attachment", "document_id": 69, "file_name": "os.pdf"}]
    #:
    #: 存的是**给右侧面板展示用的摘要**，不是工具原始入参出参 ——
    #: 后者进日志。这样"这一轮为什么提到了那份 PDF"是可回答的，
    #: 而"当时传给检索工具的向量是多少维"这种问题不会污染对话表。
    sources: Mapped[list | None] = mapped_column(JSON, nullable=True)

    #: 引用来源。联网时是网页（title/url），引用资料时是文档出处（document_id/page）。
    citations: Mapped[list | None] = mapped_column(JSON, nullable=True)

    #: 附件元信息（文件名、类型、大小、处理状态）。
    #: 附件本体走 documents 表与磁盘，这里只存"这一轮用户带了什么"。
    attachments: Mapped[list | None] = mapped_column(JSON, nullable=True)

    #: 本轮的自然语言状态轨迹，形如 ["正在翻你的资料", "正在查最新说法"]。
    #:
    #: **为什么留痕**：这些状态是流式过程中逐条推给用户的，
    #: 刷新页面后如果只剩答案，"它到底查没查"就无从判断了 ——
    #: 而这恰恰是用户最关心的问题之一（"你联网了吗？"）。
    status_trace: Mapped[list | None] = mapped_column(JSON, nullable=True)

    #: 生成失败时的降级原因。为空表示正常生成。
    degraded_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")

    __table_args__ = (Index("ix_conversation_message_conv", "conversation_id", "id"),)

    def __repr__(self) -> str:
        return f"<ConversationMessage {self.id} conv={self.conversation_id} {self.role}>"
