"""PDF 解析器（PyMuPDF）。

职责：逐页提取文本块与内嵌图片，剔除页眉页脚，识别标题，输出统一文档结构。

几个关键处理：
- **页眉页脚剔除**：位于页面上/下 8% 区域、且全文出现 ≥3 次的文本判为页眉页脚。
  只出现 1–2 次的保留（可能是正文）。
- **标题识别**：以全文加权中位字号为基准，字号明显更大且长度 ≤40 字的块判为标题；
  另有「第X章 / 1.2.3」这类序号式标题的正则兜底。
- **图片去重**：同一 xref 只落盘一次（PDF 里同一张图常被多页引用），
  但每个引用它的页面都会生成一个 figure 块 —— 因为它在那一页确实存在。
"""

from __future__ import annotations

import re
from pathlib import Path

# PyMuPDF 1.24 起模块名为 pymupdf，fitz 是即将移除的兼容别名，统一用新名。
# 保留 fitz 这个名字作为局部别名，避免全文替换带来的阅读负担。
import pymupdf as fitz

from app.core.logging import get_logger
from app.ingestion import storage
from app.ingestion.base import (
    WARN_FORMULA_NOT_EXTRACTED,
    WARN_IMAGE_EXTRACT_DISABLED,
    WARN_IMAGE_TOO_SMALL,
    WARN_NO_TEXT_LAYER,
    WARN_PAGE_PARSE_FAILED,
    WARN_SUSPECT_TEXT_LAYER,
    Block,
    Page,
    ParsedDocument,
    ParserError,
    looks_garbled,
    meaningful_ratio,
)
from app.models.chunk import BlockType

logger = get_logger(__name__)

#: 页眉/页脚区域占页面高度的比例
_HEADER_RATIO = 0.08
_FOOTER_RATIO = 0.92

#: 同一文本出现多少次才判定为页眉页脚
_REPEAT_THRESHOLD = 3

#: 标题长度上限
_MAX_HEADING_LEN = 40

#: 标题字号相对中位字号的倍数阈值
_HEADING_SIZE_RATIO = 1.15

#: 图注匹配
_CAPTION_PATTERN = re.compile(r"^\s*(图|表|Figure|Table|Fig\.?)\s*[\d一二三四五六七八九十]")
_CAPTION_MAX_LEN = 80
#: 图注与图片的垂直距离上限（点）
_CAPTION_MAX_GAP = 40.0

#: 序号式标题
_NUMBERED_HEADING = re.compile(
    r"^\s*(?:第\s*[一二三四五六七八九十百零\d]+\s*[章节节讲篇部]"
    r"|\d+(?:\.\d+){1,3}\s+\S)"
)

#: 断句标点：上一行以此结尾时，下一行另起
_SENTENCE_END = set("。！？；：.!?;:）)】」”’”")

#: 段落结束标点。上一行以此结尾时，判定为新段落的开始，不做跨块合并。
#: 刻意不含右括号与引号 —— 那些常常出现在段中。
_PARAGRAPH_END = set("。！？；.!?;：:")


# --------------------------------------------------------------------------- #
# 上传阶段探测
# --------------------------------------------------------------------------- #
def probe_pdf(source: bytes | Path) -> tuple[int, bool]:
    """读取页数与加密标志，供上传阶段做前置校验。

    返回 (页数, 是否加密)。文件损坏时抛 ParserError。

    **接受路径而不只是 bytes**：文件上限提到 300MB 之后，
    为了"探一下页数"就把整份内容读进内存不划算 ——
    PyMuPDF 可以直接从路径按需读取，内存占用只与它自己的缓存有关。
    """
    try:
        doc = fitz.open(source) if isinstance(source, Path) else fitz.open(stream=source, filetype="pdf")
        with doc:
            encrypted = bool(doc.needs_pass)
            return (0 if encrypted else doc.page_count), encrypted
    except Exception as exc:  # noqa: BLE001
        raise ParserError(f"PDF 文件无法打开，可能已损坏：{exc}", code="INVALID_PDF") from exc


