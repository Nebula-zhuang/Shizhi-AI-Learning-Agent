"""语义分块器。

把统一文档结构切成带页码的语义块。参数与边界规则见 skills/doc-ingestion/SKILL.md §5。

核心设计：
- **标题开启新块**：标题文本作为该章节首个块的首行，后续块不再重复，
  章节语境由 `heading_path` 元数据承载（面包屑），避免正文重复。
- **重叠只在章节内**：跨章节不重叠，否则上一章的尾巴会污染下一章的首块。
- **表格与图片自成一块**：即使超长也不切 —— 被切碎的表格对知识点抽取是噪声，
  危害大于超长本身。
"""

from __future__ import annotations

import re

from app.core.logging import get_logger
from app.ingestion.base import (
    WARN_FORCED_SPLIT,
    WARN_OVERSIZE_BLOCK,
    ChunkDraft,
    ParsedDocument,
)
from app.models.chunk import BlockType

logger = get_logger(__name__)

#: 句末标点，强制切分时的优先断点
_SENTENCE_BREAK = re.compile(r"(?<=[。！？；.!?;])")


def chunk_document(
    parsed: ParsedDocument,
    *,
    target_chars: int = 600,
    max_chars: int = 1200,
    min_chars: int = 150,
    overlap_chars: int = 80,
) -> list[ChunkDraft]:
    """把文档结构切成分块列表。"""
    drafts: list[ChunkDraft] = []

    section_path: list[str] = []
    # 标题栈，元素为 (level, text)。
    #
    # 为什么用栈而不是 `section_path[:level-1] + [text]` 这种算术：
    # 后者隐含假设「文档的第一层标题一定是 level 1」。当文档直接从 level 2 开始时
    # （很常见 —— 一份资料往往只是某个大章节的子节），首个标题会占据路径根位，
    # 之后每个同级标题都被截断后追加，于是 3.2 / 3.3 / 3.4 全部错嵌到 3.1 下面，
    # 整份文档的章节层级被压成一棵退化的链。
    # 栈的写法与"文档从第几层开始"无关，遇到同级或更高级标题就弹出，永远正确。
    section_stack: list[tuple[int, str]] = []
    buffer: list[str] = []
    buf_page_start = 1
    buf_page_end = 1
    prev_tail = ""
    prev_page_end = 1

    def current_text() -> str:
        return "\n".join(piece for piece in buffer if piece).strip()

    def flush() -> None:
        """把当前缓冲落成一个块。"""
        nonlocal buffer, buf_page_start, buf_page_end, prev_tail, prev_page_end

        text = current_text()
        if not text:
            buffer = []
            return

        drafts.append(
            ChunkDraft(
                chunk_index=len(drafts),
                content=text,
                page_start=buf_page_start,
                page_end=buf_page_end,
                block_type=str(BlockType.TEXT),
                heading_path=list(section_path),
            )
        )

        # 为同一章节内的下一个块准备重叠尾巴
        prev_tail = text[-overlap_chars:] if overlap_chars > 0 else ""
        prev_page_end = buf_page_end
        buffer = []

    def start_buffer(with_overlap: bool, page_no: int, reserve: int = 0) -> None:
        """开启新的缓冲。

        reserve 是即将追加内容的长度。重叠尾巴必须为它让路 ——
        否则「上一块的尾巴 + 新内容」会突破 max_chars。
        这类超标不会报错，只会让下游拿到超长块，属于很难发现的问题。
        """
        nonlocal buffer, buf_page_start, buf_page_end
        if with_overlap and prev_tail:
            room = max_chars - reserve - 1
            tail = prev_tail[-room:] if room > 0 else ""
            buffer = [tail] if tail else []
            buf_page_start = prev_page_end if tail else page_no
        else:
            buffer = []
            buf_page_start = page_no
        buf_page_end = page_no

    for page_no, block in parsed.iter_blocks():
        block_text = (block.text or "").strip()

        # ------------------------------------------------------------ 标题
        if block.type == BlockType.HEADING:
            flush()
            level = block.level or 2
            # 弹出所有层级 >= 当前标题的旧标题，再把自己压栈。
            # 于是 level 相同的标题互为兄弟，而不是父子。
            while section_stack and section_stack[-1][0] >= level:
                section_stack.pop()
            section_stack.append((level, block_text))
            section_path = [text for _, text in section_stack]
            prev_tail = ""  # 章节边界：不跨章节重叠
            start_buffer(with_overlap=False, page_no=page_no)
            buffer.append(block_text)
            buf_page_end = page_no
            continue

        # -------------------------------------------------- 表格 / 图片
        if block.type in (BlockType.TABLE, BlockType.FIGURE):
            flush()
            if block.type == BlockType.FIGURE:
                content = block.caption or f"[图片] {block.image_path or ''}".strip()
            else:
                content = block_text

            if len(content) > max_chars:
                parsed.add_warning(
                    WARN_OVERSIZE_BLOCK,
                    f"第 {page_no} 页的{_cn_name(block.type)}超过 {max_chars} 字符，"
                    "为保持内容完整未做切分。",
                    page_no,
                )

            drafts.append(
                ChunkDraft(
                    chunk_index=len(drafts),
                    content=content,
                    page_start=page_no,
                    page_end=page_no,
                    block_type=str(block.type),
                    heading_path=list(section_path),
                    image_path=block.image_path,
                )
            )
            prev_tail = ""
            prev_page_end = page_no
            start_buffer(with_overlap=False, page_no=page_no)
            continue

        # ------------------------------------------------------------ 正文
        if not block_text:
            continue

        # 硬上限只用于"切不开的单块"；正常切分以 target 为准。
        # 若用 max 做切分阈值，块会稳定落在 2 倍 target 附近，target 参数就形同虚设。
        pieces = _split_long_text(
            block_text,
            target_chars=target_chars,
            max_chars=max_chars,
            parsed=parsed,
            page_no=page_no,
        )
        for piece in pieces:
            if buffer and len(current_text()) + len(piece) + 1 > target_chars:
                flush()
                # 把 piece 的长度作为预留量传进去，重叠尾巴会据此自动缩短
                start_buffer(with_overlap=True, page_no=page_no, reserve=len(piece))

            if not buffer:
                start_buffer(with_overlap=False, page_no=page_no)

            buffer.append(piece)
            buf_page_end = page_no

    flush()
    return _merge_small_chunks(drafts, min_chars=min_chars, max_chars=max_chars)


