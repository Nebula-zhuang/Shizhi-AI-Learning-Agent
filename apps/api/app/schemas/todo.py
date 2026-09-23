"""待办事项的请求 / 响应模型。

## 两处容易做错的地方

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
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: 标题长度上限。与 `todos.title` 的 VARCHAR(255) 同一口径。
TITLE_MAX = 255


def _clean_title(value: str) -> str:
    """去首尾空白，并保证清洗后非空。"""
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError("待办标题不能为空。")
    if len(cleaned) > TITLE_MAX:
        raise ValueError(f"待办标题不能超过 {TITLE_MAX} 个字。")
    return cleaned


class TodoCreate(BaseModel):
    """新建一条待办。"""

    title: str = Field(description="一句话，不能是空白")
    #: `YYYY-MM-DD`。给别的格式 Pydantic 会直接 422，不需要自己解析。
    due_date: date | None = Field(default=None, description="截止日，可空")

    _clean = field_validator("title")(_clean_title)


class TodoUpdate(BaseModel):
    """改一条待办。**三个字段都是可选的，但"可选"和"给 null"是两回事。**

    - 字段**不在请求体里** → 这一项不动
    - 字段在请求体里且是 `null` → 这一项被清空（只有 `due_date` 允许清空）
    - `title` / `completed` 给了 `null` → 422（它们本来就不可为空）
    """

    title: str | None = None
    completed: bool | None = None
    due_date: date | None = None

    @field_validator("title")
    @classmethod
    def _check_title(cls, value: str | None) -> str | None:
        # 没提供 / 显式 null 由 service 层按 `model_fields_set` 处理；
        # 这里只管"给了个字符串"的情况。
        return None if value is None else _clean_title(value)

    @field_validator("completed")
    @classmethod
    def _check_completed(cls, value: bool | None) -> bool | None:
        # `completed` 不接受显式 null —— 它没有"空"这个状态。
        if value is None:
            raise ValueError("完成状态不能设为空。")
        return value


class TodoItem(BaseModel):
    """一条待办。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    completed: bool
    due_date: date | None = None
    created_at: datetime
    updated_at: datetime


class TodoListResponse(BaseModel):
    items: list[TodoItem]
    total: int
