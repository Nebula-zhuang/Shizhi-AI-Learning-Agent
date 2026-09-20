"""自由学习对话的领域逻辑。

控制器只做"解析请求 → 调这里 → 格式化响应"，业务规则全部在本模块。

## 一个贯穿全文的原则：**所有查询都带上 learner_id**

对话是用户的私人内容。任何一个忘记带 `learner_id` 的查询，
都会变成"能看到别人的对话"。所以这里**不提供**按 id 单独查的方法 ——
只提供 `get_owned(db, conversation_id, learner_id)`，从签名上堵死这条路。
"""

from __future__ import annotations

import pathlib
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.conversation import Conversation, ConversationMessage, MessageAuthor
from app.models.document import Document, ParseStatus
from app.models.knowledge_point import KnowledgePoint

logger = get_logger(__name__)

#: 标题长度上限。太长会把列表挤变形。
MAX_TITLE_CHARS = 40

#: 首轮自动生成的默认标题
DEFAULT_TITLE = "新的学习对话"


class ConversationNotFound(LookupError):
    """对话不存在**或不属于当前用户**。

    刻意不区分这两种情况：区分开就等于告诉攻击者"这个 id 是存在的"。
    """


# --------------------------------------------------------------------------- #
# 对话
# --------------------------------------------------------------------------- #
def create_conversation(db: Session, *, learner_id: str, title: str = "") -> Conversation:
    conversation = Conversation(
        learner_id=learner_id,
        title=(title or DEFAULT_TITLE).strip()[:255],
    )
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


def list_conversations(
    db: Session, *, learner_id: str, limit: int = 50
) -> list[Conversation]:
    """按"最近聊过"倒序。**不是按创建时间** —— 用户找的是刚聊过的那个。"""
    stmt = (
        select(Conversation)
        .where(Conversation.learner_id == learner_id)
        .order_by(Conversation.last_message_at.desc(), Conversation.id.desc())
        .limit(max(1, min(limit, 200)))
    )
    return list(db.scalars(stmt).all())


def get_owned(db: Session, conversation_id: int, learner_id: str) -> Conversation:
    """取一条属于该用户的对话。取不到就抛 `ConversationNotFound`。"""
    stmt = select(Conversation).where(
        Conversation.id == conversation_id,
        Conversation.learner_id == learner_id,
    )
    conversation = db.scalars(stmt).first()
    if conversation is None:
        raise ConversationNotFound(str(conversation_id))
    return conversation


def rename_conversation(
    db: Session, *, conversation_id: int, learner_id: str, title: str
) -> Conversation:
    conversation = get_owned(db, conversation_id, learner_id)
    cleaned = title.strip()[:255]
    if cleaned:
        conversation.title = cleaned
        db.commit()
        db.refresh(conversation)
    return conversation


def delete_conversation(db: Session, *, conversation_id: int, learner_id: str) -> None:
    conversation = get_owned(db, conversation_id, learner_id)
    db.delete(conversation)
    db.commit()


def auto_title_from(question: str) -> str:
    """首轮问题 → 对话标题。

    截断而不是概括：概括要再调一次模型，而"他问的第一句话"
    本来就是最好认的标题。截断处加省略号，避免半句话看着像说完了。
    """
    text = " ".join((question or "").split())
    if not text:
        return DEFAULT_TITLE
    if len(text) <= MAX_TITLE_CHARS:
        return text
    return text[:MAX_TITLE_CHARS].rstrip() + "…"


# --------------------------------------------------------------------------- #
# 消息
# --------------------------------------------------------------------------- #
def list_messages(
    db: Session, *, conversation_id: int, learner_id: str, limit: int = 200
) -> list[ConversationMessage]:
    get_owned(db, conversation_id, learner_id)  # 归属校验必须先做
    stmt = (
        select(ConversationMessage)
        .where(ConversationMessage.conversation_id == conversation_id)
        .order_by(ConversationMessage.id.asc())
        .limit(max(1, min(limit, 500)))
    )
    return list(db.scalars(stmt).all())


def append_message(
    db: Session,
    *,
    conversation: Conversation,
    role: MessageAuthor,
    content: str,
    sources: Sequence[dict[str, Any]] | None = None,
    citations: Sequence[dict[str, Any]] | None = None,
    attachments: Sequence[dict[str, Any]] | None = None,
    status_trace: Sequence[str] | None = None,
    degraded_reason: str | None = None,
    touch: bool = True,
) -> ConversationMessage:
    """追加一条消息，并维护对话的计数与时间戳。

    `touch=False` 用于"只写用户消息、助手回复还没生成完"的中间态 ——
    那时候不该更新"最近聊过"时间（他还没得到回复）。
    """
    message = ConversationMessage(
        conversation_id=conversation.id,
        role=role.value,
        content=content,
        sources=list(sources) if sources else None,
        citations=list(citations) if citations else None,
        attachments=list(attachments) if attachments else None,
        status_trace=list(status_trace) if status_trace else None,
        degraded_reason=degraded_reason,
    )
    db.add(message)

    conversation.message_count = (conversation.message_count or 0) + 1
    if touch:
        # 显式写时间而不是依赖 server_default —— 更新既有行时默认值不会生效
        conversation.last_message_at = datetime.now(timezone.utc).replace(tzinfo=None)

    db.commit()
    db.refresh(message)
    return message


