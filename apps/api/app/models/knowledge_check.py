"""知识点可信度校验记录（append-only）。

**这张表存在的全部意义：不覆盖 P1 的原始抽取结果，保留可追溯性。**

校验是**可重复发生的事件** —— 同一知识点可能被校验多次（换了模型、改了提示词、
后来补配了 Tavily Key 再跑一遍）。如果把校验结论写成 `knowledge_points` 上的列，
历史必然被后一次覆盖，"可追溯"就成了一句空话。写成**行**则每一次校验都完整留痕。

三层严格分开（对应 docs/05-P2-实施方案.md §5）：
  - `rule`  L1 规则校验，确定性、零成本，所有知识点都跑
  - `model` L2 模型自评，只对 importance 高或 L1 报警的知识点跑
  - `web`   L3 联网核验（Tavily），闸门最严，只对 L2 未通过者跑

`engine` 列记录是谁下的结论（`rule-v1` / `deepseek-chat` / `tavily`），
这样将来换了模型或提示词版本，历史结论依然能解释"当时是谁判的"。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base_class import Base


class CheckType(StrEnum):
    """校验层。三层结果严格分开存储，互不覆盖。"""

    RULE = "rule"
    MODEL = "model"
    WEB = "web"


class CheckVerdict(StrEnum):
    """校验结论。

    `skipped` 与 `error` 是刻意区分的：
      - `skipped`：主动跳过（未配置 Key、命中常识闸门）—— 不是故障
      - `error`：执行了但失败（超时、HTTP 错误）—— 是故障
    把两者混为一谈会让用户无法判断"没校验"到底是设计如此还是出问题了。

    ## 为什么没有 `suspicious`（存疑）

    曾经有过。它的产出规则是"模型觉得说得过头了"或"联网没找到一致证据"这类
    **软信号** —— 结果在一份 108 个知识点的资料上标出了 27 条"存疑"（25%）。

    一个每四条就命中一条的结论，**用户学会的不是"这 27 条有问题"，而是"存疑这个标签没用"**。
    它没有帮人做判断，只是制造了噪音；而"存疑"这个词一旦被无视，
    真正有问题的少数几条也一起被无视了。

    所以这个结论被**删掉**了，而不是调阈值 —— 阈值调到多少都躲不开这个矛盾：
    软信号的信噪比本身就不足以支撑一个用户可见的结论。

    留下的两个结论都是**可验证的**：
      - `passed`：找到了明确依据
      - `unsupported`：没找到依据（**注意：这不等于它有问题**）
    """

    PASSED = "passed"  # 找到了依据
    UNSUPPORTED = "unsupported"  # 没找到依据（≠ 有问题）
    SKIPPED = "skipped"  # 主动跳过（附 reason 说明原因）
    ERROR = "error"  # 执行失败


class KnowledgeCheck(Base):
    """一次校验的完整留痕。"""

    __tablename__ = "knowledge_checks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kp_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("knowledge_points.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )

    check_type: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="rule / model / web"
    )
    verdict: Mapped[str] = mapped_column(String(16), nullable=False, comment="见 CheckVerdict")
    confidence: Mapped[Decimal] = mapped_column(
        Numeric(3, 2), nullable=False, default=Decimal("0.80"), comment="该次校验给出的置信度"
    )

    reason: Mapped[str] = mapped_column(
        String(512), nullable=False, default="", comment="结论理由，面向用户可读"
    )
    evidence: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, comment="结构化证据：命中项 / 模型理由 / 联网命中摘要"
    )
    source_urls: Mapped[list | None] = mapped_column(
        JSON, nullable=True, comment="联网核验的出处链接，仅 web 层有值"
    )

    engine: Mapped[str] = mapped_column(
        String(32), nullable=False, default="", comment="下结论的主体：rule-v1 / 模型名 / tavily"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    knowledge_point: Mapped["KnowledgePoint"] = relationship(lazy="joined")  # noqa: F821

    __table_args__ = (
        Index("ix_check_kp_type", "kp_id", "check_type"),
        Index("ix_check_doc", "document_id"),
    )

    def __repr__(self) -> str:
        return f"<KnowledgeCheck kp={self.kp_id} {self.check_type}:{self.verdict}>"
