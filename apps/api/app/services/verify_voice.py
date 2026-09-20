"""把核查结论翻译成助教能自然说出口的话。

## 为什么需要这一层

P2 做出来的校验结论（`knowledge_checks`）是**给系统看的**：
`check_type=web, verdict=unsupported, confidence=0.62`。

直接塞进提示词有两个问题：
1. **模型会开始报数**。"根据校验结果，置信度 0.62" —— 学习者不需要知道这些。
2. **模型会过度汇报**。每轮都来一句"我已经核实过了"，立刻变成噪音。

所以这里做的是**翻译 + 收束**：把数据变成一句「要不要说、能说什么」，
而不是把数据原样递给它。

## 一条不可让步的约束：不许假装联网查过

未配置搜索 Key 时，L3 整层是 `skipped`。这时候如果助教说
「我上网查了一下，和你的资料一致」——**那是编的**。

所以本模块输出里带一个 `may_claim_web` 开关，
**没真正跑过联网核验时它是 `False`**，提示词里据此明确禁止这类说法。
这不是"体验优化"，是**不能说假话**的问题。

## 另一条：没什么可说的时候，就别说

绝大多数知识点是干净的（L1 通过、L2 没报警、L3 也一致）。
这种情况返回空串 —— 让助教正常讲课，而不是每轮加一句"这个我也核对过了"。
**沉默是默认值，开口要有理由。**
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.knowledge_check import CheckType, CheckVerdict, KnowledgeCheck

#: 各层结论里"值得让助教提一句"的那些。
#: 通过（passed）**不在其中** —— 它是常态，不构成说话的理由。
#:
#: `unsupported` 现在也进来了：它原本散布在 `suspicious` 与 `unsupported` 两处，
#: 合并后由这里统一处理（话术保持"没找到依据"的克制口径，不说"有问题"）。
_NOTEWORTHY = {
    CheckVerdict.UNSUPPORTED,
    CheckVerdict.ERROR,
}


@dataclass(frozen=True)
class CredibilityNote:
    """给助教的核查说明。"""

    #: 提示词里的一段话。**空串表示"没什么要说的"**，助教正常讲课即可
    text: str
    #: 是否允许在话里出现"我上网查过 / 网上说法"这类表述
    may_claim_web: bool

    @property
    def silent(self) -> bool:
        return not self.text


EMPTY_NOTE = CredibilityNote(text="", may_claim_web=False)


def _latest(checks: list[KnowledgeCheck], check_type: CheckType) -> KnowledgeCheck | None:
    """取某一层最近一次的结论。

    校验是**事件流**（同一知识点反复校验会留下多行），所以要用最后一次而不是第一行。
    依赖 `checks` 已按 `created_at` 升序 —— 查询侧保证，这里不再排一次。
    """
    found: KnowledgeCheck | None = None
    for item in checks:
        if item.check_type == check_type.value:
            found = item
    return found


def build_credibility_note(
    checks: list[KnowledgeCheck],
    *,
    web_effective: bool,
) -> CredibilityNote:
    """把某个知识点的核查记录压成一句给助教的话。

    `web_effective`：联网核验这一层**当前能不能跑**（配置了 Key 且开关打开）。
    它决定 `may_claim_web`，与实际有没有跑过是两件事 ——
    即便这次没跑到 L3（比如没到闸门），只要能力在，说"我可以查"也不算撒谎；
    反之能力不在，**一次都不许提**。
    """
    if not web_effective:
        # 没有联网能力时的硬约束。措辞刻意具体，直接把禁止的说法列出来 ——
        # 只说"不要编造"对模型约束力不够。
        return CredibilityNote(
            text=(
                "【关于核查】本次没有联网核验能力。"
                "**不要出现「我上网查过」「网上资料显示」「外部信息表明」这类表述。**"
                "你的依据只有学习者的资料本身 —— 如果他的资料没讲到，就直说"
                "「这部分不在你的资料里」，不要用外部知识冒充已核实的结论。"
            ),
            may_claim_web=False,
        )

    if not checks:
        # 有联网能力，但这个知识点还没跑过校验 —— 没什么可说的
        return CredibilityNote(text="", may_claim_web=True)

    web = _latest(checks, CheckType.WEB)
    model = _latest(checks, CheckType.MODEL)
    rule = _latest(checks, CheckType.RULE)

    parts: list[str] = []

    # ---------------------------------------------------------------- 联网层
    if web is not None:
        if web.verdict == CheckVerdict.UNSUPPORTED:
            parts.append(
                "【关于核查】这个知识点做过联网核验，**外部资料里没找到明确一致的依据**。"
                "如果他问起，可以说明这一点，并提醒他把这条当作「资料里的说法」"
                "而不是「普遍共识」。**不要说成「网上说法不一样」** —— "
                "我们只是没查到，不代表它有问题。"
            )
        elif web.verdict == CheckVerdict.ERROR:
            parts.append(
                "【关于核查】这个知识点的联网核验**执行失败了**（不是没有出入，是没查成）。"
                "不要暗示已经查过；他若问起，就说这次没查到外部依据。"
            )
        # passed：常态，刻意不生成任何话术
    else:
        parts.append(
            "【关于核查】这个知识点**还没有做过联网核验**。"
            "不要声称查过外部资料；但学习者明确问「这个说法对不对/准不准」时，"
            "你可以告诉他你**可以联网查一下**。"
        )

    # -------------------------------------------------- 模型自评层（仅在有信号时提）
    if model is not None and model.verdict in _NOTEWORTHY:
        detail = model.reason.strip()
        if detail:
            parts.append(
                f"【关于核查·模型意见】先前复核时对这条有过保留意见：{detail}"
                "如果这条正好是当前讨论的重点，可以更谨慎一些；否则不必主动提。"
            )

    # ------------------------------------------------------------------ 规则层
    if rule is not None and rule.verdict == CheckVerdict.UNSUPPORTED:
        parts.append(
            "【关于核查·原文支撑】这条里有句子在资料原文中找不到直接依据。"
            "讲解时如果用到它，说清这是**推导**而不是原文原话。"
        )

    if not parts:
        return CredibilityNote(text="", may_claim_web=True)

    parts.append(
        "**这些只是给你自己把握分寸用的，不要照着念，也不要在每轮里都提。**"
        "只有当学习者问起「准不准 / 对不对 / 有没有依据」，"
        "或者这个出入正好会影响他当前这道题的答案时，才用自然的语气说出来。"
    )
    return CredibilityNote(text="\n\n".join(parts), may_claim_web=True)
