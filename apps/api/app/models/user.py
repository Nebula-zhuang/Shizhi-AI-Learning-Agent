"""用户账号。

## 为什么这个表是"用户"和"学习数据"之间唯一的接缝

P0–P5 的学习数据（作答状态、讲法偏好、会话）全都挂在 `learner_id` 这个字符串上，
而不是挂在某个自增主键上。当时这么设计是因为**还没有用户概念**，
但现在成了好事：接入账号体系不需要动任何一张既有表 ——
只要让每个账号有一个自己的 `learner_id`，学习数据就天然隔离了。

于是这张表的职责被压缩到两件事：

1. **证明"你是你"**（用户名 + 口令哈希）
2. **告诉系统"你的学习数据在哪"**（`learner_id`）

`learner_id` 与用户主键分开、不复用同一个值，是为了保留一种可能性：
将来一个账号想带多份学习档案（比如"考研"和"期末"两套进度），
只要再生成一个 learner_id 即可，不用改主键。

## 口令怎么存的

只存 bcrypt 哈希（见 `app/core/security.py`），**永远不存明文、也不存可逆密文**。
字段长度给到 128 是因为 bcrypt 的输出本身是 60 字符，
留出余量便于将来换算法（argon2 的编码更长）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    #: 登录名。唯一、大小写不敏感地查重（入库前统一转小写）。
    username: Mapped[str] = mapped_column(
        String(32), nullable=False, unique=True, index=True, comment="登录名，唯一"
    )
    #: bcrypt 哈希，绝不放明文。
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False, comment="口令哈希")
    #: 展示名。默认与登录名相同，允许中文。
    display_name: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    #: 该账号的学习数据归属标识，与 learner_kp_states / learner_profiles / sessions 对齐。
    learner_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True, comment="学习数据归属标识"
    )

    #: 停用账号用（不删数据）。登录时直接拒绝。
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # ------------------------------------------------------------------ 派生
    @property
    def name(self) -> str:
        return self.display_name or self.username

    def as_dict(self) -> dict:
        """对外表示。**绝不包含 password_hash** —— 这个函数是它唯一的出口，
        所以只要这里不写，接口就不可能出现口令哈希。"""
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.name,
            "learner_id": self.learner_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
        }

    def __repr__(self) -> str:
        return f"<User {self.id} {self.username}>"
