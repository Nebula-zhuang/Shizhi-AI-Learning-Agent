"""知识点路由：列表与详情（含来源原文回链）。

`GET /api/knowledge-points/{id}` 是本项目「可溯源」承诺的兑现点：
它把知识点的 source_chunk_indexes 翻译成真实的原文块返回，
前端点页码就能展开原文，学生可以自己核对 AI 有没有讲错。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.api.deps import (
    current_learner_id,
    require_document,
    require_knowledge_point,
)
from app.db.session import get_db
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.knowledge_point import KnowledgePoint
from app.services import document_service, knowledge_service
from app.schemas.knowledge import (
    KnowledgePointDetail,
    KnowledgePointListResponse,
    KnowledgePointSummary,
    SourceChunk,
)

logger = get_logger(__name__)

#: 文档维度的知识点列表
documents_router = APIRouter(prefix="/documents", tags=["knowledge"])
#: 知识点维度的查询
knowledge_router = APIRouter(prefix="/knowledge-points", tags=["knowledge"])


@documents_router.get(
    "/{document_id}/knowledge-points",
    response_model=KnowledgePointListResponse,
    summary="某文档的知识点列表",
)
def list_knowledge_points(
    document_id: int,
    difficulty: int | None = Query(None, ge=1, le=5, description="按难度筛选"),
    min_importance: int | None = Query(None, ge=1, le=5, description="按重要度下限筛选"),
    order: str = Query("document", pattern="^(document|difficulty|importance)$"),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    learner_id: str = Depends(current_learner_id),
) -> KnowledgePointListResponse:
    """按文档查询知识点。

    默认按文档中的出现顺序返回（`order=document`），这样知识点的排列与阅读顺序一致；
    也可以按难度或重要度排序，便于快速定位重点与难点。
    """
    if document_service.get_document(db, document_id, learner_id=learner_id) is None:
        raise HTTPException(status_code=404, detail=f"文档 {document_id} 不存在。")

    conditions = [KnowledgePoint.document_id == document_id]
    if difficulty is not None:
        conditions.append(KnowledgePoint.difficulty == difficulty)
    if min_importance is not None:
        conditions.append(KnowledgePoint.importance >= min_importance)

    total = db.execute(
        select(func.count()).select_from(KnowledgePoint).where(*conditions)
    ).scalar_one()

    order_by = {
        "document": (KnowledgePoint.order_index.asc(), KnowledgePoint.id.asc()),
        "difficulty": (KnowledgePoint.difficulty.desc(), KnowledgePoint.order_index.asc()),
        "importance": (KnowledgePoint.importance.desc(), KnowledgePoint.order_index.asc()),
    }[order]

    rows = (
        db.execute(
            select(KnowledgePoint)
            .where(*conditions)
            .order_by(*order_by)
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )

    return KnowledgePointListResponse(
        items=[KnowledgePointSummary.model_validate(r) for r in rows], total=int(total)
    )


@knowledge_router.get(
    "/{kp_id}",
    response_model=KnowledgePointDetail,
    summary="知识点详情（含来源原文）",
)
def get_knowledge_point(
    kp_id: int,
    db: Session = Depends(get_db),
    learner_id: str = Depends(current_learner_id),
) -> KnowledgePointDetail:
    """返回知识点详情，并把来源块的真实原文一并带上。"""
    point = require_knowledge_point(db, kp_id, learner_id)
    if point is None:
        raise HTTPException(status_code=404, detail=f"知识点 {kp_id} 不存在。")

    sources: list[SourceChunk] = []
    indexes = list(point.source_chunk_indexes or [])
    if indexes:
        rows = (
            db.execute(
                select(Chunk)
                .where(
                    Chunk.document_id == point.document_id,
                    Chunk.chunk_index.in_(indexes),
                )
                .order_by(Chunk.chunk_index)
            )
            .scalars()
            .all()
        )
        sources = [SourceChunk.model_validate(c) for c in rows]

    detail = KnowledgePointDetail.model_validate(point)
    detail.sources = sources
    return detail
