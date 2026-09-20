"""P3 接口的请求 / 响应模型：向量索引与最小 RAG。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# 能力探测
# --------------------------------------------------------------------------- #
class EmbeddingCapability(BaseModel):
    """Embedding 能力。**只暴露布尔值与参数，绝不回显 Key。**"""

    provider: str
    model: str
    dim: int
    configured: bool
    batch_size: int
    max_chars: int
    timeout_s: float


class RagCapabilities(BaseModel):
    embedding: EmbeddingCapability
    llm_model: str
    llm_mode: str
    top_k: int
    max_distance: float
    max_context_chars: int
    temperature: float


# --------------------------------------------------------------------------- #
# 索引
# --------------------------------------------------------------------------- #
class IndexRequest(BaseModel):
    """索引请求。`document_ids` 省略时索引全部已解析完成的资料。"""

    document_ids: list[int] | None = Field(
        default=None,
        description="要索引的文档 id；省略则索引全部已解析完成的资料",
    )

    model_config = {
        "json_schema_extra": {"example": {"document_ids": [1, 2]}}
    }


class IndexStartResponse(BaseModel):
    accepted: bool
    message: str
    document_ids: list[int] | None = None


class DocumentIndexState(BaseModel):
    document_id: int
    exists: bool = True
    file_name: str = ""
    parse_status: str = ""
    kp_count: int = 0
    chunk_total: int = 0
    indexed_chunks: int = 0
    complete: bool = False
    error: str | None = None


class IndexStatusResponse(BaseModel):
    state: str
    progress: int = 0
    detail: str = ""
    collection: str
    collection_count: int
    store_available: bool = True
    document_total: int = 0
    document_indexed: int = 0
    items: list[DocumentIndexState] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# 问答
# --------------------------------------------------------------------------- #
class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=1000, description="用户问题")
    document_ids: list[int] | None = Field(
        default=None, description="限定检索范围的文档 id；省略则检索全部已索引资料"
    )
    top_k: int | None = Field(default=None, ge=1, le=20, description="覆盖默认 Top-K")

    model_config = {
        "json_schema_extra": {
            "example": {"question": "进程和线程有什么区别？", "document_ids": [1]}
        }
    }


class RagSourceItem(BaseModel):
    """一条来源片段。字段要够用户回查到原文的具体位置。"""

    index: int = Field(description="上下文里的编号，答案中的 [n] 与它对应")
    document_id: int
    file_name: str
    chunk_index: int
    page_start: int
    page_end: int
    page_label: str
    block_type: str
    heading_path: list[str] = Field(default_factory=list)
    distance: float
    content: str
    used: bool = Field(description="是否被放进上下文")
    dropped_reason: str = ""


class AskResponse(BaseModel):
    question: str
    answer: str
    sources: list[RagSourceItem] = Field(default_factory=list)
    model: str
    mode: str
    retrieved: int = Field(description="过门槛前检索到的片段数")
    used: int = Field(description="实际放进上下文的片段数")
    context_chars: int = 0
    timings_ms: dict[str, int] = Field(default_factory=dict)
    short_circuited: bool = Field(
        default=False, description="资料中没有相关内容，未调用模型直接作答"
    )
    generation_failed: bool = Field(
        default=False, description="检索成功但生成失败（模型不可用/余额不足/超时），sources 仍然有效"
    )
    error: str = Field(default="", description="生成失败的原因（generation_failed 为真时才有）")
