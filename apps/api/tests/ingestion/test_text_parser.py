"""文本解析器测试：编码探测、段落切分、Markdown 结构识别、虚拟分页。"""

from __future__ import annotations

import pytest

from app.ingestion.base import (
    WARN_ENCODING_FALLBACK,
    WARN_VIRTUAL_PAGINATION,
    ParserError,
)
from app.ingestion.text_parser import decode_bytes, parse_text
from app.models.chunk import BlockType

HASH = "test-hash"


def parse(content: str, *, name: str = "笔记.txt") -> object:
    return parse_text(
        content.encode("utf-8"),
        file_name=name,
        file_hash=HASH,
        is_markdown=name.endswith((".md", ".markdown")),
    )


# --------------------------------------------------------------------------- #
# 编码
# --------------------------------------------------------------------------- #
def test_decode_utf8() -> None:
    text, fallback = decode_bytes("梯度下降是优化算法".encode("utf-8"))
    assert text == "梯度下降是优化算法"
    assert fallback is None


def test_decode_gbk_and_report_fallback() -> None:
    raw = "这是一个 GBK 编码的文件".encode("gb18030")
    text, fallback = decode_bytes(raw)
    assert "GBK" in text
    assert fallback is not None, "非 UTF-8 编码必须报告，便于上层记 warning"


def test_parse_reports_encoding_warning() -> None:
    raw = "中文内容需要足够长度才能形成段落。".encode("gb18030")
    parsed = parse_text(raw, file_name="a.txt", file_hash=HASH, is_markdown=False)
    codes = [w.code for w in parsed.warnings]
    assert WARN_ENCODING_FALLBACK in codes


# --------------------------------------------------------------------------- #
# 结构识别
# --------------------------------------------------------------------------- #
def test_markdown_headings_are_detected_with_level() -> None:
    parsed = parse("# 第一章\n\n正文内容。\n\n## 1.1 小节\n\n更多正文。", name="a.md")
    headings = [
        (b.text, b.level)
        for p in parsed.pages
        for b in p.blocks
        if b.type == BlockType.HEADING
    ]
    assert ("第一章", 1) in headings
    assert ("1.1 小节", 2) in headings


def test_numbered_heading_in_plain_text() -> None:
    parsed = parse("第3章 进程管理\n\n进程是程序的一次执行过程，是资源分配的基本单位。")
    headings = [b.text for p in parsed.pages for b in p.blocks if b.type == BlockType.HEADING]
    assert "第3章 进程管理" in headings


def test_long_line_is_not_a_heading() -> None:
    """超过 40 字的行即使以序号开头也不该被当成标题。"""
    long_text = "3.1 " + "这是一段很长的正文内容" * 6
    parsed = parse(long_text)
    types = {b.type for p in parsed.pages for b in p.blocks}
    assert BlockType.HEADING not in types


def test_markdown_table_becomes_table_block() -> None:
    content = "| 维度 | 进程 | 线程 |\n|---|---|---|\n| 资源 | 独立 | 共享 |\n"
    parsed = parse(content, name="a.md")
    tables = [b for p in parsed.pages for b in p.blocks if b.type == BlockType.TABLE]
    assert len(tables) == 1
    # 分隔行 |---|---| 对人没有信息量，应被剔除
    assert "---" not in tables[0].text
    assert "进程" in tables[0].text


def test_code_fence_content_is_kept_intact() -> None:
    """围栏内的 # 不应被当成标题。"""
    content = "说明文字。\n\n```python\n# 这是注释不是标题\nx = 1\n```\n"
    parsed = parse(content, name="a.md")
    headings = [b.text for p in parsed.pages for b in p.blocks if b.type == BlockType.HEADING]
    assert not any("这是注释" in h for h in headings)


# --------------------------------------------------------------------------- #
# 空值处理
# --------------------------------------------------------------------------- #
def test_empty_file_raises() -> None:
    with pytest.raises(ParserError) as exc:
        parse_text(b"   \n  \n", file_name="a.txt", file_hash=HASH, is_markdown=False)
    assert exc.value.code == "EMPTY_FILE"


def test_whitespace_only_content_raises() -> None:
    with pytest.raises(ParserError):
        parse_text(b"\n\n\n\n", file_name="a.txt", file_hash=HASH, is_markdown=False)


# --------------------------------------------------------------------------- #
# 虚拟分页
# --------------------------------------------------------------------------- #
def test_virtual_pagination_splits_long_text() -> None:
    paragraph = "这是一段用于测试虚拟分页的正文。" * 30  # 约 480 字
    parsed = parse("\n\n".join([paragraph] * 20))  # 约 9600 字

    assert parsed.page_count >= 3
    assert [p.page_no for p in parsed.pages] == list(range(1, parsed.page_count + 1))
    codes = [w.code for w in parsed.warnings]
    assert WARN_VIRTUAL_PAGINATION in codes
    assert parsed.meta["pagination"] == "virtual"


def test_short_text_is_single_page() -> None:
    parsed = parse("只有一句话的内容。")
    assert parsed.page_count == 1
    # 单页不需要虚拟分页提示
    assert WARN_VIRTUAL_PAGINATION not in [w.code for w in parsed.warnings]
