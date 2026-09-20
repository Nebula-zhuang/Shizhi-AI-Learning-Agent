"""文本层质量检测测试。

背景：某些 PDF 的文本层是坏的（字体缺少字形映射、编码错乱），提取出来全是 `·`、`□`
之类的符号。这类文本会让知识点抽取**静默返回空结果** —— 用户看到「处理成功但零知识点」，
完全无从判断原因。这个检测就是为了把这种情况变成可解释的现象。

真实触发场景：用默认西文字体往 PDF 里写中文，母语字符没有字形，提取回来就全是 `·`。
"""

from __future__ import annotations

from app.ingestion.base import looks_garbled, meaningful_ratio

# 模拟真实坏 PDF 提取出的内容：中文字符全变成中点
GARBLED = "3.1 ·····\n" + "·" * 200 + "\n- 1 -\n······ · ··· ····"

# 正常的课程材料
HEALTHY = (
    "进程是程序的一次执行过程，是系统进行资源分配和调度的基本单位。"
    "进程具有动态性、并发性、独立性和异步性四个基本特征。"
    "线程是处理机调度的基本单位，同一进程内的线程共享地址空间。"
)

# 英文材料，同样应该被判为健康
ENGLISH = (
    "A process is an instance of a computer program that is being executed. "
    "It contains the program code and its current activity. Threads share "
    "the address space of the process that created them."
)


def test_garbled_text_is_detected() -> None:
    assert looks_garbled(GARBLED) is True
    assert meaningful_ratio(GARBLED) < 0.5


def test_healthy_chinese_is_not_flagged() -> None:
    assert looks_garbled(HEALTHY) is False
    assert meaningful_ratio(HEALTHY) > 0.9


def test_healthy_english_is_not_flagged() -> None:
    assert looks_garbled(ENGLISH) is False


def test_short_text_is_never_flagged() -> None:
    """文本过短时不判定，避免把「只有几个符号的短块」误报成乱码。"""
    assert looks_garbled("· ··") is False
    assert looks_garbled("") is False
    assert looks_garbled("   ") is False


def test_mixed_content_with_majority_readable_passes() -> None:
    """夹杂少量符号的正常排版（项目符号、公式占位）不应该被判为乱码。"""
    mixed = (
        "算法步骤：\n"
        "1. 初始化参数 θ\n"
        "2. 计算梯度 ∇L(θ)\n"
        "3. 沿负梯度方向更新参数\n"
        "4. 重复直到收敛\n"
    ) * 3
    assert looks_garbled(mixed) is False


def test_ratio_handles_whitespace_and_punctuation() -> None:
    """空白不计入分母，纯标点则大幅拉低比值。"""
    assert meaningful_ratio("   ") == 0.0
    assert meaningful_ratio("ab cd 12") == 1.0
    assert meaningful_ratio("............") == 0.0