# --------------------------------------------------------------------------- #
# 主解析入口
# --------------------------------------------------------------------------- #
def parse_pdf(
    path: Path,
    *,
    file_name: str,
    file_hash: str,
    extract_images: bool = True,
    image_min_size_px: int = 80,
) -> ParsedDocument:
    """解析 PDF，返回统一文档结构。"""
    try:
        doc = fitz.open(path)
    except Exception as exc:  # noqa: BLE001
        raise ParserError(f"PDF 文件无法打开，可能已损坏：{exc}", code="INVALID_PDF") from exc

    try:
        if doc.needs_pass:
            raise ParserError(
                "该 PDF 已加密，需要密码才能解析。请先解除密码保护再上传。",
                code="ENCRYPTED_PDF",
            )
        if doc.page_count == 0:
            raise ParserError("该 PDF 没有任何页面。", code="EMPTY_PDF")

        # ---------- 第一遍：抽取原始块（此时还不知道字号基准，不做分类）----------
        raw_pages: list[dict] = []
        for page in doc:
            page_no = page.number + 1
            try:
                raw_pages.append(_extract_raw_page(page, page_no))
            except Exception as exc:  # noqa: BLE001
                logger.warning("第 %d 页解析失败：%s", page_no, exc)
                raw_pages.append(
                    {
                        "page_no": page_no,
                        "height": page.rect.height,
                        "blocks": [],
                        "images": [],
                        "failed": str(exc),
                    }
                )

        # ---------- 全局字号基准 ----------
        median_size = _weighted_median_size(raw_pages)

        # ---------- 页眉页脚识别 ----------
        header_footer = _detect_header_footer(raw_pages)

        # ---------- 第二遍：分类与组装 ----------
        pages: list[Page] = []
        image_count = 0
        skipped_small = 0
        saved_xrefs: dict[int, str] = {}

        for raw in raw_pages:
            page_no = raw["page_no"]
            blocks: list[Block] = []

            if raw.get("failed"):
                pages.append(
                    Page(page_no=page_no, has_text_layer=False, blocks=[])
                )
                continue

            # --- 文本块（先剔除页眉页脚，再合并被排版切碎的段落）---
            text_entries: list[dict] = []
            for entry in raw["blocks"]:
                if _is_header_footer(entry["text"], header_footer):
                    continue
                text_entries.append(entry)
            text_entries = _merge_paragraph_blocks(text_entries, median_size)

            # --- 图片块（并尝试匹配图注）---
            figure_entries: list[dict] = []
            if extract_images:
                for idx, img in enumerate(raw["images"]):
                    if img["width"] < image_min_size_px or img["height"] < image_min_size_px:
                        skipped_small += 1
                        continue

                    rel = saved_xrefs.get(img["xref"])
                    if rel is None:
                        rel = storage.save_image(
                            file_hash, page_no, idx, img["ext"], img["data"]
                        )
                        saved_xrefs[img["xref"]] = rel

                    caption, consumed = _find_caption(img["bbox"], text_entries)
                    if consumed is not None:
                        text_entries.remove(consumed)

                    figure_entries.append(
                        {
                            "type": BlockType.FIGURE,
                            "bbox": img["bbox"],
                            "image_path": rel,
                            "caption": caption,
                            "width": img["width"],
                            "height": img["height"],
                        }
                    )
                    image_count += 1

            # --- 分类文本块 ---
            for entry in text_entries:
                text = entry["text"].strip()
                if not text:
                    continue
                if _looks_like_heading(text, entry["max_size"], median_size):
                    blocks.append(
                        Block(
                            type=BlockType.HEADING,
                            text=text,
                            level=_heading_level(entry["max_size"], median_size),
                            bbox=entry["bbox"],
                        )
                    )
                else:
                    blocks.append(
                        Block(type=BlockType.TEXT, text=text, bbox=entry["bbox"])
                    )

            # --- 按阅读顺序（自上而下、自左而右）合并 ---
            ordered = sorted(
                [*blocks, *figure_entries],
                key=lambda b: (
                    b.bbox[1] if isinstance(b, Block) else b["bbox"][1],
                    b.bbox[0] if isinstance(b, Block) else b["bbox"][0],
                ),
            )
            final_blocks: list[Block] = [
                b if isinstance(b, Block) else Block(**b) for b in ordered
            ]

            # has_text_layer 的依据是"最终产出里有没有文字类块"，
            # 而不是"原始块里有没有文字" —— 只含页眉页脚的页面应当被判定为无文本层。
            page_has_text = any(
                b.type in (BlockType.HEADING, BlockType.TEXT, BlockType.TABLE)
                and (b.text or "").strip()
                for b in final_blocks
            )

            pages.append(
                Page(page_no=page_no, has_text_layer=page_has_text, blocks=final_blocks)
            )

        # ---------- 组装结果 ----------
        parsed = ParsedDocument(
            document_hash=file_hash,
            file_name=file_name,
            file_type="pdf",
            page_count=len(pages),
            pages=pages,
            meta={
                "parser": "pymupdf",
                "parser_version": getattr(fitz, "VersionBind", None)
                or (getattr(fitz, "version", None) or ("unknown",))[0],
                "median_font_size": round(median_size, 2),
                "image_count": image_count,
                "warnings": [],
            },
        )

        # ---------- 警告汇总（基于最终产出，而不是原始块）----------
        for raw, page in zip(raw_pages, pages, strict=True):
            page_no = page.page_no
            if raw.get("failed"):
                parsed.add_warning(
                    WARN_PAGE_PARSE_FAILED,
                    f"第 {page_no} 页解析失败，已跳过：{raw['failed']}",
                    page_no,
                )
                continue
            if not page.has_text_layer:
                parsed.add_warning(
                    WARN_NO_TEXT_LAYER,
                    f"第 {page_no} 页没有文本层，疑似扫描图片，P1 未启用 OCR，该页内容不会被提取。",
                    page_no,
                )

        if image_count == 0 and skipped_small:
            parsed.add_warning(
                WARN_IMAGE_TOO_SMALL,
                f"跳过了 {skipped_small} 张过小的图片（小于 {image_min_size_px}px，视为图标或分隔线）。",
            )
        if not extract_images:
            parsed.add_warning(WARN_IMAGE_EXTRACT_DISABLED, "本次解析未提取内嵌图片。")
        if image_count:
            parsed.add_warning(
                WARN_FORMULA_NOT_EXTRACTED,
                f"已提取 {image_count} 张图片；公式与图表的内容理解需要多模态模型，P1 暂不支持。",
            )

        text_pages = sum(1 for p in pages if p.has_text_layer)
        if text_pages == 0:
            raise ParserError(
                "该 PDF 的所有页面都没有文本层，基本可以确定是扫描件。"
                "P1 阶段尚未启用 OCR，请改用带文本层的 PDF 或图片形式上传。",
                code="NO_TEXT_LAYER",
            )

        # 文本层质量检查：有文本层不等于文本可用。
        # 字体缺少字形映射或编码错乱时，提取出来可能全是 `·`、`□` 之类的符号，
        # 下游抽取会静默返回空结果。这里提前告警，把"处理成功但没有知识点"变成可解释的现象。
        all_text = "\n".join(
            b.text for p in pages for b in p.blocks if b.text
        )
        if looks_garbled(all_text):
            parsed.add_warning(
                WARN_SUSPECT_TEXT_LAYER,
                (
                    f"提取出的文字中可读字符仅占 {meaningful_ratio(all_text) * 100:.0f}%，"
                    "疑似文本层损坏或字体缺少字形映射。知识点抽取大概率无法产出结果，"
                    "建议改用其他来源的 PDF。"
                ),
            )

        return parsed
    finally:
        doc.close()


