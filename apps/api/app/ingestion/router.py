"""解析入口：类型校验、落盘、按类型分派到具体解析器。

职责切分（避免同一个文件被写两次）：
    - 上传路由负责：validate_upload() 快速拒绝 + store_source() 落盘
    - 摄取流水线负责：parse_stored() 读取已落盘的文件并解析

对外只需要这两个函数，调用方不需要关心格式差异。
"""

from __future__ import annotations

from pathlib import Path

from app.core.config import settings
from app.core.logging import get_logger
from app.ingestion import storage
from app.ingestion.base import (
    FileCategory,
    KNOWN_UNSUPPORTED,
    ParsedDocument,
    ParserError,
    SUPPORTED_EXTENSIONS,
    SUPPORTED_FORMAT_HINT,
)
from app.ingestion.image_parser import parse_image
from app.ingestion.docx_parser import parse_docx
from app.ingestion.pdf_parser import parse_pdf, probe_pdf
from app.ingestion.pptx_parser import parse_pptx
from app.ingestion.text_parser import parse_text

logger = get_logger(__name__)


def category_of(file_name: str) -> FileCategory | None:
    """按扩展名判断文件类别。不支持的类型返回 None。"""
    ext = storage.extension_of(file_name)
    return SUPPORTED_EXTENSIONS.get(ext)  # type: ignore[return-value]


def is_supported(file_name: str) -> bool:
    return category_of(file_name) is not None


def validate_upload(
    source: bytes | Path,
    *,
    file_name: str,
    size: int | None = None,
) -> FileCategory:
    """上传阶段的快速校验：类型、大小、PDF 页数与加密状态。

    提前拒绝的收益：用户几秒内就知道结果，也不用为一个注定失败的文件浪费磁盘与 LLM 调用。
    抛出 ParserError，其 message 可直接展示给用户。

    `source` 可以是 `bytes`（旧路径、测试用），也可以是 `Path`（流式落盘后的新路径）。
    传 `Path` 时请一并传 `size` —— 否则每次都要 `stat()` 一次，
    而这个值在流式接收时本来就已经数出来了。
    """
    category = category_of(file_name)
    if category is None:
        ext = storage.extension_of(file_name) or "无扩展名"
        # 先看是不是"能识别但解析不了"的格式。这类要给出**具体怎么办**，
        # 一句"不支持该类型"对拿着 .doc 的用户毫无帮助 ——
        # 他多半并不知道自己存的是旧格式。
        advice = KNOWN_UNSUPPORTED.get(ext)
        if advice:
            raise ParserError(advice, code="KNOWN_UNSUPPORTED_TYPE")
        raise ParserError(
            f"不支持的文件类型「{ext}」，当前支持：{SUPPORTED_FORMAT_HINT}。",
            code="UNSUPPORTED_TYPE",
        )

    actual = size
    if actual is None:
        actual = len(source) if isinstance(source, bytes) else source.stat().st_size

    if actual <= 0:
        raise ParserError("上传的文件是空的。", code="EMPTY_FILE")

    max_bytes = settings.max_upload_mb * 1024 * 1024
    if actual > max_bytes:
        raise ParserError(
            f"文件大小 {actual / 1024 / 1024:.1f}MB 超过上限 {settings.max_upload_mb}MB。",
            code="FILE_TOO_LARGE",
        )

    if category == "pdf":
        page_count, encrypted = probe_pdf(source)
        if encrypted:
            raise ParserError(
                "该 PDF 已加密，需要密码才能解析。请先解除密码保护再上传。",
                code="ENCRYPTED_PDF",
            )
        if page_count > settings.max_document_pages:
            raise ParserError(
                f"该 PDF 共 {page_count} 页，超过单文档上限 {settings.max_document_pages} 页。"
                "请拆分后再上传。",
                code="TOO_MANY_PAGES",
            )

    return category


def store_source(data: bytes, *, file_name: str) -> tuple[str, str]:
    """落盘源文件。

    返回 (file_hash, storage_path)。file_hash 同时也是存储目录名，因此先算 hash 再落盘。
    """
    file_hash = storage.compute_hash(data)
    storage_path = storage.save_source(file_hash, file_name, data)
    return file_hash, storage_path


def parse_stored(
    *,
    file_name: str,
    file_hash: str,
    storage_path: str,
) -> tuple[ParsedDocument, str]:
    """解析一个已落盘的文件，并把统一文档结构写盘。

    返回 (统一文档结构, 结构文件相对路径)。
    """
    category = category_of(file_name)
    if category is None:
        raise ParserError(
            f"不支持的文件类型「{storage.extension_of(file_name)}」。", code="UNSUPPORTED_TYPE"
        )

    source_abs = storage.resolve(storage_path)
    if not source_abs.is_file():
        raise ParserError(
            "源文件已丢失（可能被手工删除），请重新上传该资料。", code="SOURCE_MISSING"
        )

    logger.info("开始解析 %s（类型 %s）", file_name, category)

    if category == "pdf":
        parsed = parse_pdf(
            source_abs,
            file_name=file_name,
            file_hash=file_hash,
            extract_images=settings.image_extract_enabled,
            image_min_size_px=settings.image_min_size_px,
        )
    elif category == "word":
        # **从路径读**：docx 是个 zip，python-docx 内部按需取；
        # 调用方先把整份读进内存会抵消上传侧的流式改造。
        parsed = parse_docx(source_abs, file_name=file_name, file_hash=file_hash)
    elif category == "slide":
        # pptx 同样是 zip，处理方式与 docx 一致
        parsed = parse_pptx(source_abs, file_name=file_name, file_hash=file_hash)
    elif category == "text":
        data = source_abs.read_bytes()
        parsed = parse_text(
            data,
            file_name=file_name,
            file_hash=file_hash,
            is_markdown=storage.extension_of(file_name) in (".md", ".markdown"),
        )
    else:
        parsed = parse_image(source_abs, file_name=file_name, file_hash=file_hash)

    structure_path = storage.write_structure(file_hash, parsed)
    logger.info(
        "解析完成：%d 页 / %d 字符 / %d 张图片 / %d 条警告",
        parsed.page_count,
        parsed.char_count,
        parsed.image_count,
        len(parsed.warnings),
    )
    return parsed, structure_path


def absolute_source_path(relative_path: str) -> Path:
    """把库里的相对路径还原为绝对路径。"""
    return storage.resolve(relative_path)
