"""知识图谱与可信度校验的 DTO。

命名与取值风格沿用 P1：**接口只返回原始枚举值，中文标签由前端映射**。
这样新增一个关系类型或校验层时，不必同时改后端 DTO 与数据库注释两处文案。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# 图谱
# --------------------------------------------------------------------------- #
class GraphNode(BaseModel):
    """图谱节点。字段刻意与 KnowledgePointSummary 对齐，前端可复用详情组件。"""

    id: int
    title: str
    summary: str = ""
    difficulty: int
    importance: int
    confidence: float
    verify_status: str
    heading_path: list[str] | None = None
    source_pages: list[int] | None = None
    order_index: int
    #: 该节点在文档中的章节深度（heading_path 长度），前端据此分层排布
    depth: int = 1


class GraphEdge(BaseModel):
    """图谱边。带 relation_type 与**构建依据**，这是"没有随机连边"的对外证据。"""

    id: int
    from_kp_id: int
    to_kp_id: int
    relation_type: str
    #: 反向视角的类型。目前只有 contains → belongs_to 会不同，
    #: 前端反向遍历节点时（如点击子节点看"它属于谁"）用这个标签。
    inverse_type: str
    confidence: float
    source: str
    evidence: str


class GraphStats(BaseModel):
    node_count: int
    edge_count: int
    by_type: dict[str, int] = Field(default_factory=dict)
    max_depth: int = 1
    #: 没有任何连边的孤立节点数。数量高说明材料章节信息不足，值得提示用户。
    isolated_nodes: int = 0


class GraphResponse(BaseModel):
    document_id: int
    document_name: str
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    stats: GraphStats


# --------------------------------------------------------------------------- #
# 关系构建
# --------------------------------------------------------------------------- #
class RelationBuildResponse(BaseModel):
    ok: bool
    document_id: int
    message: str
    stats: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# 可信度校验
# --------------------------------------------------------------------------- #
class CheckItem(BaseModel):
    id: int
    check_type: str
    verdict: str
    confidence: float
    reason: str = ""
    evidence: dict[str, Any] | None = None
    source_urls: list[str] | None = None
    engine: str = ""
    created_at: datetime


class CheckGroup(BaseModel):
    """按校验层分组。三层分开返回，而不是揉成一个"综合结论"。"""

    check_type: str
    latest: CheckItem | None = None
    history: list[CheckItem] = Field(default_factory=list)
    count: int = 0


class KnowledgeCheckResponse(BaseModel):
    kp_id: int
    title: str
    #: P1 抽取结果摘要（只读展示，P2 从不修改这些字段）
    verify_status: str
    groups: list[CheckGroup]
    total: int


class VerifyStartResponse(BaseModel):
    document_id: int
    accepted: bool
    message: str


class VerifyStatusResponse(BaseModel):
    document_id: int
    state: str
    progress: int = 0
    detail: str = ""
    total_points: int = 0
    #: 已产生校验记录的知识点数（来自数据库统计，重启后依然准确）
    checked_points: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)
    stats: dict[str, Any] = Field(default_factory=dict)


class VerifyCapabilities(BaseModel):
    """能力探测。让前端能区分「未配置 Key」与「配置了但失败」。"""

    verify_enabled: bool
    web_verify_enabled: bool
    web_provider: str = "tavily"
    tavily_configured: bool
    web_verify_effective: bool
    model_importance_threshold: int
    web_max_per_document: int