# --------------------------------------------------------------------------- #
# 单页抽取
# --------------------------------------------------------------------------- #
def _extract_raw_page(page: "fitz.Page", page_no: int) -> dict:
    """抽取一页的原始文本块与图片。"""
    page_dict = page.get_text("dict")
    blocks: list[dict] = []

    for raw_block in page_dict.get("blocks", []):
        if raw_block.get("type") != 0:  # 0 = 文本，1 = 图片（图片另行用 get_images 处理）
            continue

        line_texts: list[str] = []
        sizes: list[tuple[float, int]] = []

        for line in raw_block.get("lines", []):
            span_texts = [
                span.get("text", "") for span in line.get("spans", [])
            ]
            line_texts.append("".join(span_texts))
            for span in line.get("spans", []):
                sizes.append((float(span.get("size", 0)), len(span.get("text", ""))))

        if not any(t.strip() for t in line_texts):
            continue

        text = _merge_lines(line_texts)
        if not text:
            continue

        max_size = max((s for s, n in sizes if n > 0), default=0.0)
        blocks.append(
            {
                "text": text,
                "bbox": [float(v) for v in raw_block.get("bbox", (0, 0, 0, 0))],
                "max_size": max_size,
            }
        )

    images: list[dict] = []
    for img_info in page.get_images(full=True):
        xref = int(img_info[0])
        try:
            base = page.parent.extract_image(xref)
        except Exception as exc:  # noqa: BLE001
            logger.debug("第 %d 页 xref=%s 图片提取失败：%s", page_no, xref, exc)
            continue

        rects = []
        try:
            rects = page.get_image_rects(xref)
        except Exception:  # noqa: BLE001
            pass
        bbox = [float(v) for v in (rects[0] if rects else (0, 0, 0, 0))]

        images.append(
            {
                "xref": xref,
                "ext": base.get("ext", "png"),
                "data": base.get("image", b""),
                "width": int(base.get("width", 0)),
                "height": int(base.get("height", 0)),
                "bbox": bbox,
            }
        )

    return {
        "page_no": page_no,
        "height": float(page.rect.height),
        "blocks": blocks,
        "images": images,
    }


