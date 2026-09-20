"""知识点关系构建。

**本模块最重要的约束：不允许随机连边。**
每一条产出都必须能回答「你凭什么认为这两个知识点有关系」——
所以内部数据结构 `RelationCandidate` 把 `source`（用了哪条规则）和
`evidence`（人类可读的证明）都做成了必填字段，数据库层面也是非空列。

五条规则（完整理由见 docs/05-P2-实施方案.md §4）：

| # | 关系 | 依据 | 置信度 |
|---|---|---|---|
| 1 | contains | B 的 heading_path 的最长严格前缀恰为 A 的 heading_path | 0.90 |
| 2 | contains | 无章节层级时，A 的标题是 B 标题的真子串且更短 | 0.70 |
| 3 | related | 共享来源块（同一段原文抽出） | 0.60–0.85 |
| 4 | related | 同一小节内 order_index 相邻 | 0.55 |
| 5 | prerequisite | 同父章节下、相邻小节，早者 → 晚者 | 0.50 |

两条关键的**取舍**：

  a) **`contains` 只连最近的祖先**。若 3 与 3.1 都是 3.1.1 的前缀，只连 3.1→3.1.1。
     否则每一层祖先都会连过来，图里出现大量传递冗余边。

  b) **同层内用「链式相邻」而不是「全连接」**。一个 8 个考点的小节若两两相连是 28 条边，
     图会被糊成一团。相邻成链既表达了"它们属于同一块知识"，又保持可读。
     这与"相邻 ≠ 前置"是两回事 —— 相邻只推出 related，不推出 prerequisite。

**幂等**：重建时先清空该文档的旧边，与 P1 的 `purge_derived` 思路一致。
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Sequence

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.models.knowledge_point import KnowledgePoint
from app.models.knowledge_relation import (
    RelationSource,
    RelationType,
    STORABLE_RELATION_TYPES,
    KnowledgeRelation,
)

logger = get_logger(__name__)

# --------------------------------------------------------------------------- #
# 置信度常量。集中在此，便于调参与测试断言。
# --------------------------------------------------------------------------- #
CONF_HEADING_PARENT = 0.90
CONF_TITLE_CONTAINS = 0.70
CONF_SHARED_CHUNK_BASE = 0.60
CONF_SHARED_CHUNK_STEP = 0.10
CONF_SHARED_CHUNK_CAP = 0.85
CONF_SAME_SECTION = 0.55
CONF_ORDER_HEURISTIC = 0.50

#: 优先级：同一个无序对命中多条规则时，只保留优先级最高的一条。
#: 数值越小优先级越高。避免同一对节点之间出现多条平行边把图弄乱。
_PRIORITY: dict[str, int] = {
    RelationType.CONTAINS: 1,
    RelationType.PREREQUISITE: 2,
    RelationType.RELATED: 3,
}

#: 归一化标题参与包含判定时的最小长度，避免「页」这类单字标题乱连
_MIN_TITLE_FOR_CONTAINS = 2


@dataclass
class RelationCandidate:
    """一条候选关系。source 与 evidence 必填 —— 这是"有依据"的强制表达。"""

    from_kp_id: int
    to_kp_id: int
    relation_type: str
    source: str
    evidence: str
    confidence: float
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[int, int]:
        """用于「每对节点只保留一条边」的去重键（无向视角）。"""
        return (min(self.from_kp_id, self.to_kp_id), max(self.from_kp_id, self.to_kp_id))


@dataclass
class BuildStats:
    """构建统计，供接口返回与日志使用。"""

    candidates: int = 0
    created: int = 0
    removed: int = 0
    skipped_low_confidence: int = 0
    skipped_duplicate: int = 0
    by_type: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidates": self.candidates,
            "created": self.created,
            "removed": self.removed,
            "skipped_low_confidence": self.skipped_low_confidence,
            "skipped_duplicate": self.skipped_duplicate,
            "by_type": self.by_type,
        }


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _hp(point: KnowledgePoint) -> list[str]:
    """取 heading_path，统一成 list[str]（JSON 列可能为 None）。"""
    raw = point.heading_path or []
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw if str(x).strip()]


def _prefix_len(shorter: list[str], longer: list[str]) -> int:
    """shorter 作为 longer 前缀的匹配长度。不匹配或长度不严格小于则为 0。"""
    if not shorter or len(shorter) >= len(longer):
        return 0
    if longer[: len(shorter)] == shorter:
        return len(shorter)
    return 0


def _section_label(hp: list[str]) -> str:
    return " / ".join(hp) if hp else "（未标注章节）"


#: 章节标题开头的编号，如「3.1 进程的概念」的 3.1
_LEADING_NUMBER = re.compile(r"^\s*(\d+(?:\.\d+)*)")


def _section_key(hp: list[str]) -> str:
    """推出该章节「所属父章节」的键，用于判断两节是否同属一章。

    优先看最深一级标题的数字编号，取它的父级编号：
        「3.1 进程的概念」 -> 3
        「3.2.1 页表」     -> 3.2

    为什么不用 `heading_path[:-1]` 直接当键：那样只能处理「路径里已经含有父级」的情况。
    而更常见的是每节自成一级（`['3.1 …']`、`['3.2 …']`），此时 `[:-1]` 全为空，
    前置关系永远不会触发。编号是标题里本就携带的层级信息，用它更可靠。

    拿不到编号时（如「第二章 内存管理」）退化为父路径；再拿不到就返回空串，
    表示"无法判断"，调用方据此不连边 —— 宁可不连，也不乱连。
    """
    if not hp:
        return ""
    matched = _LEADING_NUMBER.match(hp[-1])
    if matched:
        parts = matched.group(1).split(".")
        return ".".join(parts[:-1]) if len(parts) > 1 else parts[0]
    return " / ".join(hp[:-1])


def _normalize_pair(a: int, b: int) -> tuple[int, int]:
    """无向关系方向归一化，避免 A→B 与 B→A 同时存在。"""
    return (a, b) if a <= b else (b, a)


def _chunk_set(point: KnowledgePoint) -> set[int]:
    raw = point.source_chunk_indexes or []
    if not isinstance(raw, list):
        return set()
    out: set[int] = set()
    for item in raw:
        try:
            out.add(int(item))
        except (TypeError, ValueError):
            continue
    return out


# --------------------------------------------------------------------------- #
# 规则 1 & 2：contains
# --------------------------------------------------------------------------- #
def rule_contains(points: Sequence[KnowledgePoint]) -> list[RelationCandidate]:
    """从属关系：章节层级优先，标题包含兜底。"""
    out: list[RelationCandidate] = []

    # ---------------- 规则 1：只连最近的祖先 ----------------
    by_hp: dict[tuple[str, ...], KnowledgePoint] = {}
    for p in points:
        hp = _hp(p)
        if hp:
            # 同一 heading_path 可能有多个考点，取 order_index 最小的那个作为该节的代表
            key = tuple(hp)
            current = by_hp.get(key)
            if current is None or p.order_index < current.order_index:
                by_hp[key] = p

    for p in points:
        hp = _hp(p)
        if not hp:
            continue
        # 在所有已登记的章节里，找出「是当前章节的严格前缀」且长度最长的那一个
        best: tuple[int, KnowledgePoint] | None = None
        for key, candidate in by_hp.items():
            if candidate.id == p.id:
                continue
            matched = _prefix_len(list(key), hp)
            if matched and (best is None or matched > best[0]):
                best = (matched, candidate)
        if best is None:
            continue
        _, parent = best
        out.append(
            RelationCandidate(
                from_kp_id=parent.id,
                to_kp_id=p.id,
                relation_type=RelationType.CONTAINS,
                source=RelationSource.HEADING_PARENT,
                evidence=(
                    f"「{_section_label(_hp(parent))}」是「{_section_label(hp)}」的上级章节"
                ),
                confidence=CONF_HEADING_PARENT,
                detail={"parent_heading": _hp(parent), "child_heading": hp},
            )
        )

    # ---------------- 规则 2：标题包含（无层级信息时兜底） ----------------
    linked = {c.key for c in out}
    for a in points:
        for b in points:
            if a.id == b.id:
                continue
            ta, tb = (a.title or "").strip(), (b.title or "").strip()
            if len(ta) < _MIN_TITLE_FOR_CONTAINS or len(ta) >= len(tb):
                continue
            if ta not in tb:
                continue
            hpa, hpb = _hp(a), _hp(b)
            # 章节不同时标题包含很可能是跨章节的偶合，要求同节或都无章节信息
            if hpa != hpb:
                continue
            if _normalize_pair(a.id, b.id) in linked:
                continue
            out.append(
                RelationCandidate(
                    from_kp_id=a.id,
                    to_kp_id=b.id,
                    relation_type=RelationType.CONTAINS,
                    source=RelationSource.TITLE_CONTAINS,
                    evidence=f"标题「{ta}」被「{tb}」包含",
                    confidence=CONF_TITLE_CONTAINS,
                    detail={"child_title": tb},
                )
            )
    return out


# --------------------------------------------------------------------------- #
# 规则 3 & 4：related
# --------------------------------------------------------------------------- #
def rule_related(points: Sequence[KnowledgePoint]) -> list[RelationCandidate]:
    """相关关系：共享来源块，或同一小节内相邻。"""
    out: list[RelationCandidate] = []

    # ---------------- 规则 3：共享来源块 ----------------
    for i, a in enumerate(points):
        chunks_a = _chunk_set(a)
        if not chunks_a:
            continue
        for b in points[i + 1 :]:
            shared = chunks_a & _chunk_set(b)
            if not shared:
                continue
            lo, hi = _normalize_pair(a.id, b.id)
            confidence = min(
                CONF_SHARED_CHUNK_BASE + CONF_SHARED_CHUNK_STEP * (len(shared) - 1),
                CONF_SHARED_CHUNK_CAP,
            )
            out.append(
                RelationCandidate(
                    from_kp_id=lo,
                    to_kp_id=hi,
                    relation_type=RelationType.RELATED,
                    source=RelationSource.SHARED_CHUNK,
                    evidence=f"同出自原文块 {sorted(shared)}",
                    confidence=round(confidence, 2),
                    detail={"shared_chunk_indexes": sorted(shared)},
                )
            )

    # ---------------- 规则 4：同一小节内相邻 ----------------
    sections: dict[tuple[str, ...], list[KnowledgePoint]] = defaultdict(list)
    for p in points:
        hp = _hp(p)
        if hp:
            sections[tuple(hp)].append(p)

    for hp, members in sections.items():
        ordered = sorted(members, key=lambda x: (x.order_index, x.id))
        for left, right in zip(ordered, ordered[1:]):
            lo, hi = _normalize_pair(left.id, right.id)
            out.append(
                RelationCandidate(
                    from_kp_id=lo,
                    to_kp_id=hi,
                    relation_type=RelationType.RELATED,
                    source=RelationSource.SAME_SECTION,
                    evidence=f"同在「{_section_label(list(hp))}」小节下且顺序相邻",
                    confidence=CONF_SAME_SECTION,
                    detail={"section": list(hp)},
                )
            )
    return out


# --------------------------------------------------------------------------- #
# 规则 5：prerequisite
# --------------------------------------------------------------------------- #
def rule_prerequisite(points: Sequence[KnowledgePoint]) -> list[RelationCandidate]:
    """前置关系：同父章节下、按文档顺序相邻的两个小节，早者 → 晚者。

    只连相邻小节是刻意的：跨全部小节两两相连会产生 n² 条边，
    而"第一章教的东西是第五章的前置"这种推论太弱，不足以支撑一条边。
    """
    out: list[RelationCandidate] = []

    # 每个小节取 order_index 最小的考点作为该节代表，并按文档顺序排名
    section_first: dict[tuple[str, ...], KnowledgePoint] = {}
    for p in points:
        hp = _hp(p)
        if not hp:
            continue
        key = tuple(hp)
        current = section_first.get(key)
        if current is None or p.order_index < current.order_index:
            section_first[key] = p

    ordered_sections = sorted(
        section_first.items(), key=lambda kv: (kv[1].order_index, kv[1].id)
    )

    for (hp_a, rep_a), (hp_b, rep_b) in zip(ordered_sections, ordered_sections[1:]):
        # 必须有共同的父章节，跨章不推前置
        key_a, key_b = _section_key(list(hp_a)), _section_key(list(hp_b))
        if not key_a or key_a != key_b:
            continue
        # 前缀关系（父节 → 子节）已由 contains 覆盖，交给优先级去重
        out.append(
            RelationCandidate(
                from_kp_id=rep_a.id,
                to_kp_id=rep_b.id,
                relation_type=RelationType.PREREQUISITE,
                source=RelationSource.ORDER_HEURISTIC,
                evidence=(
                    f"「{_section_label(list(hp_a))}」在「{_section_label(list(hp_b))}」"
                    f"之前，且同属第 {key_a} 章"
                ),
                confidence=CONF_ORDER_HEURISTIC,
                detail={"from_section": list(hp_a), "to_section": list(hp_b), "chapter": key_a},
            )
        )
    return out


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
def build_candidates(
    points: Sequence[KnowledgePoint], *, min_confidence: float | None = None
) -> tuple[list[RelationCandidate], BuildStats]:
    """跑全部规则并按优先级去重、按置信度过滤。纯函数，不碰数据库。"""
    threshold = (
        min_confidence if min_confidence is not None else settings.relation_min_confidence
    )
    stats = BuildStats()

    raw: list[RelationCandidate] = []
    raw.extend(rule_contains(points))
    raw.extend(rule_related(points))
    raw.extend(rule_prerequisite(points))
    stats.candidates = len(raw)

    # ---------------------------------------------- 自环与非法类型过滤
    valid = [
        c
        for c in raw
        if c.from_kp_id != c.to_kp_id and c.relation_type in STORABLE_RELATION_TYPES
    ]

    # ---------------------------------------------- 置信度过滤
    kept: list[RelationCandidate] = []
    for c in valid:
        if c.confidence < threshold:
            stats.skipped_low_confidence += 1
            continue
        kept.append(c)

    # ---------------------------------------------- 每对节点只保留一条边
    best: dict[tuple[int, int], RelationCandidate] = {}
    for c in kept:
        existing = best.get(c.key)
        if existing is None:
            best[c.key] = c
            continue
        stats.skipped_duplicate += 1
        if _PRIORITY.get(c.relation_type, 99) < _PRIORITY.get(existing.relation_type, 99):
            best[c.key] = c
        elif (
            _PRIORITY.get(c.relation_type, 99) == _PRIORITY.get(existing.relation_type, 99)
            and c.confidence > existing.confidence
        ):
            best[c.key] = c

    result = sorted(best.values(), key=lambda c: (c.from_kp_id, c.to_kp_id))
    stats.by_type = {}
    for c in result:
        stats.by_type[c.relation_type] = stats.by_type.get(c.relation_type, 0) + 1
    return result, stats


def load_points(db: Session, document_id: int) -> list[KnowledgePoint]:
    return list(
        db.execute(
            select(KnowledgePoint)
            .where(KnowledgePoint.document_id == document_id)
            .order_by(KnowledgePoint.order_index, KnowledgePoint.id)
        )
        .scalars()
        .all()
    )


def rebuild_relations(db: Session, document_id: int) -> BuildStats:
    """清空该文档的旧边并重建。幂等 —— 反复调用结果一致。"""
    points = load_points(db, document_id)
    stats = BuildStats()

    removed = db.execute(
        delete(KnowledgeRelation).where(KnowledgeRelation.document_id == document_id)
    ).rowcount
    stats.removed = int(removed or 0)

    if len(points) < 2:
        db.commit()
        logger.info("文档 id=%s 知识点不足 2 个，跳过关系构建", document_id)
        return stats

    candidates, stats = build_candidates(points)

    for c in candidates:
        db.add(
            KnowledgeRelation(
                document_id=document_id,
                from_kp_id=c.from_kp_id,
                to_kp_id=c.to_kp_id,
                relation_type=c.relation_type,
                confidence=Decimal(str(c.confidence)),
                source=c.source,
                evidence=c.evidence,
                detail=c.detail or None,
            )
        )
    db.commit()

    stats.created = len(candidates)
    logger.info(
        "文档 id=%s 关系构建完成：%d 个知识点 -> %d 条边 %s（清理旧边 %d 条）",
        document_id,
        len(points),
        stats.created,
        stats.by_type,
        stats.removed,
    )
    return stats


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
def list_relations(db: Session, document_id: int) -> list[KnowledgeRelation]:
    return list(
        db.execute(
            select(KnowledgeRelation)
            .where(KnowledgeRelation.document_id == document_id)
            .order_by(KnowledgeRelation.from_kp_id, KnowledgeRelation.to_kp_id)
        )
        .scalars()
        .all()
    )


def count_relations(db: Session, document_id: int) -> int:
    from sqlalchemy import func

    return int(
        db.execute(
            select(func.count())
            .select_from(KnowledgeRelation)
            .where(KnowledgeRelation.document_id == document_id)
        ).scalar_one()
    )
