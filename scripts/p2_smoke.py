"""P2 离线回归基线：关系构建 + 三级校验 + 图谱数据。

默认在 **mock 模式下**跑完整条 P2 链路（建关系 → 三级校验 → 图谱 → 校验收据），
不消耗任何 API 额度、不访问网络、结果可重复，适合作为每次改动后的回归基线。

用法（项目根目录）：
    python scripts/p2_smoke.py               # mock 模式，零消耗
    python scripts/p2_smoke.py --live        # 用真实模型跑一遍模型自评
    python scripts/p2_smoke.py --tavily      # 额外跑一次真实联网核验（需配 TAVILY_API_KEY）

它覆盖的是 **P2 自己的逻辑与契约**：
  - 每条关系都必须带构建依据（不允许随机连边）
  - 三种关系类型都能产出；同级小节之间不能出现从属关系
  - 重建关系是幂等的；不产生自环
  - 三层校验各自留痕；未配 Key 时联网层记 skipped 而不是报错
  - **P2 跑完后 P1 的抽取字段一个字节都没变**（这是本阶段最重要的约定）
  - 硬造一个「疑似混入模型通识」的知识点，验证规则层能抓到它

P1 的解析与抽取链路由 scripts/p1_smoke.py 覆盖，这里不重复。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "api"))

# 必须在导入 app.core.config 之前设置：环境变量优先级高于 .env
if "--live" not in sys.argv:
    import os

    os.environ["LLM_MODE"] = "mock"

import app.db.base  # noqa: E402,F401 - 触发全部模型注册
from app.db.session import SessionLocal  # noqa: E402
from app.models.chunk import Chunk  # noqa: E402
from app.models.document import Document  # noqa: E402
from app.models.knowledge_check import CheckType, CheckVerdict  # noqa: E402
from app.models.knowledge_point import KnowledgePoint, VerifyStatus  # noqa: E402
from app.models.knowledge_relation import RelationSource, RelationType  # noqa: E402
from app.services import relation_service, verify_service  # noqa: E402

FIXTURE_HASH = "p2smoke" + "9" * 57

#: P2 只允许写 verify_status，其余抽取字段一律只读
P1_READONLY_FIELDS = (
    "title",
    "title_norm",
    "summary",
    "details",
    "key_points",
    "difficulty",
    "importance",
    "confidence",
    "tags",
    "heading_path",
    "source_chunk_indexes",
    "source_pages",
    "order_index",
)

CHUNK_TEXTS = [
    "进程是程序的一次执行过程，是系统进行资源分配和调度的基本单位。程序是静态的指令集合。",
    "进程具有动态性、并发性、独立性和异步性四个基本特征，其中动态性是最基本的特征。",
    "进程控制块是操作系统为管理进程设置的专门数据结构，是进程存在的唯一标志。",
    "线程是进程内部的执行单元，也是处理机调度的基本单位，同一进程内的线程共享地址空间。",
    "信号量机制通过 P 操作与 V 操作对信号量进行原子性的加减，是最常用的同步工具。",
    "死锁的产生必须同时满足四个必要条件：互斥、请求并保持、不可剥夺与循环等待。",
]

#: (标题, 章节路径, 来源块, 难度, 重要度, 讲解)
KPS = [
    ("进程的定义", ["3.1 进程的概念"], 0, 1, 5, "进程是程序的一次执行过程，是系统进行资源分配和调度的基本单位。"),
    ("进程与程序的区别", ["3.1 进程的概念"], 0, 2, 4, "程序是静态的指令集合，而进程是动态的执行实体，这是二者的本质区别。"),
    ("进程的四个基本特征", ["3.1 进程的概念"], 1, 2, 5, "进程具有动态性、并发性、独立性和异步性四个基本特征。"),
    ("进程控制块 PCB", ["3.2 进程控制块"], 2, 1, 5, "进程控制块是操作系统为管理进程设置的专门数据结构，是进程存在的唯一标志。"),
    ("线程的定义", ["3.3 线程与进程"], 3, 2, 4, "线程是进程内部的执行单元，也是处理机调度的基本单位。"),
    ("信号量机制", ["3.4 进程同步"], 4, 3, 5, "信号量机制通过 P 操作与 V 操作对信号量进行原子性的加减。"),
    ("死锁的四个必要条件", ["3.5 死锁"], 5, 2, 5, "死锁的产生必须同时满足互斥、请求并保持、不可剥夺与循环等待四个条件。"),
    # 故意制造一处"疑似混入模型通识"：这句话在原文里找不到依据
    (
        "进程调度的优先级策略",
        ["3.6 调度"],
        5,
        2,
        2,
        "一般来说，现代操作系统会为每个进程维护多级反馈队列，并根据历史 CPU 占用率"
        "动态调整其优先级，这一机制最早由 IBM 在 1960 年代的大型机系统上提出。",
    ),
]


class Checker:
    """极简断言收集器：跑完全部检查再统一汇报，避免第一个失败就中断。"""

    def __init__(self) -> None:
        self.total = 0
        self.failed = 0

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        self.total += 1
        mark = "✔" if ok else "✘"
        if not ok:
            self.failed += 1
        suffix = f"  {detail}" if detail else ""
        print(f"  {mark} {label}{suffix}")
        return ok


def snapshot_fields(point: KnowledgePoint) -> dict:
    return {name: getattr(point, name) for name in P1_READONLY_FIELDS}


def seed() -> tuple[int, dict[int, dict], list[str]]:
    """建一份测试文档。返回 (文档 id, 各知识点快照, 标题列表)。"""
    with SessionLocal() as session:
        for stale in (
            session.query(Document).filter(Document.file_hash == FIXTURE_HASH).all()
        ):
            session.delete(stale)
        session.commit()

        document = Document(
            file_name="_p2_smoke_test.txt",
            file_type="text",
            file_size=sum(len(t) for t in CHUNK_TEXTS),
            file_hash=FIXTURE_HASH,
            storage_path="smoke/p2.txt",
            page_count=len(CHUNK_TEXTS),
            char_count=sum(len(t) for t in CHUNK_TEXTS),
            chunk_count=len(CHUNK_TEXTS),
            kp_count=len(KPS),
            parse_status="ready",
            progress=100,
        )
        session.add(document)
        session.flush()
        document_id = document.id

        for index, text in enumerate(CHUNK_TEXTS):
            session.add(
                Chunk(
                    document_id=document_id,
                    chunk_index=index,
                    content=text,
                    page_start=index + 1,
                    page_end=index + 1,
                    block_type="text",
                    heading_path=None,
                    char_count=len(text),
                )
            )

        snapshots: dict[int, dict] = {}
        titles: list[str] = []
        for order, (title, heading, chunk, difficulty, importance, details) in enumerate(KPS):
            point = KnowledgePoint(
                document_id=document_id,
                title=title,
                title_norm=title.lower(),
                summary=details[:60],
                details=details,
                key_points=["要点一", "要点二"],
                difficulty=difficulty,
                importance=importance,
                confidence=Decimal("0.90"),
                verify_status=VerifyStatus.UNVERIFIED,
                tags=["冒烟测试"],
                heading_path=heading,
                source_chunk_indexes=[chunk],
                source_pages=[chunk + 1],
                order_index=order,
            )
            session.add(point)
            session.flush()
            snapshots[point.id] = snapshot_fields(point)
            titles.append(title)

        session.commit()
        return document_id, snapshots, titles


def cleanup(document_id: int) -> None:
    with SessionLocal() as session:
        document = session.get(Document, document_id)
        if document is not None:
            session.delete(document)  # 级联清理 chunks / kps / relations / checks
            session.commit()


async def run(with_tavily: bool) -> int:
    c = Checker()

    print("\n[1/6] 准备测试文档")
    document_id, snapshots, titles = seed()
    kp_ids = list(snapshots)
    c.check(len(snapshots) == len(KPS), "文档已建立", f"{len(snapshots)} 个知识点 / {len(CHUNK_TEXTS)} 个块")

    try:
        # ------------------------------------------------------- 关系构建
        print("\n[2/6] 知识点关系构建")
        with SessionLocal() as session:
            stats = relation_service.rebuild_relations(session, document_id)
            relations = relation_service.list_relations(session, document_id)

        c.check(stats.created > 0, "关系已构建", f"{stats.created} 条 {stats.by_type}")
        c.check(
            all(r.source and r.evidence for r in relations),
            "每条边都带构建依据",
            "无随机连边",
        )
        c.check(
            all(r.from_kp_id != r.to_kp_id for r in relations), "不产生自环"
        )
        c.check(
            all(
                r.relation_type in
                {RelationType.CONTAINS, RelationType.RELATED, RelationType.PREREQUISITE}
                for r in relations
            ),
            "关系类型都在允许集合内",
        )
        c.check(
            all(
                r.source
                in {
                    RelationSource.HEADING_PARENT,
                    RelationSource.TITLE_CONTAINS,
                    RelationSource.SHARED_CHUNK,
                    RelationSource.SAME_SECTION,
                    RelationSource.ORDER_HEURISTIC,
                }
                for r in relations
            ),
            "构建依据都来自已登记的规则",
        )

        # 同级小节之间不应出现从属关系（P1 层级 bug 曾经造成的正是这个错误）
        siblings = {
            3: {"进程的定义", "进程与程序的区别", "进程的四个基本特征"},
        }
        id_by_title = {
            title: kp_id for kp_id, title in zip(kp_ids, titles)
        }
        bad_contains = [
            r
            for r in relations
            if r.relation_type == RelationType.CONTAINS
            and {r.from_kp_id, r.to_kp_id}
            <= {id_by_title[t] for t in siblings[3]}
        ]
        c.check(not bad_contains, "同级小节之间没有从属关系", f"违规 {len(bad_contains)} 条")

        with SessionLocal() as session:
            again = relation_service.rebuild_relations(session, document_id)
            count_after = relation_service.count_relations(session, document_id)
        c.check(
            again.created == stats.created and count_after == stats.created,
            "重建关系幂等",
            f"两次都是 {stats.created} 条",
        )

        # ------------------------------------------------------- 三级校验
        print("\n[3/6] 可信度校验（三级）")
        with SessionLocal() as session:
            vstats = await verify_service.verify_document(
                session, document_id, page_count=len(CHUNK_TEXTS)
            )
        c.check(vstats.rule_done == len(KPS), "规则层覆盖全部知识点", f"{vstats.rule_done} 个")
        c.check(vstats.model_done + vstats.model_skipped == len(KPS), "模型层的每个知识点都有结论")
        c.check(vstats.web_done + vstats.web_skipped == len(KPS), "联网层的每个知识点都有结论")

        with SessionLocal() as session:
            first = verify_service.list_checks(session, kp_ids[0])
        layers = {chk.check_type for chk in first}
        c.check(
            layers == {CheckType.RULE, CheckType.MODEL, CheckType.WEB},
            "三层结果分开留痕",
            " / ".join(sorted(layers)),
        )
        c.check(
            all(chk.engine for chk in first), "每条记录都标明了判定主体", "engine 非空"
        )

        # ------------------------------------------------- 关键约定：不覆盖 P1
        print("\n[4/6] P1 抽取字段完整性")
        with SessionLocal() as session:
            changed = []
            for kp_id, before in snapshots.items():
                point = session.get(KnowledgePoint, kp_id)
                after = snapshot_fields(point)
                for name in P1_READONLY_FIELDS:
                    if after[name] != before[name]:
                        changed.append(f"{kp_id}.{name}")
            statuses = {
                session.get(KnowledgePoint, kp_id).verify_status for kp_id in kp_ids
            }
        c.check(not changed, "P1 抽取字段零改动", f"{len(P1_READONLY_FIELDS)} 个字段逐一比对")
        c.check(
            statuses != {VerifyStatus.UNVERIFIED},
            "verify_status 已被写入",
            "、".join(sorted(statuses)),
        )

        # --------------------------------------------- 规则层能抓到通识外溢
        print("\n[5/6] 质量闸门")
        leak_id = id_by_title["进程调度的优先级策略"]
        with SessionLocal() as session:
            leak_checks = [
                chk
                for chk in verify_service.list_checks(session, leak_id)
                if chk.check_type == CheckType.RULE
            ]
            leak_point = session.get(KnowledgePoint, leak_id)
        leak_items = (leak_checks[-1].evidence or {}).get("items", [])
        leak_hit = next(
            (i for i in leak_items if i["code"] == "common_knowledge_leak"), None
        )
        c.check(
            leak_hit is not None and leak_hit["ok"] is False,
            "抓到了疑似混入模型通识的表述",
            "启发式生效",
        )
        # 断言的是「被质量闸门拦下」这个意图，而不是某个具体状态值：
        # mock 模式下只有启发式报警 -> suspect；
        # live 模式下模型自评会把它判成 unsupported（更严厉）-> conflict。
        # 两者都说明闸门起作用了，写死成一个值会让用例随模式而失败。
        c.check(
            leak_point.verify_status in {VerifyStatus.SUSPECT, VerifyStatus.CONFLICT},
            "该知识点被质量闸门拦下",
            str(leak_point.verify_status),
        )

        # ------------------------------------------------------- 离线/联网
        print("\n[6/6] 联网核验行为")
        if with_tavily:
            from app.core.config import settings
            from app.search.tavily_client import tavily_client

            c.check(tavily_client.available, "Tavily Key 已配置")
            outcome = await tavily_client.search_safe("操作系统 进程 基本单位")
            c.check(
                outcome.ok,
                "真实联网检索成功",
                outcome.summary() if outcome.ok else f"{outcome.code}: {outcome.message}",
            )
            c.check(
                settings.effective_web_verify, "联网核验层已生效", "effective_web_verify=True"
            )
        else:
            web_checks = [chk for chk in first if chk.check_type == CheckType.WEB]
            c.check(
                all(chk.verdict in {CheckVerdict.SKIPPED, CheckVerdict.PASSED} for chk in web_checks),
                "未配 Key 时联网层不报错",
                "记 skipped 并说明原因",
            )
            reasons = " | ".join(chk.reason for chk in web_checks)
            c.check(
                "TAVILY_API_KEY" in reasons or "常识" in reasons or "无需" in reasons,
                "跳过原因对用户可读",
                reasons[:70],
            )

    finally:
        cleanup(document_id)
        print("\n  测试数据已清理")

    print("\n" + "=" * 74)
    if c.failed:
        print(f"  结果：{c.total - c.failed}/{c.total} 项通过，{c.failed} 项失败 ✘")
    else:
        print(f"  结果：全部 {c.total} 项通过 ✔")
    print("=" * 74)
    return 1 if c.failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="P2 离线回归基线")
    parser.add_argument("--live", action="store_true", help="用真实模型跑模型自评")
    parser.add_argument("--tavily", action="store_true", help="额外跑一次真实联网核验")
    args = parser.parse_args()

    print("=" * 74)
    print("  Learning Buddy · P2 冒烟测试（关系构建 + 三级校验）")
    print("=" * 74)
    print(f"  模式：{'live（真实模型）' if args.live else 'mock（零 API 消耗）'}")
    if args.tavily:
        print("  联网：开启真实 Tavily 检索")

    return asyncio.run(run(args.tavily))


if __name__ == "__main__":
    raise SystemExit(main())
