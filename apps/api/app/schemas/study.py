"""自由学习空间的请求 / 响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# 对话
# --------------------------------------------------------------------------- #
class ConversationCreate(BaseModel):
    title: str = Field(default="", max_length=255)


class ConversationRename(BaseModel):
    title: str = Field(min_length=1, max_length=255)


class ConversationSummary(BaseModel):
    id: int
    title: str
    message_count: int
    created_at: datetime
    last_message_at: datetime


class ConversationListResponse(BaseModel):
    items: list[ConversationSummary]
    total: int


# --------------------------------------------------------------------------- #
# 消息
# --------------------------------------------------------------------------- #
class MessageItem(BaseModel):
    id: int
    role: str
    content: str
    #: 本轮用到的外部能力摘要（资料 / 联网），供右侧面板展示
    sources: list[dict[str, Any]] | None = None
    #: 引用来源。联网时是网页，引用资料时是文档出处。
    citations: list[dict[str, Any]] | None = None
    attachments: list[dict[str, Any]] | None = None
    #: 自然语言状态轨迹。**留痕是必要的** —— 刷新后用户仍要能看出"它查没查"。
    status_trace: list[str] | None = None
    degraded_reason: str | None = None
    created_at: datetime


class MessageListResponse(BaseModel):
    conversation_id: int
    items: list[MessageItem]


# --------------------------------------------------------------------------- #
# 提问
# --------------------------------------------------------------------------- #
class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    #: 本轮要关联的资料。为空表示"不限定范围，检索我全部资料"。
    document_ids: list[int] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# 能力自检
# --------------------------------------------------------------------------- #
class McpStatus(BaseModel):
    """MCP 的**真实**状态 —— 服务端自报的身份 + 运行时发现的工具名。

    这两个字段不是我们写的常量，是协议握手与  读回来的，
    所以它们可以作为"确实经过 MCP"的可核对证据。
    """

    configured: bool
    url: str
    #: 连上过才有值：{name, version, protocol_version}
    server_info: dict[str, Any] | None = None
    #: 运行时从 tools/list 读到的工具名。**不是硬编码的。**
    discovered_tools: list[str] = Field(default_factory=list)


class CapabilityInfo(BaseModel):
    """给前端显示"现在能做到什么"。**绝不包含任何密钥或内部实现细节。**"""

    free_study: bool
    knowledge_base: bool
    web_search_available: bool
    #: 面向用户的说明原话（前端直接用，不必自己组织措辞）
    web_search_note: str
    #: 联网后端策略：auto / mcp / tavily
    web_search_backend: str = "auto"
    #: MCP 的详细状态
    mcp: McpStatus | None = None
    #: 已注册的工具名（供 "开发者" 面板展示）
    tools: list[str] = Field(default_factory=list)
    max_capabilities_per_turn: int


class AttachmentUploadResponse(BaseModel):
    """上传结果。**沿用书房上传的响应形状**，前端可以用同一套逻辑处理。"""

    document_id: int
    file_name: str
    file_type: str
    file_size: int
    #: 是不是图片 —— 前端据此决定要不要走"看图"的展示
    is_image: bool
    parse_status: str
    #: 命中去重时为 true（同一份文件之前传过）
    dedup: bool = False


class AttachmentOption(BaseModel):
    """"从资料库选择"用的一项。"""

    document_id: int
    file_name: str
    file_type: str
    parse_status: str


class AttachmentListResponse(BaseModel):
    items: list[AttachmentOption]
