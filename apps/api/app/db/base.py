"""模型聚合入口。

本模块的唯一职责：把 Base 与**全部 ORM 模型**聚到一起，
使 Alembic 的 autogenerate 能看到完整元数据。

- Base 本身定义在 app/db/base_class.py（那里没有任何模型导入，故无循环依赖）
- 模型模块一律 `from app.db.base_class import Base`，不要改成本文件

新增模型后必须在本文件末尾导入，否则 alembic revision --autogenerate 会漏表。

用法：
    from app.db.base import Base          # 需要完整元数据时（如 alembic/env.py）
    from app.db.base_class import Base    # 普通场景直接继承时
"""

from __future__ import annotations

from app.db.base_class import Base

# --------------------------------------------------------------------------- #
# 模型注册区
# --------------------------------------------------------------------------- #
from app.models.answer_evaluation import AnswerEvaluation  # noqa: E402,F401
from app.models.app_meta import AppMeta  # noqa: E402,F401
from app.models.chunk import Chunk  # noqa: E402,F401
from app.models.conversation import Conversation, ConversationMessage  # noqa: E402,F401
from app.models.document import Document  # noqa: E402,F401
from app.models.knowledge_check import KnowledgeCheck  # noqa: E402,F401
from app.models.knowledge_point import KnowledgePoint  # noqa: E402,F401
from app.models.knowledge_relation import KnowledgeRelation  # noqa: E402,F401
from app.models.learner_kp_state import LearnerKpState  # noqa: E402,F401
from app.models.learner_profile import LearnerProfile  # noqa: E402,F401
from app.models.message import Message  # noqa: E402,F401
from app.models.saved_knowledge import SavedKnowledge  # noqa: E402,F401

# Session 必须在 Message 之前导入：Message 的关系指向 sessions 表，
# 虽然 SQLAlchemy 用字符串解析，但先注册 Session 能让外键解析更早完成。
from app.models.session import Session  # noqa: E402,F401
from app.models.user import User  # noqa: E402,F401

__all__ = [
    "Base",
    "AnswerEvaluation",
    "AppMeta",
    "Chunk",
    "Conversation",
    "ConversationMessage",
    "Document",
    "KnowledgeCheck",
    "KnowledgePoint",
    "KnowledgeRelation",
    "LearnerKpState",
    "LearnerProfile",
    "Message",
    "SavedKnowledge",
    "Session",
]
