"""Embedding provider 抽象与云端实现。

设计要点：

1. **批量、截断、重试、分批的编排放在基类**，子类只实现 `_embed_batch()`。
   这样"分几批、每批几条、超长怎么办、失败重不重试"这套逻辑只写一遍，
   mock 与真实实现共用，测试也只需覆盖一份。

2. **Key 只从环境变量读取**（经 `settings`），绝不硬编码，也绝不出现在日志与响应里。

3. **错误码是显式的**，调用方据此决定 HTTP 状态码与用户提示。
   尤其 `dim_mismatch` —— 返回维度与配置不符属于配置错误，必须让人看见，
   而不是静默写入一批错维度的向量让检索悄悄失效。

4. **mock provider 是确定性向量化，不是返回随机数**。
   它用字符二元组做哈希向量化（bag-of-bigrams）+ L2 归一化，
   因此"内容相近的文本余弦距离更小"这一性质在 mock 下同样成立 ——
   否则检索相关的测试全部失去意义。
"""

from __future__ import annotations

import asyncio
import math
import re
import time
import zlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import httpx

from app.core.config import Settings, settings as default_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_WS = re.compile(r"\s+")

#: mock 向量与均匀单位向量混合的比例。取值依据见 `MockEmbeddingProvider.hash_vector`
#: 的文档：让 mock 的距离尺度与真实 embedding 可比，门槛逻辑才可验证。
MOCK_UNIFORM_BIAS = 0.45


class EmbeddingErrorCode(StrEnum):
    NOT_CONFIGURED = "not_configured"
    UNSUPPORTED = "unsupported"
    TIMEOUT = "timeout"
    HTTP_ERROR = "http_error"
    PARSE_ERROR = "parse_error"
    DIM_MISMATCH = "dim_mismatch"
    EMPTY = "empty"


class EmbeddingError(RuntimeError):
    """带明确错误码的向量化异常。"""

    def __init__(
        self,
        message: str,
        *,
        code: EmbeddingErrorCode,
        status_code: int | None = None,
        batch_index: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        #: 第几批失败（从 1 开始）。定位问题时要紧 —— 一份 100 块的资料会分成 10 批，
        #: 只说"失败了"帮不上忙，说"第 7 批失败"能直接去看那 10 条文本。
        self.batch_index = batch_index

    def __str__(self) -> str:
        where = f"（第 {self.batch_index} 批）" if self.batch_index else ""
        return f"[{self.code}]{where} {self.message}"


@dataclass
class EmbeddingResult:
    """一次向量化的结果。"""

    vectors: list[list[float]]
    model: str
    dim: int
    batch_count: int = 0
    elapsed_ms: int = 0
    usage: dict[str, Any] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.vectors)


