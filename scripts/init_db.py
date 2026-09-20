"""数据库初始化：建库 + 执行 Alembic 迁移。

用法（在项目根目录执行）：
    python scripts/init_db.py            # 建库并升级到最新版本
    python scripts/init_db.py --status   # 只查看当前迁移状态

前置条件：MySQL 已启动，且 .env 中的 MYSQL_* 配置正确。
若 DATABASE_URL 指向 SQLite，则跳过建库步骤，直接执行迁移。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
API_ROOT = PROJECT_ROOT / "apps" / "api"
sys.path.insert(0, str(API_ROOT))


def _load_alembic_config():
    from alembic.config import Config

    cfg = Config(str(API_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_ROOT / "alembic"))
    # 保证 env.py 中 `from app.*` 可导入，无论从哪个目录调用
    cfg.set_main_option("prepend_sys_path", str(API_ROOT))
    return cfg


def main() -> int:
    parser = argparse.ArgumentParser(description="Learning Buddy 数据库初始化")
    parser.add_argument("--status", action="store_true", help="只查看迁移状态，不做变更")
    args = parser.parse_args()

    from alembic import command
    from sqlalchemy import inspect

    from app.core.config import settings
    from app.db.session import engine

    cfg = _load_alembic_config()

    print("=" * 72)
    print("  Learning Buddy · 数据库初始化")
    print("=" * 72)
    print(f"  目标库：{settings.sqlalchemy_database_uri_masked}")

    if args.status:
        # 注意：两个查询必须共用同一个连接，且都在 with 块内 ——
        # 连接一旦出块就被回收，在块外再查会抛异常并被误报成「未初始化」。
        with engine.connect() as conn:
            tables = inspect(conn).get_table_names()
            revision = _current_revision(conn)
        print(f"  现有表：{tables or '（无）'}")
        print(f"  当前版本：{revision}")
        return 0

    # 1) 建库（SQLite 无需建库）
    is_sqlite = settings.sqlalchemy_database_uri.startswith("sqlite")
    if not is_sqlite:
        from app.db.session import ensure_database

        result = ensure_database()
        if not result["ok"]:
            print(f"\n  [失败] 建库失败：{result.get('error')}")
            print("         请确认 MySQL 已启动、账号密码正确，且账号具备 CREATE 权限。")
            return 1
        print(f"  [1/2] 数据库 `{result['database']}` 已就绪")
    else:
        print("  [1/2] 检测到 SQLite，跳过建库")

    # 2) 执行迁移
    try:
        command.upgrade(cfg, "head")
    except Exception as exc:  # noqa: BLE001
        print(f"\n  [失败] 迁移执行失败：{type(exc).__name__}: {exc}")
        return 1
    print("  [2/2] Alembic 迁移已升级至 head")

    # 3) 回读验证
    try:
        from sqlalchemy import select

        from app.db.session import SessionLocal
        from app.models.app_meta import AppMeta

        with SessionLocal() as db:
            rows = db.execute(select(AppMeta)).scalars().all()
        print(f"\n  app_meta 记录（{len(rows)} 条）：")
        for r in rows:
            print(f"    - {r.key} = {r.value}   {r.description or ''}")
    except Exception as exc:  # noqa: BLE001
        print(f"\n  [警告] 回读 app_meta 失败：{type(exc).__name__}: {exc}")

    print("\n  初始化完成。")
    return 0


def _current_revision(conn) -> str:
    try:
        with conn.exec_driver_sql("SELECT version_num FROM alembic_version") as rs:
            row = rs.fetchone()
        return row[0] if row else "（未标记）"
    except Exception:  # noqa: BLE001
        return "（未初始化）"


if __name__ == "__main__":
    raise SystemExit(main())
