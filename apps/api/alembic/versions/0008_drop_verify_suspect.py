"""移除「存疑」结论。

Revision ID: 0008_drop_verify_suspect
Revises: 0007_p6_document_owner
Create Date: 2026-09-20

## 为什么删掉它

`CheckVerdict.SUSPICIOUS`（存疑）的产出规则是**软信号** ——
"模型觉得说得过头了"、"联网没找到一致证据"。它在一份 108 个知识点的资料上
标出了 27 条存疑；而在本项目当前的真实库里是 **67 条 suspect + 3 条 conflict /
174 个知识点（40%）**。

**一个每两三条就命中一条的警示标签，用户学会的不是"去查那几条"，
而是"这个标签没用"** —— 于是真正有问题的少数几条也一起被无视了。
这不是阈值没调好，是软信号的信噪比本身不足以支撑一个用户可见的结论。

## 这个迁移做两件事

1. `knowledge_checks.verdict`：`suspicious` → `unsupported`（91 行）
   语义上是**变准确的**：原来的含义是"我们没查到依据"，那就如实说"没找到依据"，
   而不是暗示"这条有问题"。

2. `knowledge_points.verify_status`：`suspect` / `conflict` → `unverified`
   （67 + 3 行）。这些状态本来就是从上面那批 verdict 汇总出来的，
   verdict 降级之后状态必须跟着走，否则界面上会留着一批**再也刷新不掉**的旧标记。

## 为什么 `conflict` 也一起改

新汇总逻辑里 `unsupported` 不再升级成 `conflict`：
**"找不到依据"和"有出入"是两件事**，拿前者冒充后者就是在冤枉知识点。
于是 `conflict` 变成了不可达状态，历史行也就没有保留的理由。

`VerifyStatus.CONFLICT` 作为**枚举值保留**（历史数据要能原样读出来），
只是当前不产出 —— 和 `OUTDATED` 一样的处理方式，等有更强的比对手段再启用。

## downgrade 为什么不还原

`downgrade` 只能把 `unsupported` 改回 `unverified`（回到"没核对过"），
**无法还原成原来的 `suspicious`** —— 91 行里哪一行原本是 `suspicious`、
哪一行本来就是 `unsupported`（5 行），合并之后这个信息已经丢了。

所以这里**明确不提供 downgrade**，而不是写一个会假装还原成功的空实现：
一个撒谎的 downgrade 比没有 downgrade 更危险。
需要回退就重跑一次校验（`POST /api/documents/{id}/verify`），
新逻辑会重新产出结论。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008_drop_verify_suspect"
down_revision: str | None = "0007_p6_document_owner"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 逐行改而不是整体重算：这两个表上已经有历史结论，
    # 重算需要调模型和联网，代价高且不可重复。
    op.execute(
        "UPDATE knowledge_checks SET verdict = 'unsupported' WHERE verdict = 'suspicious'"
    )
    op.execute(
        "UPDATE knowledge_points SET verify_status = 'unverified' "
        "WHERE verify_status IN ('suspect', 'conflict')"
    )


def downgrade() -> None:
    """**故意不实现。**

    合并之后无法区分"原本是 suspicious"和"原本就是 unsupported"，
    任何"还原"都会是编造的。见文件头的说明。
    """
    raise NotImplementedError(
        "存疑结论的移除不可逆（合并后丢失了原始区分）。"
        "如需重新得到结论，请对资料重新执行一次校验。"
    )
