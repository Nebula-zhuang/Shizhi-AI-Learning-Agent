"""关系构建测试。

**本文件的核心命题：不允许无依据连边。**
所以除了验证"该连的连上了"，更要验证"不该连的没连上"——
噪声边比缺失边更有害，因为它会让用户对整张图失去信任。
"""

from __future__ import annotations

import app.db.base  # noqa: F401 - 触发全部模型注册，否则关系映射无法解析
from app.models.knowledge_point import KnowledgePoint
from app.models.knowledge_relation import RelationSource, RelationType
from app.services.relation_service import (
    CONF_HEADING_PARENT,
    CONF_SAME_SECTION,
    CONF_SHARED_CHUNK_BASE,
    CONF_TITLE_CONTAINS,
    build_candidates,
    rule_contains,
    rule_prerequisite,
    rule_related,
)


def kp(
    kp_id: int,
    title: str,
    *,
    order: int = 0,
    heading: list[str] | None = None,
    chunks: list[int] | None = None,
    difficulty: int = 3,
    importance: int = 3,
) -> KnowledgePoint:
    """构造一个不落库的知识点对象。"""
    return KnowledgePoint(
        id=kp_id,
        document_id=1,
        title=title,
        title_norm=title,
        summary="",
        details="",
        difficulty=difficulty,
        importance=importance,
        heading_path=heading,
        source_chunk_indexes=chunks or [],
        source_pages=[1],
        order_index=order,
    )


def edges(points) -> set[tuple[int, int, str]]:
    cands, _ = build_candidates(points)
    return {(c.from_kp_id, c.to_kp_id, c.relation_type) for c in cands}


# --------------------------------------------------------------------------- #
# contains：章节层级
# --------------------------------------------------------------------------- #
def test_heading_parent_creates_contains() -> None:
    points = [
        kp(1, "第二章 内存管理", order=0, heading=["第二章 内存管理"]),
        kp(2, "2.1 分页", order=1, heading=["第二章 内存管理", "2.1 分页"]),
    ]
    cands = rule_contains(points)
    assert len(cands) == 1
    c = cands[0]
    assert (c.from_kp_id, c.to_kp_id) == (1, 2)
    assert c.relation_type == RelationType.CONTAINS
    assert c.source == RelationSource.HEADING_PARENT
    assert c.confidence == CONF_HEADING_PARENT
    # 依据必须可读且指明具体章节
    assert "第二章 内存管理" in c.evidence


def test_contains_links_closest_ancestor_only() -> None:
    """只连最近祖先，不产生传递冗余边。

    若同时连 第一章→1.1.1 与 1.1→1.1.1，图里每条边都会被重复表达一次，
    节点一多就糊成一团。
    """
    points = [
        kp(1, "第一章", order=0, heading=["第一章"]),
        kp(2, "1.1 节", order=1, heading=["第一章", "1.1 节"]),
        kp(3, "1.1.1 小节", order=2, heading=["第一章", "1.1 节", "1.1.1 小节"]),
    ]
    result = edges(points)
    assert (2, 3, RelationType.CONTAINS) in result  # 最近祖先连上了
    assert (1, 3, RelationType.CONTAINS) not in result  # 隔代祖先没连
    assert (1, 2, RelationType.CONTAINS) in result


def test_sibling_headings_do_not_create_contains() -> None:
    """同级小节之间绝不能出现从属关系 —— 这正是 P1 层级 bug 会造成的错误。"""
    points = [
        kp(1, "3.1 进程的概念", order=0, heading=["3.1 进程的概念"]),
        kp(2, "3.2 进程的状态", order=1, heading=["3.2 进程的状态"]),
        kp(3, "3.3 进程控制块", order=2, heading=["3.3 进程控制块"]),
    ]
    result = edges(points)
    contains = {e for e in result if e[2] == RelationType.CONTAINS}
    assert contains == set(), f"同级小节之间不应有从属边，实际：{contains}"


