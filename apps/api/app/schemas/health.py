"""健康检查相关的响应模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ComponentStatus(BaseModel):
    """单个依赖组件的状态。"""

    name: str = Field(description="组件名：llm / mysql / chroma")
    ok: bool
    detail: dict[str, Any] = Field(default_factory=dict, description="组件自述信息")


class HealthResponse(BaseModel):
    """整体健康检查结果。

    status = ok       所有关键组件正常
    status = degraded 应用本身存活，但有组件不可用（P0 允许，便于无数据库时联调）
    """

    status: Literal["ok", "degraded"]
    app: str
    env: str
    version: str
    components: list[ComponentStatus]
