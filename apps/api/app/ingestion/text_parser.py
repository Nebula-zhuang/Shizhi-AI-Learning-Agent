"""TXT / Markdown 解析器。

这两类文件没有物理页，用**虚拟分页**（每 VIRTUAL_CHARS_PER_PAGE 字符一页）代替，
使 source_pages 在这类资料上依然有定位意义。虚拟分页会在 meta 中标注，
前端可据此把「第 N 页」显示为「第 N 段」之类的更准确措辞。

编码探测顺序：utf-8 → utf-8-sig → gb18030 → latin-1（兜底，记 warning）。
"""

from __future__ import annotations

import re

from app.core.logging import get_logger
from app.ingestion.base import (
    VIRTUAL_CHARS_PER_PAGE,
    WARN_EMPTY_PAGE,
    WARN_ENCODING_FALLBACK,
    WARN_VIRTUAL_PAGINATION,
    Block,
    Page,
    ParsedDocument,
    ParserError,
)
from app.models.chunk import BlockType

logger = get_logger(__name__)

#: 编码探测顺序。"严格"解码：只有完整解出来才采用。
_ENCODING_CANDIDATES = ("utf-8", "utf-8-sig", "gb18030", "big5")

#: Markdown 标题
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")

#: Markdown 表格分隔行（| --- | --- |）
_MD_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")

#: 序号式标题：1.2 / 1.2.3 / 第一章 / 一、
_NUMBERED_HEADING = re.compile(
    r"^\s*(?:"
    r"第\s*[一二三四五六七八九十百零\d]+\s*[章节节讲篇部]"
    r"|[一二三四五六七八九十]+\s*[、.．]"
    r"|\d+(?:\.\d+){0,3}\s*[、.．]?\s+"
    r")\s*\S"
)

#: 标题最大长度，超过则不认为是标题
_MAX_HEADING_LEN = 40


def decode_bytes(data: bytes) -> tuple[str, str | None]:
    """解码字节流，返回 (文本, 实际使用的编码)。

    命中非 UTF-8 编码时返回编码名，供调用方记 warning。
    """
    for enc in _ENCODING_CANDIDATES:
        try:
            return data.decode(enc), (None if enc.startswith("utf-8") else enc)
        except (UnicodeDecodeError, LookupError):
            continue

    logger.warning("所有候选编码均失败，回退 utf-8 + replace")
    return data.decode("utf-8", errors="replace"), "utf-8(replace)"


def _is_heading(text: str, *, from_markdown: bool = False) -> int | None:
    """判断是否标题，返回层级；不是标题返回 None。"""
    stripped = text.strip()
    if not stripped or len(stripped) > _MAX_HEADING_LEN:
        return None

    if from_markdown:
        match = _MD_HEADING.match(stripped)
        if match:
            return len(match.group(1))

    if _NUMBERED_HEADING.match(stripped):
        # 依据序号层级粗略判断：1.2.3 → 3 级
        dots = re.match(r"^\s*(\d+(?:\.\d+)*)", stripped)
        if dots:
            return min(dots.group(1).count(".") + 1, 6)
        return 2

    return None


def _looks_like_heading(text: str, *, from_markdown: bool = False) -> tuple[bool, int]:
    """标题判定的统一入口，返回 (是否标题, 层级)。"""
    level = _is_heading(text, from_markdown=from_markdown)
    if level is None:
        return False, 0
    return True, level


def _flush_paragraph(buffer: list[str], blocks: list[Block]) -> None:
    """把累积的段落行合并成一个 text 块。"""
    if not buffer:
        return
    text = "\n".join(buffer).strip()
    if text:
        blocks.append(Block(type=BlockType.TEXT, text=text))
    buffer.clear()


