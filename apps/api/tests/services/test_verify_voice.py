"""核查结论 → 助教话术的翻译测试。

这个模块的价值不在于"能生成一段话"，而在于**两条必须守住的不变量**：

  1. **没联网就不许说查过。** 未配置搜索 Key 时 L3 整层是 skipped，
     如果助教还能说出"我上网查了一下"，那是编的。
  2. **没什么可说的时候必须沉默。** 绝大多数知识点是干净的，
     每轮加一句"这个我也核实过了"会立刻变成噪音。

所以下面大部分用例断言的不是"输出了什么"，而是"没输出什么"。
"""

from __future__ import annotations

import pytest

from app.models.knowledge_check import CheckType, CheckVerdict, KnowledgeCheck
from app.services import verify_voice


def make_check(
    check_type: CheckType,
    verdict: CheckVerdict,
    *,
    reason: str = "",
    confidence: str = "0.7",
) -> KnowledgeCheck:
    """造一条核查记录。

    刻意不落库也不建 session —— 翻译逻辑是纯函数，测试不该依赖数据库。
    """
    return KnowledgeCheck(
        kp_id=1,
        document_id=1,
        check_type=check_type.value,
        verdict=verdict.value,
        confidence=__import__("decimal").Decimal(confidence),
        reason=reason,
        evidence=None,
        source_urls=None,
        engine="test",
    )


# --------------------------------------------------------------------------- #
# 不变量一：没有联网能力就不许声称查过
# --------------------------------------------------------------------------- #
def test_without_web_capability_forbids_claiming_search() -> None:
    """**最重要的一条。** 没有 Key 时必须明确禁止那几句说法。"""
    note = verify_voice.build_credibility_note([], web_effective=False)

    assert note.may_claim_web is False
    # 光说"不要编造"对模型约束力不够，必须把禁止的说法点名列出来
    for phrase in ("我上网查过", "网上资料显示", "外部信息表明"):
        assert phrase in note.text, f"没有明确禁止「{phrase}」"


def test_without_web_capability_even_with_checks() -> None:
    """历史上有过联网结论，但**当前**能力已关闭 —— 仍然不许声称。

    场景：以前配过 Key，跑出过 web 结论，后来 Key 被撤了。
    这时拿旧结论去说"我查过"是拿过期事实当现在的能力用。
    """
    checks = [make_check(CheckType.WEB, CheckVerdict.PASSED)]
    note = verify_voice.build_credibility_note(checks, web_effective=False)

    assert note.may_claim_web is False
    assert "不要出现" in note.text


# --------------------------------------------------------------------------- #
# 不变量二：没什么可说就沉默
# --------------------------------------------------------------------------- #
def test_silent_when_no_checks() -> None:
    """有联网能力但这个知识点没跑过校验 —— 沉默，但允许说"我可以查"。"""
    note = verify_voice.build_credibility_note([], web_effective=True)

    assert note.silent, "没有记录时不该硬造一句话"
    assert note.may_claim_web is True


def test_silent_when_everything_passed() -> None:
    """三层全过是**常态**，不构成说话的理由。

    这是整套设计里最容易写错的地方：把"通过"也当成一条值得汇报的结论，
    结果是每轮都来一句"已核实"，学习者很快就学会无视它。
    """
    checks = [
        make_check(CheckType.RULE, CheckVerdict.PASSED),
        make_check(CheckType.MODEL, CheckVerdict.PASSED),
        make_check(CheckType.WEB, CheckVerdict.PASSED),
    ]
    note = verify_voice.build_credibility_note(checks, web_effective=True)

    assert note.silent, "全部通过时应当沉默"


def test_skipped_is_not_note_silence_trigger() -> None:
    """`skipped`（主动跳过，如常识闸门）不该产生任何话术。"""
    checks = [make_check(CheckType.WEB, CheckVerdict.SKIPPED)]
    note = verify_voice.build_credibility_note(checks, web_effective=True)

    assert note.silent


# --------------------------------------------------------------------------- #
# 有信号时：说对话
# --------------------------------------------------------------------------- #
def test_web_miss_is_reported_as_missing_evidence_not_as_conflict() -> None:
    """**"没查到"不许说成"有出入"。**

    这是删掉"存疑"时立的核心规矩。联网没找到一致表述，只说明**我们没查到**，
    不构成"这条有问题"的证据。一旦助教把它说成"网上说法不一样"，
    学习者会怀疑一条其实没问题的知识点 —— 那比不说还糟。
    """
    checks = [make_check(CheckType.WEB, CheckVerdict.UNSUPPORTED)]
    note = verify_voice.build_credibility_note(checks, web_effective=True)

    assert not note.silent
    assert "没找到" in note.text
    assert "不要说成「网上说法不一样」" in note.text


def test_web_error_does_not_imply_verified() -> None:
    """执行失败 ≠ 没有出入。措辞不能让人以为"查过了、没问题"。"""
    checks = [make_check(CheckType.WEB, CheckVerdict.ERROR)]
    note = verify_voice.build_credibility_note(checks, web_effective=True)

    assert not note.silent
    assert "执行失败" in note.text
    assert "不要暗示已经查过" in note.text


def test_model_reservation_is_surfaced() -> None:
    """模型层的保留意见要带给助教，并带上具体理由。"""
    checks = [
        make_check(CheckType.MODEL, CheckVerdict.UNSUPPORTED, reason="与第 12 页原文表述有出入")
    ]
    note = verify_voice.build_credibility_note(checks, web_effective=True)

    assert not note.silent
    assert "与第 12 页原文表述有出入" in note.text


def test_rule_unsupported_asks_to_mark_as_inference() -> None:
    """原文支撑不足时，要求助教说明"这是推导不是原文"。"""
    checks = [make_check(CheckType.RULE, CheckVerdict.UNSUPPORTED)]
    note = verify_voice.build_credibility_note(checks, web_effective=True)

    assert not note.silent
    assert "推导" in note.text


# --------------------------------------------------------------------------- #
# 别变成噪音
# --------------------------------------------------------------------------- #
def test_always_instructs_not_to_recite() -> None:
    """凡是有话可说时，都必须附上"别照着念、别每轮都提"。"""
    cases = [
        [make_check(CheckType.WEB, CheckVerdict.UNSUPPORTED)],
        [make_check(CheckType.WEB, CheckVerdict.ERROR)],
        [make_check(CheckType.RULE, CheckVerdict.UNSUPPORTED)],
    ]
    for checks in cases:
        note = verify_voice.build_credibility_note(checks, web_effective=True)
        assert "不要照着念" in note.text
        assert "每轮" in note.text


# --------------------------------------------------------------------------- #
# 取"最近一次"而不是"第一条"
# --------------------------------------------------------------------------- #
def test_uses_latest_check_of_each_layer() -> None:
    """校验是事件流，同一层会有多行 —— 必须取最后一次。

    这个函数依赖调用方按 created_at 升序传入（查询侧负责排序）。
    """
    checks = [
        make_check(CheckType.WEB, CheckVerdict.UNSUPPORTED),
        make_check(CheckType.WEB, CheckVerdict.PASSED),  # 后来重跑，通过了
    ]
    note = verify_voice.build_credibility_note(checks, web_effective=True)

    # 最新一次是通过 → 不该再提"不一致"
    assert note.silent, "应当用最后一次结论，而不是第一次"


def test_empty_note_constant_is_really_empty() -> None:
    assert verify_voice.EMPTY_NOTE.silent
    assert verify_voice.EMPTY_NOTE.may_claim_web is False
