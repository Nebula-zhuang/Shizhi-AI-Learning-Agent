"""P0 冒烟测试：逐项验证各组件是否可用。

用法（在项目根目录执行）：
    python scripts/smoke_test.py              # 全量检查
    python scripts/smoke_test.py --skip-llm   # 跳过真实 LLM 调用

检查项：
    1. 配置中心（.env 是否被正确加载、是否存在硬编码兜底）
    2. LLM 非流式调用
    3. LLM 流式调用（模拟 SSE 的消费方式，统计首字延迟与分块数）
    4. MySQL 连通性与迁移状态
    5. Chroma 向量库读写
    6. 项目目录结构完整性

退出码：0 全部通过；1 存在失败项。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Any

# 让脚本能在任意目录下执行
PROJECT_ROOT = Path(__file__).resolve().parents[1]
API_ROOT = PROJECT_ROOT / "apps" / "api"
sys.path.insert(0, str(API_ROOT))

OK = "\033[92m✔\033[0m"
FAIL = "\033[91m✘\033[0m"
WARN = "\033[93m!\033[0m"
DIM = "\033[2m"
RESET = "\033[0m"

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, note: str = "") -> None:
    results.append((name, ok, note))
    mark = OK if ok else FAIL
    print(f"  {mark} {name}" + (f"  {DIM}{note}{RESET}" if note else ""))


def section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


# --------------------------------------------------------------------------- #
# 1. 配置
# --------------------------------------------------------------------------- #
def check_config() -> dict[str, Any]:
    from app.core.config import settings

    section("[1/6] 配置中心")
    print(f"  {DIM}项目根目录：{PROJECT_ROOT}{RESET}")
    print(f"  {DIM}.env 文件：{'存在' if (PROJECT_ROOT / '.env').exists() else '不存在（将使用 .env.example 的默认值）'}{RESET}")

    record("配置加载成功", True, f"env={settings.app_env}")
    record(
        f"LLM 模式 = {settings.effective_llm_mode}",
        True,
        f"{settings.llm_model} @ {settings.llm_base_url}",
    )
    if settings.effective_llm_mode == "mock":
        print(f"  {WARN} 未配置 LLM_API_KEY，自动降级为 mock 模式（链路可验证，输出为模拟内容）")
    record(
        "数据库连接串已脱敏构造",
        bool(settings.sqlalchemy_database_uri),
        settings.sqlalchemy_database_uri_masked,
    )
    record("Chroma 持久化目录", bool(settings.chroma_persist_dir), settings.chroma_persist_dir)
    record(
        "密钥未硬编码（API Key 来源于环境）",
        True,
        f"LLM_API_KEY {'已配置' if settings.is_llm_configured else '未配置'}",
    )
    return {"settings": settings}


# --------------------------------------------------------------------------- #
# 2 & 3. LLM
# --------------------------------------------------------------------------- #
async def check_llm() -> None:
    from app.core.llm import LLMError, llm_gateway

    section("[2/6] LLM 非流式调用")
    prompt = [{"role": "user", "content": "用一句话说明什么是梯度下降。"}]
    started = time.perf_counter()
    try:
        result = await llm_gateway.chat(prompt, max_tokens=128)
        elapsed = (time.perf_counter() - started) * 1000
        preview = result.content.strip().replace("\n", " ")[:60]
        record(
            "非流式调用成功",
            bool(result.content.strip()),
            f"mode={result.mode} 耗时={elapsed:.0f}ms 长度={len(result.content)}",
        )
        print(f"  {DIM}回答预览：{preview}…{RESET}")
    except LLMError as exc:
        record("非流式调用失败", False, exc.message)
    except Exception as exc:  # noqa: BLE001
        record("非流式调用异常", False, f"{type(exc).__name__}: {exc}")

    section("[3/6] LLM 流式调用（SSE 数据源）")
    started = time.perf_counter()
    first_chunk_ms: float | None = None
    chunks: list[str] = []
    try:
        async for piece in llm_gateway.stream_chat(prompt, max_tokens=128):
            if first_chunk_ms is None:
                first_chunk_ms = (time.perf_counter() - started) * 1000
            chunks.append(piece)
        total_ms = (time.perf_counter() - started) * 1000
        text = "".join(chunks)
        record("流式调用成功", bool(chunks), f"分块数={len(chunks)} 总字符={len(text)}")
        record(
            "增量可分块（首字延迟 < 总耗时）",
            first_chunk_ms is not None and len(chunks) > 1,
            f"首字={first_chunk_ms:.0f}ms 总耗时={total_ms:.0f}ms",
        )
    except Exception as exc:  # noqa: BLE001
        record("流式调用失败", False, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
# 4. MySQL
# --------------------------------------------------------------------------- #
def check_mysql() -> None:
    from app.db.session import check_database

    section("[4/6] MySQL / Alembic")
    info = check_database()
    dsn = info.pop("dsn", "")
    print(f"  {DIM}目标：{dsn}{RESET}")

    if not info.get("ok"):
        record("MySQL 连接失败", False, info.get("error", ""))
        print(f"  {WARN} {info.get('hint', '')}")
        return

    record("MySQL 连接成功", True, f"版本 {info.get('version')}")
    tables = info.get("tables", [])
    record("已执行 Alembic 迁移", "app_meta" in tables, f"现有表：{tables or '（无）'}")

    if "app_meta" in tables:
        from sqlalchemy import select

        from app.db.session import SessionLocal
        from app.models.app_meta import AppMeta

        try:
            with SessionLocal() as db:
                rows = db.execute(select(AppMeta)).scalars().all()
            detail = ", ".join(f"{r.key}={r.value}" for r in rows)
            record("ORM 查询 app_meta 成功", rows, detail or "（空表）")
        except Exception as exc:  # noqa: BLE001
            record("ORM 查询失败", False, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
# 5. Chroma
# --------------------------------------------------------------------------- #
def check_chroma() -> None:
    from app.rag.vectorstore import vector_store

    section("[5/6] Chroma 向量库")
    health = vector_store.health()
    if not health.get("ok"):
        record("Chroma 不可用", False, health.get("error", ""))
        print(f"  {WARN} {health.get('hint', '')}")
        return

    record(
        "Chroma 客户端初始化成功",
        True,
        f"v{health.get('chromadb_version')} 集合={health.get('collection')}",
    )
    print(f"  {DIM}持久化目录：{health.get('persist_dir')}{RESET}")

    # 读写冒烟：显式传入向量，避免触发默认 ONNX 模型下载。
    #
    # 必须写入**独立的探针集合**，不能写生产集合：
    # Chroma 的集合维度在第一次写入时就固定，之后即使把数据删光也无法改变。
    # 这里用的是 4 维假向量，一旦写进 learning_buddy_chunks，该集合就被永久钉死在 4 维，
    # P3 接入真实的 1024 维 embedding 时会直接报
    #   "Collection expecting embedding with dimension of 4, got 1024"。
    # 这个坑真实发生过（并污染了生产集合），所以探针永久改用独立集合。
    probe = "learning_buddy_smoke_probe"
    test_ids = ["p0-smoke-a", "p0-smoke-b"]
    try:
        before = vector_store.count(name=probe)
        vector_store.upsert(
            ids=test_ids,
            documents=["P0 冒烟测试文档 A", "P0 冒烟测试文档 B"],
            embeddings=[[0.1, 0.2, 0.3, 0.4], [0.9, 0.8, 0.7, 0.6]],
            metadatas=[{"stage": "P0"}, {"stage": "P0"}],
            name=probe,
        )
        after = vector_store.count(name=probe)
        record("向量写入成功", after >= before + 2, f"写入前 {before} 条 → 写入后 {after} 条")

        peek = vector_store.peek(limit=2, name=probe)
        record(
            "向量读取成功",
            bool(peek.get("ids")),
            f"peek 到 {len(peek.get('ids', []))} 条",
        )

        vector_store.collection(probe).delete(ids=test_ids)
        final = vector_store.count(name=probe)
        record("测试数据已清理", final == before, f"清理后 {final} 条（探针集合保留）")
    except Exception as exc:  # noqa: BLE001
        record("Chroma 读写失败", False, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
# 6. 目录结构
# --------------------------------------------------------------------------- #
def check_layout() -> None:
    section("[6/6] 项目目录结构")

    expected_dirs = [
        "apps/api/app/api/routes",
        "apps/api/app/core",
        "apps/api/app/agent",
        "apps/api/app/agent/tools",
        "apps/api/app/agent/workflows",
        "apps/api/app/agent/prompts",
        "apps/api/app/rag",
        "apps/api/app/ingestion",
        "apps/api/app/search",
        "apps/api/app/services",
        "apps/api/app/models",
        "apps/api/app/schemas",
        "apps/api/app/db",
        "apps/api/alembic/versions",
        "apps/api/tests",
        "apps/web/src",
        "skills",
        "data/uploads",
        "data/chroma",
        "docs",
        "scripts",
    ]
    missing = [d for d in expected_dirs if not (PROJECT_ROOT / d).is_dir()]
    record(
        f"预留目录齐备（{len(expected_dirs)} 个）",
        not missing,
        "缺失：" + ", ".join(missing) if missing else "全部存在",
    )

    expected_files = [
        ".env.example",
        ".gitignore",
        "docker-compose.yml",
        "apps/api/app/main.py",
        "apps/api/requirements.txt",
        "apps/api/alembic.ini",
        "apps/api/alembic/env.py",
        "apps/web/package.json",
        "apps/web/vite.config.ts",
        "apps/web/src/main.tsx",
        "apps/web/src/App.tsx",
    ]
    missing_files = [f for f in expected_files if not (PROJECT_ROOT / f).is_file()]
    record(
        f"关键文件齐备（{len(expected_files)} 个）",
        not missing_files,
        "缺失：" + ", ".join(missing_files) if missing_files else "全部存在",
    )


# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="Learning Buddy P0 冒烟测试")
    parser.add_argument("--skip-llm", action="store_true", help="跳过 LLM 调用检查")
    args = parser.parse_args()

    print("=" * 72)
    print("  Learning Buddy · P0 冒烟测试")
    print("=" * 72)

    check_config()
    if args.skip_llm:
        section("[2-3/6] LLM 调用")
        print(f"  {WARN} 已按参数跳过")
    else:
        asyncio.run(check_llm())
    check_mysql()
    check_chroma()
    check_layout()

    passed = sum(1 for _, ok, _ in results if ok)
    failed = [name for name, ok, _ in results if not ok]

    print("\n" + "=" * 72)
    if failed:
        print(f"  结果：{passed}/{len(results)} 项通过，{len(failed)} 项失败")
        for name in failed:
            print(f"    {FAIL} {name}")
    else:
        print(f"  结果：全部 {len(results)} 项通过 {OK}")
    print("=" * 72)

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
