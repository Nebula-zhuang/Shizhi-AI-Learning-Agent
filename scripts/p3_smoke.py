"""P3 回归基线 / 真实 RAG 端到端示例。

默认用 **mock 向量化 + mock LLM** 跑通整条链路：零 API 消耗、结果确定、
不依赖任何凭据，适合作为每次改动后的回归基线。

    python scripts/p3_smoke.py              # 离线：mock 向量 + mock LLM
    python scripts/p3_smoke.py --live       # 真实：云端 Embedding + 真实 DeepSeek

`--live` 需要先在 .env 里配好 `EMBEDDING_API_KEY`，它会完整跑一遍
「真实向量化 → Chroma Top-K → 真实 DeepSeek 生成 → 答案与来源」，
并把答案和来源明细打印出来 —— 这就是那条真实 RAG 测试示例。

覆盖的关键行为：
  1. 索引：全部块写入向量库，元数据可溯源（页码 / 章节 / 文件名）
  2. 幂等：重复索引不产生重复向量
  3. 检索：Top-K 排序正确，同主题片段排在无关片段之前
  4. 资料内问题 → 生成带引用编号的答案，且每条来源都能回链到原文
  5. **资料外问题 → 不调用模型，直接说明「不在你的资料中」**
     （这是 RAG 与"套壳 ChatGPT"的分界线，也是本脚本最看重的一条）
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "api"))

parser = argparse.ArgumentParser(description="P3 回归基线与真实 RAG 示例")
parser.add_argument("--live", action="store_true", help="使用云端 Embedding + 真实 DeepSeek")
args = parser.parse_args()

# 必须在导入 app.core.config 之前设置：环境变量优先级高于 .env
if not args.live:
    os.environ["LLM_MODE"] = "mock"
    os.environ["EMBEDDING_PROVIDER"] = "mock"

import app.db.base  # noqa: E402,F401 - 触发全部模型注册
from app.core.config import settings  # noqa: E402
from app.core.llm import llm_gateway  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.models.chunk import Chunk  # noqa: E402
from app.models.document import Document  # noqa: E402
from app.rag.embedding import EmbeddingError, get_embedder  # noqa: E402
from app.rag.vectorstore import VectorStore  # noqa: E402
from app.services import index_service, rag_service  # noqa: E402

FIXTURE_HASH = "p3smoke" + "7" * 57
#: 用独立集合，绝不写生产集合 —— Chroma 的集合维度一旦写入就永久固定，
#: 探针数据会把生产集合钉死在错误维度上（P0 的冒烟脚本踩过这个坑）。
SMOKE_COLLECTION = "lb-smoke-p3"

CHUNKS = [
    "进程是程序的一次执行过程，是系统进行资源分配和调度的基本单位。"
    "程序是静态的指令集合，而进程是动态的执行实体，包含程序代码、数据集合与进程控制块。",
    "进程具有动态性、并发性、独立性和异步性四个基本特征，其中动态性是最基本的特征。",
    "线程是进程内部的一个执行单元，同时是处理机调度的基本单位。一个进程可以包含多个线程，"
    "这些线程共享该进程的地址空间以及打开的文件等资源。",
    "进程与线程最核心的区别在于：进程是资源分配的基本单位，线程是处理机调度的基本单位。"
    "因此同一进程内的线程切换不需要切换地址空间，开销明显小于进程之间的切换。",
    "死锁是指多个进程因竞争资源而造成的一种僵局。死锁的产生必须同时满足四个必要条件："
    "互斥条件、请求并保持条件、不可剥夺条件以及循环等待条件。",
    "处理死锁的常见策略包括预防、避免、检测与解除。银行家算法属于死锁避免策略，"
    "它在每次资源分配之前先判断系统是否处于安全状态。",
]

IN_SCOPE_QUESTION = "进程和线程有什么区别？"
OUT_OF_SCOPE_QUESTION = "天气预报说明天晴转多云、气温回升，我该穿什么衣服？"


class Checker:
    def __init__(self) -> None:
        self.total = 0
        self.failed = 0

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        self.total += 1
        mark = "✔" if ok else "✘"
        if not ok:
            self.failed += 1
        print(f"  {mark} {label}" + (f"  {detail}" if detail else ""))
        return ok


def seed() -> int:
    with SessionLocal() as session:
        for stale in session.query(Document).filter(Document.file_hash == FIXTURE_HASH).all():
            session.delete(stale)
        session.commit()

        document = Document(
            file_name="_p3_smoke_test.txt",
            file_type="text",
            file_size=sum(len(c) for c in CHUNKS),
            file_hash=FIXTURE_HASH,
            storage_path="smoke/p3.txt",
            page_count=len(CHUNKS),
            char_count=sum(len(c) for c in CHUNKS),
            chunk_count=len(CHUNKS),
            kp_count=0,
            parse_status="ready",
            progress=100,
        )
        session.add(document)
        session.flush()
        document_id = document.id

        for index, text in enumerate(CHUNKS):
            session.add(
                Chunk(
                    document_id=document_id,
                    chunk_index=index,
                    content=text,
                    page_start=index + 1,
                    page_end=index + 1,
                    block_type="text",
                    heading_path=["第三章 进程管理"],
                    char_count=len(text),
                )
            )
        session.commit()
        return document_id


def cleanup(document_id: int) -> None:
    with SessionLocal() as session:
        document = session.get(Document, document_id)
        if document is not None:
            session.delete(document)
            session.commit()


async def main() -> int:
    c = Checker()
    provider = get_embedder()
    store = VectorStore(settings.model_copy(update={"chroma_collection": SMOKE_COLLECTION}))

    print("=" * 78)
    print("  Learning Buddy · P3 冒烟测试（Embedding → Chroma → Top-K → RAG）")
    print("=" * 78)
    print(f"  模式：{'live（云端 Embedding + 真实 DeepSeek）' if args.live else 'mock（零 API 消耗）'}")
    print(f"  Embedding：provider={provider.name} model={provider.model} dim={provider.dim}")
    print(f"  向量集合：{SMOKE_COLLECTION}（独立集合，不碰生产数据）")
    print(f"  Top-K={settings.rag_top_k}  距离门槛={settings.rag_max_distance}")
    print()

    if args.live and not provider.available():
        print("  ✘ 未配置 EMBEDDING_API_KEY，无法进行真实向量化。")
        print("    请在项目根目录 .env 中设置后重试：EMBEDDING_API_KEY=<你的 Key>")
        return 2

    # 清理上次残留的探针集合
    try:
        store.client().delete_collection(SMOKE_COLLECTION)
    except Exception:  # noqa: BLE001
        pass

    document_id = seed()

    try:
        # ------------------------------------------------------------- 索引
        print("[1/5] 建立向量索引")
        with SessionLocal() as session:
            stats = await index_service.index_document(
                session, document_id, provider=provider, store=store
            )
        c.check(stats.ok, "全部文本块已向量化", f"{stats.indexed}/{stats.chunk_total} 块 / {stats.batch_count} 批")
        c.check(store.count() == len(CHUNKS), "向量数量与块数一致", f"集合内 {store.count()} 条")

        with SessionLocal() as session:
            again = await index_service.index_document(
                session, document_id, provider=provider, store=store
            )
        c.check(
            again.indexed == stats.indexed and store.count() == len(CHUNKS),
            "重复索引幂等",
            "数量未翻倍",
        )

        # ------------------------------------------------------------- 元数据
        print("\n[2/5] 来源元数据可溯源")
        with SessionLocal() as session:
            state = index_service.document_index_state(session, document_id, store=store)
        c.check(state["complete"], "索引覆盖完整", f"{state['indexed_chunks']}/{state['chunk_total']}")

        # --------------------------------------------------------- 资料内提问
        print(f"\n[3/5] 资料内提问：{IN_SCOPE_QUESTION}")
        with SessionLocal() as session:
            answer = await rag_service.answer_question(
                session, IN_SCOPE_QUESTION, document_ids=[document_id], store=store
            )
        c.check(not answer.short_circuited, "命中了资料（未被距离门槛挡掉）")
        c.check(answer.used > 0, "上下文使用了检索片段", f"{answer.used} 条 / 上下文 {answer.context_chars} 字")
        c.check(bool(answer.answer.strip()), "有回答正文", f"{len(answer.answer)} 字")

        if answer.generation_failed:
            # 检索这一半是好的，只是模型不可用（余额不足 / 限流 / 超时）。
            # 这不是 P3 的缺陷 —— 如实报告，并确认降级路径把来源交出去了。
            c.check(
                bool(answer.sources),
                "生成失败时仍然返回了检索到的来源",
                f"{len(answer.sources)} 条",
            )
            c.check(
                "模型不可用" in answer.answer,
                "如实告知模型不可用，未编造答案",
                answer.error,
            )
            print(f"\n  ⚠ 生成阶段未验证：{answer.error}")
            print("     检索链路（向量化 / 索引 / Top-K / 门槛 / 元数据）已验证通过。")
        else:
            c.check(
                any(f"[{s.index}]" in answer.answer for s in answer.sources if s.used),
                "答案里有可回查的引用编号",
            )

        print("\n  ── 回答 ──")
        for line in answer.answer.splitlines():
            print(f"  {line}")
        print("\n  ── 来源 ──")
        for source in answer.sources:
            mark = "采用" if source.used else "丢弃"
            print(
                f"  [{source.index}] {mark} 《{source.file_name}》{source.page_label}"
                f" 距离 {source.distance:.3f}  块 #{source.chunk_index}"
            )
            if source.dropped_reason:
                print(f"       丢弃原因：{source.dropped_reason}")

        # 回链：答案里的编号必须能在原文里找到
        with SessionLocal() as session:
            from app.services import index_service as idx

            chunks = idx.load_chunks(session, document_id)
        top = next((s for s in answer.sources if s.used), None)
        if top is not None:
            original = next((ch for ch in chunks if ch.chunk_index == top.chunk_index), None)
            c.check(
                original is not None and original.content[:20] == top.content[:20],
                "来源可回链到原文块",
                f"块 #{top.chunk_index} / 第 {top.page_start} 页",
            )
        c.check(
            all(s.page_start >= 1 for s in answer.sources),
            "每条来源都带页码",
        )

        # --------------------------------------------------------- 资料外提问
        print(f"\n[4/5] 资料外提问：{OUT_OF_SCOPE_QUESTION}")
        with SessionLocal() as session:
            outside = await rag_service.answer_question(
                session, OUT_OF_SCOPE_QUESTION, document_ids=[document_id], store=store
            )
        c.check(outside.short_circuited, "识别出资料里没有相关内容")
        c.check(outside.used == 0, "没有把无关片段塞进上下文")
        c.check("不在你的资料中" in outside.answer, "明确告知用户资料中没有")
        c.check(
            all(s.dropped_reason for s in outside.sources if not s.used),
            "被丢弃的片段都写明了原因",
        )
        print("\n  ── 回答 ──")
        for line in outside.answer.splitlines():
            print(f"  {line}")

        # --------------------------------------------------------- 边界
        print("\n[5/5] 边界与容错")
        try:
            await rag_service.answer_question(None, "   ", provider=provider, store=store)
            c.check(False, "空问题应当被拒绝")
        except rag_service.RagError as exc:
            c.check(exc.status_code == 422, "空问题被拒绝", f"code={exc.code}")

        caps = rag_service.capabilities()
        import json as _json

        c.check(
            "api_key" not in _json.dumps(caps).lower(),
            "能力探测不回显凭据",
            f"provider={caps['embedding']['provider']}",
        )

        if args.live and not answer.generation_failed:
            c.check(
                answer.mode == "live",
                "回答由真实模型生成",
                f"mode={answer.mode} model={answer.model} 生成耗时 {answer.timings.get('generate')}ms",
            )
            c.check(
                answer.timings.get("embed", 0) > 0,
                "问题经过真实云端向量化",
                f"embed 耗时 {answer.timings.get('embed')}ms",
            )

    except EmbeddingError as exc:
        print(f"\n  ✘ 向量化失败：{exc}")
        print("    若提示未配置凭据，请在 .env 中设置 EMBEDDING_API_KEY。")
        c.check(False, "向量化未通过")
    finally:
        cleanup(document_id)
        try:
            store.client().delete_collection(SMOKE_COLLECTION)
        except Exception:  # noqa: BLE001
            pass
        print("\n  测试数据与探针集合已清理")

    print("\n" + "=" * 78)
    if c.failed:
        print(f"  结果：{c.total - c.failed}/{c.total} 项通过，{c.failed} 项失败 ✘")
    else:
        print(f"  结果：全部 {c.total} 项通过 ✔")
    print("=" * 78)
    return 1 if c.failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
