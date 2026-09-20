"""P6 路由：账号。

新增 5 个路径，**P0–P5 的 35 个路径一个都没有改动**。

## 令牌怎么交给前端

登录成功后，JWT 写进一个 **httpOnly Cookie**，而不是放在响应体里让前端自己存。

理由是我们自己定过的那条规矩：**令牌禁止进 localStorage**（一旦 XSS，脚本能直接读走），
也不许拼进 URL。放 httpOnly Cookie 之后，前端 JS 根本拿不到令牌 ——
不是"约定不要读"，而是"读不到"。

因此这些接口的响应体里**只有用户信息，没有令牌**。
需要脚本调用时，走 `Authorization: Bearer`（令牌可从登录响应的 Cookie 里取，
或在接口文档里手动带上）。

## Cookie 上的三个开关

- `httponly` —— JS 不可读，挡 XSS 窃取
- `samesite=lax` —— 挡 CSRF。用 lax 而不是 strict：strict 会让
  "从外部链接点进来"的第一次请求不带 Cookie，出现"明明登录了却显示未登录"
- `secure` —— 只在 HTTPS 下发送。本机 http 演示必须关掉（配置项 `COOKIE_SECURE`）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, OptionalUser
from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import SESSION_COOKIE, create_access_token
from app.db.session import get_db
from app.models.user import User
from app.schemas.auth import AuthResponse, LoginRequest, RegisterRequest, UserOut
from app.services import auth_service
from app.services.auth_service import AuthError

logger = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

#: 口令长度上限在 schema 里已经限制为 6–64，这里不再重复校验。


def _set_session_cookie(response: Response, *, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=settings.jwt_expire_minutes * 60,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


def _issue_session(response: Response, user: User) -> None:
    token = create_access_token(
        user_id=user.id, username=user.username, learner_id=user.learner_id
    )
    _set_session_cookie(response, token=token)


@router.post("/register", response_model=AuthResponse, summary="注册并直接登录")
def register(
    payload: RegisterRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> AuthResponse:
    """注册新账号。

    **注册成功即登录** —— 让用户填完表单再跳去登录页重输一遍口令，
    是多余的一步，还容易在跳转中丢状态。
    """
    try:
        user = auth_service.create_user(
            db,
            username=payload.username,
            password=payload.password,
            display_name=payload.display_name,
        )
    except AuthError as exc:
        from fastapi import HTTPException

        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    _issue_session(response, user)
    return AuthResponse(
        user=UserOut(**user.as_dict()),
        message=f"欢迎，{user.name}。你的学习记录会记在这个账号下。",
    )


@router.post("/login", response_model=AuthResponse, summary="登录")
def login(
    payload: LoginRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> AuthResponse:
    try:
        user = auth_service.authenticate(
            db, username=payload.username, password=payload.password
        )
    except AuthError as exc:
        from fastapi import HTTPException

        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    _issue_session(response, user)
    return AuthResponse(user=UserOut(**user.as_dict()), message=f"欢迎回来，{user.name}。")


@router.post("/logout", response_model=AuthResponse, summary="退出登录")
def logout(response: Response) -> AuthResponse:
    """退出登录。

    服务端只做一件事：让浏览器把这个 Cookie 删掉。
    JWT 是无状态的，签出去的令牌在过期前技术上仍然有效 ——
    这是 JWT 的固有性质。要真正吊销，需要引入令牌黑名单或改为服务端会话，
    那超出了本次范围。（这也是为什么把有效期设成 7 天而不是更长。）
    """
    _clear_session_cookie(response)
    return AuthResponse(
        user=UserOut(
            id=0, username="", display_name="", learner_id="", created_at=None, last_login_at=None
        ),
        message="已退出登录。",
    )


@router.get("/me", response_model=UserOut | None, summary="当前登录用户")
def me(user: OptionalUser) -> UserOut | None:
    """未登录返回 `null`（而不是 401）—— 前端用它来判断"要不要显示登录页"，
    这不是错误情况，是正常的初始状态。"""
    return UserOut(**user.as_dict()) if user else None


@router.post("/password", response_model=AuthResponse, summary="修改密码")
def change_password(
    payload: dict,
    user: CurrentUser,
    db: Session = Depends(get_db),
) -> AuthResponse:
    from fastapi import HTTPException

    old_password = str(payload.get("old_password") or "")
    new_password = str(payload.get("new_password") or "")
    if len(new_password) < 6 or len(new_password) > 64:
        raise HTTPException(status_code=422, detail="新密码长度需在 6–64 位之间。")

    try:
        auth_service.change_password(
            db, user=user, old_password=old_password, new_password=new_password
        )
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    return AuthResponse(user=UserOut(**user.as_dict()), message="密码已更新。")
