"""对话相关的请求 / 响应模型。

P0 只覆盖最小对话能力；P4 接入 Tutor Agent 后，这里会扩展出
action_type（教学动作）、references（引用片段）等字段。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant"]


class ChatMessage(BaseModel):
    """单条对话消息。"""

    role: Role = Field(..., description="角色：system / user / assistant")
    content: str = Field(..., min_length=1, description="消息正文")

    model_config = {
        "json_schema_extra": {
            "example": {"role": "user", "content": "什么是梯度下降？"}
        }
    }


class ChatRequest(BaseModel):
    """最小对话请求体。"""

    messages: list[ChatMessage] = Field(
        ..., min_length=1, description="对话历史，最后一条应为 user"
    )
    temperature: float | None = Field(
        default=None, ge=0.0, le=2.0, description="覆盖默认温度"
    )
    max_tokens: int | None = Field(default=None, ge=1, le=8192, description="覆盖默认上限")


class ChatResponse(BaseModel):
    """非流式对话响应。"""

    content: str
    model: str
    mode: str = Field(description="生效模式：live / mock")


class StreamMeta(BaseModel):
    """SSE 首帧 meta 事件的内容。"""

    model: str
    mode: str
    request_id: str
