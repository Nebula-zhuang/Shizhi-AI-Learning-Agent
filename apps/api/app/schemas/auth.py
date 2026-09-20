"""认证相关的请求/响模型。

**校验全部放在这里**（Pydantic 层），而不是散在服务层里 ——
这样"什么算合法用户名"只有一个定义，接口文档也能自动反映出来。

关于口令长度：上限设 64 个字符。这不是 bcrypt 的限制
（`app/core/security.py` 里先做 SHA-256，任意长度都能安全处理），
而是防止有人拿 10MB 的字符串来做哈希消耗攻击。
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

#: 用户名规则：字母开头，字母/数字/下划线，3–20 位。
#: 不用"任意字符"是为了避免同形字与空白带来的"看着一样实则不同"的账号。
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,19}$")


class RegisterRequest(BaseModel):
    username: str = Field(..., description="登录名：字母开头，可含数字与下划线，3–20 位")
    password: str = Field(..., min_length=6, max_length=64, description="口令，至少 6 位")
    display_name: str | None = Field(
        default=None, max_length=32, description="展示名，可中文；不填则用登录名"
    )

    @field_validator("username")
    @classmethod
    def _check_username(cls, value: str) -> str:
        # 统一小写入库：避免 "Tom" 与 "tom" 变成两个账号，
        # 也避免用户自己都记不清当初注册的是哪个大小写。
        cleaned = value.strip().lower()
        if not _USERNAME_RE.match(cleaned):
            raise ValueError("登录名需以字母开头，可含数字与下划线，长度 3–20 位")
        return cleaned

    @field_validator("display_name")
    @classmethod
    def _clean_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=32)
    password: str = Field(..., min_length=1, max_length=64)

    @field_validator("username")
    @classmethod
    def _lower(cls, value: str) -> str:
        return value.strip().lower()


class UserOut(BaseModel):
    """对外的用户表示。

    **没有 password_hash 字段** —— 这不是"忘了加"，是刻意不加：
    只要响应模型里不存在这个字段，任何代码路径都不可能把它序列化出去。
    """

    id: int
    username: str
    display_name: str
    learner_id: str
    created_at: str | None = None
    last_login_at: str | None = None


class AuthResponse(BaseModel):
    """登录/注册成功的返回。

    **不回传令牌** —— 令牌在 httpOnly Cookie 里，前端 JS 拿不到也不需要拿。
    这个响应只用来把"当前是谁"告诉界面。
    """

    user: UserOut
    message: str = ""
