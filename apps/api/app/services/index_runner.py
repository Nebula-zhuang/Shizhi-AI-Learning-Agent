"""索引任务的进程内执行器。

**与 `ingest_runner` / `verify_runner` 同构但完全独立**（独立信号量、独立上限、独立状态）。

⚠️ 这是本项目第三个同构执行器。三个还能接受，**如果 P4 再出现第四个同类需求，
就应当抽象成通用执行器** —— 届时抽象所需的三次经验已经齐备。
现在不动是因为：抽象必然要改动两个已验收的模块，而收益只是一个文件。

为什么索引必须要异步：一份 44 页的资料有 120 个文本块，按云端 embedding 的
单请求上限（10 条）要发 12 次请求，同步接口会让用户干等十几秒，
且期间浏览器超时、网关超时都会带来难以解释的失败。
"""

from __future__ import annotations

import asyncio
from typing import Any, Sequence

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_semaphore: asyncio.Semaphore | None = None
_tasks: set[asyncio.Task] = set()

#: 全局索引状态（内存态）。重启后丢失 —— 但向量本身不会丢，
#: 索引结果可以从 Chroma 里查出来，因此界面不会出现"什么都没有"的空窗。
_state: dict[str, Any] = {}


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(max(1, settings.rag_max_concurrency))
    return _semaphore


def set_state(**fields: Any) -> None:
    _state.update(fields)


def get_state() -> dict[str, Any]:
    return dict(_state)


def is_running() -> bool:
    return any(not task.done() for task in _tasks)


def active_count() -> int:
    return len(_tasks)


async def _guarded(document_ids: Sequence[int] | None) -> None:
    """受信号量保护的执行体。延迟导入服务层，避免导入环。"""
    from app.db.session import SessionLocal
    from app.services import index_service

    async with _get_semaphore():
        set_state(state="running", progress=0, detail="开始建立索引")
        try:
            with SessionLocal() as db:
                stats = await index_service.index_documents(
                    db,
                    document_ids,
                    on_progress=lambda pct, detail: set_state(progress=pct, detail=detail),
                )
            set_state(
                state="failed" if stats.failed and not stats.documents else "done",
                progress=100,
                detail=(
                    f"索引完成：{stats.documents} 份资料 / {stats.indexed_chunks} 个文本块"
                    + (f"，{len(stats.failed)} 份失败" if stats.failed else "")
                ),
                stats=stats.as_dict(),
            )
            logger.info("索引任务结束：%s", stats.as_dict())
        except asyncio.CancelledError:
            set_state(state="cancelled", detail="服务关闭，索引被中断")
            raise
        except Exception as exc:  # noqa: BLE001 - 任务失败不能拖垮进程
            logger.exception("索引任务异常：%s", exc)
            set_state(state="failed", detail=f"索引失败：{type(exc).__name__}: {exc}")


def submit(document_ids: Sequence[int] | None = None) -> bool:
    """投递索引任务。

    必须在已运行的事件循环中调用 —— 路由必须是 `async def`。
    （P1 在 reprocess 接口上踩过同步路由的坑，不再重复。）
    """
    if is_running():
        logger.info("已有索引任务在运行，忽略重复投递")
        return False

    try:
        task = asyncio.create_task(_guarded(document_ids), name="rag-index")
    except RuntimeError as exc:
        logger.error(
            "没有运行中的事件循环，无法投递索引任务：%s。请确认调用方是 async def 路由。", exc
        )
        return False

    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    set_state(state="queued", progress=0, detail="排队等待索引")
    return True


async def shutdown() -> None:
    """取消未完成的任务。已写入的向量不受影响（写入是逐份文档提交的）。"""
    if not _tasks:
        return
    logger.info("正在取消 %d 个未完成的索引任务…", len(_tasks))
    for task in list(_tasks):
        task.cancel()
    await asyncio.gather(*_tasks, return_exceptions=True)
    _tasks.clear()
