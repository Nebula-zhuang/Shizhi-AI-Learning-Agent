"""认证底座：密码哈希与会话令牌。

**只做两件事**，别的东西不要往这里塞 —— 它是安全相关的代码，
越少越好审。

## 密码为什么先过一道 SHA-256 再给 bcrypt

bcrypt 有个硬限制：**只取前 72 个字节**，超出的部分被静默丢弃。
这在中文场景下会真出事 —— 一个 30 字的中文口令就是 90 字节，
后 18 字节等于没设。

所以先做 `sha256` 再 base64（固定 44 字符，永远小于 72 字节），
把任意长度的口令安全地压进 bcrypt 的定义域。
这是 bcrypt 长度限制的通行处理方式，不是自创。

> 注意：base64 保证不会出现 `\\x00`（bcrypt 会把 NUL 当字符串结束符）。

## 令牌为什么不放 localStorage

主界面那份约定写得很清楚：**令牌禁止写进 localStorage**（XSS 一旦发生即可被读走），
也不许拼进 URL。这里的做法是把 JWT 写进 **httpOnly Cookie** ——
前端 JS 根本拿不到它，也就不存在"前端怎么存"这个问题。

同时仍然接受 `Authorization: Bearer`，方便接口文档与脚本调用。
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: 会话 Cookie 的名字。
SESSION_COOKIE = "shizhi_session"

#: 没配 JWT_SECRET 时的兜底密钥。**每次进程启动随机生成** ——
#: 这样"重启后所有人都掉线"会立刻暴露配置缺失，而不是悄悄用一个弱密钥长期跑。
_FALLBACK_SECRET = secrets.token_urlsafe(32)
_warned = False


def _secret() -> str:
    global _warned
    configured = (settings.jwt_secret or "").strip()
    if configured:
        return configured
    if not _warned:
        logger.warning(
            "JWT_SECRET 未配置，正在使用本次进程随机生成的临时密钥 —— "
            "服务重启后所有登录会失效。请在 .env 中设置 JWT_SECRET。"
        )
        _warned = True
    return _FALLBACK_SECRET


# --------------------------------------------------------------------------- #
# 密码
# --------------------------------------------------------------------------- #
def _prehash(raw: str) -> bytes:
    """把任意长度的口令压进 bcrypt 的 72 字节定义域（见模块文档）。"""
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(raw: str) -> str:
    """哈希一个明文口令。返回可直接入库的字符串。"""
    return bcrypt.hashpw(_prehash(raw), bcrypt.gensalt()).decode("ascii")


def verify_password(raw: str, hashed: str) -> bool:
    """校验口令。

    任何异常都返回 False —— 库里存了脏数据不该变成 500，
    更不该变成"校验通过"。
    """
    try:
        return bcrypt.checkpw(_prehash(raw), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- #
# 令牌
# --------------------------------------------------------------------------- #
def create_access_token(*, user_id: int, username: str, learner_id: str) -> str:
    """签发访问令牌。

    载荷只放**身份标识**，不放权限、不放业务数据 ——
    令牌是客户端持有的，放进去的东西等于公开。
    """
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "username": username,
        "learner_id": learner_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.jwt_expire_minutes)).timestamp()),
    }
    return jwt.encode(payload, _secret(), algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any] | None:
    """解出令牌载荷。无效 / 过期 / 签名不对一律返回 None。

    **显式限定算法**：不指定的话，攻击者可以把 header 改成 `alg: none`
    或换成非对称算法来绕过验签。这是 JWT 最经典的一个坑。
    同时 `verify_exp` 保持开启（PyJWT 默认开）。
    """
    try:
        return jwt.decode(
            token,
            _secret(),
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub"]},
        )
    except jwt.PyJWTError:
        return None


def token_expires_at() -> datetime:
    """令牌过期时刻。用来给 Cookie 设同样的 max-age。"""
    return datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
