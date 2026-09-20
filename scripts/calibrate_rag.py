"""用真实 embedding 测距：为 RAG_MAX_DISTANCE 校准提供依据。

跑法：
    python scripts/calibrate_rag.py [--document-id 57]

它做的事：把指定资料用**真实 embedding** 索引一遍，然后用一组
「资料内问题」和「资料外问题」去检索，把每条片段到查询的真实余弦距离打出来，
并按分位数给出建议门槛。

为什么需要这个脚本：`RAG_MAX_DISTANCE` 的默认值 0.85 是凭经验设的。
mock 向量的距离分布与真实模型完全不同（mock 相关块 ~0.5、无关块接近 1.0），
所以那个默认值在真实数据上到底该调成多少，只能测。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "api"))

parser = argparse.ArgumentParser(description="真实向量距离分布测量与门槛校准")
parser.add_argument("--document-id", type=int, default=57, help="要索引并检索的资料 id")
parser.add_argument("--top-k", type=int, default=8, help="每条查询取多少候选")
parser.add_argument(
    "--auto-questions",
    action="store_true",
    help="用资料自身的知识点标题作为「资料内问题」（不知道资料内容时用这个）",
)
args = parser.parse_args()

os.environ.setdefault("EMBEDDING_PROVIDER", "api")

import app.db.base  # noqa: E402,F401
from app.core.config import settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.models.document import Document  # noqa: E402
from app.models.knowledge_point import KnowledgePoint  # noqa: E402
from app.rag.embedding import get_embedder  # noqa: E402
from app.rag.vectorstore import VectorStore  # noqa: E402
from app.services import index_service, rag_service  # noqa: E402
from sqlalchemy import select  # noqa: E402

#: 资料内问题（应当命中）
IN_SCOPE = [
    "进程和线程有什么区别？",
    "死锁的产生需要满足哪些条件？",
    "进程控制块是什么？",
    "进程有哪些基本特征？",
]


def auto_questions(document_id: int, limit: int = 4) -> list[str]:
    """用资料自身的知识点标题当查询 —— 这些必然是"资料内"的问题。

    比人工编问题的好处：换成任何一份资料都能直接跑，
    而且问的就是这份资料真正讲过的东西。
    """
    with SessionLocal() as db:
        rows = (
            db.execute(
                select(KnowledgePoint.title)
                .where(KnowledgePoint.document_id == document_id)
                .order_by(KnowledgePoint.order_index)
                .limit(limit)
            )
            .scalars()
            .all()
        )
    return [str(t) for t in rows if t]

#: 资料外问题（应当全部被门槛挡掉）
OUT_OF_SCOPE = [
    "今天上海的天气怎么样？",
    "红烧肉怎么做才好吃？",
    "如何分析股票的技术指标？",
    "世界杯决赛是哪两支球队？",
]


async def main() -> int:
    provider = get_embedder()
    print("=" * 78)
    print("  真实向量距离分布测量 —— RAG_MAX_DISTANCE 校准依据")
    print("=" * 78)
    print(f"  provider = {provider.name} / {provider.model} / {provider.dim} 维")
    print(f"  当前 RAG_MAX_DISTANCE = {settings.rag_max_distance}")
    print(f"  当前 RAG_TOP_K        = {settings.rag_top_k}")

    if not provider.available():
        print("\n  ✘ 未配置 EMBEDDING_API_KEY，无法用真实模型测距。")
        return 2

    in_scope_questions = (
        auto_questions(args.document_id) if args.auto_questions else IN_SCOPE
    )
    if not in_scope_questions:
        in_scope_questions = IN_SCOPE
    print(f"  资料内问题（{len(in_scope_questions)} 条）：" + " / ".join(in_scope_questions[:4]))

    with SessionLocal() as db:
        document = db.get(Document, args.document_id)
        if document is None:
            print(f"\n  ✘ 文档 {args.document_id} 不存在。")
            return 2
        print(f"  资料：《{document.file_name}》{document.chunk_count} 个文本块\n")

        stats = await index_service.index_document(db, args.document_id, provider=provider)
        print(f"  已用真实 embedding 重建索引：{stats.indexed}/{stats.chunk_total} 块，"
              f"{stats.batch_count} 批，{stats.elapsed_ms}ms\n")

    store = VectorStore()

    async def distances(question: str) -> list[tuple[float, str]]:
        sources = await rag_service.retrieve_knowledge(
            question,
            document_ids=[args.document_id],
            top_k=args.top_k,
            provider=provider,
            store=store,
            # 测量时要看**未过滤**的完整分布，所以把门槛放宽到 2.0（余弦距离理论上限）
            max_distance=2.0,
        )
        return [(s.distance, s.content[:46]) for s in sources]

    print("-" * 78)
    print("  【资料内问题】期望：最近距离明显偏低")
    print("-" * 78)
    in_scope_nearest: list[float] = []
    for question in in_scope_questions:
        rows = await distances(question)
        if not rows:
            print(f"  「{question}」→ 无结果")
            continue
        in_scope_nearest.append(rows[0][0])
        print(f"  「{question}」")
        for distance, text in rows[:3]:
            print(f"        {distance:.4f}  {text}…")

    print()
    print("-" * 78)
    print("  【资料外问题】期望：最近距离明显偏高")
    print("-" * 78)
    out_scope_nearest: list[float] = []
    for question in OUT_OF_SCOPE:
        rows = await distances(question)
        if not rows:
            print(f"  「{question}」→ 无结果")
            continue
        out_scope_nearest.append(rows[0][0])
        print(f"  「{question}」 → 最近 {rows[0][0]:.4f}  {rows[0][1]}…")

    print()
    print("=" * 78)
    print("  小结")
    print("=" * 78)
    if in_scope_nearest:
        print(f"  资料内 · 最近距离：最小 {min(in_scope_nearest):.4f} / "
              f"中位 {statistics.median(in_scope_nearest):.4f} / 最大 {max(in_scope_nearest):.4f}")
    if out_scope_nearest:
        print(f"  资料外 · 最近距离：最小 {min(out_scope_nearest):.4f} / "
              f"中位 {statistics.median(out_scope_nearest):.4f} / 最大 {max(out_scope_nearest):.4f}")

    if in_scope_nearest and out_scope_nearest:
        worst_in = max(in_scope_nearest)
        best_out = min(out_scope_nearest)
        print()
        if worst_in < best_out:
            gap = best_out - worst_in
            suggested = round((worst_in + best_out) / 2, 3)
            print(f"  两类问题可分：资料内最差 {worst_in:.4f} < 资料外最好 {best_out:.4f}"
                  f"（间隔 {gap:.4f}）")
            print(f"  → 建议 RAG_MAX_DISTANCE 取两者中点：{suggested}")
        else:
            print(f"  ⚠ 两类问题有重叠（资料内最差 {worst_in:.4f} ≥ 资料外最好 {best_out:.4f}），"
                  "单靠距离门槛无法完全分开")
            print(f"  → 建议取资料内最差与资料外中位之间："
                  f"{round((worst_in + statistics.median(out_scope_nearest)) / 2, 3)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
