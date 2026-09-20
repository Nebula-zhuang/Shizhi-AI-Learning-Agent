"""分块器测试：长度约束、章节边界、重叠规则、特殊块处理、小块合并。

这些是最容易出错也最难在线发现的逻辑 —— 分块错了，知识点抽取的粒度就全乱，
但表面上系统"一切正常"。因此这里逐条钉死契约。
"""

from __future__ import annotations

from app.ingestion.base import Block, ChunkDraft, Page, ParsedDocument
from app.ingestion.chunker import chunk_document
from app.models.chunk import BlockType

PARAMS = dict(target_chars=200, max_chars=400, min_chars=50, overlap_chars=20)


def build(pages: list[list[tuple[BlockType, str, int | None]]]) -> ParsedDocument:
    """用简写构造文档结构：每页是 (类型, 文本, 标题层级) 的列表。"""
    return ParsedDocument(
        document_hash="h",
        file_name="t.txt",
        file_type="txt",
        page_count=len(pages),
        pages=[
            Page(
                page_no=index + 1,
                blocks=[
                    Block(type=block_type, text=text, level=level)
                    for block_type, text, level in blocks
                ],
            )
            for index, blocks in enumerate(pages)
        ],
        meta={},
    )


def text_of(length: int, marker: str = "正") -> str:
    return marker * length


# --------------------------------------------------------------------------- #
# 章节边界
# --------------------------------------------------------------------------- #
def test_heading_starts_new_chunk() -> None:
    parsed = build(
        [
            [
                (BlockType.HEADING, "第一章 绪论", 1),
                (BlockType.TEXT, text_of(80), None),
                (BlockType.HEADING, "第二章 方法", 1),
                (BlockType.TEXT, text_of(80), None),
            ]
        ]
    )
    drafts = chunk_document(parsed, **PARAMS)

    assert [d.chunk_index for d in drafts] == list(range(len(drafts)))
    assert drafts[0].content.startswith("第一章 绪论")
    assert drafts[1].content.startswith("第二章 方法")
    assert drafts[0].heading_path == ["第一章 绪论"]
    assert drafts[1].heading_path == ["第二章 方法"]


def test_no_overlap_across_sections() -> None:
    """跨章节不重叠，否则上一章的尾巴会污染下一章的首块。"""
    parsed = build(
        [
            [
                (BlockType.HEADING, "第一章", 1),
                (BlockType.TEXT, text_of(250, "甲"), None),
                (BlockType.HEADING, "第二章", 1),
                (BlockType.TEXT, text_of(250, "乙"), None),
            ]
        ]
    )
    drafts = chunk_document(parsed, **PARAMS)
    second = next(d for d in drafts if d.heading_path == ["第二章"])

    assert "甲" not in second.content, "第二章的块里不应出现第一章的内容"
    assert second.content.startswith("第二章")


def test_overlap_within_section() -> None:
    """同一章节内超长内容切分时，后一块应带上前一块的尾巴。"""
    parsed = build([[ (BlockType.HEADING, "唯一章节", 1), (BlockType.TEXT, text_of(700, "丙"), None) ]])
    drafts = chunk_document(parsed, **PARAMS)

    assert len(drafts) >= 2
    tail = drafts[0].content[-PARAMS["overlap_chars"] :]
    assert drafts[1].content.startswith(tail)


# --------------------------------------------------------------------------- #
# 长度约束
# --------------------------------------------------------------------------- #
def test_chunks_never_exceed_max() -> None:
    """硬上限是绝对约束：任何情况下单块都不得超过 max_chars。"""
    parsed = build([[ (BlockType.TEXT, text_of(1500, "丁"), None) ]])
    drafts = chunk_document(parsed, **PARAMS)

    assert len(drafts) >= 3
    assert all(d.char_count <= PARAMS["max_chars"] for d in drafts)


