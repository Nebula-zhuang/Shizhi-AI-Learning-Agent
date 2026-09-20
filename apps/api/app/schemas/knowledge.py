"""知识点相关的响应模型。

`KnowledgePointDetail.sources` 是本项目的核心字段：它把知识点与原文块连起来，
前端点页码就能展开原文核对 —— 这就是「可溯源」的落地形态，
也是 P3 做引用回链、P4 讲解时引用原文的同一份数据。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SourceChunk(BaseModel):
    """知识点的来源原文块。"""

    model_config = ConfigDict(from_attributes=True)

    chunk_index: int
    page_start: int
    page_end: int
    block_type: str
    content: str


class KnowledgePointSummary(BaseModel):
    """列表用的知识点摘要。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    document_id: int
    title: str
    summary: str
    difficulty: int
    importance: int
    confidence: float
    verify_status: str
    tags: list[str] | None = None
    heading_path: list[str] | None = None
    source_pages: list[int] | None = None
    order_index: int


class KnowledgePointDetail(KnowledgePointSummary):
    """详情：多出 Markdown 讲解、要点与来源原文。"""

    details: str = ""
    key_points: list[str] | None = None
    source_chunk_indexes: list[int] | None = None
    sources: list[SourceChunk] = Field(default_factory=list)
    created_at: datetime


class KnowledgePointListResponse(BaseModel):
    items: list[KnowledgePointSummary]
    total: int
