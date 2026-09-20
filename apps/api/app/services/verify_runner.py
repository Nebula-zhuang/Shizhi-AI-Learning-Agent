"""可信度校验的进程内执行器。

**与 `ingest_runner.py` 同构但完全独立** —— 独立的信号量、独立的任务集、独立的上限。

为什么不复用 `ingest_runner`（这是刻意接受的少量重复，理由见 docs/05-P2-实施方案.md §1）：

1. 直接复用会让两类任务共用同一个并发池。用户上传一份 50 页 PDF 时，
   摄取任务会把并发占满，校验任务排在它后面干等 —— 两个互不相关的操作互相拖慢。
2. 把 `ingest_runner` 改造成"可提交任意协程"的通用执行器，属于修改 P1 已验收代码。
   P1 的并发行为经过验证，不该为了省 40 行代码去动它。

等第三个同类需求出现时再抽象成通用执行器 —— 那时有三次经验，抽象才做得对。

同样**不是消息队列**（项目明确排除）。进程重启会丢失运行中的校验任务，
但校验结果本身是逐知识点落库的（每校验完一个知识点就 commit），
所以中断最多丢失"当前正在校验的那一个"，已产生的记录一条都不会丢。
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_semaphore: asyncio.Semaphore | None = None
_tasks: set[asyncio.Task] = set()

#: 文档级校验状态（内存态）。重启后丢失 —— 但已落库的校验记录不会丢，
#: 接口会用数据库统计兜底，因此界面不会出现"什么都没有"的空窗。
_states: dict[int, dict[str, Any]] = {}


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(max(1, settings.verify_max_concurrency))
    return _semaphore


def set_state(document_id: int, **fields: Any) -> None:
    state = _states.setdefault(document_id, {})
    state.update(fields)


def get_state(document_id: int) -> dict[str, Any]:
    return dict(_states.get(document_id) or {})


def clear_state(document_id: int) -> None:
    _states.pop(document_id, None)


async def _guarded(document_id: int, page_count: int) -> None:
    """受信号量保护的执行体。

    延迟导入 `verify_service`：避免 runner 与 service 之间形成导入环。
    """
    from app.db.session import SessionLocal
    from app.services import verify_service

    async with _get_semaphore():
        set_state(document_id, state="running", progress=0, detail="开始校验")
        try:
            with SessionLocal() as db:
                stats = await verify_service.verify_document(
                    db,
                    document_id,
                    page_count=page_count,
                    on_progress=lambda pct, detail: set_state(
                        document_id, progress=pct, detail=detail
                    ),
                )
            set_state(
                document_id,
                state="done",
                progress=100,
                detail="校验完成",
                stats=stats.as_dict(),
            )
            logger.info("文档 id=%s 校验任务结束：%s", document_id, stats.as_dict())
        except asyncio.CancelledError:
            set_state(document_id, state="cancelled", detail="服务关闭，校验被中断")
            raise
        except Exception as exc:  # noqa: BLE001 - 任务失败不能拖垮进程
            logger.exception("文档 id=%s 校验任务异常：%s", document_id, exc)
            set_state(document_id, state="failed", detail=f"校验失败：{type(exc).__name__}")


def submit(document_id: int, *, page_count: int) -> bool:
    """投递一个校验任务。

    必须在已运行的事件循环中调用 —— 路由必须是 `async def`。
    （P1 踩过这个坑：同步路由被 FastAPI 丢进线程池，那里没有事件循环。）
    """
    if is_running(document_id):
        logger.info("文档 id=%s 已在校验中，忽略重复投递", document_id)
        return False

    try:
        task = asyncio.create_task(
            _guarded(document_id, page_count), name=f"verify-{document_id}"
        )
    except RuntimeError as exc:
        logger.error(
            "没有运行中的事件循环，无法投递校验任务（document_id=%s）：%s。"
            "请确认调用方是 async def 路由。",
            document_id,
            exc,
        )
        return False

    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    set_state(document_id, state="queued", progress=0, detail="排队等待校验")
    logger.info("已投递校验任务 document_id=%s（当前进行中 %d 个）", document_id, len(_tasks))
    return True


def is_running(document_id: int) -> bool:
    return any(t.get_name() == f"verify-{document_id}" and not t.done() for t in _tasks)


def active_count() -> int:
    return len(_tasks)


async def shutdown() -> None:
    """取消未完成的任务。已落库的校验记录不受影响。"""
    if not _tasks:
        return
    logger.info("正在取消 %d 个未完成的校验任务…", len(_tasks))
    for task in list(_tasks):
        task.cancel()
    await asyncio.gather(*_tasks, return_exceptions=True)
    _tasks.clear()
