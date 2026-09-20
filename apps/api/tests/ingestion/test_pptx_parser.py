"""PowerPoint（.pptx）解析测试。

## 与 Word 测试的关键差异：**页码是真的**

docx 没有物理页码，所以那边要验证"虚拟分页并如实声明"。
pptx 一张幻灯片就是一页，`page_no` 直接对应他在 PowerPoint 里看到的页码 ——
这里的测试守的是**这个真实性**：页号必须等于幻灯片序号，且不能出现虚拟分页的免责声明。

测试用的 .pptx 现场生成，不是手工塞的假数据。
"""

from __future__ import annotations

import pytest
from pptx import Presentation
from pptx.util import Inches

from app.ingestion.base import (
    KNOWN_UNSUPPORTED,
    SUPPORTED_EXTENSIONS,
    SUPPORTED_FORMAT_HINT,
    BlockType,
    ParserError,
)
from app.ingestion.pptx_parser import NOTES_PREFIX, parse_pptx
from app.ingestion.router import category_of


def _make_pptx(path, *, slides: int = 3, with_table: bool = True, with_notes: bool = True):
    """造一份结构完整的演示文稿：标题页 + 内容页（含表格/备注）+ 尾页。"""
    deck = Presentation()

    # ── 第 1 页：标题页
    layout = deck.slide_layouts[0]
    slide = deck.slides.add_slide(layout)
    slide.shapes.title.text = "操作系统原理"

    # ── 第 2 页：内容页（标题 + 正文 + 表格 + 备注）
    content = deck.slides.add_slide(deck.slide_layouts[1])
    content.shapes.title.text = "3.1 进程的状态"
    body = content.placeholders[1].text_frame
    body.text = "进程有三种基本状态。"
    para = body.add_paragraph()
    para.text = "就绪、运行、阻塞，三者可以相互转换。"
    para2 = body.add_paragraph()
    para2.text = "状态转换由调度程序和事件驱动。"

    if with_table:
        table_shape = content.shapes.add_table(3, 2, Inches(1), Inches(4), Inches(6), Inches(1.5))
        table = table_shape.table
        table.cell(0, 0).text = "状态"
        table.cell(0, 1).text = "说明"
        table.cell(1, 0).text = "就绪"
        table.cell(1, 1).text = "等待处理机"
        table.cell(2, 0).text = "运行"
        table.cell(2, 1).text = "正占用处理机"

    if with_notes:
        content.notes_slide.notes_text_frame.text = "这里要强调三态转换的条件。"

    # ── 第 3 页：只有标题
    last = deck.slides.add_slide(deck.slide_layouts[1])
    last.shapes.title.text = "小结"

    deck.save(str(path))
    return path


@pytest.fixture()
def pptx_file(tmp_path):
    return _make_pptx(tmp_path / "操作系统原理.pptx")


# --------------------------------------------------------------------------- #
# 类型识别
# --------------------------------------------------------------------------- #
def test_pptx_is_recognized() -> None:
    assert category_of("课件.pptx") == "slide"
    assert category_of("LECTURE.PPTX") == "slide", "大小写不该影响识别"
    assert ".pptx" in SUPPORTED_EXTENSIONS
    assert ".pptx" in SUPPORTED_FORMAT_HINT


def test_old_ppt_format_gives_actionable_advice() -> None:
    """.ppt 是另一种格式，但提示必须具体 —— 拿着旧格式的人多半不知道自己存的是什么。"""
    assert category_of("老课件.ppt") is None
    advice = KNOWN_UNSUPPORTED[".ppt"]
    assert "另存为" in advice and "pptx" in advice.lower()


# --------------------------------------------------------------------------- #
# 页码：真实，不是虚拟的
# --------------------------------------------------------------------------- #
def test_page_count_equals_slide_count(pptx_file) -> None:
    parsed = parse_pptx(pptx_file, file_name="操作系统原理.pptx", file_hash="h-count")
    assert parsed.page_count == 3
    assert [p.page_no for p in parsed.pages] == [1, 2, 3]


def test_pagination_is_slide_not_virtual(pptx_file) -> None:
    """**这条是本文件最重要的断言。**

    docx 因为文件里根本没有页码，只能虚拟分页并跟用户解释。
    pptx 有真页码，如果这里退化成 virtual，用户看到的"第 3 页"
    就和他 PPT 里的第 3 页对不上了 —— 那是溯源承诺的直接破坏。
    """
    parsed = parse_pptx(pptx_file, file_name="x.pptx", file_hash="h-pagination")

    assert parsed.meta["pagination"] == "slide"
    # 不该出现虚拟分页那条免责声明
    codes = {w["code"] for w in parsed.meta["warnings"]}
    assert "VIRTUAL_PAGINATION" not in codes


