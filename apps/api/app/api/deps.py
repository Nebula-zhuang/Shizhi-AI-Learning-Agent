"""接口层公共依赖：当前用户 / 当前学习档案。

## 身份只来自会话，**不来自请求体**

P0–P5 期间请求体里带一个 `learner_id` 字段，服务端直接采信 ——
那时候只有一个本地用户，无所谓。引入账号之后这就是一个**越权读写的口子**：
任何人改一下请求体就能读写别人的学习数据。

所以现在的规则是：

    **已登录** → 用账号绑定的 learner_id（请求体里那个字段被忽略）
    **未登录** → 落到本地档案 `local`（同样是固定的，请求体说了不算）

于是"请求体里那个 learner_id"彻底失去了作用。字段本身保留在 schema 里
（删掉会破坏既有契约），但**不再被读取** —— 这一点在下面的代码里能直接看出来。

## 为什么允许未登录访问

有两个理由，都不是"图省事"：

1. **既有契约**：P0–P5 的 40 多个接口与 420 个测试都建立在"无需鉴权"上。
   一刀切成 401 会同时破坏契约和整套测试。
2. **产品上说得通**：登录是**界面层**的要求（前端有路由守卫，不登录进不去），
   接口层保留匿名能力，便于接口文档试用、脚本调试与将来的公开只读接口。

代价是：匿名身份固定落在 `local` 档案上。这是有意的 ——
匿名者能碰到的只有那一份本地演示数据，碰不到任何注册用户的数据。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.security import SESSION_COOKIE, decode_access_token
from app.db.session import get_db
from app.models.document import Document
from app.models.knowledge_point import KnowledgePoint
from app.models.user import User
from app.services import auth_service, document_service, learner_service


def _token_from(request: Request) -> str | None:
    """从 Cookie 或 Authorization 头取令牌。

    先看 Cookie（浏览器主路径），再看 `Authorization: Bearer`（脚本 / 接口文档）。
    两者都没有就返回 None。
    """
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        return cookie
    header = request.headers.get("Authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return None


def current_user_optional(request: Request, db: Session = Depends(get_db)) -> User | None:
    """当前登录用户；未登录或令牌无效返回 None（**不抛错**）。

    令牌无效当未登录处理，而不是报 401 —— 这样"令牌过期"对匿名可用的接口
    不会变成硬失败；需要强制登录的接口用下面的 `current_user`。
    """
    token = _token_from(request)
    if not token:
        return None
    payload = decode_access_token(token)
    if not payload:
        return None
    try:
        user_id = int(payload.get("sub") or 0)
    except (TypeError, ValueError):
        return None
    user = auth_service.get_user_by_id(db, user_id)
    if user is None or not user.is_active:
        return None
    return user


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """必须登录的接口用这个。未登录直接 401。"""
    user = current_user_optional(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="请先登录。")
    return user


def current_learner_id(request: Request, db: Session = Depends(get_db)) -> str:
    """本次请求该读写谁的学习数据。

    这是**唯一**决定身份的地方 —— 所有学习类接口都从这里取，
    而不是从请求体里读。
    """
    user = current_user_optional(request, db)
    if user is not None:
        return user.learner_id
    return learner_service.DEFAULT_LEARNER_ID


CurrentUser = Annotated[User, Depends(current_user)]
OptionalUser = Annotated[User | None, Depends(current_user_optional)]
LearnerId = Annotated[str, Depends(current_learner_id)]


def require_learner_id(user: User = Depends(current_user)) -> str:
    """**必须登录**才能用的接口，从这里取学习者标识。

    ## 为什么需要它，而不是直接用 `current_learner_id`

    `current_learner_id` 在未登录时会**回退到 `DEFAULT_LEARNER_ID = "local"`** ——
    那是 P0–P6 的刻意设计，支撑"不登录也能用"的体验。

    但演示账号 demo 的 `learner_id` **也是 `local`**。于是：

        **未登录访客 ＝ 默认学习者 ＝ 演示账号**

    对"本机自己用"的场景这没问题；而对**对话与上传的文件**这类私人内容，
    它意味着"任何人都能看到 demo 的聊天记录"。

    所以自由学习这组接口改用它：未登录直接 401。

    ⚠️ **刻意只作用于自由学习**，不动 P1–P6 的全局鉴权 ——
    改全局会波及资料库、学习状态、学习地图等所有既有功能，
    那不是这个阶段该做的事。
    """
    return user.learner_id


RequireLearnerId = Annotated[str, Depends(require_learner_id)]


# --------------------------------------------------------------------------- #
# 归属校验
# --------------------------------------------------------------------------- #
def require_document(db: Session, document_id: int, learner_id: str) -> Document:
    """取资料并按归属校验。不属于当前账号的，按"不存在"处理。"""
    document = document_service.get_document(db, document_id, learner_id=learner_id)
    if document is None:
        raise HTTPException(status_code=404, detail=f"文档 {document_id} 不存在。")
    return document


def require_knowledge_point(db: Session, kp_id: int, learner_id: str) -> KnowledgePoint:
    """取知识点，**沿着它所属的资料校验归属**。

    知识点自身没有归属字段（它挂在资料下），所以归属要顺着
    `knowledge_points.document_id → documents.owner_learner_id` 判。

    取不到、或不属于该账号，都抛 404 而不是 403 —— 403 等于告诉对方
    "这个 id 确实存在"，那就是一个用来探测别人资料的接口。
    """
    point = db.get(KnowledgePoint, kp_id)
    if point is None:
        raise HTTPException(status_code=404, detail=f"知识点 {kp_id} 不存在。")
    if point.document_id is not None:
        document = db.get(Document, point.document_id)
        if document is not None and document.owner_learner_id != learner_id:
            raise HTTPException(status_code=404, detail=f"知识点 {kp_id} 不存在。")
    return point