def test_chunks_aim_for_target_when_text_is_breakable() -> None:
    """切分阈值应当是 target 而不是 max。

    若用 max 做阈值，块会长到 2 倍 target 附近，target 参数就形同虚设。
    这里用带句号的正常文本（可断句），块应稳定落在 target 附近。
    """
    sentence = "这是用于测试分块粒度的一句话，长度约二十四个字符。"  # 24 字
    parsed = build([[ (BlockType.TEXT, sentence * 40, None) ]])  # 960 字
    drafts = chunk_document(parsed, **PARAMS)

    assert len(drafts) >= 4
    # 每块都不超过 上限；且明显小于"用 max 当阈值"时的 400
    assert all(d.char_count <= PARAMS["max_chars"] for d in drafts)
    assert max(d.char_count for d in drafts) <= PARAMS["target_chars"] + len(sentence)


def test_long_block_without_punctuation_is_force_split() -> None:
    parsed = build([[ (BlockType.TEXT, text_of(900, "戊"), None) ]])
    drafts = chunk_document(parsed, **PARAMS)

    assert len(drafts) >= 3
    assert all(d.char_count <= PARAMS["max_chars"] for d in drafts)
    codes = [w.code for w in parsed.warnings]
    assert "FORCED_SPLIT" in codes


def test_small_tail_chunks_are_merged() -> None:
    """章节末尾的碎块应并入前一块，避免产出噪声知识点。"""
    parsed = build(
        [
            [
                (BlockType.TEXT, text_of(180, "己"), None),
                (BlockType.TEXT, text_of(20, "尾"), None),
            ]
        ]
    )
    drafts = chunk_document(parsed, **PARAMS)

    assert len(drafts) == 1
    assert "尾" in drafts[0].content


# --------------------------------------------------------------------------- #
# 特殊块
# --------------------------------------------------------------------------- #
def test_table_and_figure_are_standalone_chunks() -> None:
    parsed = build(
        [
            [
                (BlockType.TEXT, text_of(100, "庚"), None),
                (BlockType.TABLE, "| a | b |\n| 1 | 2 |", None),
                (BlockType.FIGURE, "", None),
            ]
        ]
    )
    # 给图片块补上必要字段
    parsed.pages[0].blocks[2].caption = "图 1 结构示意"
    parsed.pages[0].blocks[2].image_path = "h/images/p1_0.png"

    drafts = chunk_document(parsed, **PARAMS)
    by_type = {d.block_type: d for d in drafts}

    assert BlockType.TABLE in by_type
    assert by_type[BlockType.TABLE].content.startswith("| a | b |")
    assert BlockType.FIGURE in by_type
    assert by_type[BlockType.FIGURE].content == "图 1 结构示意"
    assert by_type[BlockType.FIGURE].image_path == "h/images/p1_0.png"


def test_oversize_table_is_not_cut() -> None:
    """表格被切断的危害大于超长，因此即使超过上限也不切。"""
    big_table = "| 行 | 值 |\n" + "\n".join(f"| {i} | {i * 2} |" for i in range(60))
    parsed = build([[ (BlockType.TABLE, big_table, None) ]])
    drafts = chunk_document(parsed, **PARAMS)

    assert len(drafts) == 1
    assert drafts[0].char_count > PARAMS["max_chars"]
    assert "OVERSIZE_BLOCK" in [w.code for w in parsed.warnings]


# --------------------------------------------------------------------------- #
# 页码
# --------------------------------------------------------------------------- #
def test_page_range_is_tracked() -> None:
    parsed = build(
        [
            [(BlockType.TEXT, text_of(120, "辛"), None)],
            [(BlockType.TEXT, text_of(120, "壬"), None)],
            [(BlockType.TEXT, text_of(120, "癸"), None)],
        ]
    )
    drafts = chunk_document(parsed, **PARAMS)

    assert drafts[0].page_start == 1
    assert drafts[-1].page_end == 3
    for draft in drafts:
        assert draft.page_start <= draft.page_end


def test_empty_document_returns_no_chunks() -> None:
    parsed = build([[ (BlockType.TEXT, "   ", None) ]])
    assert chunk_document(parsed, **PARAMS) == []


def test_chunk_index_is_continuous_after_merge() -> None:
    parsed = build(
        [
            [
                (BlockType.TEXT, text_of(180, "子"), None),
                (BlockType.TEXT, text_of(10, "丑"), None),
                (BlockType.TABLE, "| x |\n| 1 |", None),
                (BlockType.TEXT, text_of(180, "寅"), None),
            ]
        ]
    )
    drafts = chunk_document(parsed, **PARAMS)
    assert [d.chunk_index for d in drafts] == list(range(len(drafts)))


