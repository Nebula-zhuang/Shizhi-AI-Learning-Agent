"""自由学习空间路由。

对话的增删改查在 `/conversations` 下，核心是 `POST /conversations/{id}/ask` ——
它用 SSE 把一轮学习过程摊开：自然语言状态、正文增量、来源、引用、完成。

## 为什么状态是"真"的而不是假进度

推给前端的每一条状态（"正在翻你的资料…"）都对应后台**真的在做那件事**。
没有"假装加载到 80%"这种帧 —— 一旦有一条是编的，
用户就再也没法从状态里判断"它到底到哪一步了"，状态栏也就失去了意义。
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.agent import free_study
from app.api.deps import require_learner_id
from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import get_db
from app.models.conversation import MessageAuthor
from app.models.saved_knowledge import SavedKnowledge
from app.schemas.saved_knowledge import (
    SavedKnowledgeCreate,
    SavedKnowledgeItem,
    SavedKnowledgeListResponse,
)
from app.schemas.study import (
    AttachmentUploadResponse,
    McpStatus,
    AskRequest,
    AttachmentListResponse,
    CapabilityInfo,
    ConversationCreate,
    ConversationListResponse,
    ConversationRename,
    ConversationSummary,
    MessageItem,
    MessageListResponse,
)
from app.ingestion import storage
from app.ingestion.base import ParserError
from app.ingestion.router import validate_upload
from app.services import (
    document_service,
    ingest_runner,
    saved_knowledge_service,
    study_service,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/study", tags=["free-study"])


async def _upload_chunks(upload: UploadFile):
    """把 UploadFile 变成异步字节流。

    **不一次性 read() 进内存** —— 上传上限是几百 MB，
    一次性读进来会直接变成内存问题。摄取那边也是同样的写法。
    """
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        yield chunk


def _sse(event: str, data: dict[str, Any]) -> str:
    """SSE 帧。格式与 Tutor 的完全一致 —— 前端可以用同一个解析器。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _to_message_item(message) -> MessageItem:
    return MessageItem(
        id=message.id,
        role=message.role,
        content=message.content,
        sources=message.sources,
        citations=message.citations,
        attachments=message.attachments,
        status_trace=message.status_trace,
        degraded_reason=message.degraded_reason,
        created_at=message.created_at,
    )


def _to_summary(conversation) -> ConversationSummary:
    return ConversationSummary(
        id=conversation.id,
        title=conversation.title,
        message_count=conversation.message_count,
        created_at=conversation.created_at,
        last_message_at=conversation.last_message_at,
    )


# --------------------------------------------------------------------------- #
# 对话
# --------------------------------------------------------------------------- #
@router.get("/conversations", response_model=ConversationListResponse, summary="我的对话")
def list_conversations(
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    learner_id: str = Depends(require_learner_id),
):
    rows = study_service.list_conversations(db, learner_id=learner_id, limit=limit)
    return ConversationListResponse(items=[_to_summary(c) for c in rows], total=len(rows))


@router.post("/conversations", response_model=ConversationSummary, summary="新建对话")
def create_conversation(
    payload: ConversationCreate,
    db: Session = Depends(get_db),
    learner_id: str = Depends(require_learner_id),
):
    conversation = study_service.create_conversation(
        db, learner_id=learner_id, title=payload.title
    )
    return _to_summary(conversation)


@router.patch(
    "/conversations/{conversation_id}", response_model=ConversationSummary, summary="重命名"
)
def rename_conversation(
    conversation_id: int,
    payload: ConversationRename,
    db: Session = Depends(get_db),
    learner_id: str = Depends(require_learner_id),
):
    try:
        conversation = study_service.rename_conversation(
            db, conversation_id=conversation_id, learner_id=learner_id, title=payload.title
        )
    except study_service.ConversationNotFound:
        raise HTTPException(status_code=404, detail="这个对话不存在。") from None
    return _to_summary(conversation)


@router.delete("/conversations/{conversation_id}", summary="删除对话")
def delete_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    learner_id: str = Depends(require_learner_id),
):
    try:
        study_service.delete_conversation(
            db, conversation_id=conversation_id, learner_id=learner_id
        )
    except study_service.ConversationNotFound:
        raise HTTPException(status_code=404, detail="这个对话不存在。") from None
    return {"deleted": True, "conversation_id": conversation_id}


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=MessageListResponse,
    summary="对话历史",
)
def list_messages(
    conversation_id: int,
    db: Session = Depends(get_db),
    learner_id: str = Depends(require_learner_id),
):
    try:
        rows = study_service.list_messages(
            db, conversation_id=conversation_id, learner_id=learner_id
        )
    except study_service.ConversationNotFound:
        raise HTTPException(status_code=404, detail="这个对话不存在。") from None
    return MessageListResponse(
        conversation_id=conversation_id, items=[_to_message_item(m) for m in rows]
    )


