"""基础设施表：app_meta。

刻意不含任何业务语义 —— 它是用来承载「系统级元信息」的键值表，
同时作为 P0 阶段验证 ORM → Alembic → MySQL 整条链路的载体。

P1 起按技术方案的第九节逐步加入业务表：
    users / documents / chunks / knowledge_points / knowledge_relations /
    knowledge_checks / sessions / messages / answer_evaluations / learner_kp_states
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

# 从 base_class 导入而非 db.base：后者会反向导入本模块，形成循环
from app.db.base_class import Base


class AppMeta(Base):
    """系统元信息键值表。"""

    __tablename__ = "app_meta"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, comment="配置键")
    value: Mapped[str] = mapped_column(Text, nullable=False, default="", comment="配置值")
    description: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="用途说明"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<AppMeta key={self.key!r} value={self.value!r}>"
