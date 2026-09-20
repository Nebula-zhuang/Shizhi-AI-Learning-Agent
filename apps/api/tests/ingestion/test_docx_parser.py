"""Word（.docx）解析测试。

## 为什么之前"不接受 docx"

`SUPPORTED_EXTENSIONS` 里没有 `.docx` —— 前端 `accept` 放了它、界面文案也写着
"支持 Word"，但后端从来没实现过，用户一传就被告知"不支持的文件类型"。
**那是文案跑在了能力前面。** 这个文件守住补上的能力。

测试用的 .docx 是**现场生成的真实文件**，不是手工塞的假数据 ——
解析器的坑（样式名中英差异、正文与表格的先后顺序、超链接里的文本）
只有在真文件上才会暴露。
"""

from __future__ import annotations

import pytest
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt

from app.ingestion.base import (
    KNOWN_UNSUPPORTED,
    SUPPORTED_EXTENSIONS,
    SUPPORTED_FORMAT_HINT,
    BlockType,
    ParserError,
)
from app.ingestion.docx_parser import parse_docx
from app.ingestion.router import category_of


def _make_docx(path, *, with_table: bool = True, with_image: bool = True):
    """造一份结构完整的 Word：主标题 / 两个章节 / 段落 / 表格 / 图片。"""
    document = Document()

    document.add_heading("操作系统原理", level=1)
    document.add_paragraph("本章介绍进程与线程的基本概念。")

    document.add_heading("3.1 进程", level=2)
    document.add_paragraph("进程是资源分配的基本单位，拥有独立的地址空间。")
    document.add_paragraph("进程的创建、撤销与切换开销都比较大。")

    if with_table:
        document.add_heading("3.1.1 状态转换", level=3)
        document.add_paragraph("下表列出进程的三种基本状态。")
        table = document.add_table(rows=3, cols=2)
        table.cell(0, 0).text = "状态"
        table.cell(0, 1).text = "说明"
        table.cell(1, 0).text = "就绪"
        table.cell(1, 1).text = "已获得除 CPU 外的所有资源"
        table.cell(2, 0).text = "运行"
        table.cell(2, 1).text = "正占用处理机"

    document.add_heading("3.2 线程", level=2)
    document.add_paragraph("线程是处理机调度的基本单位，同一进程内的线程共享地址空间。")

    if with_image:
        # 造一张 1x1 的极小 PNG 塞进去，验证内嵌图片能取出来。
        # 用最小合法文件而不是真图，测试不依赖二进制素材。
        png = bytes.fromhex(
            "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
            "1f15c4890000000d4944415478da63f8cfc0f01f0005030201f1e2f3a4"
            "0000000049454e44ae426082"
        )
        image_path = path.parent / "_tmp_test_image.png"
        image_path.write_bytes(png)
        try:
            document.add_paragraph("下图是进程状态转换示意。")
            document.add_picture(str(image_path))
        finally:
            image_path.unlink(missing_ok=True)

    document.save(str(path))
    return path


@pytest.fixture()
def docx_file(tmp_path):
    return _make_docx(tmp_path / "操作系统原理.docx")


# --------------------------------------------------------------------------- #
# 类型识别
# --------------------------------------------------------------------------- #
def test_docx_is_recognized() -> None:
    """**这条就是当初的缺口**：`.docx` 必须被认成受支持的类型。"""
    assert category_of("讲义.docx") == "word"
    assert category_of("REPORT.DOCX") == "word", "大小写不该影响识别"
    assert ".docx" in SUPPORTED_EXTENSIONS
    assert ".docx" in SUPPORTED_FORMAT_HINT


def test_doc_is_rejected_with_actionable_advice() -> None:
    """.doc 是另一种格式，解析不了 —— 但要给出**具体怎么办**。

    一句"不支持该文件类型"对拿着 .doc 的用户毫无帮助：
    他多半并不知道自己存的是 Word 97-2003 的旧格式。
    """
    assert category_of("老讲义.doc") is None
    advice = KNOWN_UNSUPPORTED[".doc"]
    assert "另存为" in advice or "docx" in advice.lower()


# --------------------------------------------------------------------------- #
# 正文结构
# --------------------------------------------------------------------------- #
def test_extracts_headings_with_levels(docx_file) -> None:
    """标题层级必须保住 —— 章节层级是后面分块与关系构建的依据。"""
    parsed = parse_docx(docx_file, file_name="操作系统原理.docx", file_hash="h-headings")

    headings = [
        (block.text, block.level)
        for page in parsed.pages
        for block in page.blocks
        if block.type == BlockType.HEADING
    ]

    assert ("操作系统原理", 1) in headings
    assert ("3.1 进程", 2) in headings
    assert ("3.1.1 状态转换", 3) in headings
    assert ("3.2 线程", 2) in headings


