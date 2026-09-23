"""学习报告的响应模型（Phase 5E）。

形状与 `app/services/report_service.py` 的 `collect()` 一一对应。

## 关于 `mastery`

响应里**保留** `mastery` 这个浮点数 —— 前端要靠它做**强度分级**（画轨迹的深浅、
排序），但它**不许出现在界面上**。这条规矩在项目里是硬的：
「置信度 / 相似度 / 数据库字段不许拼进 UI」（见 `features/learn/voice.ts` 的约定）。

所以后端如实提供数据，**由前端保证不显示** —— 并且有一条前端测试专门钉住这件事
（`reportState.test.ts` 里断言渲染出的文字里不含原始数字）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class MasteryOverview(BaseModel):
    """总体掌握度。直接来自 `memory_service.mastery_overview`。"""

    knowledge_point_total: int
    tracked: int
    untouched: int
    new: int
    learning: int
    weak: int
    mastered: int


class ReportPoint(BaseModel):
    """一个知识点在报告里的样子。来自 `WeakPoint.as_dict()`。"""

    model_config = ConfigDict(extra="ignore")

    knowledge_point_id: int
    title: str
    #: ⚠️ 供前端分级用，**不许显示**
    mastery: float = 0.0
    status: str = "new"
    attempt_count: int = 0
    consecutive_wrong: int = 0
    importance: int = 0
    last_error_type: str | None = None
    next_review_at: str | None = None
    urgency: float = 0.0
    due: bool = False


class SessionStats(BaseModel):
    """窗口内这位学习者的学习次数与教学轮次。"""

    count: int = 0
    turns: int = 0


class TrajectoryPoint(BaseModel):
    at: str
    #: ⚠️ 同上：供画图用，**不许显示**
    mastery: float


class Trajectory(BaseModel):
    """一个知识点的掌握度变化（从 `messages.state_snapshot` 还原）。"""

    knowledge_point_id: int
    title: str
    points: list[TrajectoryPoint] = Field(default_factory=list)


class Narrative(BaseModel):
    """那段人话。

    `available=false` 时 `text` 为空串 —— **结构化数据不受影响**，
    前端用确定性文案兜底。
    """

    available: bool = False
    text: str = ""


class LearningReportResponse(BaseModel):
    """学习报告。"""

    generated_at: str
    window_days: int
    window_since: str
    #: `error_types` 是**全量历史**口径，不随窗口变化 —— 如实告知，由前端说明
    error_types_are_all_time: bool = True

    overview: MasteryOverview
    weak_points: list[ReportPoint] = Field(default_factory=list)
    due_reviews: list[ReportPoint] = Field(default_factory=list)
    #: `ErrorType` 取值 → 次数
    error_types: dict[str, int] = Field(default_factory=dict)
    sessions: SessionStats = Field(default_factory=SessionStats)
    trajectory: list[Trajectory] = Field(default_factory=list)
    narrative: Narrative = Field(default_factory=Narrative)
