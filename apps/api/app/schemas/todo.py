"""待办事项的请求 / 响应模型。

## 三处容易做错的地方

### 一、标题的"非空"是**清洗之后**的非空

`"   "` 这种输入必须在 schema 层就挡掉（422），而不是让一个全是空格的标题进数据库。
所以用 `field_validator` 先 `strip()` 再判空 —— 顺序反了的话，
`"   "` 会通过 `min_length=1` 然后变成空串落库。

### 二、PATCH 必须区分"没提供"和"显式给 null"

这是本模块最要紧的一条：

| 请求体 | 含义 | 期望行为 |
|---|---|---|
| `{"title": "新标题"}` | 只改标题 | `due_date` **不动** |
| `{"due_date": null}` | **主动清空**截止日 | `due_date` 置空 |
| `{}` | 什么都没说 | **什么都不改** |

如果 PATCH 的模型写成 `due_date: date` 带一个 None 默认值、然后直接读 `.due_date`，
上面第二种和第一种就分不开了 —— 改个标题会**顺手把截止日抹掉** ✗

解法是**不看字段值，看字段有没有被提供**：Pydantic v2 的
`model_fields_set` / `model_dump(exclude_unset=True)` 正好表达这件事 ✓
service 层据此判断"这次到底要改哪几项"。

### 三、计时是**一对**字段，必须一起给

`timer_mode` 与 `timer_minutes` 是自洽的一对：

- `countdown` **必须**给分钟数 —— 不给就不知道从哪儿往下数 ✗
- `countup` / `none` **必须不给** —— 给了会让人以为它会响 ✗

所以约束是：**只要碰其中任何一个，两个都要在请求体里出现**
（`{"timer_mode": "countup", "timer_minutes": null}` ✓）。
这样"把倒计时改成正计时"就被表达成"显式把分钟数设成 null"，
和 `due_date` 的清空是同一种语义 ✓ 不需要额外的"清空"接口。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: 标题长度上限。与 `todos.title` 的 VARCHAR(255) 同一口径。
TITLE_MAX = 255

#: 倒计时分钟数范围。上限 600（10 小时）—— 再长就不是"专注一段"了，多半填错了。
TIMER_MINUTES_MIN = 1
TIMER_MINUTES_MAX = 600

TimerModeIn = Literal["none", "countup", "countdown"]


def _clean_title(value: str) -> str:
    """去首尾空白，并保证清洗后非空。"""
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError("待办标题不能为空。")
    if len(cleaned) > TITLE_MAX:
        raise ValueError(f"待办标题不能超过 {TITLE_MAX} 个字。")
    return cleaned


def _check_timer_pair(mode: str, minutes: int | None) -> None:
    """校验计时方式与分钟数自洽。"""
    if mode == "countdown":
        if minutes is None:
            raise ValueError("倒计时需要给出分钟数。")
        if not (TIMER_MINUTES_MIN <= minutes <= TIMER_MINUTES_MAX):
            raise ValueError(
                f"倒计时分钟数需在 {TIMER_MINUTES_MIN}–{TIMER_MINUTES_MAX} 之间。"
            )
    elif minutes is not None:
        raise ValueError("只有倒计时才需要分钟数。")


class TodoCreate(BaseModel):
    """新建一条待办。"""

    title: str = Field(description="一句话，不能是空白")
    #: `YYYY-MM-DD`。给别的格式 Pydantic 会直接 422，不需要自己解析。
    due_date: date | None = Field(default=None, description="截止日，可空")
    timer_mode: TimerModeIn = "none"
    timer_minutes: int | None = None

    _clean = field_validator("title")(_clean_title)

    @model_validator(mode="after")
    def _check(self) -> TodoCreate:
        _check_timer_pair(self.timer_mode, self.timer_minutes)
        return self


class TodoUpdate(BaseModel):
    """改一条待办。**每个字段都是可选的，但"可选"和"给 null"是两回事。**

    - 字段**不在请求体里** → 这一项不动
    - 字段在请求体里且是 `null` → 这一项被清空（`due_date` / `timer_minutes` 允许）
    - `title` / `completed` / `timer_mode` 给了 `null` → 422（它们本来就不可为空）
    """

    title: str | None = None
    completed: bool | None = None
    due_date: date | None = None
    timer_mode: TimerModeIn | None = None
    timer_minutes: int | None = None
    #: 累计秒数，只在"暂停 / 收尾"时由前端回写。**不接受负数。**
    spent_seconds: int | None = Field(default=None, ge=0)

    @field_validator("title")
    @classmethod
    def _check_title(cls, value: str | None) -> str | None:
        # 没提供 / 显式 null 由 service 层按 `model_fields_set` 处理；
        # 这里只管"给了个字符串"的情况。
        return None if value is None else _clean_title(value)

    @field_validator("completed", "timer_mode")
    @classmethod
    def _check_not_null(cls, value, info):  # noqa: ANN001, ANN206
        # ⚠️ 这两个字段没有"空"这个状态。**没提供**不会走到这里（Pydantic 不校验未提供的字段）
        if value is None:
            which = "完成状态" if info.field_name == "completed" else "计时方式"
            raise ValueError(f"{which}不能设为空。")
        return value

    @model_validator(mode="after")
    def _check(self) -> TodoUpdate:
        """计时那两个字段必须**一起**给。

        ⚠️ 只在请求真的碰了计时的时候校验自洽性 ——
        只改标题的请求不该因为库里存着的旧组合而被拒 ✗
        """
        touched = {"timer_mode", "timer_minutes"} & self.model_fields_set
        if not touched:
            return self  # 没碰计时 → 这一项整体不动，不用校验
        if "timer_mode" not in self.model_fields_set:
            raise ValueError("改计时的时候要把计时方式一起给出。")
        _check_timer_pair(self.timer_mode, self.timer_minutes)
        return self


class TodoItem(BaseModel):
    """一条待办。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    completed: bool
    due_date: date | None = None
    #: `none` / `countup` / `countdown`
    timer_mode: str = "none"
    #: 只有倒计时才有值
    timer_minutes: int | None = None
    #: 累计已计时秒数（不是单次时长）
    spent_seconds: int = 0
    created_at: datetime
    updated_at: datetime


class TodoListResponse(BaseModel):
    items: list[TodoItem]
    total: int