def _cn_name(block_type: str) -> str:
    return {
        str(BlockType.TABLE): "表格",
        str(BlockType.FIGURE): "图片",
    }.get(block_type, "内容块")


def _split_long_text(
    text: str,
    *,
    target_chars: int,
    max_chars: int,
    parsed: ParsedDocument,
    page_no: int,
) -> list[str]:
    """把一个超长的文本块切成若干片段，供外层按 target 打包。

    切分基准是 **target** 而不是 max：
    外层负责"把片段打包到 target 大小"，因此片段本身必须小于等于 target，
    否则外层的 target 约束永远生效不了 —— 一个 384 字的片段塞进 200 的筐里，
    打出来的包就是 384 而不是 200。

    只有当文本完全没有断句标点时，才退化为按 max 硬切 —— 此时没有更好的切法，
    少切几刀（块大一些）比多切几刀造成更多语义断裂要好。
    """
    if len(text) <= target_chars:
        return [text]

    sentences = [s for s in _SENTENCE_BREAK.split(text) if s]

    if len(sentences) <= 1:
        parsed.add_warning(
            WARN_FORCED_SPLIT,
            f"第 {page_no} 页存在超长且无标点的文本，已按 {max_chars} 字符强制切分。",
            page_no,
        )
        return [text[i : i + max_chars] for i in range(0, len(text), max_chars)]

    pieces: list[str] = []
    current = ""
    for sentence in sentences:
        if current and len(current) + len(sentence) > target_chars:
            pieces.append(current)
            current = sentence
        else:
            current += sentence
    if current:
        pieces.append(current)

    # 单个句子仍超过硬上限时只能截断
    if any(len(p) > max_chars for p in pieces):
        parsed.add_warning(
            WARN_FORCED_SPLIT,
            f"第 {page_no} 页存在超过 {max_chars} 字符的超长句子，已强制切分。",
            page_no,
        )
        pieces = [
            p[i : i + max_chars] for p in pieces for i in range(0, len(p), max_chars)
        ]
    return pieces


def _is_bare_heading(draft: ChunkDraft) -> bool:
    """判断一块里是否只有标题本身、没有任何实质内容。

    什么时候会出现这种块：章节标题之后紧跟一个超长（且切不开）的内容块时，
    标题先被 flush 成一块，新内容再另起一块。这种块只有几个字，
    送去做知识点抽取只会产出噪声，必须被吸收掉。
    """
    return (
        draft.block_type == str(BlockType.TEXT)
        and bool(draft.heading_path)
        and draft.content.strip() == draft.heading_path[-1].strip()
    )


def _merge_small_chunks(
    drafts: list[ChunkDraft], *, min_chars: int, max_chars: int
) -> list[ChunkDraft]:
    """处理碎块：过短的尾块向后并入，孤立标题块向前并入。

    两遍处理对应两种真实场景：
      1. 章节末尾常留下几十字的碎块 → 并入同章节的前一块
      2. 章节标题后紧跟超长内容 → 标题单独成块 → 并入后一块
    """
    if not drafts:
        return []

    # ---------------------------------------------------------- 第一遍：向后合并
    backward: list[ChunkDraft] = []
    for draft in drafts:
        if (
            backward
            and draft.block_type == str(BlockType.TEXT)
            and backward[-1].block_type == str(BlockType.TEXT)
            and draft.char_count < min_chars
            and backward[-1].heading_path == draft.heading_path
            and backward[-1].char_count + draft.char_count <= max_chars
        ):
            previous = backward[-1]
            previous.content = f"{previous.content}\n{draft.content}".strip()
            previous.page_end = max(previous.page_end, draft.page_end)
            continue
        backward.append(draft)

    # ---------------------------------------------------------- 第二遍：向前合并
    # 只有标题的块必须被后一块吸收，因此这里不设长度上限 ——
    # 标题前缀很短，为它略微突破上限远好过留下一个孤立的碎块。
    forward: list[ChunkDraft] = []
    for draft in backward:
        if forward and _is_bare_heading(forward[-1]):
            head = forward.pop()
            draft.content = f"{head.content}\n{draft.content}".strip()
            draft.page_start = min(head.page_start, draft.page_start)
            draft.page_end = max(head.page_end, draft.page_end)
        forward.append(draft)

    # 兜底：若最后一块仍是孤立标题（文档以标题结尾），说明它后面没有任何内容，
    # 这样的块不含可考查的信息，直接丢弃。
    if len(forward) > 1 and _is_bare_heading(forward[-1]):
        forward.pop()

    # 合并后重新编号，保证 chunk_index 连续且从 0 开始
    for index, draft in enumerate(forward):
        draft.chunk_index = index
    return forward
