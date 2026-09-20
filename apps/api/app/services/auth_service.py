"""账号服务：注册、登录、查询。

## 为什么 learner_id 不直接用用户主键

用户主键是自增整数，拿它当 `learner_id` 有两个问题：
一是**外部可枚举**（`learner_id=1` 一眼看出是第一个用户），
二是把"账号"与"学习档案"绑死成一个东西。

这里生成的 `learner_id` 是一段随机字符串（`u` + 16 位十六进制），
既不可枚举，也留出了"一个账号将来带多份学习档案"的余地。

## 唯一的例外是演示账号

`scripts/seed_demo_user.py` 会把演示账号的 `learner_id` 显式指定为 `local`
（`DEFAULT_LEARNER_ID`），因为 P0–P5 期间积累的样例学习数据都挂在这个标识上。
所以 `create_user` 允许调用方传入 `learner_id`，但**只在播种时用**。
"""

from __future__ import annotations

import secrets
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.security import hash_password, verify_password
from app.models.user import User

logger = get_logger(__name__)


class AuthError(RuntimeError):
    """对用户可读的认证错误。`status_code` 供路由直接用。"""

    def __init__(self, message: str, *, status_code: int = 400, code: str = "auth_error") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


def new_learner_id() -> str:
    """生成一个不可枚举的学习档案标识。"""
    return f"u{secrets.token_hex(8)}"


def get_user_by_username(db: Session, username: str) -> User | None:
    return db.execute(select(User).where(User.username == username.strip().lower())).scalar_one_or_none()


def get_user_by_id(db: Session, user_id: int) -> User | None:
    return db.get(User, user_id)


def get_user_by_learner_id(db: Session, learner_id: str) -> User | None:
    return db.execute(select(User).where(User.learner_id == learner_id)).scalar_one_or_none()


def create_user(
    db: Session,
    *,
    username: str,
    password: str,
    display_name: str | None = None,
    learner_id: str | None = None,
) -> User:
    """建账号。

    **查重交给数据库的唯一索引**，而不是"先 select 再 insert" ——
    后者在并发注册时会漏（两个请求同时查到"不存在"，然后都插入）。
    这里靠 `IntegrityError` 兜住，任何一个先到都只会成功一个。
    """
    user = User(
        username=username.strip().lower(),
        password_hash=hash_password(password),
        display_name=(display_name or "").strip(),
        learner_id=learner_id or new_learner_id(),
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # 用户名与 learner_id 都有唯一索引，这里分辨一下是哪个撞了
        detail = str(getattr(exc, "orig", exc))
        if "uq_users_username" in detail or "username" in detail:
            raise AuthError("这个登录名已经被用了，换一个吧。", status_code=409, code="username_taken") from exc
        raise AuthError("账号创建失败，换一个再试。", status_code=409, code="duplicate") from exc

    db.refresh(user)
    logger.info("新账号注册：%s（learner_id=%s）", user.username, user.learner_id)
    return user


def authenticate(db: Session, *, username: str, password: str) -> User:
    """校验登录。

    两种失败（用户不存在 / 口令不对）**返回同一句提示**，
    避免把"这个用户名存在"这件事泄露给尝试者。
    """
    generic = AuthError("登录名或密码不对。", status_code=401, code="bad_credentials")

    user = get_user_by_username(db, username)
    if user is None:
        # 仍然跑一次哈希校验，让"用户不存在"和"密码错误"的耗时接近，
        # 不给时序侧信道留下"这个账号存在"的信号。
        verify_password(password, "$2b$12$" + "x" * 53)
        raise generic

    if not verify_password(password, user.password_hash):
        raise generic

    if not user.is_active:
        raise AuthError("这个账号已停用。", status_code=403, code="inactive")

    user.last_login_at = datetime.now()
    db.commit()
    db.refresh(user)
    return user


def change_password(db: Session, *, user: User, old_password: str, new_password: str) -> None:
    if not verify_password(old_password, user.password_hash):
        raise AuthError("当前密码不对。", status_code=400, code="bad_old_password")
    user.password_hash = hash_password(new_password)
    db.commit()
    logger.info("账号 %s 修改了密码", user.username)
