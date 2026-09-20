"""进程内轻量任务执行器。

**这不是消息队列**（项目明确排除 MQ）。它只做三件事：

1. 用信号量限制并发，避免连续上传把 LLM 限流打爆
2. 持有任务引用，防止被 GC 提前回收
3. 提供优雅关闭与运行状态查询

已知局限与处置：进程重启会丢失运行中的任务，文档会卡在中间状态。
`document_service.cleanup_stale_documents()` 在应用启动时把它们标记为失败并提示可重跑，
因此不会出现永远转圈的僵尸文档。
"""

from __future__ import annotations

import asyncio

from app.core.config import settings
from app.core.logging import get_logger
from app.services.ingest_pipeline import run_pipeline

logger = get_logger(__name__)

_semaphore: asyncio.Semaphore | None = None
_tasks: set[asyncio.Task] = set()


def _get_semaphore() -> asyncio.Semaphore:
    """懒创建信号量。并发上限来自配置，运行期不会变化。"""
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(max(1, settings.ingest_max_concurrency))
    return _semaphore


async def _guarded(document_id: int) -> None:
    """受信号量保护的执行体。"""
    async with _get_semaphore():
        await run_pipeline(document_id)


def submit(document_id: int) -> bool:
    """投递一个摄取任务。

    必须在已运行的事件循环中调用（FastAPI 的 async 路由满足这一条件）。
    返回 True 表示已投递。
    """
    try:
        task = asyncio.create_task(_guarded(document_id), name=f"ingest-{document_id}")
    except RuntimeError as exc:
        # 最常见的成因：调用方是同步函数，被 FastAPI 丢进了线程池 —— 那里没有运行中的
        # 事件循环。凡是会投递任务的接口，都必须是 `async def`。
        logger.error(
            "没有运行中的事件循环，无法投递摄取任务（document_id=%s）：%s。"
            "请确认调用方是 async def 路由。",
            document_id,
            exc,
        )
        return False

    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    logger.info("已投递摄取任务 document_id=%s（当前进行中 %d 个）", document_id, len(_tasks))
    return True


def active_count() -> int:
    """当前进行中的任务数。"""
    return len(_tasks)


async def shutdown() -> None:
    """取消未完成的任务。被取消的文档会在下次启动时被 cleanup 标记为失败。"""
    if not _tasks:
        return

    logger.info("正在取消 %d 个未完成的摄取任务…", len(_tasks))
    for task in list(_tasks):
        task.cancel()
    await asyncio.gather(*_tasks, return_exceptions=True)
    _tasks.clear()
