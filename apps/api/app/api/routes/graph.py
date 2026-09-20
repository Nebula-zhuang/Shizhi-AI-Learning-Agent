"""P2 路由：知识图谱、关系构建、可信度校验、校验收据。

全部是**新增路径**，P1 的 14 个路径一个都没有改动。
三个 router 按资源前缀拆分，与 P1 的 `documents.py` / `knowledge.py` 保持一致的写法。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.api.deps import (
    current_learner_id,
    require_document,
    require_knowledge_point,
)
from app.db.session import get_db
from app.models.knowledge_check import KnowledgeCheck
from app.models.knowledge_point import KnowledgePoint
from app.models.knowledge_relation import INVERSE_RELATION_TYPE
from app.schemas.graph import (
    CheckGroup,
    CheckItem,
    GraphEdge,
    GraphNode,
    GraphResponse,
    GraphStats,
    KnowledgeCheckResponse,
    RelationBuildResponse,
    VerifyCapabilities,
    VerifyStartResponse,
    VerifyStatusResponse,
)
from app.services import document_service, relation_service, verify_runner, verify_service

logger = get_logger(__name__)

documents_router = APIRouter(prefix="/documents", tags=["graph"])
knowledge_router = APIRouter(prefix="/knowledge-points", tags=["verification"])
verify_router = APIRouter(prefix="/verify", tags=["verification"])

#: 按校验层固定顺序输出，前端不必自己排序
_CHECK_TYPE_ORDER = ("rule", "model", "web")


def _get_document_or_404(db: Session, document_id: int, learner_id: str):
    """取资料并校验归属。不属于当前账号的一律按"不存在"处理。"""
    return require_document(db, document_id, learner_id)


# --------------------------------------------------------------------------- #
# 知识图谱
# --------------------------------------------------------------------------- #
@documents_router.get(
    "/{document_id}/graph", response_model=GraphResponse, summary="知识图谱（节点 + 关系）"
)
def get_graph(
    document_id: int,
    db: Session = Depends(get_db),
    learner_id: str = Depends(current_learner_id),
) -> GraphResponse:
    """返回该文档的知识点与它们之间的关系。

    一次性返回全量节点与边 —— 不做分页、不做增量同步。
    单份学习资料的知识点规模在几十到一两百之间，一次拉完最简单也最快；
    引入图数据库或增量协议属于为不存在的规模问题付出复杂度。
    """
    document = _get_document_or_404(db, document_id, learner_id)

    points = relation_service.load_points(db, document_id)
    relations = relation_service.list_relations(db, document_id)

    nodes = [
        GraphNode(
            id=p.id,
            title=p.title,
            summary=p.summary or "",
            difficulty=p.difficulty,
            importance=p.importance,
            confidence=float(p.confidence or 0),
            verify_status=p.verify_status,
            heading_path=p.heading_path,
            source_pages=p.source_pages,
            order_index=p.order_index,
            depth=len(p.heading_path or []) or 1,
        )
        for p in points
    ]

    edges = [
        GraphEdge(
            id=r.id,
            from_kp_id=r.from_kp_id,
            to_kp_id=r.to_kp_id,
            relation_type=r.relation_type,
            inverse_type=INVERSE_RELATION_TYPE.get(r.relation_type, r.relation_type),
            confidence=float(r.confidence or 0),
            source=r.source,
            evidence=r.evidence or "",
        )
        for r in relations
    ]

    by_type: dict[str, int] = {}
    for e in edges:
        by_type[e.relation_type] = by_type.get(e.relation_type, 0) + 1

    connected = {e.from_kp_id for e in edges} | {e.to_kp_id for e in edges}
    max_depth = max((n.depth for n in nodes), default=1)

    return GraphResponse(
        document_id=document_id,
        document_name=document.file_name,
        nodes=nodes,
        edges=edges,
        stats=GraphStats(
            node_count=len(nodes),
            edge_count=len(edges),
            by_type=by_type,
            max_depth=max_depth,
            isolated_nodes=sum(1 for n in nodes if n.id not in connected),
        ),
    )


@documents_router.post(
    "/{document_id}/relations",
    response_model=RelationBuildResponse,
    summary="构建/重建知识点关系",
)
def build_relations(
    document_id: int,
    db: Session = Depends(get_db),
    learner_id: str = Depends(current_learner_id),
) -> RelationBuildResponse:
    """基于章节层级、来源块重叠与文档顺序重新构建关系。

    **同步执行**：规则是确定性的本地计算，几十个知识点的建边在毫秒级完成，
    没有必要为它引入异步任务与进度轮询。

    幂等：每次调用先清空旧边再重建，反复点击结果一致。
    """
    document = _get_document_or_404(db, document_id, learner_id)

    point_count = len(relation_service.load_points(db, document_id))
    if point_count < 2:
        return RelationBuildResponse(
            ok=True,
            document_id=document_id,
            message=f"仅 {point_count} 个知识点，构不成关系，已清空旧边。",
            stats={"created": 0, "removed": 0},
        )

    stats = relation_service.rebuild_relations(db, document_id)
    detail = stats.as_dict()
    type_text = "、".join(f"{k} {v} 条" for k, v in sorted(detail["by_type"].items())) or "无"
    return RelationBuildResponse(
        ok=True,
        document_id=document_id,
        message=(
            f"已基于 {point_count} 个知识点构建 {detail['created']} 条关系"
            f"（{type_text}），清理旧边 {detail['removed']} 条。"
        ),
        stats=detail,
    )


# --------------------------------------------------------------------------- #
# 可信度校验
# --------------------------------------------------------------------------- #
@verify_router.get(
    "/capabilities", response_model=VerifyCapabilities, summary="校验能力探测"
)
def verify_capabilities() -> VerifyCapabilities:
    """告诉前端当前具备哪些校验能力。

    存在的意义：让界面能区分「未配置 Tavily Key（该去配）」与
    「配置了但调用失败（该重试）」—— 这两种状态对用户的下一步动作完全不同。
    """
    return VerifyCapabilities(
        verify_enabled=settings.verify_enabled,
        web_verify_enabled=settings.verify_web_enabled,
        web_provider="tavily",
        tavily_configured=settings.is_tavily_configured,
        web_verify_effective=settings.effective_web_verify,
        model_importance_threshold=settings.verify_model_importance_threshold,
        web_max_per_document=settings.verify_web_max_per_document,
    )


@documents_router.post(
    "/{document_id}/verify",
    status_code=202,
    response_model=VerifyStartResponse,
    summary="触发可信度校验（异步）",
)
async def start_verify(
    document_id: int,
    db: Session = Depends(get_db),
    learner_id: str = Depends(current_learner_id),
) -> VerifyStartResponse:
    """投递后台校验任务。

    必须是 `async def` —— `verify_runner.submit` 内部用 `asyncio.create_task`，
    而 FastAPI 会把同步路由放进线程池执行，那里没有运行中的事件循环。
    （P1 在 reprocess 接口上踩过这个坑，不再重复。）
    """
    if not settings.verify_enabled:
        raise HTTPException(
            status_code=409,
            detail="可信度校验已被配置关闭（VERIFY_ENABLED=false）。",
        )

    document = _get_document_or_404(db, document_id, learner_id)

    if document.kp_count <= 0:
        raise HTTPException(
            status_code=409, detail="该文档还没有知识点，请先完成资料解析与抽取。"
        )

    if verify_runner.is_running(document_id):
        raise HTTPException(status_code=409, detail="该文档正在校验中，请等待当前校验结束。")

    if not verify_runner.submit(document_id, page_count=document.page_count):
        raise HTTPException(status_code=503, detail="服务当前无法接收校验任务，请稍后重试。")

    web_hint = (
        "含联网核验"
        if settings.effective_web_verify
        else "未配置 TAVILY_API_KEY，本次仅做规则与模型自评"
    )
    return VerifyStartResponse(
        document_id=document_id,
        accepted=True,
        message=f"已投递校验任务（{web_hint}）。请轮询 /verify/status 获取进度。",
    )


@documents_router.get(
    "/{document_id}/verify/status",
    response_model=VerifyStatusResponse,
    summary="校验进度",
)
def get_verify_status(
    document_id: int,
    db: Session = Depends(get_db),
    learner_id: str = Depends(current_learner_id),
) -> VerifyStatusResponse:
    """进度来自内存态，已校验数量来自数据库统计。

    这个组合的好处：进程重启后进度条会归零，但「已校验 N 个」依然准确 ——
    因为校验结果是逐知识点落库的。界面不会出现"什么都没有"的空窗。
    """
    document = _get_document_or_404(db, document_id, learner_id)
    state = verify_runner.get_state(document_id)
    breakdown = verify_service.check_status_breakdown(db, document_id)
    checked = sum(count for status, count in breakdown.items() if status != "unverified")

    total = document.kp_count or 0
    state_name = state.get("state") or ("idle" if checked == 0 else "done")
    progress = int(state.get("progress") or 0)
    if state_name == "idle" and checked:
        progress = 100

    return VerifyStatusResponse(
        document_id=document_id,
        state=state_name,
        progress=progress,
        detail=state.get("detail") or ("尚未校验" if checked == 0 else "已完成校验"),
        total_points=total,
        checked_points=checked,
        by_status=breakdown,
        stats=state.get("stats") or {},
    )


@knowledge_router.get(
    "/{kp_id}/checks",
    response_model=KnowledgeCheckResponse,
    summary="知识点的校验收据（规则 / 模型 / 联网 三层分开）",
)
def get_knowledge_checks(
    kp_id: int,
    db: Session = Depends(get_db),
    learner_id: str = Depends(current_learner_id),
) -> KnowledgeCheckResponse:
    """返回某个知识点的全部校验记录。

    **三层分开返回，不合并成"综合结论"** —— 用户需要能分辨
    「程序按规则查的」「模型自己说的」「网上查到的」，
    三者的可信度与失效方式完全不同。每组包含最新一条与完整历史。

    同时暴露 P1 的抽取结果（title / verify_status）供对照：
    **P2 从不修改抽取内容**，校验发现的问题只以记录形式并存。
    """
    point = require_knowledge_point(db, kp_id, learner_id)
    if point is None:
        raise HTTPException(status_code=404, detail=f"知识点 {kp_id} 不存在。")

    rows = verify_service.list_checks(db, kp_id)

    grouped: dict[str, list[KnowledgeCheck]] = {t: [] for t in _CHECK_TYPE_ORDER}
    for row in rows:
        grouped.setdefault(str(row.check_type), []).append(row)

    groups: list[CheckGroup] = []
    for check_type in _CHECK_TYPE_ORDER:
        items = grouped.get(check_type) or []
        items.sort(key=lambda r: r.id, reverse=True)
        groups.append(
            CheckGroup(
                check_type=check_type,
                latest=_to_item(items[0]) if items else None,
                history=[_to_item(r) for r in items],
                count=len(items),
            )
        )
    # 兜底：万一将来新增了未登记的校验层，也不能吞掉
    for check_type, items in grouped.items():
        if check_type in _CHECK_TYPE_ORDER or not items:
            continue
        items.sort(key=lambda r: r.id, reverse=True)
        groups.append(
            CheckGroup(
                check_type=check_type,
                latest=_to_item(items[0]),
                history=[_to_item(r) for r in items],
                count=len(items),
            )
        )

    return KnowledgeCheckResponse(
        kp_id=point.id,
        title=point.title,
        verify_status=point.verify_status,
        groups=groups,
        total=len(rows),
    )


def _to_item(row: KnowledgeCheck) -> CheckItem:
    return CheckItem(
        id=row.id,
        check_type=row.check_type,
        verdict=row.verdict,
        confidence=float(row.confidence or 0),
        reason=row.reason or "",
        evidence=row.evidence,
        source_urls=row.source_urls,
        engine=row.engine or "",
        created_at=row.created_at,
    )


# --------------------------------------------------------------------------- #
# 关系明细（便于从图谱外的入口查看"这个知识点和谁有关系"）
# --------------------------------------------------------------------------- #
@knowledge_router.get("/{kp_id}/relations", summary="某个知识点参与的全部关系")
def get_point_relations(
    kp_id: int,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    learner_id: str = Depends(current_learner_id),
) -> dict:
    """以当前知识点为视角列出关系，边带反向标签。

    与图谱接口的区别：图谱是给可视化用的（一次拉全量），
    这里是给详情面板用的（只看一个节点的邻居）。
    """
    point = require_knowledge_point(db, kp_id, learner_id)
    if point is None:
        raise HTTPException(status_code=404, detail=f"知识点 {kp_id} 不存在。")

    relations = relation_service.list_relations(db, point.document_id)
    involved = [r for r in relations if r.from_kp_id == kp_id or r.to_kp_id == kp_id][:limit]

    titles = {
        p.id: p.title
        for p in relation_service.load_points(db, point.document_id)
    }

    items = []
    for r in involved:
        outgoing = r.from_kp_id == kp_id
        items.append(
            {
                "id": r.id,
                # 以当前节点为起点表述，前端不必自己判断方向
                "direction": "out" if outgoing else "in",
                "relation_type": (
                    r.relation_type
                    if outgoing
                    else INVERSE_RELATION_TYPE.get(r.relation_type, r.relation_type)
                ),
                "peer_id": r.to_kp_id if outgoing else r.from_kp_id,
                "peer_title": titles.get(r.to_kp_id if outgoing else r.from_kp_id, ""),
                "confidence": float(r.confidence or 0),
                "source": r.source,
                "evidence": r.evidence or "",
            }
        )

    return {"kp_id": kp_id, "title": point.title, "total": len(items), "items": items}