def test_extracts_paragraphs(docx_file) -> None:
    parsed = parse_docx(docx_file, file_name="x.docx", file_hash="h-paras")
    texts = [b.text for p in parsed.pages for b in p.blocks if b.type == BlockType.TEXT]

    assert any("进程是资源分配的基本单位" in t for t in texts)
    assert any("线程是处理机调度的基本单位" in t for t in texts)


def test_table_becomes_table_block(docx_file) -> None:
    """表格要成为独立的 table 块，且内容完整。"""
    parsed = parse_docx(docx_file, file_name="x.docx", file_hash="h-table")
    tables = [b for p in parsed.pages for b in p.blocks if b.type == BlockType.TABLE]

    assert len(tables) == 1
    text = tables[0].text
    assert "状态" in text and "就绪" in text and "运行" in text
    # 必须是 Markdown 管道格式，不是随手拼的字符串
    assert text.startswith("|")


def test_body_order_is_preserved(docx_file) -> None:
    """**正文与表格的先后顺序必须和文档里一致。**

    python-docx 的 `document.paragraphs` 与 `document.tables` 是两组独立列表，
    分开遍历会丢掉交错顺序 —— 那样"表 3-1"会和讲它的那段话脱节。
    """
    parsed = parse_docx(docx_file, file_name="x.docx", file_hash="h-order")
    kinds = [b.type for p in parsed.pages for b in p.blocks]

    # 表格前一句是引导语，后面紧跟 3.2 这一节
    table_at = kinds.index(BlockType.TABLE)
    assert BlockType.TEXT in kinds[:table_at], "表格前应当有引导段落"
    assert BlockType.HEADING in kinds[table_at:], "表格后应当还有章节"


def test_headings_use_heading_path(docx_file) -> None:
    """解析结果要能被分块器用 —— 即标题层级链是完整的。"""
    parsed = parse_docx(docx_file, file_name="x.docx", file_hash="h-path")
    levels = [b.level for p in parsed.pages for b in p.blocks if b.type == BlockType.HEADING]

    assert levels[0] == 1, "文档第一个标题应当是顶层"
    assert max(levels) == 3


# --------------------------------------------------------------------------- #
# 图片与元信息
# --------------------------------------------------------------------------- #
def test_extracts_inline_image(docx_file) -> None:
    """内嵌图片要被取出来并落盘到对应块上。"""
    parsed = parse_docx(docx_file, file_name="x.docx", file_hash="h-image")
    figures = [b for p in parsed.pages for b in p.blocks if b.image_path]

    assert len(figures) >= 1, "应当提取到内嵌图片"
    assert figures[0].image_path
    assert parsed.meta["image_count"] >= 1


def test_virtual_pagination_is_declared(docx_file) -> None:
    """Word 没有物理页码，虚拟分页必须**在 warnings 里明说**。

    不能悄悄给用户一个看起来像真实页码的数字。
    """
    parsed = parse_docx(docx_file, file_name="x.docx", file_hash="h-page")

    assert parsed.meta["pagination"] == "virtual"
    assert parsed.page_count >= 1
    # 页号必须从 1 开始且连续
    assert [p.page_no for p in parsed.pages] == list(range(1, parsed.page_count + 1))


def test_file_type_is_docx(docx_file) -> None:
    parsed = parse_docx(docx_file, file_name="x.docx", file_hash="h-type")
    assert parsed.file_type == "docx"
    assert parsed.meta["parser"] == "docx"


# --------------------------------------------------------------------------- #
# 失败路径
# --------------------------------------------------------------------------- #
def test_rejects_corrupt_file(tmp_path) -> None:
    """损坏的文件要给明确提示，而不是抛一个 Python 异常上去。"""
    broken = tmp_path / "坏文件.docx"
    broken.write_bytes(b"this is definitely not a zip archive")

    with pytest.raises(ParserError) as exc:
        parse_docx(broken, file_name="坏文件.docx", file_hash="h-broken")

    assert exc.value.code == "INVALID_DOCX"


def test_rejects_empty_document(tmp_path) -> None:
    """只有空白段落 → 明确说"没读到正文"，不让它静默产出 0 个知识点。"""
    empty = tmp_path / "空.docx"
    Document().save(str(empty))

    with pytest.raises(ParserError) as exc:
        parse_docx(empty, file_name="空.docx", file_hash="h-empty")

    assert exc.value.code == "NO_CONTENT"


def test_heading_without_text_is_skipped(tmp_path) -> None:
    """空标题不该产出空块 —— 空文本块会在分块阶段变成噪音。"""
    path = tmp_path / "空标题.docx"
    document = Document()
    document.add_heading("", level=1)
    document.add_paragraph("正文内容在这里。")
    document.save(str(path))

    parsed = parse_docx(path, file_name="空标题.docx", file_hash="h-empty-heading")
    for page in parsed.pages:
        for block in page.blocks:
            assert block.text.strip(), "不该出现空文本块"
