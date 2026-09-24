"""待办事项服务。

## 归属隔离照抄 `study_service` 那套

单条资源的每一次读/改/删都走 `get_owned(db, todo_id, learner_id)` ——
**查询里直接带上 `learner_id`**，而不是"先查出来再比对"。

差别很实在：先查再比对，一旦有人漏写那个 `if` 就越权了，
而且日志里会留下一条"确实查到过别人的数据"的痕迹；
带在 `where` 里则**根本查不到**，漏写的可能性只剩下"完全忘了调用它"。

## 不存在与不属于自己，返回同一个错误

与 `study_service.ConversationNotFound` 同一理由：**区分开就等于告诉攻击者
"这个 id 是存在的"**，那是一个用来枚举别人数据的接口。所以统一 404。

## 排序为什么在后端也要做

后端排 `completed, created_at` 是为了分页正确（在内存里排只能排当前页）；
前端 `todoState.ts` 里也有一份同样的排序，是为了**勾选之后不重新拉取**也能
保持顺序正确。两处的规则必须一致 —— 已经写进各自的注释。
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.todo import TimerMode, Todo

#: 单页上限。防一个超大 limit 把整表拉出来。
DEFAULT_LIMIT = 100
MAX_LIMIT = 200


class TodoNotFound(LookupError):
    """待办不存在**或不属于当前用户**。

    刻意不区分这两种情况：区分开就等于告诉攻击者"这个 id 是存在的"。
    """


def get_owned(db: Session, todo_id: int, learner_id: str) -> Todo:
    """取一条属于该用户的待办。取不到就抛 `TodoNotFound`。"""
    stmt = select(Todo).where(
        Todo.id == todo_id,
        Todo.learner_id == learner_id,
    )
    todo = db.scalars(stmt).first()
    if todo is None:
        raise TodoNotFound(str(todo_id))
    return todo


def list_todos(
    db: Session,
    *,
    learner_id: str,
    completed: bool | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> tuple[list[Todo], int]:
    """列出待办，返回 `(本页, 总数)`。

    **未完成在前，同组内新的在上**（`completed ASC, created_at DESC`）——
    与 `apps/web/src/features/todo/todoState.ts` 的排序一致。
    分页必须在这里做：在内存里排只能排当前页，跨页顺序会乱。
    """
    bounded = max(1, min(limit, MAX_LIMIT))
    conditions = [Todo.learner_id == learner_id]
    if completed is not None:
        conditions.append(Todo.completed.is_(completed))

    total = len(db.scalars(select(Todo.id).where(*conditions)).all())

    stmt = (
        select(Todo)
        .where(*conditions)
        .order_by(Todo.completed.asc(), Todo.created_at.desc(), Todo.id.desc())
        .limit(bounded)
        .offset(max(0, offset))
    )
    return list(db.scalars(stmt).all()), total


def create_todo(
    db: Session,
    *,
    learner_id: str,
    title: str,
    due_date: date | None = None,
    timer_mode: str = TimerMode.NONE,
    timer_minutes: int | None = None,
) -> Todo:
    """新建一条待办。

    `title` 的清洗与校验、以及**计时方式与分钟数的自洽性**
    都在 schema 层完成（那里能直接给 422 ✓）；这里只管落库。
    """
    todo = Todo(
        learner_id=learner_id,
        title=title,
        due_date=due_date,
        completed=False,
        timer_mode=timer_mode,
        timer_minutes=timer_minutes,
        spent_seconds=0,
    )
    db.add(todo)
    db.commit()
    db.refresh(todo)
    return todo


def update_todo(
    db: Session,
    *,
    todo_id: int,
    learner_id: str,
    changes: dict,
) -> Todo:
    """改一条待办。

    ⚠️ `changes` 必须是**已经被提供过的那些字段**
    （路由层用 `payload.model_dump(exclude_unset=True)` 得到）。

    为什么不能在这里用 `.get("due_date")` 兜底：那样"没提供 due_date"
    和"主动把 due_date 设成 null"就分不开了 —— 改个标题会顺手抹掉截止日 ✗
    只遍历 `changes` 里的键，语义才准确。

    ⚠️ 计时那两个字段（`timer_mode` / `timer_minutes`）由 schema 保证**成对出现** ✓
    （见 `schemas/todo.py` 的第三条说明），所以这里直接逐个 setattr 就是对的 ✓
    """
    todo = get_owned(db, todo_id, learner_id)
    for field in (
        "title",
        "completed",
        "due_date",
        "timer_mode",
        "timer_minutes",
        "spent_seconds",
    ):
        if field in changes:
            setattr(todo, field, changes[field])
    db.commit()
    db.refresh(todo)
    return todo


def delete_todo(db: Session, *, todo_id: int, learner_id: str) -> None:
    """删一条待办。删别人的 → `TodoNotFound`（路由层转 404）。"""
    todo = get_owned(db, todo_id, learner_id)
    db.delete(todo)
    db.commit()
