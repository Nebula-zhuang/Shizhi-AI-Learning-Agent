"""全局配置中心。

所有可变配置一律来自环境变量 / .env 文件，代码中不允许出现任何密钥、口令、
地址的硬编码。新增配置项时，请同步更新项目根目录的 .env.example。

用法：
    from app.core.config import settings
    settings.llm_model
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus, urlsplit, urlunsplit

from pydantic import Field, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 目录定位：app/core/config.py -> app/core -> app -> api -> apps -> <项目根>
API_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = Path(__file__).resolve().parents[4]
DATA_DIR = PROJECT_ROOT / "data"


def _mask_uri(uri: str) -> str:
    """把连接串中的口令替换为 ***，用于日志输出。"""
    try:
        parts = urlsplit(uri)
        if parts.password:
            host = parts.hostname or ""
            netloc = f"{parts.username}:***@{host}"
            if parts.port:
                netloc += f":{parts.port}"
            return urlunsplit(
                (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
            )
    except Exception:  # noqa: BLE001 - 脱敏失败时退回原串
        pass
    return uri


class Settings(BaseSettings):
    """应用配置。字段名大小写不敏感，环境变量同名即可覆盖。"""

    model_config = SettingsConfigDict(
        # 依次尝试：项目根 .env -> apps/api/.env -> 真实环境变量（后者优先级最高）
        env_file=(PROJECT_ROOT / ".env", API_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------ 应用
    app_name: str = "Learning Buddy API"
    app_env: Literal["dev", "test", "prod"] = "dev"
    debug: bool = True
    api_prefix: str = "/api"
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"

    # 允许跨域的前端来源，逗号分隔
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # ------------------------------------------------------------------ LLM
    # 走 OpenAI 兼容协议，指向任意一家即可（DeepSeek / 通义 / 智谱 / OpenAI）
    llm_mode: Literal["auto", "live", "mock"] = "auto"
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_temperature: float = 0.7
    llm_max_tokens: int = 1024
    llm_timeout: float = 60.0

    # ------------------------------------------------------------- 数据库
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: str = ""
    mysql_database: str = "learning_buddy"
    mysql_charset: str = "utf8mb4"

    # 可选：直接给完整连接串（优先级最高，便于切库 / 测试）
    database_url: str = ""

    # ----------------------------------------------------------- 向量数据库
    chroma_persist_dir: str = str(DATA_DIR / "chroma")
    chroma_collection: str = "learning_buddy_chunks"

    # ----------------------------------------------------------- Embedding（P3）
    # P0 登记，P3 真正使用。
    # provider="api" 走云端 OpenAI 兼容 /embeddings（默认阿里云百炼 DashScope）；
    # provider="mock" 返回由文本确定的本地向量，供离线测试与无 Key 时回归，**绝不联网**。
    embedding_provider: Literal["api", "local", "mock"] = "api"
    embedding_model: str = "text-embedding-v3"
    embedding_api_key: str = ""
    embedding_base_url: str = ""
    embedding_dim: int = 1024

    # 单请求最多嵌入多少条。DashScope text-embedding-v3 的上限是 10，
    # 换供应商时按对方文档调整；超限会被服务端直接拒绝。
    embedding_batch_size: int = 10
    embedding_timeout_s: float = 30.0
    # 单条文本超过该长度先截断 —— 超长请求会返回 400，截断比失败更实用。
    embedding_max_chars: int = 2000
    # 失败后重试次数（仅对超时与 5xx 重试，4xx 不重试）
    embedding_max_retries: int = 2

    # --------------------------------------------------------------- RAG（P3）
    # 检索返回的片段数
    rag_top_k: int = 5
    # 拼接进提示词的上下文总长度上限，超出按相似度截断
    rag_max_context_chars: int = 6000
    # 余弦距离上限。Top-K 永远会返回 K 条，没有这道门槛的话，
    # 问一个资料里根本没有的问题也会检索出 K 条无关片段，模型只能硬编答案。
    #
    # **这个值是实测校准出来的，不是拍的。** 用真实 embedding（text-embedding-v3）
    # 在两份语料上测距的结果（scripts/calibrate_rag.py）：
    #
    #   | 语料 | 资料内问题·最近距离 | 资料外问题·最近距离 |
    #   |---|---|---|
    #   | 9 块的课程讲义 | 0.145 / 0.178 / 0.261 | 0.577 / 0.596 / 0.642 |
    #   | 120 块的真实论文 | 0.226 / 0.246 / 0.286 | 0.415 / 0.546 / 0.568 |
    #
    # 合起来看：资料内最大 0.286，资料外最小 0.415。取偏上界一点的 0.40 ——
    # **刻意偏向"宁可多留一条，也不要误杀相关内容"**：
    # 误杀会让用户问一个资料里确实有的问题却被告知"不在你的资料中"（可见的坏体验），
    # 而多留一条无关片段还有提示词里"只依据片段作答"那条规则兜着。
    #
    # 换 embedding 模型后应当重新校准，别直接沿用这个数。
    rag_max_distance: float = 0.40
    # 事实问答要稳，不要发散
    rag_temperature: float = 0.2
    rag_max_tokens: int = 1500
    rag_max_concurrency: int = 1

    # ------------------------------------------------- 外部工具（P2 接入）
    tavily_api_key: str = ""
    serper_api_key: str = ""

    # ------------------------------------------- 联网搜索后端选择（阶段2）
    #
    #   auto   优先 MCP，运行时故障（连不上/401/超时）回退 Tavily 并**上报回退**
    #   mcp    只用 MCP。失败就明确失败，不偷偷换（便于验证 MCP 真的在工作）
    #   tavily 只用 Tavily，**不发起任何 MCP 请求**
    #
    # 默认 auto 的理由：用户不关心走哪条路，但**系统必须知道** ——
    # 所以 auto 的回退不是静默的，它会带 fallback_reason 上报。
    web_search_backend: str = "auto"

    # --------------------------------------------- Tavily 官方 MCP Server
    # 用远程端点而不是本地 npx：
    #   · 远程端点已实测可用（initialize / tools/list / tools/call 全部成功）
    #   · 不需要本机跑 Node 子进程，绕开了本项目已知的 npm 环境问题
    mcp_web_search_url: str = "https://mcp.tavily.com/mcp/"
    mcp_timeout_s: float = 20.0

    # ------------------------------------------- Agent Loop 三条硬限制
    #
    # 缺一不可：
    #   · 只有调用数限制 → 模型可以反复"不调工具也不回答"，空转
    #   · 只有步数限制   → 一步里并发调多个工具，成本失控
    #   · 没有总时限     → 每次调用都很快但次数多，用户干等
    #
    # 超限不是报错，是降级到 FINAL_ANSWER（与 P4"无论如何都给回复"一致）。
    loop_max_steps: int = 6
    loop_max_tool_calls: int = 3
    loop_timeout_s: float = 30.0

    # ------------------------------------------- 模型能力声明
    #
    # ⚠️ **默认 False，因为这必须是显式开启的。**
    #
    # 实测：当前配置的模型（DeepSeek）**不支持视觉** ——
    # 图片按 OpenAI 多模态格式发过去，接口不报错，
    # 但模型回一句我无法查看或分析图片。
    #
    # 这种不报错但也没看图的情况特别危险：工具会返回 ok=True，
    # Agent 拿到一个没用的结果，还会白转好几轮。
    # 所以**能力必须显式声明**，而不是靠发过去就知道行不行。
    #
    # 要打开这个能力，请把 LLM 换成支持视觉的模型
    # （Qwen-VL / GPT-4o / GLM-4V 等），然后设为 true。
    #: 视觉任务单独用的模型。为空表示不支持视觉。
    #:
    #: **不整体把主模型换成 VL 模型**：那会让每一轮纯文本对话都付 VL 的价钱，
    #: 而图只在"用户真的传了"的时候才出现。网关里 `llm_model_fast` 是同一个模式。
    llm_vision_model: str = ""

    #: 视觉能力开关。**默认 False，必须按部署显式声明。**
    #:
    #: 不能靠"发过去看行不行"：实测不支持视觉的模型**接口不报错**，
    #: 只是回一句"我看不到图"，工具于是返回 ok=True 的废结果，
    #: Agent 还会白转好几轮。
    #:
    #: 打开条件：`llm_vision_model` 已配置且经实测能读图。
    llm_supports_vision: bool = False

    # ----------------------------------------------------------- 文件上传
    # 单文件上限（MB）。
    #
    # 300 是"本地单机 + 分块落盘"下能稳住的量级，不是拍脑袋的大数：
    #   · 后端**流式写盘**（见 `storage.stream_to_temp`），内存占用固定在 1MB 量级，
    #     所以上限取决于磁盘而不是内存；
    #   · 但解析阶段仍会把这个文件整份交给 PyMuPDF / OCR，
    #     300MB 的扫描件在解析时本身就会吃掉可观内存，所以不宜再往上抬。
    # 前端 `MAX_UPLOAD_MB` 必须与此保持一致（不一致会出现"前端放行、后端拒绝"）。
    max_upload_mb: int = 300
    upload_dir: str = str(DATA_DIR / "uploads")

    # ------------------------------------------------- P1 摄取流水线
    # 后台同时处理的文档数上限，防止连续上传打爆 LLM 限流
    ingest_max_concurrency: int = 2

    # 单文档最多允许多少个抽取批次（每批约 `extract_batch_chunks` 个文本块）。
    #
    # **这是与 `max_upload_mb` 无关的第二个上限，别把两者混为一谈。**
    # 上传上限管的是"能不能收下这个文件"；这个管的是
    # "收下之后要调多少次模型" —— 后者才是真正花时间和花钱的地方。
    #
    # 300 批 × 每批 4 块 ≈ 1200 个文本块 ≈ 两三百万字，
    # 已经远超一门课一学期的材料量。PDF 另有 `max_document_pages` 兜着，
    # 这一项主要防的是超大 docx / md / txt。
    max_extract_batches: int = 300
    # 单文档页数上限（超限在上传阶段直接拒绝）
    max_document_pages: int = 50

    # 分块参数，取值理由见 skills/doc-ingestion/SKILL.md
    chunk_target_chars: int = 600
    chunk_max_chars: int = 1200
    chunk_min_chars: int = 150
    chunk_overlap_chars: int = 80

    # 抽取批次参数，取值理由见 skills/knowledge-extraction/SKILL.md
    extract_batch_chunks: int = 4
    extract_batch_max_chars: int = 4000
    extract_max_kp_per_batch: int = 5
    extract_llm_max_tokens: int = 3000
    # 结构化输出解析失败时的重试次数
    llm_json_retry: int = 1

    # PDF 内嵌图片提取
    image_extract_enabled: bool = True
    # 小于该边长（像素）的图片视为图标/分隔线，跳过
    image_min_size_px: int = 80

    # ------------------------------------------------- P2 关系构建
    # 低于该置信度的关系不落库。宁可少连边，也不要噪声边污染图谱。
    relation_min_confidence: float = 0.50
    # 是否用模型对规则产出的 prerequisite 候选做一次确认。
    # 注意：模型只能保留或删除候选边，不能新增 —— 保证「无依据不连边」不被突破。
    relation_llm_refine: bool = False
    relation_refine_max_candidates: int = 20
    relation_llm_max_tokens: int = 1500

    # ------------------------------------------------- P2 可信度校验
    # 总开关。关掉后 /verify 接口返回 409，不影响其它功能。
    verify_enabled: bool = True
    # 联网核验开关。即便打开，没有配置 TAVILY_API_KEY 也会整层跳过（记 skipped）。
    verify_web_enabled: bool = True
    # L2 模型自评的触发门槛：importance >= 该值，或 L1 已报警
    verify_model_importance_threshold: int = 4
    verify_llm_max_tokens: int = 1200
    # 单文档联网核验次数上限，防止一次点按钮把搜索配额打光
    verify_web_max_per_document: int = 8
    # 校验执行的并发上限（独立于摄取并发，两者互不挤占）
    verify_max_concurrency: int = 1

    # ------------------------------------------------- P2 联网检索（Tavily）
    tavily_max_results: int = 5
    tavily_timeout_s: float = 10.0
    # basic 更便宜，advanced 更准
    tavily_search_depth: str = "basic"
    # 单次搜索返回内容的截断长度，避免把长正文灌进 evidence
    tavily_snippet_chars: int = 300

    # ------------------------------------------------------ 空值兜底校验
    # .env 里写 `CHROMA_PERSIST_DIR=` 会把字段读成空串并冲掉默认值，
    # 这里统一把空值还原为按项目根推导的默认路径。
    @field_validator("chroma_persist_dir", mode="after")
    @classmethod
    def _default_chroma_dir(cls, v: str) -> str:
        return v.strip() or str(DATA_DIR / "chroma")

    @field_validator("upload_dir", mode="after")
    @classmethod
    def _default_upload_dir(cls, v: str) -> str:
        return v.strip() or str(DATA_DIR / "uploads")

    # ------------------------------------------------------------ 认证与用户
    #: JWT 签名密钥。**必须放在 .env 里**，不要写进代码或文档。
    #: 代码里留空是"没配置时的兜底"——启动时会打警告，绝不静默用弱密钥。
    #: 判断类任务（作答评估、决策）用的轻量模型。留空 = 与主模型相同（保持原行为）。
    #: 依据：这两步输出短、模式固定，实测轻量模型快 2.5–3.4 倍，
    #: 而判定差异只出现在 vague / not_mastered 这条本就模糊的边界上。
    llm_model_fast: str = ""

    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    #: 访问令牌有效期（分钟）。默认 7 天：学习产品里"隔天再来"是常态，
    #: 太短会让人反复登录。安全与体验的折中，可用环境变量收紧。
    jwt_expire_minutes: int = 60 * 24 * 7
    #: 令牌写进 Cookie 时是否只在 HTTPS 下发送。
    #: 本机 http 演示必须是 false；生产必须为 true。
    cookie_secure: bool = False
    #: 对外暴露的前端地址，用于登录成功后重定向与 Cookie 域判断。
    frontend_base_url: str = "http://localhost:5173"

    # ---------------------------------------------------- 演示账号（仅播种用）
    #: `scripts/seed_demo_user.py` 读它建演示账号，**代码里不写死口令**。
    #: 空着就拒绝播种 —— 默认口令是账号体系最典型的失守点。
    demo_username: str = ""
    demo_password: str = ""

    # ---------------------------------------------------------------- 派生
    @computed_field  # type: ignore[prop-decorator]
    @property
    def cors_origin_list(self) -> list[str]:
        """把逗号分隔的来源串解析为列表。"""
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_llm_configured(self) -> bool:
        """是否具备真实调用 LLM 的条件。"""
        return bool(self.llm_api_key.strip())

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_tavily_configured(self) -> bool:
        """是否配置了 Tavily Key。**只暴露布尔值，不暴露 Key 本身。**"""
        return bool(self.tavily_api_key.strip())

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_embedding_configured(self) -> bool:
        """云端 embedding 是否具备调用条件。mock provider 视为"可用"。

        **只暴露布尔值，绝不回显 Key。**
        """
        if self.embedding_provider == "mock":
            return True
        return bool(self.embedding_api_key.strip()) and bool(self.embedding_base_url.strip())

    @property
    def effective_web_verify(self) -> bool:
        """联网核验是否真正生效。

        需要**同时**满足：开关打开 且 Key 已配置。
        未配置 Key 时 L3 整层记 skipped（说明原因），而不是报错 —— 这样
        「没配 Key」与「配了但调用失败」在界面上是两种完全不同的状态。
        """
        return self.verify_web_enabled and self.is_tavily_configured

    @property
    def effective_llm_mode(self) -> Literal["live", "mock"]:
        """实际生效的 LLM 模式。

        mock 模式用于在没有 API Key 时验证"接口 / SSE / 前端渲染"整条链路，
        不会发起任何外部请求。
        """
        if self.llm_mode == "mock":
            return "mock"
        if self.llm_mode == "live":
            return "live"
        return "live" if self.is_llm_configured else "mock"

    @property
    def sqlalchemy_database_uri(self) -> str:
        """SQLAlchemy 连接串。password 做 URL 编码，避免特殊字符炸掉 DSN。"""
        if self.database_url.strip():
            return self.database_url.strip()
        pwd = quote_plus(self.mysql_password)
        user = quote_plus(self.mysql_user)
        return (
            f"mysql+pymysql://{user}:{pwd}@{self.mysql_host}:{self.mysql_port}"
            f"/{self.mysql_database}?charset={self.mysql_charset}"
        )

    @property
    def sqlalchemy_database_uri_masked(self) -> str:
        """用于日志与健康检查输出的脱敏连接串。

        会真实反映当前生效的连接目标（含 DATABASE_URL 覆盖的情况），
        但把口令替换为 ***。
        """
        if self.database_url.strip():
            return _mask_uri(self.database_url.strip())
        return (
            f"mysql+pymysql://{self.mysql_user}:***@{self.mysql_host}:{self.mysql_port}"
            f"/{self.mysql_database}?charset={self.mysql_charset}"
        )

    @property
    def sqlalchemy_server_uri(self) -> str:
        """不带库名的连接串，用于「建库」这类需要连到实例级别的场景。"""
        pwd = quote_plus(self.mysql_password)
        user = quote_plus(self.mysql_user)
        return (
            f"mysql+pymysql://{user}:{pwd}@{self.mysql_host}:{self.mysql_port}"
            f"/?charset={self.mysql_charset}"
        )


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """带缓存的配置单例。测试中可用 get_settings.cache_clear() 重置。"""
    return Settings()


settings = get_settings()
