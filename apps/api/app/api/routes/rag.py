"""P3 路由：向量索引与最小 RAG 问答。

全部是**新增路径**，P0/P1/P2 的 21 个路径一个都没有改动。

关于 `POST /api/rag/index` 为什么是 `async def`：
`index_runner.submit` 内部用 `asyncio.create_task`，而 FastAPI 会把同步路由丢进
线程池执行 —— 那里没有运行中的事件循环，投递必然失败。P1 在 reprocess 上踩过这个坑。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.api.deps import (
    current_learner_id,
    require_document,
    require_knowledge_point,
)
from app.db.session import get_db
from app.models.document import Document
from app.schemas.rag import (
    AskRequest,
    AskResponse,
    IndexRequest,
    IndexStartResponse,
    IndexStatusResponse,
    RagCapabilities,
)
from app.services import document_service, index_runner, index_service, rag_service

logger = get_logger(__name__)

router = APIRouter(prefix="/rag", tags=["rag"])


def _owned_document_ids(db: Session, learner_id: str, requested: list[int] | None) -> list[int]:
    """把"要检索哪些资料"收窄到当前账号拥有的那些。

    **关键在于 `requested` 为空时不能理解为"全部"** —— 那是整个资料库，
    包含别人的文件。这里把它解释为"我的全部资料"。

    调用方必须再判一次空：`answer_question` 里 `if document_ids:` 会把空列表
    当成"不限范围"，等于绕回全库检索。所以这个函数只负责"算范围"，
    "范围为空该怎么办"由调用方显式决定。
    """
    if requested:
        stray = [
            doc_id
            for doc_id in requested
            if document_service.get_document(db, doc_id, learner_id=learner_id) is None
        ]
        if stray:
            raise HTTPException(status_code=404, detail=f"文档 {stray[0]} 不存在。")
        return list(requested)
    rows, _ = document_service.list_documents(db, learner_id=learner_id, limit=100000, offset=0)
    return [d.id for d in rows]


def _parse_ids(raw: str | None) -> list[int] | None:
    if not raw:
        return None
    ids: list[int] = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            ids.append(int(piece))
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"document_ids 含非整数：{piece!r}"
            ) from exc
    return ids or None


# --------------------------------------------------------------------------- #
# 能力探测
# --------------------------------------------------------------------------- #
@router.get("/capabilities", response_model=RagCapabilities, summary="RAG 能力探测")
def rag_capabilities() -> RagCapabilities:
    """告诉前端当前具备哪些能力。

    存在的意义与 P2 的校验能力探测一致：让界面能区分
    「未配 Embedding Key（该去配）」与「配了但调用失败（该重试）」。
    """
    return RagCapabilities(**rag_service.capabilities())


# --------------------------------------------------------------------------- #
# 索引
# --------------------------------------------------------------------------- #
@router.post(
    "/index",
    status_code=202,
    response_model=IndexStartResponse,
    summary="建立/重建向量索引（异步）",
)
async def start_index(
    payload: IndexRequest | None = None,
    db: Session = Depends(get_db),
    learner_id: str = Depends(current_learner_id),
) -> IndexStartResponse:
    """投递后台索引任务。

    重建语义：同一份资料再次索引时，会先删掉它已有的向量再写入 ——
    否则换了 embedding 模型后，同一份资料会同时存在新旧两套向量，检索排序完全不可信。
    """
    if not settings.is_embedding_configured:
        raise HTTPException(
            status_code=409,
            detail=(
                "未配置云端 Embedding 凭据，无法建立索引。"
                "请在 .env 中设置 EMBEDDING_API_KEY（当前提供方："
                f"{settings.embedding_model}）后重启后端。"
            ),
        )

    document_ids = payload.document_ids if payload else None

    if document_ids:
        missing = [
            doc_id
            for doc_id in document_ids
            if document_service.get_document(db, doc_id, learner_id=learner_id) is None
        ]
        if missing:
            raise HTTPException(status_code=404, detail=f"文档不存在：{missing}")

    if index_runner.is_running():
        raise HTTPException(status_code=409, detail="已有索引任务在运行，请等待其完成。")

    if not index_runner.submit(document_ids):
        raise HTTPException(status_code=503, detail="服务当前无法接收索引任务，请稍后重试。")

    scope = f"{len(document_ids)} 份指定资料" if document_ids else "全部已解析资料"
    return IndexStartResponse(
        accepted=True,
        message=f"已投递索引任务（{scope}）。请轮询 /api/rag/index/status 获取进度。",
        document_ids=document_ids,
    )


@router.get("/index/status", response_model=IndexStatusResponse, summary="索引状态")
def index_status(
    document_ids: str | None = Query(
        default=None, description="逗号分隔的文档 id；省略则统计全部已解析资料"
    ),
    db: Session = Depends(get_db),
    learner_id: str = Depends(current_learner_id),
) -> IndexStatusResponse:
    """索引进度与每份资料的覆盖情况。

    进度来自内存态，覆盖情况来自向量库 —— 进程重启后进度会归零，
    但"哪些块已索引"依然准确，因为向量本身是持久化的。
    """
    # 省略范围时收窄为"我的资料"，而不是全库
    ids = _owned_document_ids(db, learner_id, _parse_ids(document_ids))
    overview = index_service.index_overview(db, ids)
    state = index_runner.get_state()

    return IndexStatusResponse(
        state=state.get("state") or ("idle" if overview["document_indexed"] == 0 else "done"),
        progress=int(state.get("progress") or 0),
        detail=state.get("detail") or "",
        collection=overview["collection"],
        collection_count=overview["collection_count"],
        store_available=overview["store_available"],
        document_total=overview["document_total"],
        document_indexed=overview["document_indexed"],
        items=overview["items"],
        stats=state.get("stats") or {},
    )


# --------------------------------------------------------------------------- #
# 问答
# --------------------------------------------------------------------------- #
@router.post("/ask", response_model=AskResponse, summary="最小 RAG 问答")
async def ask(
    payload: AskRequest,
    db: Session = Depends(get_db),
    learner_id: str = Depends(current_learner_id),
) -> AskResponse:
    """检索增强问答。

    链路：问题向量化 → Chroma Top-K → 距离门槛过滤 → 拼接带编号的上下文
    → DeepSeek 生成 → 返回答案与来源。

    **资料里没有相关内容时不会调用模型**，直接返回「这部分不在你的资料中」——
    让模型面对空上下文回答，只会得到一段编造的内容。
    """
    scoped = _owned_document_ids(db, learner_id, payload.document_ids)
    if not scoped:
        # 这个账号还没有资料。**不能把空列表传给 answer_question** ——
        # 它内部 `if document_ids:` 会把空列表当成"不限范围"，退化成全库检索。
        raise HTTPException(
            status_code=409,
            detail="你还没有上传资料，先到「资料」页传一份再问。",
        )

    try:
        result = await rag_service.answer_question(
            db,
            payload.question,
            document_ids=scoped,
            top_k=payload.top_k,
        )
    except rag_service.RagError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    return AskResponse(**result.as_dict())


@router.get(
    "/sources/{document_id}/{chunk_index}",
    summary="按块回链原文",
)
def get_source(
    document_id: int,
    chunk_index: int,
    db: Session = Depends(get_db),
    learner_id: str = Depends(current_learner_id),
) -> dict:
    """按文档 + 块序号取回原文块。

    供前端从答案里的 `[n]` 直接跳到原文 —— 与 P1 的知识点溯源是同一种体验。
    """

    document = require_document(db, document_id, learner_id)

    chunks = index_service.load_chunks(db, document_id)
    target = next((c for c in chunks if c.chunk_index == chunk_index), None)
    if target is None:
        raise HTTPException(
            status_code=404, detail=f"文档 {document_id} 中不存在块 {chunk_index}。"
        )

    return {
        "document_id": document_id,
        "file_name": document.file_name,
        "chunk_index": target.chunk_index,
        "page_start": target.page_start,
        "page_end": target.page_end,
        "block_type": target.block_type,
        "heading_path": target.heading_path,
        "content": target.content,
        "char_count": target.char_count,
    }
