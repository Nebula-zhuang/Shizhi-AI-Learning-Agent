"""FastAPI 应用入口。

启动方式（在 apps/api 目录下）：
    uvicorn app.main:app --reload --port 8000
或：
    python -m app.main
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__ as APP_VERSION
from app.api.routes import auth as auth_routes
from app.api.routes import chat, health
from app.api.routes import documents as documents_routes
from app.api.routes import graph as graph_routes
from app.api.routes import knowledge as knowledge_routes
from app.api.routes import rag as rag_routes
from app.api.routes import tutor as tutor_routes
from app.api.routes import study as study_routes
from app.api.routes import todos as todos_routes
from app.core.config import settings
from app.core.llm import llm_gateway
from app.core.logging import get_logger, setup_logging
from app.services import document_service, index_runner, ingest_runner, verify_runner

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期钩子。

    启动时只做轻量初始化（打印运行配置）；
    对 MySQL / Chroma 的连通性探测放在 /api/health，避免依赖不可用时拖垮启动。
    """
    setup_logging()
    describe = llm_gateway.describe()
    logger.info("=" * 68)
    logger.info("%s v%s 启动中（env=%s）", settings.app_name, APP_VERSION, settings.app_env)
    logger.info("LLM 模式：%s | 模型：%s", describe["mode"], describe["model"])
    logger.info("LLM 网关：%s", describe["base_url"])
    logger.info("数据库：%s", settings.sqlalchemy_database_uri_masked)
    logger.info("向量库：%s", settings.chroma_persist_dir)
    logger.info("接口前缀：%s | 文档：http://%s:%d/docs", settings.api_prefix, settings.host, settings.port)
    logger.info("=" * 68)
    if describe["mode"] == "mock":
        logger.warning(
            "当前为 mock 模式：未检测到 LLM_API_KEY，模型输出为本地模拟内容（用于链路验证）。"
        )

    # 联网核验是否生效 —— **必须在启动时就说清楚**。
    #
    # 这一层没开的时候，助教是被明确禁止声称"我上网查过"的
    # （见 services/verify_voice.py）。如果启动日志不提这件事，
    # 演示时看到助教说"这部分不在你的资料里"，很容易被误判成
    # "模型能力不行"，而实际上是这一层根本没配 Key。
    if settings.effective_web_verify:
        logger.info("联网核验：已启用（Tavily）")
    else:
        reason = "开关关闭" if not settings.verify_web_enabled else "未配置 TAVILY_API_KEY"
        logger.warning(
            "联网核验：未生效（%s）—— L3 整层记 skipped，助教不会声称查过外部资料。", reason
        )

    # 清理因上次进程退出而中断的摄取任务，避免文档永久停留在中间状态
    stale = document_service.cleanup_stale_documents()
    if stale:
        logger.warning("已把 %d 个被中断的文档标记为失败，用户可重新处理。", stale)

    # 清理上传中转目录的残骸。
    # 进程被强杀时 `discard_temp` 不会执行，磁盘上会留下 `.part` 文件 ——
    # 300MB 上限下这些残骸很占地方，所以每次启动都扫一遍。
    from app.ingestion import storage

    storage.cleanup_temp_files()

    yield

    await ingest_runner.shutdown()
    await verify_runner.shutdown()
    await index_runner.shutdown()
    await llm_gateway.aclose()
    logger.info("%s 已关闭", settings.app_name)


app = FastAPI(
    title=settings.app_name,
    version=APP_VERSION,
    description=(
        "面向大学生学习场景的多模态学习智能体后端。"
        "P0：地基与最小 LLM 流式闭环；P1：资料解析与知识点抽取。"
    ),
    lifespan=lifespan,
)

# 跨域。开发期前端通过 Vite 代理访问，同源无需 CORS；
# 这里保留是为了直连调试与后续独立部署。
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# P6：账号与登录
app.include_router(auth_routes.router, prefix=settings.api_prefix)
app.include_router(health.router, prefix=settings.api_prefix)
app.include_router(chat.router, prefix=settings.api_prefix)
# P1：资料摄取与知识点
app.include_router(documents_routes.router, prefix=settings.api_prefix)
app.include_router(knowledge_routes.documents_router, prefix=settings.api_prefix)
app.include_router(knowledge_routes.knowledge_router, prefix=settings.api_prefix)
# P2：知识图谱、关系构建、可信度校验
app.include_router(graph_routes.documents_router, prefix=settings.api_prefix)
app.include_router(graph_routes.knowledge_router, prefix=settings.api_prefix)
app.include_router(graph_routes.verify_router, prefix=settings.api_prefix)
# P3：向量索引与最小 RAG
app.include_router(rag_routes.router, prefix=settings.api_prefix)
# P4：Tutor Agent、教学动作决策与学习状态
app.include_router(tutor_routes.router, prefix=settings.api_prefix)
app.include_router(study_routes.router, prefix=settings.api_prefix)
# 5D：基础待办清单（独立小功能，不接 Agent / RAG / Memory）
app.include_router(todos_routes.router, prefix=settings.api_prefix)


@app.get("/", tags=["system"], summary="服务信息")
def root() -> dict[str, str]:
    """根路径，返回基础信息与常用入口，方便人工确认服务已启动。"""
    return {
        "app": settings.app_name,
        "version": APP_VERSION,
        "env": settings.app_env,
        "llm_mode": llm_gateway.mode,
        "docs": "/docs",
        "health": f"{settings.api_prefix}/health",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.app_env == "dev",
        log_level=settings.log_level.lower(),
    )