def test_token_estimate_is_derived_from_length() -> None:
    draft = ChunkDraft(chunk_index=0, content="甲" * 100, page_start=1, page_end=1, block_type="text")
    assert draft.char_count == 100
    assert draft.token_estimate == 75


# --------------------------------------------------------------------------- #
# 章节层级（回归测试）
# --------------------------------------------------------------------------- #
def test_same_level_headings_are_siblings_even_without_level_one() -> None:
    """回归：文档直接从 level 2 开始时，同级标题必须是兄弟而不是父子。

    旧实现用 `section_path[:level-1] + [text]` 推导层级，这隐含假设
    「文档第一层标题一定是 level 1」。当整份资料只是一个子章节
    （标题全是 level 2、没有 level 1）时，首个标题会占住路径根位，
    之后每个同级标题都被截断后追加，于是 3.2 / 3.3 / 3.4 全部错嵌到 3.1 下面，
    整份文档的章节树退化成一条链。

    危害不止于元数据难看：P2 的知识点关系构建直接依赖 heading_path，
    层级错了会让 contains 关系全部指向第一个知识点，图谱退化成一个星形。
    """
    parsed = build(
        [
            [
                (BlockType.HEADING, "3.1 进程的概念", 2),
                (BlockType.TEXT, text_of(80), None),
                (BlockType.HEADING, "3.2 进程的状态", 2),
                (BlockType.TEXT, text_of(80), None),
                (BlockType.HEADING, "3.3 进程控制块", 2),
                (BlockType.TEXT, text_of(80), None),
            ]
        ]
    )
    drafts = chunk_document(parsed, **PARAMS)

    paths = [d.heading_path for d in drafts]
    assert ["3.1 进程的概念"] in paths
    assert ["3.2 进程的状态"] in paths
    assert ["3.3 进程控制块"] in paths
    # 关键断言：3.2 绝不能挂在 3.1 下面
    assert ["3.1 进程的概念", "3.2 进程的状态"] not in paths


def test_nested_headings_still_nest_correctly() -> None:
    """正常的父子层级不能被修坏。"""
    parsed = build(
        [
            [
                (BlockType.HEADING, "第二章 内存管理", 1),
                (BlockType.TEXT, text_of(80), None),
                (BlockType.HEADING, "2.1 分页", 2),
                (BlockType.TEXT, text_of(80), None),
                (BlockType.HEADING, "2.1.1 页表", 3),
                (BlockType.TEXT, text_of(80), None),
                (BlockType.HEADING, "2.2 分段", 2),
                (BlockType.TEXT, text_of(80), None),
            ]
        ]
    )
    drafts = chunk_document(parsed, **PARAMS)
    paths = [d.heading_path for d in drafts]

    assert ["第二章 内存管理"] in paths
    assert ["第二章 内存管理", "2.1 分页"] in paths
    # 三级标题嵌在二级下面
    assert ["第二章 内存管理", "2.1 分页", "2.1.1 页表"] in paths
    # 2.2 与 2.1 同级，不能嵌在 2.1 下
    assert ["第二章 内存管理", "2.2 分段"] in paths
    assert ["第二章 内存管理", "2.1 分页", "2.2 分段"] not in paths


def test_heading_level_jump_back_finds_correct_parent() -> None:
    """层级回升时要回到正确的父节点，而不是残留上一个深层标题。"""
    parsed = build(
        [
            [
                (BlockType.HEADING, "第一章", 1),
                (BlockType.HEADING, "1.1 节", 2),
                (BlockType.HEADING, "1.1.1 小节", 3),
                (BlockType.TEXT, text_of(80), None),
                (BlockType.HEADING, "1.2 节", 2),
                (BlockType.TEXT, text_of(80), None),
            ]
        ]
    )
    drafts = chunk_document(parsed, **PARAMS)
    paths = [d.heading_path for d in drafts]

    assert ["第一章", "1.2 节"] in paths
    assert ["第一章", "1.1 节", "1.2 节"] not in paths

