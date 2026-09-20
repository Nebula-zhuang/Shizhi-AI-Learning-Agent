"""解析 Word（.docx）。

## 为什么之前"不接受 docx"

`SUPPORTED_EXTENSIONS` 里没有 `.docx` —— 前端 `accept` 里放了它、界面上也写着
"支持 Word"，但后端从来没有实现过。**那是文案跑在了能力前面。**

这个文件把它补上，而不是把前端那行删掉：技术方案里本来就规划了 `python-docx`。

## 两个与 PDF 不同的地方

1. **没有物理页码**。docx 是流式文档，"页"由渲染器决定，文件里根本没存。
   所以和 TXT / Markdown 一样走**虚拟分页**（每 N 字符一页），
   并在 warnings 里明说这件事 —— 用户看到的页码不该被误解成真实页。
2. **正文与表格在文档树里是分开的两组**。`document.paragraphs` 和
   `document.tables` 各自成表，直接分别遍历会**丢掉它们的先后顺序**
   （教材里"表 3-1 进程状态转换"通常紧跟在讲它的那段话后面）。
   所以这里遍历 `document.element.body` 的子元素，按真实顺序取。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.core.logging import get_logger
from app.ingestion import storage
from app.ingestion.base import (
    Block,
    BlockType,
    Page,
    ParsedDocument,
    ParserError,
)
from app.models.chunk import BlockType as _BlockType  # noqa: F401  —— 契约同源，显式声明

logger = get_logger(__name__)

#: 虚拟分页的每页字符数。与 text_parser 保持一致，让同类文件的页码手感统一。
VIRTUAL_CHARS_PER_PAGE = 1800

#: Word 内置标题样式的名字。
#: **必须同时覆盖中英文** —— 中文版 Word 存的是 `标题 1`，
#: 英文版存的是 `Heading 1`，而同一份文档在不同语言环境下会被另存成不同名字。
_HEADING_STYLE = re.compile(r"^(?:Heading| heading|标题)\s*([1-9])$", re.IGNORECASE)

#: 有些文档不用内置样式，直接叫「一级标题」这类自定义名
_CN_LEVEL_WORDS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6}
_CUSTOM_HEADING = re.compile(r"^(?:标题|heading)\s*([一二三四五六1-6])\s*级?$", re.IGNORECASE)

#: 表格单元格里的换行会被替换成空格，避免一个格子里塞进多行破坏表格语义
_CELL_NEWLINE = re.compile(r"\s*\n\s*")


def _heading_level(style_name: str | None) -> int | None:
    """把 Word 的段落样式名映射成标题层级。不是标题返回 None。"""
    if not style_name:
        return None

    name = style_name.strip()

    # 内置样式：Heading 1 / 标题 1
    match = _HEADING_STYLE.match(name)
    if match:
        return int(match.group(1))

    # 自定义中文样式：一级标题 / 标题一
    custom = _CUSTOM_HEADING.match(name)
    if custom:
        token = custom.group(1)
        if token.isdigit():
            return int(token)
        return _CN_LEVEL_WORDS.get(token)

    # Title 样式当作一级标题 —— 它通常就是文档主标题
    if name.lower() in {"title", "文档标题"}:
        return 1

    return None


def _paragraph_text(paragraph) -> str:
    """取段落文本。

    **不用 `paragraph.text`**：它只拼接 `runs` 的直接文本，
    对超链接（`w:hyperlink` 里的 run）返回空 —— 而学术资料里
    "参见第 3 章" 这类关键引用经常是超链接，丢了会让上下文断裂。
    这里走 XML 把所有 `w:t` 取出来。
    """
    parts = paragraph._element.xpath(".//w:t")
    return "".join(node.text or "" for node in parts).strip()


def _table_block(table) -> Block | None:
    """把 Word 表格转成 table 块。

    用 Markdown 的管道格式而不是制表符：块内容后面要经过分块与模型抽取，
    管道格式对模型来说边界更清晰（制表符和空格在纯文本里分不出来）。
    """
    rows: list[str] = []
    for row in table.rows:
        cells = [_CELL_NEWLINE.sub(" ", cell.text).strip() for cell in row.cells]
        if any(cells):
            rows.append("| " + " | ".join(cells) + " |")

    if not rows:
        return None
    return Block(type=BlockType.TABLE, text="\n".join(rows))


def _collect_blocks(document) -> tuple[list[Block], list[tuple[bytes, str]]]:
    """按**文档真实顺序**遍历正文，产出块流。

    返回值第二项与 `FIGURE` 块**一一对应**：`(图片字节, 扩展名)`。

    图片本身**单独成为一个 figure 块**，而不是挂到邻近的文字块上。
    一开始写的就是"挂到邻近块"，结果遇到"只有图片、没有文字的段落"时
    下标直接越界，图片全部丢失 —— 因为那个段落不产出任何块。
    让图片自己成块既修掉了这个错，也符合语义：
    图本来就是一个独立的内容单元（PDF 路径产出的也是 figure 块）。
    """
    blocks: list[Block] = []
    figures: list[tuple[bytes, str]] = []

    body = document.element.body
    for child in body.iterchildren():
        tag = child.tag.split("}")[-1]

        if tag == "p":
            from docx.text.paragraph import Paragraph

            paragraph = Paragraph(child, document)
            text = _paragraph_text(paragraph)
            level = _heading_level(paragraph.style.name if paragraph.style else None)
            blobs = _extract_images(paragraph)

            if level is not None:
                # 标题里不会插图；真有图也只保留标题语义
                if text:
                    blocks.append(Block(type=BlockType.HEADING, text=text, level=level))
                continue

            if text:
                blocks.append(Block(type=BlockType.TEXT, text=text))

            for blob, ext in blobs:
                # 紧邻的段落文字当作图注
                blocks.append(
                    Block(type=BlockType.FIGURE, caption=text or None, text=text or "")
                )
                figures.append((blob, ext))

        elif tag == "tbl":
            from docx.table import Table

            block = _table_block(Table(child, document))
            if block is not None:
                blocks.append(block)

    return blocks, figures


def _extract_images(paragraph) -> list[tuple[bytes, str]]:
    """取出段落里的内嵌图片字节。

    `document.part.related_parts` 里存着图片二进制，通过 r:embed 关系 id 关联。
    """
    found: list[tuple[bytes, str]] = []
    try:
        blips = paragraph._element.xpath(".//a:blip")
    except Exception:  # noqa: BLE001
        return found

    for blip in blips:
        rid = blip.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed")
        if not rid:
            continue
        part = paragraph.part.related_parts.get(rid)
        if part is None:
            continue
        ext = (Path(part.partname).suffix or ".png").lstrip(".").lower()
        found.append((part.blob, storage.normalize_image_ext(ext)))
    return found


def _paginate(blocks: list[Block]) -> tuple[list[Page], list[dict[str, Any]]]:
    """把块流按字符数切成虚拟页。一个块不跨页。"""
    pages: list[Page] = []
    warnings: list[dict[str, Any]] = []
    current: list[Block] = []
    current_chars = 0

    def commit() -> None:
        if current:
            pages.append(
                Page(page_no=len(pages) + 1, has_text_layer=True, blocks=list(current))
            )

    for block in blocks:
        size = len(block.text)
        if current and current_chars + size > VIRTUAL_CHARS_PER_PAGE:
            commit()
            current = []
            current_chars = 0
        current.append(block)
        current_chars += size

    commit()

    if not pages:
        pages.append(Page(page_no=1, has_text_layer=True, blocks=[]))
        warnings.append({"code": "EMPTY_PAGE", "page_no": 1, "message": "解析结果为空"})

    if len(pages) > 1:
        warnings.append(
            {
                "code": "VIRTUAL_PAGINATION",
                "page_no": None,
                "message": (
                    f"Word 文档本身不含页码，已按每 {VIRTUAL_CHARS_PER_PAGE} 字符"
                    f"划分虚拟页，共 {len(pages)} 页。"
                ),
            }
        )

    return pages, warnings


def parse_docx(
    path: Path,
    *,
    file_name: str,
    file_hash: str,
) -> ParsedDocument:
    """解析 .docx。

    **从路径读而不是从 bytes**：docx 本质是个 zip，python-docx 内部也会按需读取，
    没有必要在调用方先把整份内容读进内存（那正好会抵消上传侧的流式改造）。
    """
    try:
        from docx import Document
        from docx.opc.exceptions import PackageNotFoundError
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的兜底
        raise ParserError(
            "服务端缺少 Word 解析依赖（python-docx），无法处理 .docx。",
            code="MISSING_DEPENDENCY",
        ) from exc

    try:
        document = Document(str(path))
    except PackageNotFoundError as exc:
        raise ParserError(
            "这个 .docx 打不开，文件可能已损坏或其实不是 Word 格式。",
            code="INVALID_DOCX",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise ParserError(f"解析 Word 文档失败：{exc}", code="INVALID_DOCX") from exc

    blocks, figures = _collect_blocks(document)

    if not any(block.type in {BlockType.TEXT, BlockType.TABLE} for block in blocks):
        # 只有标题没有正文，或整份文档是空的 —— 明确告知，别让它静默产出 0 个知识点
        raise ParserError(
            "这份 Word 文档里没有读到正文内容。"
            "如果内容都在文本框、SmartArt 或图片里，目前还读不出来。",
            code="NO_CONTENT",
        )

    pages, warnings = _paginate(blocks)

    # 图片落盘：此时才知道每张图落在第几页
    _save_images(pages, figures, file_hash=file_hash)
    if figures:
        warnings.append(
            {
                "code": "DOCX_IMAGES_EXTRACTED",
                "page_no": None,
                "message": f"从文档中提取了 {len(figures)} 张内嵌图片。",
            }
        )

    parsed = ParsedDocument(
        document_hash=file_hash,
        file_name=file_name,
        file_type="docx",
        page_count=len(pages),
        pages=pages,
        meta={
            "parser": "docx",
            "pagination": "virtual",
            "chars_per_page": VIRTUAL_CHARS_PER_PAGE,
            "image_count": len(figures),
            "paragraph_count": sum(1 for b in blocks if b.type == BlockType.TEXT),
            "warnings": warnings,
        },
    )
    return parsed


def _save_images(
    pages: list[Page], figures: list[tuple[bytes, str]], *, file_hash: str
) -> None:
    """把图片写盘，逐张回填到 `FIGURE` 块上。

    `figures` 与 `FIGURE` 块**顺序一一对应**（这是 `_collect_blocks` 保证的），
    所以这里只需按顺序配对，不需要再去算"图片属于第几号块"。

    落盘路径里带页码，所以要先把块摊平、拿到它所处的页。
    """
    if not figures:
        return

    # 摊平成 [(页号, 块)]，顺序与 figures 一致
    flat: list[tuple[int, Block]] = [
        (page.page_no, block) for page in pages for block in page.blocks
    ]
    figure_slots = [(page_no, block) for page_no, block in flat if block.type == BlockType.FIGURE]

    # 每页内的图片序号单独计数，路径形如 <hash>/images/p2_0.png
    counters: dict[int, int] = {}

    for (page_no, block), (blob, ext) in zip(figure_slots, figures):
        serial = counters.get(page_no, 0)
        counters[page_no] = serial + 1

        try:
            block.image_path = storage.save_image(file_hash, page_no, serial, ext, blob)
        except Exception as exc:  # noqa: BLE001
            # 单张图存不下不该让整份文档失败 —— 正文才是主体
            logger.warning("Word 内嵌图片保存失败（第 %d 张）：%s", serial + 1, exc)