# --------------------------------------------------------------------------- #
# 保存知识（Phase 3A）
#
# 与上面"对话"的关系：**只进不出** —— 从一段对话里把某一轮**复制**进知识库，
# 此后再也不依赖那条对话。所以这里没有"删除对话时级联删保存"的逻辑（也不该有）。
# --------------------------------------------------------------------------- #
def _to_saved_item(record: SavedKnowledge) -> SavedKnowledgeItem:
    return SavedKnowledgeItem(
        id=int(record.id),
        question=record.question,
        answer=record.answer,
        source_message_id=record.source_message_id,
        source_urls=record.source_urls,
        kp_ids=record.kp_ids,
        tags=record.tags,
        embedding_id=record.embedding_id,
        created_at=record.created_at,
    )


@router.post("/knowledge", response_model=SavedKnowledgeItem, summary="保存一轮到知识库")
def save_knowledge(
    payload: SavedKnowledgeCreate,
    db: Session = Depends(get_db),
    learner_id: str = Depends(require_learner_id),
):
    """把一条**助手消息**保存进知识库。

    正文由服务端从消息里取（客户端只给 id）—— 见 `SavedKnowledgeCreate` 的理由。
    """
    try:
        record = saved_knowledge_service.save_from_message(
            db,
            learner_id=learner_id,
            message_id=payload.message_id,
            tags=payload.tags,
        )
    except saved_knowledge_service.SavedKnowledgeNotFound:
        # 不存在与不是你的**都返回 404** —— 区分开就等于确认这个 id 存在。
        raise HTTPException(status_code=404, detail="这条回答不存在。") from None
    return _to_saved_item(record)


