"""P4 接口的请求 / 响应模型：Tutor 教学闭环。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# 请求
# --------------------------------------------------------------------------- #
class TutorStartRequest(BaseModel):
    """开始一次学习。"""

    knowledge_point_id: int = Field(..., description="要学的知识点")
    document_id: int | None = Field(
        default=None, description="所属文档；省略则用知识点自身的文档"
    )
    session_id: int | None = Field(
        default=None, description="带上则在该会话上继续，省略则新建会话"
    )
    learner_id: str | None = Field(
        default=None, max_length=64, description="学习者标识；省略用默认学习者"
    )

    model_config = {
        "json_schema_extra": {"example": {"knowledge_point_id": 1}}
    }


class TutorAnswerRequest(BaseModel):
    """提交一次作答。"""

    session_id: int = Field(..., description="会话 id")
    answer: str = Field(..., min_length=1, max_length=4000, description="学习者的回答")
    learner_id: str | None = Field(default=None, max_length=64)

    model_config = {
        "json_schema_extra": {
            "example": {"session_id": 1, "answer": "进程是资源分配的基本单位，线程是调度的基本单位。"}
        }
    }


# --------------------------------------------------------------------------- #
# 响应
# --------------------------------------------------------------------------- #
class TutorSourceItem(BaseModel):
    document_id: int | None = None
    file_name: str = ""
    chunk_index: int | None = None
    page_label: str = ""
    page_start: int | None = None
    heading_path: list[str] = Field(default_factory=list)
    distance: float = 0.0
    content: str = ""


class TutorAssessment(BaseModel):
    """作答评估。注意 `confidence` 是评估本身的把握，**不是掌握度**。"""

    correct: bool
    score: float
    confidence: float = Field(description="评估本身的把握，与 mastery 严格区分")
    level: str
    error_type: str
    missing_points: list[str] = Field(default_factory=list)
    misunderstood_points: list[str] = Field(default_factory=list)
    feedback: str = ""
    engine: str = ""


class TutorLearnerState(BaseModel):
    """学习状态快照。`mastery` 才是掌握度。"""

    learner_id: str = ""
    knowledge_point_id: int | None = None
    exists: bool = False
    mastery: float = 0.0
    attempt_count: int = 0
    correct_count: int = 0
    consecutive_correct: int = 0
    consecutive_wrong: int = 0
    status: str = "new"
    last_error_type: str | None = None
    last_attempt_at: str | None = None
    updated_at: str | None = None
    accuracy: float = 0.0


class TutorTurnResponse(BaseModel):
    """一轮教学的完整结果。"""

    session_id: int
    knowledge_point_id: int
    title: str
    #: 本轮的教学动作：probe/explain/rephrase/harder/easier/summarize
    action: str
    content: str
    reason: str
    #: 状态机流转轨迹（LOAD_STATE → … → WAIT_USER）
    trace: list[str] = Field(default_factory=list)
    #: Policy 的决策基线：命中规则 / 是否强制 / 允许集合
    decision: dict[str, Any] = Field(default_factory=dict)
    #: LLM 提案的校验结果（`ok=false` 表示被 Policy 拦截）
    proposal: dict[str, Any] | None = None
    assessment: TutorAssessment | None = None
    state_before: TutorLearnerState | dict[str, Any] = Field(default_factory=dict)
    state_after: TutorLearnerState | dict[str, Any] = Field(default_factory=dict)
    state_updated: bool = False
    sources: list[TutorSourceItem] = Field(default_factory=list)
    tools: dict[str, Any] = Field(default_factory=dict)
    degraded: bool = False
    notes: list[str] = Field(default_factory=list)
    #: P5：本轮用到的长期记忆（讲法偏好 / 是否到期 / 主动回顾提示）
    memory: dict[str, Any] = Field(default_factory=dict)
    session_finished: bool = False
    thresholds: dict[str, float] = Field(default_factory=dict)


class TutorMessageItem(BaseModel):
    id: int
    role: str
    content: str
    action_type: str | None = None
    reason: str = ""
    state_snapshot: dict[str, Any] | None = None
    refs: list[dict[str, Any]] | None = None
    engine: str = ""
    created_at: str


class TutorEvaluationItem(BaseModel):
    id: int
    message_id: int | None = None
    knowledge_point_id: int
    question: str = ""
    user_answer: str = ""
    correct: bool
    score: float
    confidence: float
    level: str
    error_type: str
    missing_points: list[str] = Field(default_factory=list)
    misunderstood_points: list[str] = Field(default_factory=list)
    feedback: str = ""
    engine: str = ""
    created_at: str


class TutorSessionResponse(BaseModel):
    """会话全貌：消息 + 每轮决策 + 每次评估。"""

    session_id: int
    learner_id: str
    document_id: int | None = None
    knowledge_point_id: int | None = None
    title: str = ""
    status: str = ""
    action_count: int = 0
    created_at: str
    last_active_at: str
    messages: list[TutorMessageItem] = Field(default_factory=list)
    evaluations: list[TutorEvaluationItem] = Field(default_factory=list)
    #: 从消息里提取出的动作序列，便于一眼看出教学策略的变化
    action_sequence: list[str] = Field(default_factory=list)
    state: TutorLearnerState | None = None


class TutorCapabilities(BaseModel):
    """教学策略探测：动作集合与阈值（阈值来自 Policy，前端不必硬编码）。"""

    actions: list[str]
    action_labels: dict[str, str]
    thresholds: dict[str, float]
    max_tool_calls: int
    max_seconds: float
    llm_mode: str
    llm_model: str


# --------------------------------------------------------------------------- #
# P5：学习看板与画像
# --------------------------------------------------------------------------- #
class MasteryOverview(BaseModel):
    """掌握度概览：各类状态各有多少个知识点。"""

    knowledge_point_total: int = 0
    tracked: int = 0
    untouched: int = Field(default=0, description="从未作答过的知识点数")
    new: int = 0
    learning: int = 0
    weak: int = 0
    mastered: int = 0


class WeakPointItem(BaseModel):
    """一个待加强的知识点。"""

    knowledge_point_id: int
    title: str
    mastery: float
    status: str
    attempt_count: int = 0
    consecutive_wrong: int = 0
    importance: int = 3
    last_error_type: str | None = None
    next_review_at: str | None = None
    urgency: float = Field(default=0.0, description="紧迫度，越高越该先看")
    due: bool = Field(default=False, description="是否已到复习时间")


class LearnerProfileSummary(BaseModel):
    """画像摘要。`style_instruction` 就是注入 Prompt 的那段文字，前后端不为两套说法。"""

    preferred_style: str
    style_label: str
    style_source: str = Field(description="derived（自动推导）/ manual（用户设定）/ default")
    style_instruction: str
    style_evidence: dict[str, Any] = Field(default_factory=dict)


class ReviewCurveInfo(BaseModel):
    """当前生效的遗忘曲线参数。展示给用户看"复习计划是怎么排的"。"""

    tiers: list[dict[str, float]] = Field(default_factory=list)
    wrong_factor: float = 0.0
    wrong_streak_threshold: int = 0
    min_seconds: int = 0


class TutorDashboardResponse(BaseModel):
    """首屏看板：掌握度概览 + 待复习 + 薄弱点 + 画像。"""

    learner_id: str
    generated_at: str
    overview: MasteryOverview
    profile: LearnerProfileSummary
    due_reviews: list[WeakPointItem] = Field(default_factory=list)
    weak_points: list[WeakPointItem] = Field(default_factory=list)
    review_curve: ReviewCurveInfo


class ProfileUpdateRequest(BaseModel):
    """手动设定讲法偏好。写入后不再被自动推导覆盖。"""

    preferred_style: str = Field(
        ...,
        description="balanced / contrast / structured / stepwise / clarify",
    )
    learner_id: str | None = Field(default=None, max_length=64)

    model_config = {
        "json_schema_extra": {"example": {"preferred_style": "contrast"}}
    }


class TutorMemoryInfo(BaseModel):
    """一轮教学里用到的长期记忆。挂在 turn 响应上。"""

    learner_id: str = ""
    preferred_style: str = "balanced"
    style_label: str = ""
    style_source: str = "default"
    style_instruction: str = ""
    knowledge_state: dict[str, Any] | None = None
    is_due: bool = False
    last_error_type: str | None = None
    other_weak_points: list[dict[str, Any]] = Field(default_factory=list)
    should_recall: bool = False
    recall_note: str = ""
    recalled: bool = Field(default=False, description="本轮是否真的做了主动回顾")