def parse_text(
    data: bytes,
    *,
    file_name: str,
    file_hash: str,
    is_markdown: bool,
) -> ParsedDocument:
    """解析 TXT / Markdown。"""
    if not data.strip():
        raise ParserError("文件内容为空，没有可解析的文本。", code="EMPTY_FILE")

    text, fallback_enc = decode_bytes(data)

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    blocks: list[Block] = []
    paragraph: list[str] = []
    table_buffer: list[str] = []
    in_code_fence = False

    for raw_line in lines:
        line = raw_line.rstrip()

        # 代码围栏内的内容原样保留，不参与标题/表格判定
        if line.strip().startswith("```"):
            _flush_table(table_buffer, blocks)
            in_code_fence = not in_code_fence
            paragraph.append(line)
            continue
        if in_code_fence:
            paragraph.append(line)
            continue

        if is_markdown and line.strip().startswith("|"):
            _flush_paragraph(paragraph, blocks)
            table_buffer.append(line)
            continue
        _flush_table(table_buffer, blocks)

        is_heading, level = _looks_like_heading(line, from_markdown=is_markdown)
        if is_heading:
            _flush_paragraph(paragraph, blocks)
            title = _MD_HEADING.sub(r"\2", line.strip()) if is_markdown else line.strip()
            blocks.append(Block(type=BlockType.HEADING, text=title, level=level))
            continue

        if not line.strip():
            _flush_paragraph(paragraph, blocks)
            continue

        paragraph.append(line)

    _flush_table(table_buffer, blocks)
    _flush_paragraph(paragraph, blocks)

    if not blocks:
        raise ParserError("文件解析后没有产生任何内容块。", code="EMPTY_CONTENT")

    # ---------------------------------------------------------- 虚拟分页
    pages, warnings = _paginate(blocks)

    parsed = ParsedDocument(
        document_hash=file_hash,
        file_name=file_name,
        file_type="md" if is_markdown else "txt",
        page_count=len(pages),
        pages=pages,
        meta={
            "parser": "text",
            "pagination": "virtual",
            "chars_per_page": VIRTUAL_CHARS_PER_PAGE,
            "warnings": warnings,
        },
    )

    if fallback_enc:
        parsed.add_warning(
            WARN_ENCODING_FALLBACK,
            f"文件不是 UTF-8 编码，已按 {fallback_enc} 解码，可能出现个别字符异常。",
        )
    return parsed


def _flush_table(buffer: list[str], blocks: list[Block]) -> None:
    """把累积的表格行合并成一个 table 块。"""
    if not buffer:
        return
    # 去掉 Markdown 表格的 |---|---| 分隔行，它对人没有信息量
    rows = [row for row in buffer if not _MD_TABLE_SEP.match(row)]
    text = "\n".join(rows).strip()
    if text:
        blocks.append(Block(type=BlockType.TABLE, text=text))
    buffer.clear()


def _paginate(blocks: list[Block]) -> tuple[list[Page], list[dict]]:
    """把块流按字符数切成虚拟页。

    一个块不会跨虚拟页（保持块的完整性），因此某页可能略小于目标字符数。
    """
    pages: list[Page] = []
    warnings: list[dict] = []
    current: list[Block] = []
    current_chars = 0

    def commit() -> None:
        nonlocal current, current_chars
        if not current:
            return
        page_no = len(pages) + 1
        pages.append(Page(page_no=page_no, has_text_layer=True, blocks=current))
        current = []
        current_chars = 0

    for block in blocks:
        block_chars = len(block.text)
        if current and current_chars + block_chars > VIRTUAL_CHARS_PER_PAGE:
            commit()
        current.append(block)
        current_chars += block_chars

    commit()

    if not pages:
        pages.append(Page(page_no=1, has_text_layer=True, blocks=[]))
        warnings.append(
            {"code": WARN_EMPTY_PAGE, "page_no": 1, "message": "解析结果为空"}
        )

    if len(pages) > 1:
        warnings.append(
            {
                "code": WARN_VIRTUAL_PAGINATION,
                "page_no": None,
                "message": (
                    f"该文件无物理页码，已按每 {VIRTUAL_CHARS_PER_PAGE} 字符划分虚拟页，"
                    f"共 {len(pages)} 页。"
                ),
            }
        )
    return pages, warnings
