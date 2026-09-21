"""对话路由：最小 LLM 对话接口（含 SSE 流式）。

P0 刻意只有一个「直接转发给 LLM」的通道，用来验证：
    前端表单 → HTTP → LLM 网关 → SSE 流 → 前端增量渲染
整条链路的连通性。

P4 接入 Tutor Agent 后，/chat/stream 的职责会改为「驱动 Agent Loop」，
响应中会增加 action_type（教学动作）与 references（引用片段）等字段；
届时建议保留当前接口作为「直接对话」调试通道。

## ⚠️ 这两个接口**必须登录**（2026-09-21 安全审计后加）

它们原来完全无鉴权 —— 那是一台**未授权的 LLM 代理**：
任何人（含未登录）都能拿它消耗本项目的模型配额，
而且限流、审计、成本归属都无从谈起。

改法就是接入项目已有的认证体系（`CurrentUser` → 未登录 401），
**没有新建任何认证机制**，LLM 网关与调用逻辑一行未改。

前端对应入口是隐藏的开发者页（`?dev=1` 或 Ctrl+Shift+D 打开侧栏
「开发者 → 接口调试」），需要登录后才能用。
"""

from __future__ import annotations

import time
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.api.deps import CurrentUser
from app.api.sse import SSE_HEADERS, sse_frame
from app.core.llm import LLMError, llm_gateway
from app.core.logging import get_logger
from app.schemas.chat import ChatRequest, ChatResponse

logger = get_logger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


def _to_messages(req: ChatRequest) -> list[dict[str, str]]:
    return [{"role": m.role, "content": m.content} for m in req.messages]


@router.post("", response_model=ChatResponse, summary="最小对话（非流式）")
async def chat(req: ChatRequest, _user: CurrentUser) -> ChatResponse:
    """一次性返回完整回答。便于接口自测与自动化测试。**需要登录。**"""
    request_id = uuid.uuid4().hex[:12]
    started = time.perf_counter()
    try:
        result = await llm_gateway.chat(
            _to_messages(req),
            temperature=req.temperature,
            max_tokens=req.max_tokens,
        )
    except LLMError as exc:
        logger.warning("[%s] LLM 调用失败：%s", request_id, exc.message)
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    logger.info(
        "[%s] 非流式对话完成 mode=%s 耗时=%.0fms 长度=%d",
        request_id,
        result.mode,
        (time.perf_counter() - started) * 1000,
        len(result.content),
    )
    return ChatResponse(content=result.content, model=result.model, mode=result.mode)


@router.post("/stream", summary="最小对话（SSE 流式）")
async def chat_stream(req: ChatRequest, _user: CurrentUser) -> StreamingResponse:
    """以 SSE 逐块下发模型输出。**需要登录。**

    帧序列：meta → delta* → done；异常时以 error 帧终止。

    ⚠️ `_user` 这个依赖**必须在建流之前解析**：FastAPI 会在进入函数体之前
    求值依赖，所以未登录时拿到的是 **401 响应**，而不是"先 200 建流、
    再在流里发一个 error 帧"。这一点很重要 —— 后者会让
    "鉴权失败"伪装成"流中途出错"，前端与调用方都难以区分。
    """
    request_id = uuid.uuid4().hex[:12]
    messages = _to_messages(req)
    describe = llm_gateway.describe()

    async def event_source():
        started = time.perf_counter()
        # 先发注释帧打开通道，并立即送出 meta，前端据此展示运行模式
        yield sse_frame(
            "meta",
            {
                "model": describe["model"],
                "mode": describe["mode"],
                "request_id": request_id,
            },
        )
        chunk_count = 0
        total_chars = 0
        try:
            async for piece in llm_gateway.stream_chat(
                messages,
                temperature=req.temperature,
                max_tokens=req.max_tokens,
            ):
                chunk_count += 1
                total_chars += len(piece)
                yield sse_frame("delta", {"text": piece})
        except LLMError as exc:
            logger.warning("[%s] 流式调用失败：%s", request_id, exc.message)
            yield sse_frame("error", {"message": exc.message})
            return
        except Exception as exc:  # noqa: BLE001 - 兜底，避免连接悬空
            logger.exception("[%s] 流式调用异常", request_id)
            yield sse_frame("error", {"message": f"服务内部错误：{exc}"})
            return

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "[%s] 流式对话完成 mode=%s 分块=%d 字符=%d 耗时=%dms",
            request_id,
            describe["mode"],
            chunk_count,
            total_chars,
            elapsed_ms,
        )
        yield sse_frame(
            "done",
            {
                "request_id": request_id,
                "chunks": chunk_count,
                "chars": total_chars,
                "elapsed_ms": elapsed_ms,
            },
        )

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
