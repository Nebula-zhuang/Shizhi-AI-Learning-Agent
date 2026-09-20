"""知识点抽取服务测试。

重点覆盖三件最容易出错、且出错后很难被发现的事：
  1. 页码必须由代码回填 —— 模型没有机会编造
  2. 越界/空来源的知识点必须被丢弃
  3. 去重与合并的字段取舍规则
"""

from __future__ import annotations

from app.models.chunk import Chunk
from app.services.knowledge_service import (
    KPRecord,
    KPExtract,
    build_batches,
    build_mock_payload,
    merge_duplicates,
    normalize_title,
    parse_response,
    validate_and_backfill,
)
from app.models.chunk import BlockType


def make_chunk(
    index: int,
    content: str,
    *,
    page_start: int = 1,
    page_end: int | None = None,
    heading_path: list[str] | None = None,
    block_type: str = str(BlockType.TEXT),
) -> Chunk:
    """构造一个不落库的 Chunk 对象，用于纯逻辑测试。"""
    chunk = Chunk(
        document_id=1,
        chunk_index=index,
        content=content,
        page_start=page_start,
        page_end=page_end if page_end is not None else page_start,
        block_type=block_type,
        heading_path=heading_path,
    )
    return chunk


# --------------------------------------------------------------------------- #
# 标题归一化
# --------------------------------------------------------------------------- #
def test_normalize_strips_punctuation_and_case() -> None:
    assert normalize_title("进程与线程的区别") == normalize_title("进程线程区别")
    assert normalize_title("  PCB 的作用 ") == normalize_title("pcb的作用")
    assert normalize_title("页表（Page Table）") == normalize_title("页表PageTable")


def test_normalize_keeps_distinct_concepts_apart() -> None:
    """归一化不能过度，否则会把不同考点并成一个。"""
    assert normalize_title("进程") != normalize_title("线程")
    assert normalize_title("死锁") != normalize_title("饥饿")
    assert normalize_title("分页") != normalize_title("分段")


# --------------------------------------------------------------------------- #
# 批次组装
# --------------------------------------------------------------------------- #
def test_batches_group_by_heading_path() -> None:
    chunks = [
        make_chunk(0, "甲" * 100, heading_path=["第一章"]),
        make_chunk(1, "乙" * 100, heading_path=["第一章"]),
        make_chunk(2, "丙" * 100, heading_path=["第二章"]),
    ]
    batches = build_batches(chunks, batch_chunks=10, batch_max_chars=4000)

    assert len(batches) == 2
    assert [c.chunk_index for c in batches[0]] == [0, 1]
    assert [c.chunk_index for c in batches[1]] == [2]


def test_batches_respect_size_limits() -> None:
    chunks = [make_chunk(i, "丁" * 100) for i in range(10)]

    by_count = build_batches(chunks, batch_chunks=3, batch_max_chars=10000)
    assert all(len(b) <= 3 for b in by_count)

    by_chars = build_batches(chunks, batch_chunks=100, batch_max_chars=250)
    assert all(sum(len(c.content) for c in b) <= 250 for b in by_chars)


def test_figure_and_tiny_chunks_are_not_extractable() -> None:
    """图片块与过短的块送去抽取只会产出噪声。"""
    chunks = [
        make_chunk(0, "戊" * 100),
        make_chunk(1, "[图片] h/images/p1_0.png", block_type=str(BlockType.FIGURE)),
        make_chunk(2, "太短", block_type=str(BlockType.TEXT)),
    ]
    batches = build_batches(chunks, batch_chunks=10, batch_max_chars=4000)

    indexes = [c.chunk_index for b in batches for c in b]
    assert indexes == [0]


# --------------------------------------------------------------------------- #
# 页码回填与溯源校验
# --------------------------------------------------------------------------- #
def test_pages_are_backfilled_from_chunks() -> None:
    """页码必须由 chunk 反查得到 —— 模型没有输出页码的字段。"""
    batch = [make_chunk(3, "己" * 100, page_start=5, page_end=6)]
    warnings: list[dict] = []

    records = validate_and_backfill(
        [KPExtract(title="某知识点", source_chunk_indexes=[3])],
        batch=batch,
        heading_path=["第二章"],
        warnings=warnings,
    )

    assert len(records) == 1
    assert records[0].source_pages == [5, 6]
    assert records[0].source_chunk_indexes == [3]
    assert not warnings


def test_kp_without_valid_source_is_dropped() -> None:
    batch = [make_chunk(3, "庚" * 100)]
    warnings: list[dict] = []

    records = validate_and_backfill(
        [
            KPExtract(title="没有来源", source_chunk_indexes=[]),
            KPExtract(title="越界来源", source_chunk_indexes=[999]),
        ],
        batch=batch,
        heading_path=[],
        warnings=warnings,
    )

    assert records == []
    assert any(w["code"] == "INVALID_SOURCE" for w in warnings)


