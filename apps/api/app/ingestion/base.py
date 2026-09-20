"""解析层的统一数据结构（跨模块契约）。

所有解析器（PDF / TXT / MD / 图片）都必须产出同一个 `ParsedDocument`，下游只认这个结构，
不关心原始格式。契约定义与取值理由见 skills/doc-ingestion/SKILL.md。

关于 BlockType 的来源：类型词汇表定义在 `app/models/chunk.py`（数据库里存的就是这些字符串），
本模块直接复用，避免两处定义漂移。ingestion 不反向依赖任何业务逻辑，因此不存在循环依赖。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models.chunk import BlockType

# --------------------------------------------------------------------------- #
# 警告码。前端与测试都按这些码做断言，不要随意改名。
# --------------------------------------------------------------------------- #
WARN_NO_TEXT_LAYER = "NO_TEXT_LAYER"
WARN_EMPTY_PAGE = "EMPTY_PAGE"
WARN_PAGE_PARSE_FAILED = "PAGE_PARSE_FAILED"
WARN_ENCODING_FALLBACK = "ENCODING_FALLBACK"
WARN_IMAGE_TOO_SMALL = "IMAGE_TOO_SMALL"
WARN_OVERSIZE_BLOCK = "OVERSIZE_BLOCK"
WARN_FORCED_SPLIT = "FORCED_SPLIT"
WARN_FORMULA_NOT_EXTRACTED = "FORMULA_NOT_EXTRACTED"
WARN_IMAGE_EXTRACT_DISABLED = "IMAGE_EXTRACT_DISABLED"
WARN_VIRTUAL_PAGINATION = "VIRTUAL_PAGINATION"
#: 提取出的文字里"有意义的字符"占比过低，通常意味着文本层损坏或字体缺少字形映射
WARN_SUSPECT_TEXT_LAYER = "SUSPECT_TEXT_LAYER"

#: 文本类文件（TXT/MD）的虚拟分页大小。它们没有物理页，用固定字符数切"虚拟页"，
#: 使 source_pages 在这类资料上依然有定位意义。
VIRTUAL_CHARS_PER_PAGE = 3000

#: 判定"疑似乱码"的阈值：有意义字符占比低于该值即告警
MEANINGFUL_RATIO_THRESHOLD = 0.5
#: 文本太短时不判定，避免误报
MEANINGFUL_MIN_CHARS = 50


def meaningful_ratio(text: str) -> float:
    """计算文本中"有意义字符"的占比。

    有意义字符 = 汉字 / 拉丁字母 / 数字。

    用途：某些 PDF 的文本层是坏的（字体缺字形映射、编码错乱），提取出来全是 `·`、`□`
    或私有区码位。这类文本会让知识点抽取静默返回空结果 —— 用户看到的是
    「处理成功但一个知识点都没有」，完全无从判断原因。有了这个指标就能明确告警。
    """
    if not text:
        return 0.0
    total = 0
    meaningful = 0
    for ch in text:
        if ch.isspace():
            continue
        total += 1
        if ch.isalnum() or "\u4e00" <= ch <= "\u9fff":
            meaningful += 1
    if total == 0:
        return 0.0
    return meaningful / total


def looks_garbled(text: str) -> bool:
    """判断文本是否疑似乱码。文本过短时一律返回 False。"""
    if len(text.strip()) < MEANINGFUL_MIN_CHARS:
        return False
    return meaningful_ratio(text) < MEANINGFUL_RATIO_THRESHOLD


class ParserError(Exception):
    """解析失败。message 面向用户可读，会写入 documents.parse_error。"""

    def __init__(self, message: str, *, code: str = "PARSE_FAILED") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class Block(BaseModel):
    """文档中的一个内容块。"""

    type: BlockType
    text: str = ""
    #: 标题层级，仅 heading 有值
    level: int | None = None
    #: PDF 坐标 [x0, y0, x1, y1]，文本类文件为 None
    bbox: list[float] | None = None
    #: 图片相对路径（相对 UPLOAD_DIR，形如 <hash>/images/p1_0.png），仅 figure 有值
    image_path: str | None = None
    caption: str | None = None
    width: int | None = None
    height: int | None = None


class Page(BaseModel):
    """一页。文本类文件使用虚拟页。"""

    page_no: int = Field(ge=1, description="从 1 开始")
    has_text_layer: bool = True
    blocks: list[Block] = Field(default_factory=list)


class ParseWarning(BaseModel):
    """解析过程中的非致命问题。"""

    code: str
    page_no: int | None = None
    message: str


class ParsedDocument(BaseModel):
    """统一文档结构。这是解析层的输出契约，也是前端原文预览的数据来源。"""

    document_hash: str
    file_name: str
    file_type: str
    page_count: int
    pages: list[Page]
    meta: dict[str, Any] = Field(default_factory=dict)

    # ------------------------------------------------------------ 派生统计
    @property
    def warnings(self) -> list[ParseWarning]:
        raw = self.meta.get("warnings") or []
        return [ParseWarning(**w) if isinstance(w, dict) else w for w in raw]

    @property
    def char_count(self) -> int:
        return sum(len(b.text) for p in self.pages for b in p.blocks if b.text)

    @property
    def image_count(self) -> int:
        return sum(1 for p in self.pages for b in p.blocks if b.type == BlockType.FIGURE)

    def add_warning(self, code: str, message: str, page_no: int | None = None) -> None:
        bucket = self.meta.setdefault("warnings", [])
        bucket.append(ParseWarning(code=code, page_no=page_no, message=message).model_dump())

    def iter_blocks(self):
        """按阅读顺序遍历所有块，产出 (page_no, block)。"""
        for page in self.pages:
            for block in page.blocks:
                yield page.page_no, block


@dataclass(slots=True)
class ChunkDraft:
    """分块器的输出。字段与 chunks 表一一对应，便于直接落库。"""

    chunk_index: int
    content: str
    page_start: int
    page_end: int
    block_type: str
    heading_path: list[str] = field(default_factory=list)
    image_path: str | None = None

    @property
    def char_count(self) -> int:
        return len(self.content)

    @property
    def token_estimate(self) -> int:
        """粗略估算。中文约 1 字 ≈ 0.75 token，英文约 4 字符 ≈ 1 token，取折中系数。"""
        return int(self.char_count * 0.75)


#: 支持的扩展名 → 解析器类别
SUPPORTED_EXTENSIONS: dict[str, str] = {
    ".pdf": "pdf",
    ".docx": "word",
    ".pptx": "slide",
    ".txt": "text",
    ".md": "text",
    ".markdown": "text",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
}

#: 供错误提示使用的可读格式列表
SUPPORTED_FORMAT_HINT = "PDF / Word(.docx) / PPT(.pptx) / TXT / MD / PNG / JPG / JPEG / WEBP"

#: **能识别、但解析不了**的扩展名 → 给用户的解释。
#:
#: `.doc` 是 Word 97-2003 的二进制格式，和 `.docx`（zip + XML）完全是两种东西，
#: python-docx 读不了。与其让它落到"不支持的文件类型"那句毫无信息量的提示里，
#: 不如明确告诉用户怎么办 —— 这类文件的用户往往并不知道自己存的是旧格式。
KNOWN_UNSUPPORTED: dict[str, str] = {
    ".doc": "这是 Word 97-2003 的旧格式（.doc），和 .docx 是两种不同的文件。"
    "请用 Word 打开后「另存为」.docx 再上传。",
    ".wps": "WPS 的 .wps 格式暂不支持，请另存为 .docx 或 .pdf 再上传。",
    ".ppt": "这是 PowerPoint 97-2003 的旧格式（.ppt），和 .pptx 是两种不同的文件。"
    "请用 PowerPoint 打开后「另存为」.pptx 再上传。",
    ".xlsx": "暂不支持表格文件。可以先导出为 PDF 或 CSV 再上传。",
    ".epub": "暂不支持电子书格式，请转换为 PDF 再上传。",
}


FileCategory = Literal["pdf", "text", "image", "word", "slide"]