@router.get("/knowledge", response_model=SavedKnowledgeListResponse, summary="我保存的知识")
def list_knowledge(
    limit: int = Query(default=saved_knowledge_service.DEFAULT_LIMIT, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    learner_id: str = Depends(require_learner_id),
):
    rows, total = saved_knowledge_service.list_saved(
        db, learner_id=learner_id, limit=limit, offset=offset
    )
    return SavedKnowledgeListResponse(
        items=[_to_saved_item(r) for r in rows], total=total
    )


# --------------------------------------------------------------------------- #
# 提问（SSE）
# --------------------------------------------------------------------------- #
@router.post("/conversations/{conversation_id}/ask", summary="提问（流式）")
async def ask(
    conversation_id: int,
    payload: AskRequest,
    db: Session = Depends(get_db),
    learner_id: str = Depends(require_learner_id),
):
    """一轮自由学习。

    **用 `async def` 是必须的** —— 同步路由会被 FastAPI 丢进线程池，
    那里没有事件循环，流式生成会直接失效（P1 踩过这个坑）。
    """
    try:
        conversation = study_service.get_owned(db, conversation_id, learner_id)
    except study_service.ConversationNotFound:
        raise HTTPException(status_code=404, detail="这个对话不存在。") from None

    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空。")

    # 首轮用问题当标题 —— 截断而不是概括（概括要再调一次模型，
    # 而"他问的第一句话"本来就是最好认的标题）
    previous = study_service.list_messages(
        db, conversation_id=conversation.id, learner_id=learner_id, limit=1
    )
    if not previous and (not conversation.title or conversation.title == "新的学习对话"):
        conversation.title = study_service.auto_title_from(question)
        db.commit()

    history = study_service.recent_history(db, conversation_id=conversation.id)

    # 归属校验过的资料范围：**只在这个用户的资料里检索**。
    # 显式传 document_ids 时取交集，避免越权检索别人的资料。
    owned_ids = set(study_service.learner_document_ids(db, learner_id=learner_id))
    if payload.document_ids:
        scoped = [int(x) for x in payload.document_ids if int(x) in owned_ids]
        if not scoped:
            raise HTTPException(status_code=403, detail="这些资料不属于你。")
    else:
        scoped = sorted(owned_ids)

    kb_size = study_service.learner_knowledge_size(db, learner_id=learner_id)

    # 附件解析：**每一个都必须属于当前用户**（越权在服务层整批拒绝）
    try:
        attachments = study_service.require_attachments(
            db, learner_id=learner_id, document_ids=payload.document_ids
        )
    except study_service.AttachmentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    # 只有**图片**才进多模态通道 —— 把 PDF 的二进制塞给视觉模型没有意义。
    # 落在本地磁盘上的图要转成绝对路径：`storage.resolve` 负责这件事，
    # 它同时会挡住"相对路径跳出上传目录"这类路径穿越。
    image_paths: list[str] = []
    for doc in attachments:
        if not study_service.is_image_extension(doc.file_name):
            continue
        try:
            image_paths.append(str(storage.resolve(doc.storage_path)))
        except Exception as exc:  # noqa: BLE001
            # 单张图读不出来不该让整轮失败 —— 记下来，让这一轮退回纯文本
            logger.warning("附件 %s 的图片路径无法解析：%s", doc.id, exc)

    # ── 图片文档**不当作"可检索资料"**递给 Agent。
    #
    # 上传的图片在库里就是一条 `Document`，于是会被 `scoped` 顺带带进
    # `document_ids`。后果实测过：提示词里会同时出现
    # 「附了 1 张图片」和「用户指定了 1 份资料（document_ids=[...]）」，
    # 而**没有任何字段说明那份"资料"就是这张图** ——
    # Agent 于是以为除了图之外还有别的材料，转而去调 `retrieve_knowledge`。
    #
    # 而图片文档的 chunk 里**只有文件名**（实测 `code.png` 这种），
    # 检索它不可能拿回任何图片内容 —— 那一次调用是**纯浪费**。
    #
    # 图片内容已经由上面的 `images` 交给 `image_analysis`，
    # 所以这里把它摘出去**不损失任何能力**；
    # 真实文档（PDF / DOCX / PPTX / TXT / MD …）照旧保留。
    image_doc_ids = {
        doc.id for doc in attachments if study_service.is_image_extension(doc.file_name)
    }
    if not payload.document_ids:
        # 没显式传 ids 时 `attachments` 是空的 —— 得把 scoped 这批查一遍
        # 才认得出其中哪些是图片。（`scoped` 取自 owned_ids，不会触发越权拒绝。）
        image_doc_ids |= {
            doc.id
            for doc in study_service.require_attachments(
                db, learner_id=learner_id, document_ids=scoped
            )
            if study_service.is_image_extension(doc.file_name)
        }
    material_ids = [x for x in scoped if x not in image_doc_ids]

    study_service.append_message(
        db,
        conversation=conversation,
        role=MessageAuthor.USER,
        content=question,
        attachments=(
            [
                {
                    "document_id": doc.id,
                    "file_name": doc.file_name,
                    "kind": "image" if study_service.is_image_extension(doc.file_name) else "document",
                }
                for doc in attachments
            ]
            or None
        ),
        touch=False,
    )

    async def event_source() -> AsyncIterator[str]:
        """跑完一轮并落库。

        ⚠️ 落库放在**流结束之后**：正文是边生成边推的，
        只有收完所有 delta 才知道完整答案。中途失败也要落一条 —
        否则用户看到了一段回答，刷新后却什么都没有。
        """
        pieces: list[str] = []
        final: dict[str, Any] = {}

        try:
            async for event in free_study.stream_turn(
                question=question,
                history=history,
                # ⚠️ 用 `material_ids` 而不是 `scoped` —— 图片文档已被摘掉，
                # 否则 Agent 会把"图"误当成另一份可检索资料（见上面的说明）。
                # 全都是图片时这里为空，提示词就不会再出现"指定了 N 份资料"。
                document_ids=material_ids or None,
                # 图片路径会**绑定进 image_analysis 工具**：
                # 模型只需要说"我想知道什么"，不需要知道图在哪
                images=image_paths,
                kb_size=kb_size,
                has_attachments=bool(attachments),
                # 跨对话检索要按人隔离 —— 绑定进 `search_saved_knowledge`，
                # 模型给不出、也不该给这个值。
                learner_id=learner_id,
                allow_web=True,
            ):
                if event.event == "delta":
                    pieces.append(str(event.data.get("text") or ""))
                elif event.event == "done":
                    final = dict(event.data)
                yield _sse(event.event, event.data)
        except Exception as exc:  # noqa: BLE001
            # **绝不让堆栈流到前端**。给一句人话，细节进日志。
            logger.exception("自由学习一轮失败：%s", exc)
            note = "这轮回答没能完成。你可以换个说法再问一次。"
            pieces.append(note)
            yield _sse("error", {"text": note, "retryable": True})

        content = "".join(pieces).strip()
        if not content:
            return

        try:
            study_service.append_message(
                db,
                conversation=conversation,
                role=MessageAuthor.ASSISTANT,
                content=content,
                sources=final.get("sources"),
                citations=final.get("citations"),
                status_trace=final.get("status_trace"),
                degraded_reason=final.get("degraded_reason"),
            )
        except Exception as exc:  # noqa: BLE001
            # 落库失败不该让用户看不到已经生成的回答（它已经推过去了），
            # 但必须留下记录，否则"聊过但历史里没有"会变成一个查不出的怪现象
            logger.error("自由学习消息落库失败：%s", exc)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------------- #
# 能力与资料
# --------------------------------------------------------------------------- #
@router.get("/capabilities", response_model=CapabilityInfo, summary="能力自检")
def capabilities(
    learner_id: str = Depends(require_learner_id),
):
    """**如实报告能力状态**，包括 MCP 的真实身份。

    这里的 `mcp.server_info` 与 `mcp.discovered_tools` 都不是常量 ——
    它们是 MCP 协议握手与 `tools/list` 读回来的值，连过才有。
    所以这个接口同时也是一个"MCP 到底通没通"的检查点。
    """
    from app.agent import free_study
    from app.search.provider import describe_search_capability

    web = describe_search_capability()
    mcp_raw = web.get("mcp") if isinstance(web.get("mcp"), dict) else {}

    return CapabilityInfo(
        free_study=True,
        knowledge_base=True,
        web_search_available=bool(web["available"]),
        web_search_note=str(web["note"]),
        web_search_backend=str(web.get("backend") or "auto"),
        mcp=McpStatus(
            configured=bool((mcp_raw or {}).get("configured")),
            url=str((mcp_raw or {}).get("url") or ""),
            server_info=(mcp_raw or {}).get("server_info"),
            discovered_tools=list((mcp_raw or {}).get("discovered_tools") or []),
        ),
        tools=free_study.build_registry().names(),
        max_capabilities_per_turn=free_study._limits_from_settings().max_tool_calls,
    )


@router.post(
    "/attachments",
    response_model=AttachmentUploadResponse,
    summary="上传对话附件",
)
async def upload_attachment(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    learner_id: str = Depends(require_learner_id),
):
    """上传一张图片（或一份文档）作为对话附件。

    **必须 `async def`** —— 上传是流式的，同步路由会被 FastAPI 丢进线程池，
    那里没有事件循环。

    这条路刻意和 `POST /api/documents` 走同一套存储与摄取：
    一份文件不管从书房传还是从对话里传，都该落到同一个地方、走同一条处理链。
    两套实现迟早会在"去重规则""大小限制""格式判定"上分叉。
    """
    file_name = storage.sanitize_filename(file.filename or "未命名")
    try:
        study_service.validate_attachment_name(file_name)
    except study_service.AttachmentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    temp_path: pathlib.Path | None = None
    try:
        chunks = _upload_chunks(file)
        temp_path, size, file_hash = await storage.stream_to_temp(
            chunks, max_bytes=int(settings.max_upload_mb) * 1024 * 1024
        )

        try:
            validate_upload(temp_path, file_name=file_name, size=size)
        except ParserError as exc:
            raise HTTPException(status_code=400, detail=exc.message) from exc

        existing = document_service.find_by_hash(db, file_hash, learner_id=learner_id)
        if existing is not None:
            storage.discard_temp(temp_path)
            temp_path = None
            return AttachmentUploadResponse(
                document_id=existing.id,
                file_name=existing.file_name,
                file_type=existing.file_type,
                file_size=existing.file_size,
                is_image=study_service.is_image_extension(existing.file_name),
                parse_status=existing.parse_status,
                dedup=True,
            )

        storage_path = storage.finalize_source(temp_path, file_hash, file_name)
        temp_path = None  # 已归位，后面任何异常都不该再删它

        document = document_service.create_document(
            db,
            owner_learner_id=learner_id,
            file_name=file_name,
            file_size=size,
            file_hash=file_hash,
            storage_path=storage_path,
        )

        # 投递摄取是**顺手做的事**，不是前置条件：
        # 看图只需要 storage_path，它在落盘那一刻就有了。
        if not ingest_runner.submit(document.id):
            logger.warning("附件 %s 的摄取任务投递失败，但图片仍可用于本轮分析", document.id)
    finally:
        storage.discard_temp(temp_path)

    return AttachmentUploadResponse(
        document_id=document.id,
        file_name=document.file_name,
        file_type=document.file_type,
        file_size=document.file_size,
        is_image=study_service.is_image_extension(document.file_name),
        parse_status=document.parse_status,
    )


@router.get("/attachments", response_model=AttachmentListResponse, summary="可选资料")
def list_attachments(
    limit: int = Query(default=20, ge=1, le=50),
    db: Session = Depends(get_db),
    learner_id: str = Depends(require_learner_id),
):
    items = study_service.list_recent_attachments(db, learner_id=learner_id, limit=limit)
    return AttachmentListResponse(items=items)  # type: ignore[arg-type]
