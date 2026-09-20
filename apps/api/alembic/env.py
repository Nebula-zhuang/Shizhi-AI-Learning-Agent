"""Alembic 运行环境。

关键点：
1. 连接串从 app.core.config 读取，不写死在 alembic.ini 里，凭据不进版本库；
2. 导入 app.db.base 会连带注册全部 ORM 模型，保证 autogenerate 不漏表；
3. 支持通过环境变量 DATABASE_URL 覆盖目标库，用于在无 MySQL 环境下
   （例如 CI 用 SQLite）验证迁移文件的正确性。
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.core.config import settings
from app.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# autogenerate 的对比基准
target_metadata = Base.metadata


def get_url() -> str:
    """目标数据库连接串。"""
    return settings.sqlalchemy_database_uri


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连接数据库。"""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连接数据库并执行迁移。"""
    connectable = create_engine(get_url(), poolclass=pool.NullPool, future=True)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
