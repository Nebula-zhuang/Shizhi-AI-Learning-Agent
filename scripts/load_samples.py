"""把样例资料包装载进系统，跑完整条流水线。

    python scripts/load_samples.py              # 装载（已存在的会跳过）
    python scripts/load_samples.py --reset      # 先删掉旧的样例，再重新装载
    python scripts/load_samples.py --skip-verify  # 跳过可信度校验（省时间）

对每份样例资料依次执行：
    解析 → 分块 → 知识点抽取 → 关系构建 → 向量索引 → 可信度校验

**走的是产品自己的服务层，不是另写一条捷径** —— 所以这个脚本本身也是
一次端到端回归：它会真实地调用解析器、抽取器、关系规则、embedding 与校验。
演示前跑一遍，等于把整条链路验一次。

装载完直接打开前端就能演示。三份资料分别对应演示链路的三段：
主资料看知识点与图谱，实验指导书看多资料，复习笔记看核验。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "api"))

SAMPLES_DIR = PROJECT_ROOT / "samples"

parser = argparse.ArgumentParser(description="装载样例资料包")
parser.add_argument("--reset", action="store_true", help="先删除已装载的样例再重新装载")
parser.add_argument("--skip-verify", action="store_true", help="跳过可信度校验")
parser.add_argument("--skip-index", action="store_true", help="跳过向量索引")
args = parser.parse_args()

import app.db.base  # noqa: E402,F401
from app.db.session import SessionLocal  # noqa: E402
from app.ingestion.router import store_source  # noqa: E402
from app.models.document import Document  # noqa: E402
from app.rag.vectorstore import vector_store  # noqa: E402
from app.services import (  # noqa: E402
    document_service,
    index_service,
    ingest_pipeline,
    relation_service,
    verify_service,
)


def sample_files() -> list[Path]:
    """样例目录里的资料，按文件名排序（主资料在最前）。"""
    if not SAMPLES_DIR.exists():
        return []
    return sorted(
        [p for p in SAMPLES_DIR.iterdir() if p.is_file() and p.suffix.lower() in {".pdf", ".txt", ".md"}],
        key=lambda p: p.name,
    )


def find_loaded(file_name: str) -> Document | None:
    """按文件名找已装载的资料 —— 样例文件名是稳定的，用它做幂等键。"""
    with SessionLocal() as db:
        return (
            db.query(Document).filter(Document.file_name == file_name).first()
        )


def remove_loaded(file_name: str) -> bool:
    with SessionLocal() as db:
        document = db.query(Document).filter(Document.file_name == file_name).first()
        if document is None:
            return False
        # 先清向量，再删记录 —— `document_service.delete_document` 只清数据库与磁盘文件，
        # 不含向量库（向量库由 index_service 在"先删后写"时自己管）。
        try:
            vector_store.delete_document(document.id)
        except Exception as exc:  # noqa: BLE001 - 向量没建过也无所谓
            print(f"      （清理向量时忽略：{exc}）")
        document_service.delete_document(db, document)
        return True


async def load_one(path: Path) -> int:
    """装载一份资料，返回 document_id。"""
    data = path.read_bytes()

    if args.reset and remove_loaded(path.name):
        print(f"      已删除同名旧资料")

    existing = find_loaded(path.name)
    if existing is not None and not args.reset:
        print(f"      已存在（id={existing.id}），跳过。加 --reset 可重新装载")
        return existing.id

    _hash, storage_path = store_source(data, file_name=path.name)

    with SessionLocal() as db:
        document = document_service.create_document(
            db,
            file_name=path.name,
            file_size=len(data),
            file_hash=_hash,
            storage_path=storage_path,
        )
        document_id = document.id

    print(f"      解析与知识点抽取中…（id={document_id}）")
    await ingest_pipeline.run_pipeline(document_id)

    with SessionLocal() as db:
        document = document_service.get_document(db, document_id)
        if document is None:
            raise RuntimeError(f"文档 {document_id} 在流水线后消失了")
        print(
            f"      解析完成：{document.page_count or 1} 页 / "
            f"{document.chunk_count} 个文本块 / {document.kp_count} 个知识点"
        )
        if document.warnings:
            print(f"      警告 {len(document.warnings)} 条（不影响演示）")

    # ------------------------------------------------------------ 关系构建
    with SessionLocal() as db:
        stats = relation_service.rebuild_relations(db, document_id)
        total = relation_service.count_relations(db, document_id)
        print(f"      关系构建：{total} 条")

    # ------------------------------------------------------------ 向量索引
    if args.skip_index:
        print("      向量索引：已跳过")
    else:
        try:
            with SessionLocal() as db:
                result = await index_service.index_documents(db, [document_id])
            print(f"      向量索引：{result.indexed_chunks} 个块已向量化")
        except Exception as exc:  # noqa: BLE001 - 索引失败不该挡住演示
            print(f"      向量索引失败（不影响知识点与图谱演示）：{exc}")

    # ---------------------------------------------------------- 可信度校验
    if args.skip_verify:
        print("      可信度校验：已跳过")
    else:
        with SessionLocal() as db:
            document = document_service.get_document(db, document_id)
            stats_verify = await verify_service.verify_document(
                db, document_id, page_count=document.page_count or 1
            )
            by_status = stats_verify.by_status or {}
            summary = "  ".join(f"{k}={v}" for k, v in sorted(by_status.items()))
            print(
                f"      可信度校验：{stats_verify.total_points} 个知识点"
                f"（规则 {stats_verify.rule_done} / 模型 {stats_verify.model_done}）"
            )
            if summary:
                print(f"          判定分布：{summary}")

    return document_id


async def main() -> int:
    files = sample_files()
    print("=" * 78)
    print("  Learning Buddy · 装载样例资料包")
    print("=" * 78)

    if not files:
        print(f"\n  样例目录里没有资料：{SAMPLES_DIR}")
        print("  先生成：python scripts/make_samples.py")
        return 2

    print(f"  样例目录：{SAMPLES_DIR}")
    print(f"  共 {len(files)} 份资料\n")

    loaded: list[tuple[str, int]] = []
    for index, path in enumerate(files, start=1):
        size_kb = path.stat().st_size / 1024
        print(f"[{index}/{len(files)}] {path.name}（{size_kb:.0f} KB）")
        try:
            document_id = await load_one(path)
            loaded.append((path.name, document_id))
        except Exception as exc:  # noqa: BLE001 - 一份失败不挡住其它
            print(f"      ✘ 装载失败：{type(exc).__name__}: {exc}")
        print()

    print("=" * 78)
    print("  装载结果")
    print("=" * 78)
    with SessionLocal() as db:
        for name, document_id in loaded:
            document = document_service.get_document(db, document_id)
            if document is None:
                continue
            relations = relation_service.count_relations(db, document_id)
            print(
                f"  id={document_id:<5} {name}\n"
                f"          {document.page_count or 1} 页 / {document.chunk_count} 块 / "
                f"{document.kp_count} 知识点 / {relations} 条关系 / {document.parse_status}"
            )

    print()
    print("  演示建议：")
    print("    1. 资料库 → 看知识点与原文回链（点知识点能跳回原页）")
    print("    2. 知识图谱 → 看章节分带与包含/前置/相关关系")
    print("    3. Tutor 学习 → 选「进程与线程的区别」，故意答错两次，看它换讲法")
    print("    4. 复习笔记里那三处「⚠」标记的陈述，应被核验判为可疑")
    print("=" * 78)
    return 0 if loaded else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
