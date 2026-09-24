"""待办事项接口。

## 四个端点，都在自己的资料之外

```
POST   /todos            新建
GET    /todos            列出（可按完成状态过滤）
PATCH  /todos/{todo_id}  改（标题 / 完成状态 / 截止日）
DELETE /todos/{todo_id}  删
```

**刻意不做 `GET /todos/{todo_id}`**：列表已经带回全部字段，
行内编辑用的是列表里那一条，单独取一条没有调用方。
少一个端点就是少一处越权面 —— 这一条在 5D 的方案里已经确认过。

## 为什么每个端点都写 `require_learner_id`

`current_learner_id` 在未登录时会**回退到 `DEFAULT_LEARNER_ID = "local"`**，
而演示账号 demo 的 `learner_id` **也是 `local`** —— 于是"未登录访客 = demo 账号"。
对"本机自己用"的场景这没问题；但待办是**私人内容**，
用 `current_learner_id` 就意味着任何人都能看到 demo 的待办。
所以这里与 `study.py` 的所有端点保持同一把门：**必须登录**。

## 归属校验

单条操作全部走 `todo_service.get_owned(db, todo_id, learner_id)`，
取不到（**不存在、或不属于自己**）统一抛 `TodoNotFound` → 这里转 **404**。
不返回 403 —— 403 等于确认"这个 id 存在"，那是一个枚举接口。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import require_learner_id
from app.db.session import get_db
from app.schemas.todo import (
    TodoCreate,
    TodoItem,
    TodoListResponse,
    TodoUpdate,
)
from app.services import todo_service

router = APIRouter(prefix="/todos", tags=["todos"])

#: "不存在或不属于你" 的统一说法。**两种情况必须一模一样** ——
#: 任何措辞上的差别都会泄露"这个 id 到底存不存在"。
_NOT_FOUND = "这条待办不存在。"


@router.post(
    "",
    response_model=TodoItem,
    status_code=status.HTTP_201_CREATED,
    summary="新建待办",
)
def create_todo(
    payload: TodoCreate,
    db: Session = Depends(get_db),
    learner_id: str = Depends(require_learner_id),
) -> TodoItem:
    """新建一条待办。标题的清洗与"非空"由 schema 层保证（空白标题进不来）。"""
    todo = todo_service.create_todo(
        db,
        learner_id=learner_id,
        title=payload.title,
        due_date=payload.due_date,
        timer_mode=payload.timer_mode,
        timer_minutes=payload.timer_minutes,
    )
    return TodoItem.model_validate(todo)


@router.get("", response_model=TodoListResponse, summary="我的待办")
def list_todos(
    completed: bool | None = Query(None, description="只看已完成 / 只看未完成"),
    limit: int = Query(todo_service.DEFAULT_LIMIT, ge=1, le=todo_service.MAX_LIMIT),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    learner_id: str = Depends(require_learner_id),
) -> TodoListResponse:
    """列出**自己的**待办，未完成在前、新的在上。"""
    items, total = todo_service.list_todos(
        db, learner_id=learner_id, completed=completed, limit=limit, offset=offset
    )
    return TodoListResponse(items=[TodoItem.model_validate(t) for t in items], total=total)


@router.patch("/{todo_id}", response_model=TodoItem, summary="改一条待办")
def update_todo(
    todo_id: int,
    payload: TodoUpdate,
    db: Session = Depends(get_db),
    learner_id: str = Depends(require_learner_id),
) -> TodoItem:
    """改标题 / 完成状态 / 截止日。

    ⚠️ **只改请求体里真正出现过的字段**（`exclude_unset=True`）——
    这样 `{"title": "x"}` 不会动截止日，而 `{"due_date": null}` 才是主动清空。
    两者在模型上长得一样，只有"有没有被提供"能区分。
    """
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        # 什么都没提供 ── 不是错误，只是没有可改的。返回当前状态，避免多余写入。
        return TodoItem.model_validate(
            todo_service.get_owned(db, todo_id, learner_id)
        )
    try:
        todo = todo_service.update_todo(
            db, todo_id=todo_id, learner_id=learner_id, changes=changes
        )
    except todo_service.TodoNotFound:
        raise HTTPException(status_code=404, detail=_NOT_FOUND) from None
    return TodoItem.model_validate(todo)


@router.delete("/{todo_id}", summary="删一条待办")
def delete_todo(
    todo_id: int,
    db: Session = Depends(get_db),
    learner_id: str = Depends(require_learner_id),
) -> dict:
    try:
        todo_service.delete_todo(db, todo_id=todo_id, learner_id=learner_id)
    except todo_service.TodoNotFound:
        raise HTTPException(status_code=404, detail=_NOT_FOUND) from None
    return {"deleted": True, "todo_id": todo_id}