# --------------------------------------------------------------------------- #
# contains：标题包含兜底
# --------------------------------------------------------------------------- #
def test_title_contains_within_same_section() -> None:
    points = [
        kp(1, "信号量机制", order=0, heading=["3.5 同步"]),
        kp(2, "信号量机制与P、V操作", order=1, heading=["3.5 同步"]),
    ]
    cands = [c for c in rule_contains(points) if c.source == RelationSource.TITLE_CONTAINS]
    assert len(cands) == 1
    assert cands[0].confidence == CONF_TITLE_CONTAINS
    assert cands[0].from_kp_id == 1


def test_title_contains_blocked_across_sections() -> None:
    """跨章节的标题包含很可能是偶合，不该连边。"""
    points = [
        kp(1, "进程", order=0, heading=["3.1 概念"]),
        kp(2, "进程与线程的区别", order=1, heading=["3.4 区别"]),
    ]
    cands = [c for c in rule_contains(points) if c.source == RelationSource.TITLE_CONTAINS]
    assert cands == []


def test_title_contains_ignores_single_char_titles() -> None:
    """单字标题参与包含判定会大面积误连。"""
    points = [
        kp(1, "页", order=0, heading=["3.5 同步"]),
        kp(2, "页表", order=1, heading=["3.5 同步"]),
    ]
    assert rule_contains(points) == []


# --------------------------------------------------------------------------- #
# related
# --------------------------------------------------------------------------- #
def test_shared_chunk_creates_related() -> None:
    points = [
        kp(1, "甲", order=0, chunks=[3, 4]),
        kp(2, "乙", order=1, chunks=[4, 5]),
    ]
    cands = rule_related(points)
    shared = [c for c in cands if c.source == RelationSource.SHARED_CHUNK]
    assert len(shared) == 1
    assert shared[0].confidence == CONF_SHARED_CHUNK_BASE
    assert shared[0].detail["shared_chunk_indexes"] == [4]
    assert "4" in shared[0].evidence


def test_shared_chunk_confidence_grows_with_overlap() -> None:
    points = [
        kp(1, "甲", order=0, chunks=[1, 2, 3]),
        kp(2, "乙", order=1, chunks=[1, 2, 3]),
    ]
    shared = [c for c in rule_related(points) if c.source == RelationSource.SHARED_CHUNK]
    assert shared[0].confidence > CONF_SHARED_CHUNK_BASE
    assert shared[0].confidence <= 0.85


def test_related_is_direction_normalized() -> None:
    """无向关系方向归一化，避免图里出现 A→B 与 B→A 两条重影边。"""
    points = [
        kp(9, "后", order=1, chunks=[7]),
        kp(3, "前", order=0, chunks=[7]),
    ]
    shared = [c for c in rule_related(points) if c.source == RelationSource.SHARED_CHUNK]
    assert shared[0].from_kp_id == 3 and shared[0].to_kp_id == 9


def test_no_shared_chunk_no_related() -> None:
    points = [
        kp(1, "甲", order=0, chunks=[1]),
        kp(2, "乙", order=1, chunks=[2]),
    ]
    assert [c for c in rule_related(points) if c.source == RelationSource.SHARED_CHUNK] == []


def test_same_section_links_adjacent_only() -> None:
    """同小节内用链式相邻而非全连接：4 个节点应只产生 3 条边而不是 6 条。"""
    points = [kp(i, f"考点{i}", order=i, heading=["同一节"]) for i in range(1, 5)]
    same = [c for c in rule_related(points) if c.source == RelationSource.SAME_SECTION]
    assert len(same) == 3
    assert all(c.confidence == CONF_SAME_SECTION for c in same)
    for c in same:
        assert abs(c.from_kp_id - c.to_kp_id) == 1


