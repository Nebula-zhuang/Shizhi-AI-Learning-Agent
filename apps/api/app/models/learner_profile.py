"""学习者画像（P5）—— 长期记忆里"这个人习惯怎么学"的那部分。

## 为什么单独建表，而不是从作答记录实时推导

因为**讲法风格必须稳定**。技术方案的验收是「讲解风格与上次一致」——
如果每轮都从最近的作答重新推导，风格会随最近的几次错因来回横跳，
学习者会觉得"这个老师每次讲得都不一样"，反而更懵。

所以：推导一次、存下来、**只在证据足够充分时才切换**。
表里的 `style_source` 还区分了"自动推导"与"用户手动设定"——
手动设定的永不被自动覆盖。

## 与 learner_kp_states 的分工

| | `learner_kp_states` | `learner_profiles` |
|---|---|---|
| 粒度 | 每个知识点一行 | 每个学习者一行 |
| 回答 | "这个知识点学得怎么样" | "这个人习惯怎么学" |
| 生命周期 | 随作答高频更新 | 低频更新（仅风格确实变化时） |
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base_class import Base


class ExplanationStyle(StrEnum):
    """偏好讲法。每一个都对应 Prompt 里一句**具体**的指令，不是空泛的形容。"""

    BALANCED = "balanced"  # 均衡：还没有足够证据判断
    CONTRAST = "contrast"  # 对比式：把易混概念并排讲清边界
    STRUCTURED = "structured"  # 结构化：给可记忆的框架 / 分类 / 口诀
    STEPWISE = "stepwise"  # 分步式：把推导链一步步补全
    CLARIFY = "clarify"  # 审题式：先澄清问题到底在问什么


class StyleSource(StrEnum):
    DERIVED = "derived"  # 从作答错因自动推导
    MANUAL = "manual"  # 用户手动设定（自动推导不再覆盖）
    DEFAULT = "default"  # 还没有任何依据


class LearnerProfile(Base):
    __tablename__ = "learner_profiles"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    #: 学习者标识，唯一。与 learner_kp_states 用同一个标识口径。
    learner_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, comment="学习者标识，唯一"
    )

    preferred_style: Mapped[str] = mapped_column(
        String(24), nullable=False, default=ExplanationStyle.BALANCED
    )
    style_source: Mapped[str] = mapped_column(
        String(16), nullable=False, default=StyleSource.DEFAULT, comment="derived/manual/default"
    )
    #: 推导依据的快照：各错因的出现次数。展示给用户看"凭什么这么判断"。
    style_evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    #: 汇总统计（已学知识点数、薄弱数、待复习数等），避免看板每次都重算。
    stats: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # ------------------------------------------------------------------ 派生
    @property
    def is_manual(self) -> bool:
        return self.style_source == StyleSource.MANUAL

    def as_dict(self) -> dict:
        return {
            "learner_id": self.learner_id,
            "preferred_style": self.preferred_style,
            "style_source": self.style_source,
            "style_evidence": self.style_evidence or {},
            "stats": self.stats or {},
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:
        return f"<LearnerProfile {self.learner_id} style={self.preferred_style}({self.style_source})>"
