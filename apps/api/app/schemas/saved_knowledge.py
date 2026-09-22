"""用户保存的知识的请求 / 响应模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class SavedKnowledgeCreate(BaseModel):
    """保存一轮问答。

    客户端只需要给**一条助手消息的 id** —— 问题与回答由服务端从对话里取。
    这样做的理由：

    - **正文不能由客户端给。** 否则用户（或任何一个脚本）可以把任意文本
      塞进"我保存的知识"，而这条记录将来会被检索、被引用。
      以消息 id 为准，等于强制"保存的必须是真实发生过的一轮"。
    - **归属校验天然成立**：消息属于某条对话，对话有 `learner_id`。
      拿别人的消息 id 会在这一步被挡下（404，不区分"不存在"与"不是你的"）。
    """

    message_id: int = Field(gt=0, description="要保存的那条**助手**消息 id")
    tags: list[str] | None = Field(
        default=None,
        max_length=10,
        description="用户标签，最多 10 个。Phase 3A 只存不自动推荐。",
    )


class SavedKnowledgeItem(BaseModel):
    """一条已保存的知识。"""

    id: int
    question: str
    answer: str
    source_message_id: int | None = None
    source_urls: list | None = None
    kp_ids: list | None = None
    tags: list | None = None

    #: 向量库里的 id。**为 null 表示这条暂时搜不到** ——
    #: 写向量失败不影响保存成功（向量是派生数据），可以事后回填。
    embedding_id: str | None = None

    created_at: datetime


class SavedKnowledgeListResponse(BaseModel):
    items: list[SavedKnowledgeItem]
    total: int