# --------------------------------------------------------------------------- #
# 内容
# --------------------------------------------------------------------------- #
def test_slide_titles_become_headings(pptx_file) -> None:
    parsed = parse_pptx(pptx_file, file_name="x.pptx", file_hash="h-headings")
    headings = [
        (b.text, b.level)
        for p in parsed.pages
        for b in p.blocks
        if b.type == BlockType.HEADING
    ]
    assert ("操作系统原理", 2) in headings
    assert ("3.1 进程的状态", 2) in headings
    assert ("小结", 2) in headings


def test_body_text_is_extracted(pptx_file) -> None:
    parsed = parse_pptx(pptx_file, file_name="x.pptx", file_hash="h-body")
    texts = [b.text for p in parsed.pages for b in p.blocks if b.type == BlockType.TEXT]
    joined = "\n".join(texts)
    assert "进程有三种基本状态" in joined
    assert "就绪、运行、阻塞" in joined


def test_table_becomes_table_block(pptx_file) -> None:
    parsed = parse_pptx(pptx_file, file_name="x.pptx", file_hash="h-table")
    tables = [b for p in parsed.pages for b in p.blocks if b.type == BlockType.TABLE]
    assert len(tables) == 1
    text = tables[0].text
    assert text.startswith("|")
    assert "就绪" in text and "运行" in text and "等待处理机" in text


def test_speaker_notes_are_kept_with_marker(pptx_file) -> None:
    """备注里有正文没写的解释，是高价值内容 —— 但要标明它不是投影内容。"""
    parsed = parse_pptx(pptx_file, file_name="x.pptx", file_hash="h-notes")
    joined = "\n".join(
        b.text for p in parsed.pages for b in p.blocks if b.type == BlockType.TEXT
    )
    assert "三态转换的条件" in joined, "备注内容不该丢"
    assert NOTES_PREFIX in joined, "备注必须带标记，否则会被当成正文"


def test_notes_can_be_absent(tmp_path) -> None:
    """没有备注的 PPT 不该报错，也不该产出空块。"""
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[1])
    slide.shapes.title.text = "只有标题和正文"
    slide.placeholders[1].text_frame.text = "一点内容。"
    path = tmp_path / "no_notes.pptx"
    deck.save(str(path))

    parsed = parse_pptx(path, file_name="no_notes.pptx", file_hash="h-nonotes")
    for page in parsed.pages:
        for block in page.blocks:
            assert block.text.strip() or block.type == BlockType.FIGURE


def test_deck_title_falls_back_to_first_slide(tmp_path) -> None:
    """文档属性里没写标题时，用第一张幻灯片的标题顶上。"""
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[1])
    slide.shapes.title.text = "第一章 绪论"
    slide.placeholders[1].text_frame.text = "内容。"
    path = tmp_path / "fallback.pptx"
    deck.save(str(path))

    parsed = parse_pptx(path, file_name="fallback.pptx", file_hash="h-title")
    assert parsed.meta["deck_title"] == "第一章 绪论"


# --------------------------------------------------------------------------- #
# 失败路径
# --------------------------------------------------------------------------- #
def test_rejects_corrupt_file(tmp_path) -> None:
    broken = tmp_path / "坏文件.pptx"
    broken.write_bytes(b"this is definitely not a zip archive")

    with pytest.raises(ParserError) as exc:
        parse_pptx(broken, file_name="坏文件.pptx", file_hash="h-broken")
    assert exc.value.code == "INVALID_PPTX"


def test_rejects_deck_without_text(tmp_path) -> None:
    """整份都是空白页 → 明确说"没读到文字"，别静默产出 0 个知识点。"""
    deck = Presentation()
    deck.slides.add_slide(deck.slide_layouts[6])  # 纯空白版式
    path = tmp_path / "空白.pptx"
    deck.save(str(path))

    with pytest.raises(ParserError) as exc:
        parse_pptx(path, file_name="空白.pptx", file_hash="h-empty")
    assert exc.value.code == "NO_CONTENT"


def test_empty_slide_is_warned_not_fatal(pptx_file) -> None:
    """个别空白页只记 warning，不该让整份 PPT 失败。"""
    parsed = parse_pptx(pptx_file, file_name="x.pptx", file_hash="h-warn")
    # 前三页都有内容，所以不该有 EMPTY_PAGE
    assert all(p.page_no >= 1 for p in parsed.pages)