# --------------------------------------------------------------------------- #
# prerequisite
# --------------------------------------------------------------------------- #
def test_prerequisite_links_adjacent_sections() -> None:
    points = [
        kp(1, "3.1 概念", order=0, heading=["3.1 概念"]),
        kp(2, "3.2 状态", order=1, heading=["3.2 状态"]),
        kp(3, "3.3 控制块", order=2, heading=["3.3 控制块"]),
    ]
    cands = rule_prerequisite(points)
    pairs = [(c.from_kp_id, c.to_kp_id) for c in cands]
    assert (1, 2) in pairs
    assert (2, 3) in pairs
    # 只连相邻小节，不产生 3.1 → 3.3
    assert (1, 3) not in pairs
    assert cands[0].source == RelationSource.ORDER_HEURISTIC


def test_prerequisite_blocked_across_top_chapters() -> None:
    """跨顶层章节不推前置关系 —— 第三章不该是第四章的前置。"""
    points = [
        kp(1, "3.1 概念", order=0, heading=["第三章 进程", "3.1 概念"]),
        kp(2, "4.1 分页", order=1, heading=["第四章 内存", "4.1 分页"]),
    ]
    assert rule_prerequisite(points) == []


def test_prerequisite_needs_heading_info() -> None:
    """没有章节信息就不推前置，宁可不连。"""
    points = [
        kp(1, "甲", order=0, heading=None),
        kp(2, "乙", order=1, heading=None),
    ]
    assert rule_prerequisite(points) == []


# --------------------------------------------------------------------------- #
# 汇总与去重
# --------------------------------------------------------------------------- #
def test_one_edge_per_pair_with_priority() -> None:
    """一对节点同时命中多条规则时，只留优先级最高的一条。

    这里 1→2 同时满足 contains（章节）与 related（共享块），应保留 contains。
    """
    points = [
        kp(1, "第二章", order=0, heading=["第二章"], chunks=[5]),
        kp(2, "2.1 分页", order=1, heading=["第二章", "2.1 分页"], chunks=[5]),
    ]
    result = edges(points)
    for a, b, _t in result:
        if (a, b) == (1, 2):
            break
    assert (1, 2, RelationType.CONTAINS) in result
    assert (1, 2, RelationType.RELATED) not in result


def test_no_self_loops() -> None:
    points = [
        kp(1, "甲", order=0, heading=["第二章"], chunks=[1, 2]),
        kp(2, "乙", order=1, heading=["第二章"], chunks=[1, 2]),
    ]
    cands, _ = build_candidates(points)
    assert all(c.from_kp_id != c.to_kp_id for c in cands)


def test_empty_and_single_point_documents() -> None:
    assert build_candidates([])[0] == []
    assert build_candidates([kp(1, "唯一", order=0)])[0] == []


def test_confidence_threshold_filters_noise() -> None:
    """阈值调高时低置信度的边被过滤掉。"""
    points = [
        kp(1, "3.1 概念", order=0, heading=["3.1 概念"]),
        kp(2, "3.2 状态", order=1, heading=["3.2 状态"]),
    ]
    strict, stats = build_candidates(points, min_confidence=0.9)
    # prerequisite 只有 0.50，应被全部过滤
    assert strict == []
    assert stats.skipped_low_confidence > 0


def test_every_candidate_has_source_and_evidence() -> None:
    """统括性检查：任何一条产出边都必须同时具备规则来源与可读依据。"""
    points = [
        kp(1, "第二章 内存管理", order=0, heading=["第二章 内存管理"], chunks=[1]),
        kp(2, "2.1 分页", order=1, heading=["第二章 内存管理", "2.1 分页"], chunks=[1]),
        kp(3, "2.2 分段", order=2, heading=["第二章 内存管理", "2.2 分段"], chunks=[2]),
        kp(4, "页表", order=3, heading=["第二章 内存管理", "2.1 分页"], chunks=[1]),
    ]
    cands, _ = build_candidates(points)
    assert cands, "这组数据应当能构建出关系"
    for c in cands:
        assert c.source, f"{c} 缺少构建依据来源"
        assert c.evidence and len(c.evidence) > 4, f"{c} 缺少可读依据"
        assert 0 < c.confidence <= 1
        assert c.relation_type in {RelationType.CONTAINS, RelationType.RELATED, RelationType.PREREQUISITE}
