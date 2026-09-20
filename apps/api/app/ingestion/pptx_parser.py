"""解析 PowerPoint（.pptx）。

## 与 Word 解析器最大的区别：**页码是真的**

docx 是流式文档，"页"由渲染器决定，文件里根本没存 —— 所以那边只能做虚拟分页，
还得在 warnings 里跟用户解释"这个页码不是真的"。

pptx 不一样：**一张幻灯片就是一个确定的页**，`slide_index + 1` 就是页码。
所以这里直接产出真实页码，不做虚拟分页、也不会有那条免责说明。
对学习者来说这是好事 —— "这个知识点在第 7 页"能直接对上他在 PPT 里看到的页。

## 结构怎么映射

一张幻灯片天然自带层级：

    幻灯片标题  → heading（level 2）
    正文占位符里的每个段落 → text
    表格       → table
    图片       → figure（图片自己成块，与 docx 同一处理）
    演讲者备注  → text（带标记）

顶层标题取演示文稿自带的标题（core properties），拿不到就用第一张幻灯片的标题 ——
这样图谱的分带就是"每张幻灯片一带"，与翻 PPT 的心理模型一致。

## 备注要不要读

**要。** PPT 的演讲者备注里经常放着正文没写的解释 ——
对"读懂这份资料"来说那是高价值内容，扔掉很可惜。
但备注里也常有"点击切换""本页停留 2 分钟"这类给自己看的提示，
所以这里会给它加一个明确前缀，让后续抽取知道这段的性质。
"""

from __future__ import annotations

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

logger = get_logger(__name__)

#: 备注的前缀。给后续抽取一个明确信号：这段不是投影内容
NOTES_PREFIX = "（演讲者备注）"

#: 单个文本块的最大段落数。超过就不当"一页的正文"了 ——
#: 有些 PPT 会把整章内容塞进一张幻灯片，那种情况拆开更利于分块。
_MAX_PARAGRAPHS_PER_BLOCK = 40


