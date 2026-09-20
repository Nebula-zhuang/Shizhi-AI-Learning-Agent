"""文档相关的请求 / 响应模型。

刻意不暴露 storage_path 与 structure_path —— 那是服务端内部路径，
前端只需要 document_id，其余通过专门的接口获取。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ParseStatusLiteral = Literal[
    "pending", "parsing", "chunking", "extracting", "ready", "failed"
]


class DocumentSummary(BaseModel):
    """列表页用的文档摘要。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    file_name: str
    file_type: str
    file_size: int
    parse_status: ParseStatusLiteral
    progress: int
    stage_detail: str | None = None
    page_count: int
    char_count: int
    image_count: int
    chunk_count: int
    kp_count: int
    created_at: datetime


class DocumentDetail(DocumentSummary):
    """详情页：多出错误信息与警告。"""

    parse_error: str | None = None
    warnings: list[dict[str, Any]] = Field(default_factory=list)


class DocumentStatus(BaseModel):
    """轻量轮询响应。字段刻意保持精简，2 秒一次也不心疼。"""

    document_id: int
    parse_status: ParseStatusLiteral
    progress: int
    stage_detail: str | None = None
    chunk_count: int = 0
    kp_count: int = 0
    parse_error: str | None = None
    warning_count: int = 0

    @property
    def is_terminal(self) -> bool:
        return self.parse_status in ("ready", "failed")


class UploadResponse(BaseModel):
    """上传结果。"""

    document: DocumentSummary
    dedup: bool = Field(description="true 表示命中了内容去重，直接复用已有记录")
    message: str


class DocumentListResponse(BaseModel):
    items: list[DocumentSummary]
    total: int


class ChunkItem(BaseModel):
    """文本块。"""

    model_config = ConfigDict(from_attributes=True)

    chunk_index: int
    content: str
    page_start: int
    page_end: int
    block_type: str
    heading_path: list[str] | None = None
    image_path: str | None = None
    char_count: int


class ChunkListResponse(BaseModel):
    items: list[ChunkItem]
    total: int