def _merge_lines(line_texts: list[str]) -> str:
    """把 PDF 里被硬换行的同一段落重新拼成语义段落。

    PDF 的行是排版结果而非语义，直接按行保留会让每个视觉行变成断句，
    严重影响后续分块与知识点抽取。
    """
    out = ""
    for raw_line in line_texts:
        line = raw_line.strip()
        if not line:
            continue
        if not out:
            out = line
            continue

        prev = out[-1]
        if prev in _SENTENCE_END:
            out += "\n" + line
        elif _is_word_char(prev) and _is_word_char(line[0]):
            # 英文断词，补一个空格
            out += " " + line
        else:
            out += line
    return out


def _merge_paragraph_blocks(entries: list[dict], median_size: float) -> list[dict]:
    """把被排版切成多个块的同一段落重新拼起来。

    这是 PDF 解析绕不开的一步：很多 PDF（尤其是 Office 导出的）把**每一视觉行**
    输出成独立的文本块，一个 4 行的段落会变成 4 个块。若不合并：
      - 分块器会把每一行当成独立内容，产出大量十几字的碎块
      - 知识点抽取拿到的上下文是断的，摘要与讲解都会失真

    合并条件（全部满足才合并）：
      1. 上一块结尾**不是**段落结束标点 —— 句子没写完，说明还有下文
      2. 两块垂直间距足够小 —— 排除了跨栏、跨表格的情况
      3. 上一块**不是标题**、且下一块字号没有明显变大 —— 避免把标题并进正文
    """
    if not entries:
        return []

    # 允许的最大垂直间距。取中位字号的 2.5 倍，兼顾紧排版与松排版。
    max_gap = max(median_size * 2.5, 18.0) if median_size > 0 else 24.0

    merged: list[dict] = []
    for entry in entries:
        if merged and _should_merge(merged[-1], entry, max_gap, median_size):
            previous = merged[-1]
            previous["text"] = _merge_lines([previous["text"], entry["text"]])
            previous["bbox"] = [
                min(previous["bbox"][0], entry["bbox"][0]),
                min(previous["bbox"][1], entry["bbox"][1]),
                max(previous["bbox"][2], entry["bbox"][2]),
                max(previous["bbox"][3], entry["bbox"][3]),
            ]
            previous["max_size"] = max(previous["max_size"], entry["max_size"])
            continue
        merged.append(dict(entry))
    return merged


def _should_merge(previous: dict, current: dict, max_gap: float, median_size: float) -> bool:
    """判断当前块是否是上一段的续行。"""
    prev_text = (previous["text"] or "").rstrip()
    cur_text = (current["text"] or "").strip()
    if not prev_text or not cur_text:
        return False

    # 条件 1：上一块没写完
    if prev_text[-1] in _PARAGRAPH_END:
        return False

    # 条件 3a：上一块本身是标题
    if _looks_like_heading(prev_text, previous["max_size"], median_size):
        return False
    # 条件 3b：下一块字号明显变大（可能是新标题）
    if previous["max_size"] > 0 and current["max_size"] > previous["max_size"] * 1.2:
        return False

    # 条件 2：垂直间距合理
    gap = current["bbox"][1] - previous["bbox"][3]
    if gap < -2.0 or gap > max_gap:
        return False

    # 条件 2b：水平方向应当有重叠（排除左右分栏被误并）
    overlap = min(previous["bbox"][2], current["bbox"][2]) - max(
        previous["bbox"][0], current["bbox"][0]
    )
    if overlap <= 0:
        return False

    return True


def _is_word_char(ch: str) -> bool:
    return ch.isascii() and (ch.isalnum())


