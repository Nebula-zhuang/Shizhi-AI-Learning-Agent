"""SSE（Server-Sent Events）工具函数。

协议约定（前端按同一套约定解析，见 apps/web/src/api/client.ts）：

    event: meta    {"model": "...", "mode": "live|mock", "request_id": "..."}   首帧，用于告知本次调用的运行信息
    event: delta   {"text": "增量文本"}                                          中间帧，可多次
    event: done    {"request_id": "...", "elapsed_ms": 1234}                     结束帧
    event: error   {"message": "可读的错误信息"}                                  异常帧（终止流）

每帧格式为标准的 SSE 块：
    event: <name>\\n
    data: <json>\\n
    \\n
"""

from __future__ import annotations

import json
from typing import Any


def sse_frame(event: str, data: dict[str, Any]) -> str:
    """把事件名与数据序列化为一帧 SSE 文本。"""
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def sse_comment(text: str) -> str:
    """SSE 注释行。可用于提前打开通道、绕过部分代理的缓冲。"""
    return f": {text}\n\n"


# 关闭反向代理缓冲，确保增量能被实时看到（Nginx 等环境必需）
SSE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}