def recent_history(
    db: Session, *, conversation_id: int, limit: int = 6
) -> list[dict[str, str]]:
    """最近几轮对话，供模型理解上下文。

    **只取对话正文，不取 sources / citations** —— 那些是给界面看的，
    塞进提示词只会占 token 并干扰模型。检索到的东西会由本轮重新检索。
    """
    stmt = (
        select(ConversationMessage)
        .where(ConversationMessage.conversation_id == conversation_id)
        .order_by(ConversationMessage.id.desc())
        .limit(max(1, limit))
    )
    rows = list(db.scalars(stmt).all())
    rows.reverse()  # 反回时间顺序
    return [{"role": row.role, "content": row.content} for row in rows if row.content]


# --------------------------------------------------------------------------- #
# 供路由决策用的上下文
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# 附件（对话里上传的图片 / 文件）
# --------------------------------------------------------------------------- #
#: 允许作为"对话附件"的扩展名。与摄取支持的集合一致，但**只取常用的几种** ——
#: 对话附件的意图是"我现在要看这个"，不是"我要建一个资料库"。
ATTACHMENT_EXTENSIONS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".pdf", ".txt", ".md", ".docx", ".pptx"}
)


class AttachmentError(ValueError):
    """附件不合规（格式不支持 / 太大 / 不是当前用户的）。"""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def is_image_extension(file_name: str) -> bool:
    """这份附件是不是图片。

    **由扩展名判断而不是嗅探内容**：摄取流水线本来就这么分派，
    两处用同一套判据才不会出现"我以为它是图，流水线以为它是文档"。
    """
    return pathlib.PurePosixPath(str(file_name).lower()).suffix in {
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".gif",
        ".bmp",
    }


def validate_attachment_name(file_name: str) -> str:
    """校验并规范化附件名。返回干净的扩展名（小写，带点）。"""
    suffix = pathlib.PurePosixPath(str(file_name).lower()).suffix
    if not suffix:
        raise AttachmentError("这个文件没有扩展名，认不出是什么格式。")
    if suffix not in ATTACHMENT_EXTENSIONS:
        allowed = "、".join(sorted(ATTACHMENT_EXTENSIONS))
        raise AttachmentError(f"对话里不支持 {suffix} 格式。可以上传：{allowed}。")
    return suffix


def require_attachments(db: Session, *, learner_id: str, document_ids: Sequence[int]) -> list[Document]:
    """取出一批附件，**每一个都必须属于当前用户**。

    越权在这里堵死：返回的 id 里只要有一个不属于他，整批拒绝 ——
    而不是"能看到的就返回"（那会让攻击者靠试探知道哪些 id 存在）。
    """
    wanted = [int(x) for x in document_ids]
    if not wanted:
        return []

    stmt = select(Document).where(
        Document.id.in_(wanted),
        Document.owner_learner_id == learner_id,
    )
    found = {int(doc.id): doc for doc in db.scalars(stmt).all()}

    missing = [str(x) for x in wanted if int(x) not in found]
    if missing:
        raise AttachmentError(
            f"这些附件不属于你，或者已经不存在：{'、'.join(missing)}。",
            status_code=403,
        )
    return [found[int(x)] for x in wanted]

def learner_knowledge_size(db: Session, *, learner_id: str) -> int:
    """这个用户有多少可检索的知识点。

    决策要用它判断"值不值得去检索" —— 空库上检索只会白等一次 embedding。
    """
    stmt = (
        select(func.count(KnowledgePoint.id))
        .join(Document, Document.id == KnowledgePoint.document_id)
        .where(Document.owner_learner_id == learner_id)
    )
    return int(db.scalar(stmt) or 0)


def learner_document_ids(db: Session, *, learner_id: str) -> list[int]:
    """这个用户的资料 id 列表。

    检索时用它限定范围 —— **不加这个限制，检索会从别人的资料里取内容出来**，
    那是最严重的一类越权。
    """
    stmt = select(Document.id).where(
        Document.owner_learner_id == learner_id,
        Document.parse_status == ParseStatus.READY,
    )
    return [int(x) for x in db.scalars(stmt).all()]


def list_recent_attachments(
    db: Session, *, learner_id: str, limit: int = 5
) -> list[dict[str, Any]]:
    """用户最近的资料，供"从资料库选择"用。"""
    stmt = (
        select(Document)
        .where(Document.owner_learner_id == learner_id)
        .order_by(Document.id.desc())
        .limit(max(1, min(limit, 50)))
    )
    return [
        {
            "document_id": doc.id,
            "file_name": doc.file_name,
            "file_type": doc.file_type,
            "parse_status": doc.parse_status,
        }
        for doc in db.scalars(stmt).all()
    ]