def test_partial_valid_source_keeps_kp() -> None:
    """部分来源越界时保留合法的那部分，而不是整条丢弃。"""
    batch = [make_chunk(1, "辛" * 100, page_start=2), make_chunk(2, "壬" * 100, page_start=3)]

    records = validate_and_backfill(
        [KPExtract(title="混合来源", source_chunk_indexes=[1, 999, 2])],
        batch=batch,
        heading_path=[],
        warnings=[],
    )

    assert len(records) == 1
    assert records[0].source_chunk_indexes == [1, 2]
    assert records[0].source_pages == [2, 3]


def test_values_are_clamped_into_range() -> None:
    batch = [make_chunk(1, "癸" * 100)]
    records = validate_and_backfill(
        [
            KPExtract(
                title="异常数值",
                difficulty=99,
                importance=-3,
                confidence=1.8,
                source_chunk_indexes=[1],
            )
        ],
        batch=batch,
        heading_path=[],
        warnings=[],
    )

    assert records[0].difficulty == 5
    assert records[0].importance == 1
    assert records[0].confidence == 1.0


def test_title_and_lists_are_truncated() -> None:
    batch = [make_chunk(1, "子" * 100)]
    records = validate_and_backfill(
        [
            KPExtract(
                title="这是一个明显超过四十个字符上限的超长知识点标题用于验证截断逻辑是否生效",
                key_points=[f"要点{i}" for i in range(9)],
                tags=[f"标签{i}" for i in range(8)],
                source_chunk_indexes=[1],
            )
        ],
        batch=batch,
        heading_path=[],
        warnings=[],
    )

    assert len(records[0].title) <= 40
    assert len(records[0].key_points) <= 4
    assert len(records[0].tags) <= 3


# --------------------------------------------------------------------------- #
# 去重合并
# --------------------------------------------------------------------------- #
def make_record(title: str, **kwargs) -> KPRecord:
    defaults = dict(
        title=title,
        title_norm=normalize_title(title),
        summary="",
        details="",
        key_points=[],
        difficulty=3,
        importance=3,
        confidence=0.8,
        tags=[],
        heading_path=[],
        source_chunk_indexes=[],
        source_pages=[],
    )
    defaults.update(kwargs)
    return KPRecord(**defaults)


def test_merge_identical_normalized_titles() -> None:
    merged = merge_duplicates(
        [
            make_record("进程与线程的区别", details="A", source_chunk_indexes=[1], source_pages=[1]),
            make_record("进程线程区别", details="B", source_chunk_indexes=[2], source_pages=[2]),
        ]
    )

    assert len(merged) == 1
    assert "A" in merged[0].details and "B" in merged[0].details
    assert merged[0].source_chunk_indexes == [1, 2]
    assert merged[0].source_pages == [1, 2]
    assert merged[0].order_index == 0


def test_merge_field_rules() -> None:
    """合并时的取舍：难度/重要度取大，置信度取小，来源取并集。"""
    merged = merge_duplicates(
        [
            make_record("页表", difficulty=2, importance=3, confidence=0.9, source_pages=[1]),
            make_record("页表", difficulty=5, importance=4, confidence=0.6, source_pages=[2]),
        ]
    )

    assert merged[0].difficulty == 5
    assert merged[0].importance == 4
    assert merged[0].confidence == 0.6
    assert merged[0].source_pages == [1, 2]


def test_distinct_titles_are_not_merged() -> None:
    """「进程」与「进程与线程」是不同考点，不能因为包含关系被合并。"""
    merged = merge_duplicates([make_record("进程"), make_record("进程与线程")])
    assert len(merged) == 2
    assert [r.order_index for r in merged] == [0, 1]


# --------------------------------------------------------------------------- #
# 模型输出解析
# --------------------------------------------------------------------------- #
def test_parse_plain_dict() -> None:
    items = parse_response({"knowledge_points": [{"title": "A"}, {"title": "B"}]})
    assert [i.title for i in items] == ["A", "B"]


def test_parse_top_level_list() -> None:
    """有些模型会直接返回数组，要兼容。"""
    items = parse_response([{"title": "A"}])
    assert [i.title for i in items] == ["A"]


def test_parse_skips_malformed_items() -> None:
    items = parse_response(
        {"knowledge_points": [{"title": "有效"}, {"no_title": True}, "字符串", {"title": "也有效"}]}
    )
    assert [i.title for i in items] == ["有效", "也有效"]


def test_parse_missing_key_raises() -> None:
    import pytest

    with pytest.raises(ValueError):
        parse_response({"unexpected": []})


# --------------------------------------------------------------------------- #
# mock 派生
# --------------------------------------------------------------------------- #
def test_mock_payload_is_valid_and_shaped() -> None:
    """mock 模式必须产出结构合法的数据，才能支撑离线回归。"""
    batch = [
        make_chunk(0, "进程是程序的一次执行过程，是资源分配的基本单位。" * 3),
        make_chunk(1, "线程是 CPU 调度的基本单位，共享进程的资源。" * 3),
    ]
    payload = build_mock_payload(batch, ["第一章"])

    items = parse_response(payload)
    assert len(items) == 2
    for item in items:
        assert item.title.startswith("[MOCK]")
        assert item.source_chunk_indexes, "mock 数据也必须带来源，否则测不出回填逻辑"
        assert 1 <= item.difficulty <= 5
        assert 0 <= item.confidence <= 1
