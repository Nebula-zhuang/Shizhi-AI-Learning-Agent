"""数据库连接与会话管理。

设计要点：
1. engine 是「懒连接」的 —— 仅创建连接池对象，不建立实际连接。
   因此即使 MySQL 未启动或凭据未配置，应用依然能正常启动，
   只是健康检查会报 degraded。这样保证了 P0 阶段前后端联调不被数据库卡住。
2. 提供 check_database() 供健康检查使用，返回结构化结果而非抛异常。
3. 提供 ensure_database() 用于首次建库（需要账号具备 CREATE 权限）。
"""

from __future__ import annotations

import re
from collections.abc import Generator
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings as app_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# pool_pre_ping：取连接前先探活，避免 MySQL 空闲断连导致的偶发报错
# pool_recycle：小于 MySQL 默认 wait_timeout(8h)，防止使用到已被服务端关闭的连接
engine: Engine = create_engine(
    app_settings.sqlalchemy_database_uri,
    pool_pre_ping=True,
    pool_recycle=3600,
    pool_size=5,
    max_overflow=10,
    echo=False,
    future=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖注入用的会话工厂。

    用法：
        @router.get("/x")
        def handler(db: Session = Depends(get_db)): ...
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _safe_db_name(name: str) -> str:
    """校验库名，防止拼 SQL 时被注入。"""
    if not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise ValueError(f"非法的数据库名：{name!r}（只允许字母、数字、下划线）")
    return name


def ensure_database() -> dict[str, Any]:
    """确保目标库存在，不存在则创建。需要账号具备 CREATE 权限。"""
    db_name = _safe_db_name(app_settings.mysql_database)
    ddl = (
        f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
        "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
    )
    try:
        # 建库必须连到实例级别，且 DDL 不能跑在事务里
        server_engine = create_engine(
            app_settings.sqlalchemy_server_uri,
            isolation_level="AUTOCOMMIT",
            future=True,
        )
        with server_engine.connect() as conn:
            conn.execute(text(ddl))
        server_engine.dispose()
        logger.info("数据库 `%s` 已就绪", db_name)
        return {"ok": True, "database": db_name}
    except Exception as exc:  # noqa: BLE001
        logger.warning("建库失败：%s", exc)
        return {"ok": False, "database": db_name, "error": f"{type(exc).__name__}: {exc}"}


def check_database() -> dict[str, Any]:
    """探测数据库连通性。永不抛异常，供健康检查使用。"""
    result: dict[str, Any] = {"dsn": app_settings.sqlalchemy_database_uri_masked}
    try:
        with engine.connect() as conn:
            version = conn.execute(text("SELECT VERSION()")).scalar()
            tables = list(conn.execute(text("SHOW TABLES")).scalars())
        result.update(ok=True, version=version, tables=tables, table_count=len(tables))
    except Exception as exc:  # noqa: BLE001
        result.update(
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            hint="请启动 MySQL 并检查 .env 中的 MYSQL_* 配置；首次使用可执行 scripts/init_db.py 建库并迁移。",
        )
    return result


def check_chroma() -> dict[str, Any]:
    """探测 Chroma 可用性。延迟导入，避免影响主流程启动。"""
    from app.rag.vectorstore import vector_store

    return vector_store.health()