class EmbeddingProvider(ABC):
    """向量化提供方。子类只需要实现 `_embed_batch()`。"""

    def __init__(self, config: Settings | None = None) -> None:
        self._settings = config or default_settings

    # ------------------------------------------------------------ 子类实现
    @property
    @abstractmethod
    def name(self) -> str:
        """供应商标识，写进日志与能力接口。"""

    @property
    @abstractmethod
    def model(self) -> str:
        """模型名。"""

    @property
    @abstractmethod
    def dim(self) -> int:
        """向量维度。"""

    @abstractmethod
    def available(self) -> bool:
        """是否具备调用条件。"""

    @abstractmethod
    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """调用一次接口，返回与输入等长的向量列表。失败抛 EmbeddingError。"""

    # -------------------------------------------------------------- 编排逻辑
    @property
    def batch_size(self) -> int:
        return max(1, self._settings.embedding_batch_size)

    @property
    def max_chars(self) -> int:
        return max(1, self._settings.embedding_max_chars)

    def capabilities(self) -> dict[str, Any]:
        """供能力探测接口使用。**只暴露布尔值与参数，绝不回显 Key。**"""
        return {
            "provider": self.name,
            "model": self.model,
            "dim": self.dim,
            "configured": self.available(),
            "batch_size": self.batch_size,
            "max_chars": self.max_chars,
            "timeout_s": self._settings.embedding_timeout_s,
        }

    def _prepare(self, texts: list[str]) -> list[str]:
        """清洗与截断。空文本替换为占位符 —— 供应商通常不接受空串。"""
        prepared: list[str] = []
        for text in texts:
            cleaned = (text or "").strip()
            if not cleaned:
                cleaned = "（空内容）"
            if len(cleaned) > self.max_chars:
                cleaned = cleaned[: self.max_chars]
            prepared.append(cleaned)
        return prepared

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        """向量化一批文本。内部自动分批。"""
        if not texts:
            return EmbeddingResult(vectors=[], model=self.model, dim=self.dim)
        if not self.available():
            raise EmbeddingError(
                f"未配置 {self.name} 的调用凭据，无法向量化。"
                "请设置 EMBEDDING_API_KEY 与 EMBEDDING_BASE_URL。",
                code=EmbeddingErrorCode.NOT_CONFIGURED,
            )

        prepared = self._prepare(texts)
        started = time.perf_counter()
        vectors: list[list[float]] = []
        batch_count = 0

        for start in range(0, len(prepared), self.batch_size):
            batch = prepared[start : start + self.batch_size]
            batch_index = batch_count + 1
            batch_count += 1
            # 串行而非并发：供应商普遍按 QPS 限流，并发只会换来一串 429。
            result = await self._with_retry(batch, batch_index=batch_index)
            if len(result) != len(batch):
                raise EmbeddingError(
                    f"返回 {len(result)} 条向量，与请求的 {len(batch)} 条不一致，"
                    "返回体不可信。",
                    code=EmbeddingErrorCode.PARSE_ERROR,
                    batch_index=batch_index,
                )
            for vector in result:
                if len(vector) != self.dim:
                    raise EmbeddingError(
                        f"向量维度为 {len(vector)}，与配置的 EMBEDDING_DIM={self.dim} 不符。"
                        "这是配置错误，请核对模型与维度设置。",
                        code=EmbeddingErrorCode.DIM_MISMATCH,
                        batch_index=batch_index,
                    )
            vectors.extend(result)

        return EmbeddingResult(
            vectors=vectors,
            model=self.model,
            dim=self.dim,
            batch_count=batch_count,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )

    async def _with_retry(self, batch: list[str], *, batch_index: int) -> list[list[float]]:
        """失败重试。只重试"可能是偶发"的失败（超时、5xx、429），4xx 立刻放弃。"""
        attempts = max(0, self._settings.embedding_max_retries) + 1
        last: EmbeddingError | None = None

        for attempt in range(1, attempts + 1):
            try:
                return await self._embed_batch(batch)
            except EmbeddingError as exc:
                last = exc
                retryable = exc.code == EmbeddingErrorCode.TIMEOUT or (
                    exc.status_code is not None and (exc.status_code >= 500 or exc.status_code == 429)
                )
                if not retryable or attempt == attempts:
                    exc.batch_index = batch_index
                    raise
                logger.warning(
                    "%s 第 %d 批第 %d 次尝试失败（%s），稍后重试",
                    self.name,
                    batch_index,
                    attempt,
                    exc.code,
                )
                await asyncio.sleep(0.5 * attempt)

        assert last is not None  # pragma: no cover - 循环必然返回或抛出
        raise last


