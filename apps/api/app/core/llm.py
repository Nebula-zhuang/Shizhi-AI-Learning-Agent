"""LLM 网关。

走 OpenAI 兼容协议，因此切换模型服务商只需改 .env 里的 BASE_URL / API_KEY / MODEL，
不改一行业务代码。

两种模式：
  live —— 真实调用远端模型
  mock —— 不发起任何外部请求，本地按字符节奏吐出模拟回答。
          用途：在没有 API Key 的情况下，依然能验证「接口 → SSE → 前端渲染」整条链路。

模式选择由 Settings.effective_llm_mode 决定（LLM_MODE=auto 时按是否配置 Key 自动判断）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    RateLimitError,
)

from app.core.config import Settings, settings as default_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

Message = dict[str, Any]
"""一条发给模型的消息。

`content` 有两种形态，**都合法**：

    {"role": "user", "content": "什么是 JVM？"}                     ← 纯文本（默认）

    {"role": "user", "content": [                                   ← 多模态
        {"type": "text", "text": "这张图里是什么？"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
    ]}

第二种就是 OpenAI 兼容的多模态格式本身 —— **不需要为它造新的消息类型**，
原来的 `Message` 是个普通的 dict，扩宽值的类型就够了。

⚠️ 但读 content 的地方必须走 `message_text()` 而不是直接 `.strip()`：
多模态时 content 是 list，直接 `.strip()` 会 AttributeError。
"""


# --------------------------------------------------------------------------- #
# 多模态消息的构造与提取
# --------------------------------------------------------------------------- #
def text_part(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def image_part(url: str, *, detail: str = "auto") -> dict[str, Any]:
    """构造一个图片 part。

    `url` 可以是 https 链接，也可以是 data URI（`data:image/png;base64,...`）。
    本项目把用户上传的图片以 **data URI** 发出去 —— 那些图存在本地磁盘上，
    模型服务商访问不到本机路径，用 data URI 是唯一不依赖公网可达性的做法。
    """
    return {"type": "image_url", "image_url": {"url": url, "detail": detail}}


def user_message(text: str, images: Sequence[str] = ()) -> Message:
    """构造一条用户消息。

    **没有图片时就用纯文本形态** —— 不必要地套成 parts 数组会让测试断言、
    日志、mock 回显都变复杂，而且部分服务商对 parts 形态的支持不如纯文本稳。
    """
    if not images:
        return {"role": "user", "content": text}
    parts: list[dict[str, Any]] = [text_part(text)]
    parts.extend(image_part(url) for url in images)
    return {"role": "user", "content": parts}


def message_text(message: Message) -> str:
    """从任意形态的消息里取出纯文本。**读 content 一律走这里。**"""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ).strip()
    return ""


def message_images(message: Message) -> list[str]:
    """取出消息里的图片 URL / data URI。"""
    content = message.get("content")
    if not isinstance(content, list):
        return []
    urls: list[str] = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "image_url":
            continue
        payload = part.get("image_url")
        if isinstance(payload, dict) and payload.get("url"):
            url = str(payload["url"])
            if url not in urls:
                urls.append(url)
    return urls



class LLMError(RuntimeError):
    """LLM 调用失败的统一异常，携带对用户友好的中文提示。"""

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(slots=True)
class LLMResult:
    """一次非流式调用的结果。"""

    content: str
    model: str
    mode: str
    usage: dict[str, Any] = field(default_factory=dict)
    finish_reason: str | None = None


# --------------------------------------------------------------------------- #
# Mock 模式的模拟回答
# --------------------------------------------------------------------------- #

_MOCK_TEMPLATE = """【Mock 模式】当前未检测到有效的 LLM_API_KEY，这段文字是本地模拟的流式输出，用于验证接口与前端渲染链路。

你刚才的提问是：「{question}」

链路验证要点：
1. 后端 FastAPI 已收到请求并正确解析了对话消息；
2. 响应以 text/event-stream 逐块下发，前端每收到一块就追加渲染；
3. 若你现在看到这段文字是逐字出现的，说明 SSE 流式通道工作正常。

要切换到真实模型，请在项目根目录的 .env 中配置：
  LLM_MODE=live
  LLM_BASE_URL=https://api.deepseek.com/v1
  LLM_API_KEY=你的密钥
  LLM_MODEL=deepseek-chat
保存后重启后端即可。"""


def _last_user_question(messages: Iterable[Message]) -> str:
    """从消息列表中取出最后一条 user 消息，用于 mock 模式回显。"""
    for msg in reversed(list(messages)):
        if msg.get("role") == "user":
            # 必须走 message_text：多模态消息的 content 是 list，直接 .strip() 会炸
            return message_text(msg) or "（空）"
    return "（未提供问题）"


# --------------------------------------------------------------------------- #
# 网关
# --------------------------------------------------------------------------- #


class LLMGateway:
    """OpenAI 兼容协议的统一入口。

    刻意保持极简：P0 只提供 chat / stream_chat 两个能力，
    后续 Agent Runtime、Tool Calling 都复用同一个网关实例。
    """

    def __init__(self, config: Settings | None = None) -> None:
        self._settings = config or default_settings
        self._client: AsyncOpenAI | None = None

    # ------------------------------------------------------------- 基础属性
    @property
    def mode(self) -> str:
        """当前生效模式：live / mock。"""
        return self._settings.effective_llm_mode

    @property
    def model(self) -> str:
        return self._settings.llm_model

    @property
    def base_url(self) -> str:
        return self._settings.llm_base_url

    def describe(self) -> dict[str, Any]:
        """给健康检查用的自述信息（不含任何密钥）。"""
        return {
            "mode": self.mode,
            "configured": self._settings.is_llm_configured,
            "base_url": self.base_url,
            "model": self.model,
        }

    def _get_client(self) -> AsyncOpenAI:
        """懒加载客户端。用得到才建连接，mock 模式下永远不会被调用。"""
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self._settings.llm_api_key,
                base_url=self._settings.llm_base_url,
                timeout=self._settings.llm_timeout,
                max_retries=1,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    # --------------------------------------------------------------- 流式
    async def stream_chat(
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """流式生成，逐块 yield 文本增量。

        调用方拿到的是纯文本 delta，不需要关心是 live 还是 mock。
        """
        if self.mode == "mock":
            async for piece in self._stream_mock(messages):
                yield piece
            return

        client = self._get_client()
        try:
            stream = await client.chat.completions.create(  # type: ignore[call-overload]
                model=self._settings.llm_model,
                messages=messages,  # type: ignore[arg-type]
                temperature=(
                    self._settings.llm_temperature if temperature is None else temperature
                ),
                max_tokens=(
                    self._settings.llm_max_tokens if max_tokens is None else max_tokens
                ),
                stream=True,
            )
        except AuthenticationError as exc:
            raise LLMError(
                "LLM 鉴权失败：API Key 无效或已过期，请检查 .env 中的 LLM_API_KEY。",
                status_code=401,
            ) from exc
        except RateLimitError as exc:
            raise LLMError(
                "LLM 触发限流或余额不足，请稍后重试或检查账户额度。", status_code=429
            ) from exc
        except APITimeoutError as exc:
            raise LLMError("LLM 请求超时，请检查网络或调大 LLM_TIMEOUT。", status_code=504) from exc
        except APIConnectionError as exc:
            raise LLMError(
                f"无法连接 LLM 服务（{self._settings.llm_base_url}），请检查网络或 BASE_URL 配置。",
                status_code=502,
            ) from exc
        except APIStatusError as exc:
            raise LLMError(
                f"LLM 服务返回错误状态 {exc.status_code}：{exc.message}", status_code=502
            ) from exc

        try:
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                piece = getattr(delta, "content", None)
                if piece:
                    yield piece
        except Exception as exc:  # noqa: BLE001 - 流中断也要转成可读错误
            logger.warning("LLM 流式读取中断: %s", exc)
            raise LLMError("LLM 流式响应中断，请重试。", status_code=502) from exc

    async def _stream_mock(self, messages: list[Message]) -> AsyncIterator[str]:
        """把模拟回答按小块吐出，模拟真实的打字机节奏。"""
        text = _MOCK_TEMPLATE.format(question=_last_user_question(messages))
        step = 6
        for i in range(0, len(text), step):
            yield text[i : i + step]
            await asyncio.sleep(0.02)

    # ------------------------------------------------------------- 非流式
    async def chat(
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        """非流式调用，返回完整结果。内部复用 stream_chat 以便统一错误处理。"""
        parts: list[str] = []
        async for piece in self.stream_chat(
            messages, temperature=temperature, max_tokens=max_tokens
        ):
            parts.append(piece)
        return LLMResult(
            content="".join(parts),
            model=self._settings.llm_model,
            mode=self.mode,
        )

    # ----------------------------------------------------- 结构化输出（P1）
    async def chat_json(
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        retry: int | None = None,
        mock_builder: Callable[[list[Message]], Any] | None = None,
    ) -> Any:
        """要求模型返回 JSON 并解析为 Python 对象。

        与 chat() 的区别：
        - 使用 response_format={"type":"json_object"} 约束输出（服务商不支持时自动降级为
          仅靠提示词约束）
        - 内置解析容错（剥离 ```json 围栏、提取首个完整 JSON 对象）
        - 解析失败自动重试 retry 次（默认取 settings.llm_json_retry）

        mock_builder：mock 模式下用于生成合法 JSON 的回调。P1 的摄取流水线强依赖 LLM，
        若每次测试都打真实 API 既慢又烧额度；由调用方提供 mock_builder 后，整条流水线
        可离线回归、零 API 消耗。为 None 时返回占位结构。

        新增于 P1，不改变 chat / stream_chat 的任何行为。
        """
        if self.mode == "mock":
            if mock_builder is not None:
                return mock_builder(messages)
            return {
                "_mock": True,
                "note": "mock 模式未提供 mock_builder，返回占位结构",
            }

        attempts = (self._settings.llm_json_retry if retry is None else retry) + 1
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                raw = await self._chat_json_raw(
                    messages, temperature=temperature, max_tokens=max_tokens
                )
                return extract_json(raw)
            except LLMError:
                raise
            except Exception as exc:  # noqa: BLE001 - 解析失败要重试
                last_error = exc
                logger.warning("结构化输出解析失败（第 %d/%d 次）：%s", attempt, attempts, exc)

        raise LLMError(
            f"模型未返回可解析的 JSON（已重试 {attempts} 次）：{last_error}", status_code=502
        )

    async def _chat_json_raw(
        self,
        messages: list[Message],
        *,
        temperature: float | None,
        max_tokens: int | None,
    ) -> str:
        """发一次带 JSON 约束的请求，返回原始文本。"""
        client = self._get_client()
        kwargs: dict[str, Any] = {
            "model": self._settings.llm_model,
            "messages": messages,
            "temperature": (
                self._settings.llm_temperature if temperature is None else temperature
            ),
            "max_tokens": (
                self._settings.llm_max_tokens if max_tokens is None else max_tokens
            ),
        }

        try:
            resp = await client.chat.completions.create(
                **kwargs, response_format={"type": "json_object"}
            )
        except APIStatusError as exc:
            # 部分兼容网关不支持 response_format，降级为仅靠提示词约束
            if exc.status_code in (400, 404, 422):
                logger.info("服务商不支持 response_format，降级为提示词约束模式")
                try:
                    resp = await client.chat.completions.create(**kwargs)
                except APIStatusError as inner:
                    raise LLMError(
                        f"LLM 服务返回错误状态 {inner.status_code}：{inner.message}",
                        status_code=502,
                    ) from inner
            else:
                raise LLMError(
                    f"LLM 服务返回错误状态 {exc.status_code}：{exc.message}", status_code=502
                ) from exc
        except AuthenticationError as exc:
            raise LLMError(
                "LLM 鉴权失败：API Key 无效或已过期，请检查 .env 中的 LLM_API_KEY。",
                status_code=401,
            ) from exc
        except RateLimitError as exc:
            raise LLMError(
                "LLM 触发限流或余额不足，请稍后重试或检查账户额度。", status_code=429
            ) from exc
        except APITimeoutError as exc:
            raise LLMError("LLM 请求超时，请检查网络或调大 LLM_TIMEOUT。", status_code=504) from exc
        except APIConnectionError as exc:
            raise LLMError(
                f"无法连接 LLM 服务（{self._settings.llm_base_url}），请检查网络或 BASE_URL 配置。",
                status_code=502,
            ) from exc

        if not resp.choices:
            raise LLMError("LLM 返回了空的选择列表。", status_code=502)
        return resp.choices[0].message.content or ""


def extract_json(text: str) -> Any:
    """从模型输出中尽力解析出 JSON。

    容忍三种常见污染：
    1. 被 ```json ... ``` 围栏包裹
    2. 前后有多余的解释性文字
    3. JSON 后面跟了多余的说明
    """
    if text is None:
        raise ValueError("待解析文本为 None")

    cleaned = text.strip()

    # 剥离 Markdown 代码围栏
    if cleaned.startswith("```"):
        first_newline = cleaned.find("\n")
        if first_newline != -1:
            cleaned = cleaned[first_newline + 1 :]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
        cleaned = cleaned.strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 退而求其次：截取第一个 { 或 [ 到与之匹配的结尾
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = cleaned.find(open_ch)
        end = cleaned.rfind(close_ch)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                continue

    raise ValueError(f"无法从模型输出中解析 JSON，前 120 字符：{cleaned[:120]!r}")


# 全局单例。Agent Runtime 后续直接复用，避免重复建连接。
llm_gateway = LLMGateway()


def build_fast_gateway() -> LLMGateway | None:
    """判断类任务用的轻量网关。

    **没配置就返回 None** —— 调用方据此回落到主网关，行为与接入前完全一致。
    这样"分模型"是一个可开关的优化，而不是新的必要条件：
    `.env` 里删掉 `LLM_MODEL_FAST` 就退回单模型。
    """
    model = (default_settings.llm_model_fast or "").strip()
    if not model or model == default_settings.llm_model:
        return None
    return LLMGateway(default_settings.model_copy(update={"llm_model": model}))


#: 轻量网关；为 None 时所有调用走主网关。
fast_llm_gateway = build_fast_gateway()

# --------------------------------------------------------------------------- #
# 视觉网关
# --------------------------------------------------------------------------- #
def vision_gateway() -> LLMGateway:
    """返回一个用**视觉模型**的网关。

    与 `llm_model_fast` 是同一个模式：不新建连接池配置，
    只是把 `llm_model` 换掉。这样超时、密钥、base_url 全部继承，
    不会出现"视觉模型走了另一套配置"这种分裂。

    没配 `llm_vision_model` 时返回主网关 —— 调用方应当先用
    `settings.llm_supports_vision` 判断，而不是靠这里的兜底。
    """
    model = (default_settings.llm_vision_model or "").strip()
    if not model or model == default_settings.llm_model:
        return llm_gateway
    return LLMGateway(default_settings.model_copy(update={"llm_model": model}))
