"""用户**主动保存**的知识。

## 为什么必须是独立的表（而不是复用已有的任何一张）

设计依据：`docs/25a-自由学习空间设计.md` §3 ——
「**不要把 Conversation、Knowledge、Memory 混成一张表**」。

四者描述的是**四件不同的事**，硬合会各自变形：

| 表 | 回答的问题 | 谁产生 |
|---|---|---|
| `conversation_messages` | 「我当时聊了什么」 | 每一轮对话，**自动**落库 |
| `knowledge_points` | 「这份资料讲了什么」 | 摄取流水线，**自动**抽取 |
| `learner_kp_states` | 「我掌握得怎么样」 | 作答评估，**自动**更新 |
| **`saved_knowledge`** | 「**我特意留下**了什么」 | **用户手动**点保存 |

差别在**意图**：前三张是系统的副产物，这一张是用户的决定。
把它塞进 `conversation_messages` 的后果是——对话表要开始承载"检索语义"
（哪些消息可被跨对话搜到），于是每次查历史都得判类型；
塞进 `knowledge_points` 更糟：那个 `document_id` 非空，而保存的内容
**不来自任何一份资料**，只能全填 NULL。

## 几个字段的取舍

- **`question` / `answer` 冗余存一份**：保存的是"那一轮"的快照。
  若只存 `source_message_id` 再去 join，消息被删或对话被清空后，
  用户保存的东西就跟着消失了 —— 而"保存"这个动作的语义恰恰是
  **"即使对话没了它也还在"**。所以正文自己存。
  `source_message_id` 只做**回溯**用，允许为空、允许指向已删除的消息。
- **`source_message_id` 不设外键**：消息可能被删（对话删除时级联），
  加了外键会连带删掉用户**特意**保存的内容 —— 与上一条自相矛盾。
- **`kp_ids` / `tags` / `source_urls` 用 JSON**：都是"相关的东西"，
  形状随能力增减而变，拆关联表不值当；且它们**不参与检索**
  （检索走 `embedding_id` 指向的向量）。
- **`embedding_id` 可为空**：向量是**派生数据**。写向量失败不该让保存失败 ——
  用户的意图是"存下来"，向量只是"将来能被搜到"的加速手段。
  失败时留 NULL，可以事后回填。见 `saved_knowledge_service`。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base_class import Base


class SavedKnowledge(Base):
    """一条用户主动保存的问答。"""

    __tablename__ = "saved_knowledge"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    #: 归属的学习档案标识。沿用全项目统一的 `learner_id`（String(64)），
    #: 与 `conversations` / `learner_profiles` 保持完全一致的隔离口径。
    learner_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="local", comment="归属账号的学习档案标识"
    )

    #: 保存的那一轮问题。**快照**，不依赖消息表还活着。
    #: 用 Text 而不是 String(N)：问题可能很长，且 MySQL utf8mb4 下
    #: VARCHAR 上限只有 16383 字符（见 `ConversationMessage.content` 的注释）。
    question: Mapped[str] = mapped_column(Text, nullable=False, default="")

    #: 保存的那一轮回答。同样是快照，理由见模块开头。
    answer: Mapped[str] = mapped_column(Text, nullable=False, default="")

    #: 回溯到对话里的那条助手消息。**刻意不设外键**：
    #: 消息随对话删除时不该连带删掉用户特意保存的内容（见模块开头）。
    source_message_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="回溯到 conversation_messages.id，可为空"
    )

    #: 联网来源，形如 `[{"title": "...", "url": "..."}]`。
    source_urls: Mapped[list | None] = mapped_column(JSON, nullable=True)

    #: 相关知识点 id，形如 `[12, 34]`。**仅供展示与跳转，不参与检索**。
    kp_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)

    #: 用户标签。Phase 3A 只存不自动生成（自动推荐留给 3.5）。
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True)

    #: 向量库里的 id。为空表示这条**暂时搜不到**（写向量失败，可事后回填）。
    embedding_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="向量库 id，空 = 尚未建立索引"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # 列表查询恒定是"我保存的，按时间倒序"
        Index("ix_saved_knowledge_learner_recent", "learner_id", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<SavedKnowledge {self.id} learner={self.learner_id} {self.question[:16]!r}>"
