"""PDF 解析器测试。

测试素材**现场生成**，不依赖仓库里的二进制样例：
可重复、可审查、不含版权内容，也不会随时间腐坏。

覆盖：页码、页眉页脚剔除、跨块段落合并、标题识别、图片提取、加密与异常处理。
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pymupdf
import pytest

from app.core.config import settings
from app.ingestion import storage
from app.ingestion.base import ParserError
from app.ingestion.pdf_parser import _looks_like_heading, parse_pdf, probe_pdf
from app.models.chunk import BlockType

HEADER_TEXT = "操作系统原理 · 课程讲义"
BODY_FONT = 11
HEADING_FONT = 17

#: 每页正文行宽（字符），用于模拟真实的硬换行
WRAP = 34


@pytest.fixture
def workdir():
    """为每个用例分配独立的 file_hash，并在结束后清理落盘文件。"""
    file_hash = f"test{uuid.uuid4().hex[:12]}"
    yield file_hash
    target = storage.document_dir(file_hash)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)


def make_pdf(
    path: Path,
    *,
    pages: int = 3,
    with_header: bool = True,
    with_page_number: bool = True,
    with_images: bool = True,
    blank_pages: tuple[int, ...] = (),
) -> None:
    """生成一份结构受控的测试 PDF。"""
    doc = pymupdf.open()
    cjk = pymupdf.Font("china-s")

    for index in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_font(fontname="F0", fontbuffer=cjk.buffer)

        if index + 1 in blank_pages:
            continue  # 留出"无文本层"的页面

        if with_header:
            page.insert_text((72, 45), HEADER_TEXT, fontname="F0", fontsize=9)
        if with_page_number:
            # 逐页不同 —— 必须靠归一化（去数字）才能识别为页脚
            page.insert_text((280, 807), f"- 第 {index + 1} 页 -", fontname="F0", fontsize=9)

        y = 100
        page.insert_text((72, y), "第3章 进程管理", fontname="F0", fontsize=HEADING_FONT)
        y += 40

        paragraph = (
            "进程是程序的一次执行过程，是系统进行资源分配和调度的基本单位。"
            "进程实体由程序段、数据段和进程控制块三部分组成。"
        )
        for offset in range(0, len(paragraph), WRAP):
            page.insert_text(
                (72, y), paragraph[offset : offset + WRAP], fontname="F0", fontsize=BODY_FONT
            )
            y += 18

        if with_images and index == 0:
            rect = pymupdf.Rect(72, y + 20, 292, y + 160)
            pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 220, 140))
            pixmap.set_rect(pixmap.irect, (180, 120, 90))
            page.insert_image(rect, pixmap=pixmap)

    doc.save(str(path))
    doc.close()


# --------------------------------------------------------------------------- #
# 页码与结构
# --------------------------------------------------------------------------- #
def test_page_numbers_match_source(tmp_path: Path, workdir: str) -> None:
    source = tmp_path / "doc.pdf"
    make_pdf(source, pages=4)

    parsed = parse_pdf(source, file_name="doc.pdf", file_hash=workdir)

    assert parsed.page_count == 4
    assert [p.page_no for p in parsed.pages] == [1, 2, 3, 4]


def test_header_and_footer_removed(tmp_path: Path, workdir: str) -> None:
    """跨页重复的页眉与逐页变化的页码都应被剔除。"""
    source = tmp_path / "doc.pdf"
    make_pdf(source, pages=5)

    parsed = parse_pdf(source, file_name="doc.pdf", file_hash=workdir)
    all_text = "\n".join(b.text or "" for _, b in parsed.iter_blocks())

    assert HEADER_TEXT not in all_text, "重复页眉应被剔除"
    assert "第 1 页" not in all_text, "页码归一化后应被识别为页脚并剔除"
    assert "第 3 页" not in all_text
    # 正文必须保留
    assert "进程是程序的一次执行过程" in all_text


def test_single_occurrence_stamp_is_kept(tmp_path: Path, workdir: str) -> None:
    """只出现一两次的文本不应当作页眉页脚 —— 它可能是正文。"""
    source = tmp_path / "doc.pdf"
    make_pdf(source, pages=2)

    parsed = parse_pdf(source, file_name="doc.pdf", file_hash=workdir)
    all_text = "\n".join(b.text or "" for _, b in parsed.iter_blocks())
    # 只有 2 页，未达到 3 次阈值，页眉应保留
    assert HEADER_TEXT in all_text


# --------------------------------------------------------------------------- #
# 段落合并与标题
# --------------------------------------------------------------------------- #
def test_visual_lines_are_merged_into_paragraph(tmp_path: Path, workdir: str) -> None:
    """每一视觉行是独立文本块时，必须重新拼成语义段落。

    否则一个 3 行的段落会变成 3 个十几字的碎块，分块与抽取质量都会崩坏。

    注意：本用例只有 1 页，页眉页脚未达到「重复 ≥3 次」的阈值因而会被保留 ——
    这是预期行为（只出现一两次的文本可能是正文），所以这里只针对正文块断言。
    """
    source = tmp_path / "doc.pdf"
    make_pdf(source, pages=1)

    parsed = parse_pdf(source, file_name="doc.pdf", file_hash=workdir)
    paragraphs = [
        b.text or ""
        for _, b in parsed.iter_blocks()
        if b.type == BlockType.TEXT and "进程是程序" in (b.text or "")
    ]

    assert len(paragraphs) == 1, f"被排版切碎的段落应合并成一块，实际得到 {len(paragraphs)} 块"
    assert "进程实体由程序段" in paragraphs[0], "段落的后半部分必须也在同一块里"


def test_heading_detected_by_font_size(tmp_path: Path, workdir: str) -> None:
    source = tmp_path / "doc.pdf"
    make_pdf(source, pages=1)

    parsed = parse_pdf(source, file_name="doc.pdf", file_hash=workdir)
    headings = [b.text for _, b in parsed.iter_blocks() if b.type == BlockType.HEADING]

    assert "第3章 进程管理" in headings
    assert all(len(h) <= 40 for h in headings)


# --------------------------------------------------------------------------- #
# 图片
# --------------------------------------------------------------------------- #
def test_embedded_image_extracted_and_saved(tmp_path: Path, workdir: str) -> None:
    source = tmp_path / "doc.pdf"
    make_pdf(source, pages=1, with_images=True)

    parsed = parse_pdf(source, file_name="doc.pdf", file_hash=workdir)
    figures = [b for _, b in parsed.iter_blocks() if b.type == BlockType.FIGURE]

    assert len(figures) == 1
    image_path = figures[0].image_path
    assert image_path, "figure 块必须记录 image_path"
    assert image_path.startswith(f"{workdir}/images/"), "路径应相对 UPLOAD_DIR"

    absolute = storage.resolve(image_path)
    assert absolute.is_file(), "图片必须真实落盘"
    assert absolute.stat().st_size > 0
    assert figures[0].width and figures[0].height


def test_tiny_images_are_filtered(tmp_path: Path, workdir: str) -> None:
    """小于阈值的图片视为图标/分隔线，应当跳过。

    页面必须带正文 —— 否则整份 PDF 会被判定为扫描件而直接报错，
    测不到图片过滤这条逻辑。
    """
    source = tmp_path / "doc.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    cjk = pymupdf.Font("china-s")
    page.insert_font(fontname="F0", fontbuffer=cjk.buffer)
    page.insert_text(
        (72, 100),
        "本页包含一张极小的图片，正文用于提供文本层以便解析器继续处理。",
        fontname="F0",
        fontsize=11,
    )

    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 20, 20))
    pixmap.set_rect(pixmap.irect, (10, 10, 10))
    page.insert_image(pymupdf.Rect(72, 200, 92, 220), pixmap=pixmap)
    doc.save(str(source))
    doc.close()

    parsed = parse_pdf(
        source,
        file_name="doc.pdf",
        file_hash=workdir,
        image_min_size_px=settings.image_min_size_px,
    )

    assert [b for _, b in parsed.iter_blocks() if b.type == BlockType.FIGURE] == []
    assert "IMAGE_TOO_SMALL" in [w.code for w in parsed.warnings]


def test_image_extraction_can_be_disabled(tmp_path: Path, workdir: str) -> None:
    source = tmp_path / "doc.pdf"
    make_pdf(source, pages=1, with_images=True)

    parsed = parse_pdf(
        source, file_name="doc.pdf", file_hash=workdir, extract_images=False
    )
    assert [b for _, b in parsed.iter_blocks() if b.type == BlockType.FIGURE] == []
    assert "IMAGE_EXTRACT_DISABLED" in [w.code for w in parsed.warnings]


# --------------------------------------------------------------------------- #
# 异常处理
# --------------------------------------------------------------------------- #
def test_page_without_text_layer_reports_warning(tmp_path: Path, workdir: str) -> None:
    source = tmp_path / "doc.pdf"
    make_pdf(source, pages=3, blank_pages=(2,))

    parsed = parse_pdf(source, file_name="doc.pdf", file_hash=workdir)

    assert parsed.pages[1].has_text_layer is False
    warnings = [w for w in parsed.warnings if w.code == "NO_TEXT_LAYER"]
    assert warnings and warnings[0].page_no == 2
    # 其余页面必须照常产出 —— 局部失败不能拖垮整体
    assert parsed.pages[0].blocks and parsed.pages[2].blocks


def test_blank_pdf_raises_readable_error(tmp_path: Path, workdir: str) -> None:
    """整份都是扫描件（无文本层）时应给出可读错误，而不是产出空结果。"""
    source = tmp_path / "doc.pdf"
    make_pdf(source, pages=2, blank_pages=(1, 2))

    with pytest.raises(ParserError) as exc:
        parse_pdf(source, file_name="doc.pdf", file_hash=workdir)
    assert exc.value.code == "NO_TEXT_LAYER"
    assert "扫描件" in exc.value.message


def test_encrypted_pdf_rejected_at_probe(tmp_path: Path) -> None:
    source = tmp_path / "locked.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.save(
        str(source),
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        owner_pw="owner",
        user_pw="user",
    )
    doc.close()

    page_count, encrypted = probe_pdf(source.read_bytes())
    assert encrypted is True
    assert page_count == 0


def test_corrupted_file_raises() -> None:
    with pytest.raises(ParserError) as exc:
        probe_pdf(b"this is definitely not a pdf")
    assert exc.value.code == "INVALID_PDF"


# --------------------------------------------------------------------------- #
# 公式行不能被当成章节标题
#
# 回归背景：一份含大量 LaTeX 公式的论文 PDF 里，公式行因为字号较大被「字号明显大」
# 这条规则误判为标题，heading_path 里出现了 `d x P x d x= ∑` 这种"章节名"。
# heading_path 是分块语境与 P2 关系构建的共同依据，脏标题会让不相干的知识点归到同一节，
# 并在图谱里凭空产生从属关系。
# --------------------------------------------------------------------------- #
def test_formula_lines_are_not_headings() -> None:
    # 取自真实数据的两个脏标题（\uf0ce / \uf0e5 是 Symbol 字体被提取后的私有区码位）
    assert _looks_like_heading("d x P x d x\uf0ce= \uf0e5", 20.0, 10.0) is False
    assert _looks_like_heading("== −−\uf0e5", 20.0, 10.0) is False
    # 纯数学表达式
    assert _looks_like_heading("∑∫√±≤≥", 20.0, 10.0) is False
    assert _looks_like_heading("∂f/∂x = 2x + 1", 20.0, 10.0) is False


def test_normal_headings_survive_formula_filter() -> None:
    """过滤公式不能误伤正常标题。"""
    for text in (
        "3.1 进程的概念",
        "第二章 内存管理",
        "一、 问题重述",
        "摘  要",
        "附录 A",
        "2.1.1 页表",
        "3-1 节 绪论",  # 连字符不该被当成数学运算符
        "2.3 死锁的处理",
    ):
        assert _looks_like_heading(text, 20.0, 10.0) is True, f"误伤了正常标题：{text}"


def test_private_use_area_alone_marks_formula() -> None:
    """含私有区码位即可判定，不必依赖字号。"""
    assert _looks_like_heading("\uf0ce\uf0e5", 10.0, 10.0) is False