def _weighted_median_size(raw_pages: list[dict]) -> float:
    """按字符数加权的字号中位数，作为「正文大小」的基准。"""
    pairs: list[tuple[float, int]] = []
    for raw in raw_pages:
        for block in raw.get("blocks", []):
            if block["max_size"] > 0 and block["text"]:
                pairs.append((block["max_size"], len(block["text"])))

    if not pairs:
        return 0.0

    pairs.sort(key=lambda p: p[0])
    total = sum(n for _, n in pairs)
    if total == 0:
        return pairs[len(pairs) // 2][0]

    acc = 0
    for size, count in pairs:
        acc += count
        if acc >= total / 2:
            return size
    return pairs[-1][0]


def _detect_header_footer(raw_pages: list[dict]) -> set[str]:
    """识别跨页重复的页眉页脚文本。返回需要丢弃的归一化文本集合。"""
    counter: dict[str, int] = {}
    candidates: dict[str, str] = {}

    for raw in raw_pages:
        height = raw.get("height") or 0
        if height <= 0:
            continue
        for block in raw.get("blocks", []):
            bbox = block["bbox"]
            in_header = bbox[3] <= height * _HEADER_RATIO
            in_footer = bbox[1] >= height * _FOOTER_RATIO
            if not (in_header or in_footer):
                continue
            normalized = _normalize_stamp(block["text"])
            if not normalized:
                continue
            counter[normalized] = counter.get(normalized, 0) + 1
            candidates[normalized] = block["text"]

    return {key for key, count in counter.items() if count >= _REPEAT_THRESHOLD}


def _normalize_stamp(text: str) -> str:
    """页眉页脚归一化：去掉数字与空白（页码会逐页变化，否则无法匹配）。"""
    return re.sub(r"[\d\s\-—_·.]+", "", text or "").strip()


def _is_header_footer(text: str, header_footer: set[str]) -> bool:
    if not header_footer:
        return False
    return _normalize_stamp(text) in header_footer


#: 明确的数学运算符。刻意**不含** `.`、`-`、`_`、括号 —— 它们在正常标题里很常见
#: （如「3.1 进程的概念」「2-1 节」），放进来会误伤。
_MATH_OPERATORS = frozenset("=+−×÷∑∫√±≤≥≠∂∞∈∀∃→←↔^")


def _looks_like_formula(text: str) -> bool:
    """判断一行文本是公式碎片而不是章节标题。

    动机来自真实数据：一份含大量 LaTeX 公式的论文 PDF，公式行因为字号较大被
    「字号明显大」这条规则误判为标题，于是 heading_path 里出现了
    `d x P x d x= ∑`、`== −−` 这种"章节名"。

    危害不只是难看 —— heading_path 是 P1 分块语境与 P2 知识点关系、图谱分带的共同依据，
    脏标题会把毫不相干的知识点归到同一节，并让 contains 关系凭空产生。

    两条判定依据（满足其一即认为是公式）：
      1. 含私有区码位（U+E000–U+F8FF）。这是 Symbol 字体符号被提取后的典型残留，
         正常正文与标题都不会有。
      2. 含数学运算符、且几乎没有中日韩文字。真正的章节名不长这样。
    """
    if not text:
        return False
    if any(0xE000 <= ord(ch) <= 0xF8FF for ch in text):
        return True
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    if cjk >= 4:
        # 有足够多的汉字，基本可确定是正常标题（公式行里的汉字通常是零星几个）
        return False
    return any(ch in _MATH_OPERATORS for ch in text)


def _looks_like_heading(text: str, max_size: float, median_size: float) -> bool:
    """标题判定：字号明显大，或匹配序号式标题。

    公式行先被排除 —— 公式的字号往往比正文大，是最容易被误判成标题的一类内容。
    """
    if len(text) > _MAX_HEADING_LEN or not text:
        return False
    if _looks_like_formula(text):
        return False
    if _NUMBERED_HEADING.match(text):
        return True
    if median_size > 0 and max_size >= median_size * _HEADING_SIZE_RATIO:
        return True
    return False


def _heading_level(max_size: float, median_size: float) -> int:
    """按字号倍数粗略推断标题层级。"""
    if median_size <= 0:
        return 2
    ratio = max_size / median_size
    if ratio >= 1.6:
        return 1
    if ratio >= 1.35:
        return 2
    return 3


def _find_caption(
    image_bbox: list[float], text_entries: list[dict]
) -> tuple[str | None, dict | None]:
    """在图片下方寻找图注。

    返回 (图注文本, 被消费的文本块)。找到图注时把它从文本块里移除，
    避免同一段文字既当图注又当正文，造成重复内容。
    """
    img_bottom = image_bbox[3]
    best: dict | None = None
    best_gap = _CAPTION_MAX_GAP

    for entry in text_entries:
        text = entry["text"].strip()
        if not text or len(text) > _CAPTION_MAX_LEN:
            continue
        if not _CAPTION_PATTERN.match(text):
            continue
        gap = entry["bbox"][1] - img_bottom
        if 0 <= gap <= best_gap:
            best = entry
            best_gap = gap

    if best is None:
        return None, None
    return best["text"].strip(), best