def parse_pptx(
    path: Path,
    *,
    file_name: str,
    file_hash: str,
) -> ParsedDocument:
    """解析 .pptx。

    **从路径读**：pptx 和 docx 一样是 zip，python-pptx 内部按需取，
    没必要由调用方先把整份读进内存。
    """
    try:
        from pptx import Presentation
        from pptx.exc import PackageNotFoundError
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的兜底
        raise ParserError(
            "服务端缺少 PPT 解析依赖（python-pptx），无法处理 .pptx。",
            code="MISSING_DEPENDENCY",
        ) from exc

    try:
        deck = Presentation(str(path))
    except PackageNotFoundError as exc:
        raise ParserError(
            "这个 .pptx 打不开，文件可能已损坏或其实不是 PowerPoint 格式。",
            code="INVALID_PPTX",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise ParserError(f"解析 PowerPoint 失败：{exc}", code="INVALID_PPTX") from exc

    slides = list(deck.slides)
    if not slides:
        raise ParserError("这份 PPT 里一张幻灯片都没有。", code="NO_CONTENT")

    deck_title = _deck_title(deck, slides)

    pages: list[Page] = []
    warnings: list[dict[str, Any]] = []
    images: list[tuple[bytes, str]] = []
    text_slides = 0

    for index, slide in enumerate(slides):
        page_no = index + 1
        blocks: list[Block] = []

        # ── 标题：既是本页的标题块，也是图谱分带的依据
        title = _slide_title(slide)
        if title:
            blocks.append(Block(type=BlockType.HEADING, text=title, level=2))

        # ── 正文 / 表格 / 图片
        for shape in slide.shapes:
            try:
                _collect_shape(shape, slide, blocks, images)
            except Exception as exc:  # noqa: BLE001
                # 单个形状出问题不该毁掉整份 PPT —— 真实演示文稿里
                # 总有一两个奇形怪状的对象（SmartArt、图表、嵌入对象）
                logger.warning("第 %d 页有个形状解析失败，已跳过：%s", page_no, exc)
                warnings.append(
                    {
                        "code": "SHAPE_SKIPPED",
                        "page_no": page_no,
                        "message": "这一页有个对象没能读出来（可能是图表或嵌入对象），其余内容不受影响。",
                    }
                )

        # ── 备注
        notes = _slide_notes(slide)
        if notes:
            blocks.append(Block(type=BlockType.TEXT, text=f"{NOTES_PREFIX}{notes}"))

        if any(b.type in {BlockType.TEXT, BlockType.TABLE} for b in blocks):
            text_slides += 1
        elif not blocks:
            # 纯图/纯空页：保留页码但标记没有文本层，与 PDF 扫描件的处理一致
            warnings.append(
                {
                    "code": "EMPTY_PAGE",
                    "page_no": page_no,
                    "message": "这一页没有可读的文字。",
                }
            )

        pages.append(Page(page_no=page_no, has_text_layer=bool(blocks), blocks=blocks))

    if text_slides == 0:
        raise ParserError(
            "这份 PPT 里没有读到文字内容。如果内容都在图片里，"
            "可以先把幻灯片导出成 PDF 再上传。",
            code="NO_CONTENT",
        )

    parsed = ParsedDocument(
        document_hash=file_hash,
        file_name=file_name,
        file_type="pptx",
        page_count=len(pages),
        pages=pages,
        meta={
            "parser": "pptx",
            # 与 docx 的 "virtual" 相对：这里的页码是幻灯片序号，**真实可对应**
            "pagination": "slide",
            "deck_title": deck_title,
            "slide_count": len(pages),
            "image_count": len(images),
            "warnings": warnings,
        },
    )

    _save_images(pages, images, file_hash=file_hash)
    return parsed


def _deck_title(deck, slides: list) -> str:
    """演示文稿标题。优先用文档属性，退回第一张幻灯片的标题。"""
    try:
        core_title = (deck.core_properties.title or "").strip()
    except Exception:  # noqa: BLE001
        core_title = ""
    if core_title:
        return core_title
    return _slide_title(slides[0]) if slides else ""


def _slide_title(slide) -> str:
    """取幻灯片标题。没有标题占位符时返回空串。"""
    try:
        placeholder = slide.shapes.title
    except Exception:  # noqa: BLE001
        return ""
    if placeholder is None:
        return ""
    return _frame_text(placeholder.text_frame)


def _frame_text(text_frame) -> str:
    """把文本框里的段落拼成文本。

    段落之间用换行保留 —— 后续分块要靠这个判断语义边界，
    全部拼成一行会让"这一页讲了几个点"无从判断。
    """
    if text_frame is None:
        return ""
    lines: list[str] = []
    for paragraph in text_frame.paragraphs:
        text = "".join(run.text for run in paragraph.runs).strip()
        if text:
            lines.append(text)
    return "\n".join(lines).strip()


def _slide_notes(slide) -> str:
    """取演讲者备注。没有就返回空串。"""
    try:
        if not slide.has_notes_slide:
            return ""
        return _frame_text(slide.notes_slide.notes_text_frame)
    except Exception:  # noqa: BLE001
        return ""


def _collect_shape(shape, slide, blocks: list[Block], images: list[tuple[bytes, str]]) -> None:
    """把一个形状转成块（或图片字节）。不认识的对象静默跳过。"""
    # 标题已经在上面单独处理过了，这里跳过以免重复
    if getattr(shape, "is_placeholder", False) and shape == _title_placeholder(slide):
        return

    # 图片
    if getattr(shape, "shape_type", None) == 13 or hasattr(shape, "image"):  # PICTURE
        try:
            image = shape.image
        except Exception:  # noqa: BLE001
            image = None
        if image is not None:
            ext = storage.normalize_image_ext((image.ext or "png").lower())
            images.append((image.blob, ext))
            blocks.append(Block(type=BlockType.FIGURE, caption=None, text=""))
            return

    # 表格
    if getattr(shape, "has_table", False):
        block = _table_block(shape.table)
        if block is not None:
            blocks.append(block)
        return

    # 文本框
    if getattr(shape, "has_text_frame", False):
        text = _frame_text(shape.text_frame)
        if not text:
            return
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        # 一页塞了太多段就拆成多个块 —— 保持每个块的语义聚焦，
        # 不至于让一个"巨型块"在分块阶段无法再切
        for start in range(0, len(lines), _MAX_PARAGRAPHS_PER_BLOCK):
            chunk = lines[start : start + _MAX_PARAGRAPHS_PER_BLOCK]
            blocks.append(Block(type=BlockType.TEXT, text="\n".join(chunk)))


def _title_placeholder(slide):
    try:
        return slide.shapes.title
    except Exception:  # noqa: BLE001
        return None


def _table_block(table) -> Block | None:
    """把 PPT 表格转成 table 块（与 Word 一样用 Markdown 管道格式）。"""
    rows: list[str] = []
    for row in table.rows:
        cells = [" ".join(cell.text.split()) for cell in row.cells]
        if any(cells):
            rows.append("| " + " | ".join(cells) + " |")
    if not rows:
        return None
    return Block(type=BlockType.TABLE, text="\n".join(rows))


def _save_images(
    pages: list[Page], images: list[tuple[bytes, str]], *, file_hash: str
) -> None:
    """把图片写盘并回填到 FIGURE 块上。

    与 docx 同一套做法：`images` 与 `FIGURE` 块**顺序一一对应**
    （由遍历顺序保证），按序配对即可，不必去算"第几号块"。
    """
    if not images:
        return

    flat = [(page.page_no, block) for page in pages for block in page.blocks]
    slots = [(page_no, b) for page_no, b in flat if b.type == BlockType.FIGURE]

    counters: dict[int, int] = {}
    for (page_no, block), (blob, ext) in zip(slots, images):
        serial = counters.get(page_no, 0)
        counters[page_no] = serial + 1
        try:
            block.image_path = storage.save_image(file_hash, page_no, serial, ext, blob)
        except Exception as exc:  # noqa: BLE001
            logger.warning("PPT 内嵌图片保存失败（第 %d 页第 %d 张）：%s", page_no, serial + 1, exc)