# --------------------------------------------------------------------------- #
# 云端实现（OpenAI 兼容协议）
# --------------------------------------------------------------------------- #
class APIEmbeddingProvider(EmbeddingProvider):
    """云端 embedding。按 OpenAI 兼容的 `/embeddings` 协议调用。

    默认指向阿里云百炼（DashScope）的兼容模式，但**任何同协议的供应商都可以**
    （硅基流动、智谱、OpenAI…），只需改 `EMBEDDING_BASE_URL` 与 `EMBEDDING_MODEL`。
    """

    def __init__(
        self,
        config: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(config)
        #: 测试注入 MockTransport，从而完全不打网络
        self._transport = transport

    @property
    def name(self) -> str:
        return "api"

    @property
    def model(self) -> str:
        return self._settings.embedding_model

    @property
    def dim(self) -> int:
        return self._settings.embedding_dim

    @property
    def endpoint(self) -> str:
        return f"{self._settings.embedding_base_url.rstrip('/')}/embeddings"

    def available(self) -> bool:
        return bool(self._settings.embedding_api_key.strip()) and bool(
            self._settings.embedding_base_url.strip()
        )

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not self.available():
            raise EmbeddingError(
                "未配置 EMBEDDING_API_KEY 或 EMBEDDING_BASE_URL。",
                code=EmbeddingErrorCode.NOT_CONFIGURED,
            )

        payload: dict[str, Any] = {
            "model": self.model,
            "input": texts,
            "encoding_format": "float",
        }
        # text-embedding-v3 支持显式指定维度；不支持的供应商会返回 400，
        # 错误信息里会带上响应体，便于定位。
        if self.dim > 0:
            payload["dimensions"] = self.dim

        headers = {
            "Authorization": f"Bearer {self._settings.embedding_api_key.strip()}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(
                timeout=self._settings.embedding_timeout_s, transport=self._transport
            ) as client:
                response = await client.post(self.endpoint, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise EmbeddingError(
                f"向量化请求超时（>{self._settings.embedding_timeout_s}s）。",
                code=EmbeddingErrorCode.TIMEOUT,
            ) from exc
        except httpx.HTTPError as exc:
            raise EmbeddingError(
                f"向量化请求失败：{type(exc).__name__}", code=EmbeddingErrorCode.HTTP_ERROR
            ) from exc

        if response.status_code >= 400:
            raise EmbeddingError(
                f"向量化接口返回 HTTP {response.status_code}：{response.text[:200]}",
                code=EmbeddingErrorCode.HTTP_ERROR,
                status_code=response.status_code,
            )

        try:
            body = response.json()
            items = body.get("data") or []
        except Exception as exc:  # noqa: BLE001
            raise EmbeddingError(
                f"向量化返回体无法解析：{type(exc).__name__}",
                code=EmbeddingErrorCode.PARSE_ERROR,
            ) from exc

        if not items:
            raise EmbeddingError("向量化接口没有返回任何数据。", code=EmbeddingErrorCode.EMPTY)

        # 按 index 排序：**不少供应商返回的 data 顺序与输入顺序不一致**，
        # 直接按返回顺序使用会把向量和文本错配 —— 这类错误检索时完全不报错，
        # 只会静默地给出错误答案，属于最难排查的一类问题。
        ordered: list[tuple[int, list[float]]] = []
        for position, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            raw = item.get("embedding")
            if not isinstance(raw, list) or not raw:
                continue
            index = item.get("index")
            ordered.append((int(index) if isinstance(index, int) else position, raw))
        ordered.sort(key=lambda pair: pair[0])

        if not ordered:
            raise EmbeddingError(
                "向量化返回体里没有可用的 embedding 字段。", code=EmbeddingErrorCode.PARSE_ERROR
            )
        return [vector for _, vector in ordered]


# --------------------------------------------------------------------------- #
# Mock 实现（离线、确定性）
# --------------------------------------------------------------------------- #
class MockEmbeddingProvider(EmbeddingProvider):
    """本地确定性向量化，供离线测试与无 Key 时的链路回归。

    **不是随机数**：用字符二元组做哈希向量化 + L2 归一化，所以
    "内容相近的文本余弦距离更小"在 mock 下依然成立。
    这一点很重要 —— 否则所有与检索排序有关的测试都失去意义，
    测试会变成"只要能跑通就行"的摆设。
    """

    @property
    def name(self) -> str:
        return "mock"

    @property
    def model(self) -> str:
        return f"mock-{self._settings.embedding_model}"

    @property
    def dim(self) -> int:
        return self._settings.embedding_dim

    def available(self) -> bool:
        return True

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.hash_vector(text, self.dim) for text in texts]

    @staticmethod
    def hash_vector(text: str, dim: int) -> list[float]:
        """字符哈希向量化 + 均匀分量偏置 + L2 归一化。纯函数，输出确定。

        这套构造的唯一目的是让**距离尺度与真实 embedding 可比**，
        否则 `RAG_MAX_DISTANCE` 这道门槛在 mock 下要么全放行、要么全拦下，
        门槛逻辑就无从验证。经过两次实测校准：

        **第一版：把二元组换成单字符。**
        二元组太稀疏，连相关块的距离都逼近 1.0，会被门槛全部滤掉。

        **第二版：再加均匀分量偏置（当前）。**
        只用单字符时，相关块距离仍在 0.60 上下，而真实模型只有 0.15~0.29 ——
        差距来自"查询短、段落长、稀疏哈希几乎没有重叠"。
        把向量与均匀单位向量按 `1-b : b` 混合再归一化，相当于给所有文本叠一层共同的
        "背景语义"：两两余弦整体上移、距离整体下移，尺度就对上了。

        实测对照（查询「进程和线程有什么区别」对相关块的最近距离 / 无关查询的最近距离）：

        | 构造 | 相关块 | 无关查询 | 阈值 0.40 可用？ |
        |---|---|---|---|
        | 单字符，无偏置 | 0.605 | 0.890 | ✗ 相关块本身就被滤掉 |
        | 单字符，b=0.45 | **0.344** | **0.457** | ✓ |
        | 真实 text-embedding-v3 | 0.145~0.286 | 0.415~0.577 | （基准） |

        b 的可用区间约 0.42~0.48：再小则相关块被滤掉，再大则无关查询也会混进来。取 0.45 居中。
        """
        vector = [0.0] * max(1, dim)
        normalized = _WS.sub("", text or "")
        if not normalized:
            return vector

        for gram in list(normalized) or [normalized]:
            index = zlib.crc32(gram.encode("utf-8")) % len(vector)
            vector[index] += 1.0

        norm = math.sqrt(sum(value * value for value in vector))
        if norm <= 0:
            return vector
        vector = [value / norm for value in vector]

        # 均匀单位向量的每个分量是 1/sqrt(dim)，因此它自身的模长正好是 1。
        uniform = 1.0 / math.sqrt(len(vector))
        mixed = [
            (1.0 - MOCK_UNIFORM_BIAS) * value + MOCK_UNIFORM_BIAS * uniform
            for value in vector
        ]
        mixed_norm = math.sqrt(sum(value * value for value in mixed))
        if mixed_norm <= 0:
            return vector
        return [value / mixed_norm for value in mixed]


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #
def get_embedder(
    config: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> EmbeddingProvider:
    """按配置返回 embedding provider。

    刻意**不做"没配 Key 就自动降级为 mock"**：那样会静默写入一批无意义的向量，
    用户以为 RAG 在工作，实际检索结果毫无意义 —— 比直接报错危险得多。
    没有 Key 就是没配置，接口明确返回 409 并提示去配。
    """
    cfg = config or default_settings
    provider = cfg.embedding_provider

    if provider == "mock":
        return MockEmbeddingProvider(cfg)
    if provider == "api":
        return APIEmbeddingProvider(cfg, transport=transport)
    if provider == "local":
        raise EmbeddingError(
            "EMBEDDING_PROVIDER=local 需要本地模型（sentence-transformers），P3 未实现。"
            "请改用 api（云端）或 mock（离线）。",
            code=EmbeddingErrorCode.UNSUPPORTED,
        )
    raise EmbeddingError(
        f"未知的 EMBEDDING_PROVIDER：{provider}", code=EmbeddingErrorCode.UNSUPPORTED
    )


#: 默认单例。业务层直接用它；测试注入自定义 provider。
embedder: EmbeddingProvider = get_embedder()
