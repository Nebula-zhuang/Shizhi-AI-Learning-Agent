"""独立图片解析器。

P1 对图片的策略是「识别 + 保留」，不做内容理解：

- **识别**：读取出图片的真实像素尺寸，登记为一个 figure 块
- **保留**：图片作为文档的一部分被登记在统一结构里，可被前端预览、可被知识点引用

内容理解（识别图里的公式、电路图、流程图讲了什么）需要多模态模型，当前配置的
`deepseek-chat` 是纯文本模型，因此留到 P2 接入视觉模型后实现。
"""

from __future__ import annotations

from pathlib import Path

import pymupdf as fitz  # PyMuPDF，见 pdf_parser.py 的说明

from app.core.logging import get_logger
from app.ingestion.base import Block, Page, ParsedDocument, ParserError
from app.ingestion.storage import document_dir, extension_of
from app.models.chunk import BlockType

logger = get_logger(__name__)


def parse_image(path: Path, *, file_name: str, file_hash: str) -> ParsedDocument:
    """把一张独立图片解析为「单页单图」的文档结构。"""
    try:
        with fitz.open(path) as doc:
            if doc.page_count == 0:
                raise ParserError("图片文件没有可读取的页面。", code="INVALID_IMAGE")
            page = doc[0]
            width = int(round(page.rect.width))
            height = int(round(page.rect.height))
    except ParserError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ParserError(f"图片无法识别，可能已损坏或格式不受支持：{exc}") from exc

    # 源文件本身就在文档目录下，直接引用它，不复制第二份
    source_rel = f"{file_hash}/source{extension_of(file_name)}"
    if not (document_dir(file_hash) / f"source{extension_of(file_name)}").exists():
        source_rel = ""

    block = Block(
        type=BlockType.FIGURE,
        text="",
        image_path=source_rel,
        caption=file_name,
        width=width,
        height=height,
    )

    parsed = ParsedDocument(
        document_hash=file_hash,
        file_name=file_name,
        file_type="image",
        page_count=1,
        pages=[Page(page_no=1, has_text_layer=False, blocks=[block])],
        meta={
            "parser": "pymupdf-image",
            "width": width,
            "height": height,
            "image_count": 1,
            "warnings": [],
        },
    )
    parsed.add_warning(
        "IMAGE_CONTENT_NOT_UNDERSTOOD",
        "P1 只保留图片本身，不识别图片内容（需要多模态模型，计划在 P2 接入）。",
        1,
    )
    return parsed
