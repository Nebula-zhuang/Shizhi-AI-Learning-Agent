"""待办事项。

## 为什么另建一张表

依据 `app/models/saved_knowledge.py` 那条既有价值观 ——
**「不要把 Conversation、Knowledge、Memory 混成一张表」**：每张表只回答一个问题。

| 表 | 回答的问题 | 谁产生 |
|---|---|---|
| `conversation_messages` | 「我当时聊了什么」 | 每一轮对话，自动 |
| `knowledge_points` | 「这份资料讲了什么」 | 摄取流水线，自动 |
| `learner_kp_states` | 「我掌握得怎么样」 | 作答评估，自动 |
| `saved_knowledge` | 「我特意留下了什么」 | 用户手动 |
| **`todos`** | 「**我要做什么**」 | **用户手动** |

待办不属于上面任何一个问题：它既不是对话、也不是知识点、更不是掌握度。
塞进任何一张都会让那张表开始承载两种语义。

## 几个字段的取舍

- **`learner_id` 不设外键**：沿用 `conversations` / `sessions` / `saved_knowledge`
  的做法，只按字符串隔离数据。加了外键会引入级联删除的语义，
  而"删账号"这件事目前不由数据库负责。
- **`due_date` 用 `Date` 而不是 `DateTime`**：待办问的是"**哪一天**到期"，
  不是"几点几分"。用 `DateTime` 会逼着前端去编一个时刻（通常是 00:00 或 23:59），
  而这个编出来的时刻会进比较逻辑 —— 于是"今天到期"变得取决于时区与编法。
  用 `Date` 就没有这个自由度，也就没有这个 bug。
- **`completed` 而不是 `status` 枚举**：MVP 只有"做完没做完"两种状态。
  枚举是为了承载**将来**的状态（进行中 / 已取消…），现在加就是在猜。
- **`updated_at` 用 `onupdate=func.now()`**：与 `learner_kp_state` 同一做法 ——
  由数据库侧兜底，不依赖每个 service 记得手动改。
- **`title` 用 `String(255)`**：待办是一句话，不该是一篇文章。
  真正的长度约束在 schema 层（`max_length`），这里只保证列够宽。
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base


class Todo(Base):
    """一条待办。归属靠 `learner_id` 字符串，沿 `documents.owner_learner_id` 那套思路。"""

    __tablename__ = "todos"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    learner_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="local", comment="归属账号的学习档案标识"
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False, comment="一句话，非空")

    completed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="做完没做完"
    )

    due_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="哪一天到期；空 = 没有截止日"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        # 列表查询恒定是"我的，未完成优先，按时间"。
        # `completed` 放进索引是刻意的：列表要按它分段，而不是在内存里排。
        Index("ix_todos_learner_completed_created", "learner_id", "completed", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"<Todo id={self.id} learner={self.learner_id!r} done={self.completed}>"
