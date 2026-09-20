"""系统路由：健康检查。

设计原则：健康检查永远返回 200，把各组件状态放在 body 里。
这样前端能区分「服务挂了」（连不上）与「服务活着但某个依赖不可用」（degraded）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app import __version__ as APP_VERSION
from app.core.config import settings
from app.core.llm import llm_gateway
from app.db.session import check_chroma, check_database
from app.rag.embedding import embedder
from app.schemas.health import ComponentStatus, HealthResponse

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse, summary="整体健康检查")
def health() -> HealthResponse:
    """检查 LLM / MySQL / Chroma / Embedding 四个依赖组件的可用性。

    P3 起加入 embedding —— 它是 RAG 检索的前置依赖，用户需要一眼看出
    「检索不可用」是因为没配 Key 还是别的原因。detail 里**只有布尔值与模型名，
    绝不回显 Key**（有测试守着这一条）。
    """
    llm_detail = llm_gateway.describe()
    llm_ok = llm_detail["mode"] == "mock" or bool(llm_detail["configured"])

    db_detail = check_database()
    db_ok = bool(db_detail.pop("ok", False))

    chroma_detail = check_chroma()
    chroma_ok = bool(chroma_detail.pop("ok", False))

    embedding_detail = _check_embedding()
    embedding_ok = bool(embedding_detail.pop("ok", False))

    components = [
        ComponentStatus(name="llm", ok=llm_ok, detail=llm_detail),
        ComponentStatus(name="mysql", ok=db_ok, detail=db_detail),
        ComponentStatus(name="chroma", ok=chroma_ok, detail=chroma_detail),
        ComponentStatus(name="embedding", ok=embedding_ok, detail=embedding_detail),
    ]

    return HealthResponse(
        status="ok" if all(c.ok for c in components) else "degraded",
        app=settings.app_name,
        env=settings.app_env,
        version=APP_VERSION,
        components=components,
    )


def _check_embedding() -> dict[str, Any]:
    """探测 embedding 提供方。永不抛异常，也绝不回显 Key。

    **注意这里不发起真实请求**：健康检查会被前端定时轮询，
    每次都调一次云端 embedding 既慢又浪费额度。只检查配置是否齐备。
    `mock` provider 视为就绪（它本来就不需要凭据）。
    """
    provider = embedder.name
    if settings.embedding_provider == "local":
        return {
            "ok": False,
            "provider": provider,
            "error": "EMBEDDING_PROVIDER=local 未实现，请改用 api 或 mock",
        }
    return {
        "ok": settings.is_embedding_configured,
        "provider": provider,
        "model": settings.embedding_model,
        "dim": settings.embedding_dim,
        "configured": settings.is_embedding_configured,
        "batch_size": settings.embedding_batch_size,
    }


@router.get("/health/live", summary="存活探针（不检查依赖）")
def liveness() -> dict[str, str]:
    """仅证明进程存活，用于容器探针与快速联通性验证。"""
    return {"status": "alive", "app": settings.app_name, "version": APP_VERSION}
