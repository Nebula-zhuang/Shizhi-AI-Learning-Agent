"""P5 回归基线 + 跨会话长期记忆演示。

这个脚本要回答的是技术方案给 P5 定的那一条验收：

> 关闭浏览器重开会话，Tutor 能主动提到上次的薄弱点并优先复习，讲解风格与上次一致。

所以它刻意**跑两个会话**（不是同一会话的下一轮）——
"关闭浏览器重开"在数据上就是"新建一个 session，但学习状态还在"。

    python scripts/p5_smoke.py          # 脚本化评估与决策（确定、零消耗）
    python scripts/p5_smoke.py --live   # 真实 DeepSeek（余额可用时）

覆盖：
  1. 简化遗忘曲线：掌握度越高间隔越长、答错缩短、连错压到最短
  2. 会话一留下薄弱点（连错、错因、复习时间都被记录）
  3. **会话二首轮主动提到上次的薄弱点**（验收核心）
  4. 到期信号进入决策（"该复习了"）
  5. **讲法偏好跨会话一致**（从错因推导，且两次注入同一段指令）
  6. 首屏看板：掌握度概览 + 待复习 + 薄弱点 + 画像
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "api"))

parser = argparse.ArgumentParser(description="P5 长期记忆冒烟测试")
parser.add_argument("--live", action="store_true", help="用真实 DeepSeek 做评估与决策")
args = parser.parse_args()

if not args.live:
    os.environ["LLM_MODE"] = "mock"
    os.environ["EMBEDDING_PROVIDER"] = "mock"

import app.db.base  # noqa: E402,F401
from app.agent.runtime import TutorRuntime  # noqa: E402
from app.core.llm import LLMResult  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.models.chunk import Chunk  # noqa: E402
from app.models.document import Document  # noqa: E402
from app.models.knowledge_point import KnowledgePoint  # noqa: E402
from app.models.learner_kp_state import LearnerKpState  # noqa: E402
from app.models.learner_profile import LearnerProfile  # noqa: E402
from app.models.message import ActionType  # noqa: E402
from app.models.session import Session as TutorSession  # noqa: E402
from app.rag.embedding import MockEmbeddingProvider  # noqa: E402
from app.services import learner_service, memory_service  # noqa: E402

FIXTURE_HASH = "p5smoke" + "6" * 57
KP_TITLE = "进程与线程的区别"

CHUNK_TEXTS = [
    "进程是程序的一次执行过程，是系统进行资源分配和调度的基本单位。",
    "线程是进程内部的执行单元，是处理机调度的基本单位，同一进程内的线程共享地址空间。",
    "进程与线程最核心的区别：进程是资源分配的基本单位，线程是处理机调度的基本单位。",
]

GOOD_ANSWER = (
    "进程是程序的一次执行过程，是系统进行资源分配和调度的基本单位；"
    "线程是进程内部的执行单元，是处理机调度的基本单位。"
    "两者最核心的区别在于：进程是资源分配的基本单位，线程是处理机调度的基本单位；"
    "同一进程内的线程共享该进程的地址空间与打开的文件，因此线程切换开销更小。"
)
BAD_ANSWER = "概念有点混，说不清这两个到底差在哪。"


class ScriptedLLM:
    """脚本化 LLM：评估按队列返回，决策遵循系统倾向。"""

    model = "scripted"
    mode = "mock"

    def __init__(self, verdicts: list[tuple[bool, float, str]] | None = None) -> None:
        self.verdicts = list(verdicts or [])
        self.calls = 0

    async def chat_json(self, messages, *, max_tokens=None, mock_builder=None):  # noqa: ANN001, ANN202
        self.calls += 1
        system = messages[0]["content"] if messages else ""
        if "作答评估" in system:
            correct, score, level = (
                self.verdicts.pop(0) if self.verdicts else (True, 0.95, "mastered")
            )
            return {
                "correct": correct,
                "score": score,
                "confidence": 0.9,
                "level": level,
                "error_type": "none" if correct else "concept_confusion",
                "missing_points": [] if correct else ["没讲清资源与调度的分工"],
                "misunderstood_points": [] if correct else ["把两个概念混为一谈"],
                "feedback": "脚本化评估：" + ("答对了" if correct else "概念混淆"),
            }
        if "教学策略" in system:
            import re

            matched = re.search(r"系统倾向 `([a-z]+)`", messages[-1]["content"])
            return {
                "action": matched.group(1) if matched else "probe",
                "reason": "遵循系统倾向",
                "confidence": 0.7,
            }
        return {"content": f"【脚本化教学内容 #{self.calls}】请继续说明你的理解。"}

    async def chat(self, messages, **_kwargs):  # noqa: ANN001, ANN202
        return LLMResult(content="脚本化", model=self.model, mode=self.mode)


class Checker:
    def __init__(self) -> None:
        self.total = 0
        self.failed = 0

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        self.total += 1
        if not ok:
            self.failed += 1
        print(f"  {'✔' if ok else '✘'} {label}" + (f"  {detail}" if detail else ""))
        return ok


def seed() -> tuple[int, str]:
    """建一份测试资料与学习者，返回 (知识点 id, 学习者标识)。"""
    learner_id = f"p5smoke-{uuid4().hex[:8]}"
    with SessionLocal() as session:
        for stale in session.query(Document).filter(Document.file_hash == FIXTURE_HASH).all():
            session.delete(stale)
        session.commit()

        document = Document(
            file_name="_p5_smoke_test.txt",
            file_type="text",
            file_size=sum(len(c) for c in CHUNK_TEXTS),
            file_hash=FIXTURE_HASH,
            storage_path="smoke/p5.txt",
            page_count=len(CHUNK_TEXTS),
            char_count=sum(len(c) for c in CHUNK_TEXTS),
            chunk_count=len(CHUNK_TEXTS),
            kp_count=1,
            parse_status="ready",
            progress=100,
        )
        session.add(document)
        session.flush()

        for index, text in enumerate(CHUNK_TEXTS):
            session.add(
                Chunk(
                    document_id=document.id,
                    chunk_index=index,
                    content=text,
                    page_start=index + 1,
                    page_end=index + 1,
                    block_type="text",
                    heading_path=["第三章 进程管理"],
                    char_count=len(text),
                )
            )

        point = KnowledgePoint(
            document_id=document.id,
            title=KP_TITLE,
            title_norm=KP_TITLE,
            summary="进程是资源分配的基本单位，线程是处理机调度的基本单位。",
            details=(
                "进程是程序的一次执行过程，是系统进行资源分配和调度的基本单位。"
                "线程是进程内部的执行单元，是处理机调度的基本单位；"
                "同一进程内的线程共享地址空间，因此线程切换开销小于进程切换。"
            ),
            key_points=["进程是资源分配的基本单位", "线程是处理机调度的基本单位", "线程共享地址空间"],
            difficulty=3,
            importance=5,
            confidence=Decimal("0.90"),
            verify_status="unverified",
            heading_path=["第三章 进程管理"],
            source_chunk_indexes=[0, 1, 2],
            source_pages=[1, 2, 3],
            order_index=0,
        )
        session.add(point)
        session.commit()
        return point.id, learner_id


def cleanup(kp_id: int, learner_id: str) -> None:
    with SessionLocal() as session:
        session.query(LearnerKpState).filter(
            LearnerKpState.learner_id == learner_id
        ).delete()
        session.query(LearnerProfile).filter(
            LearnerProfile.learner_id == learner_id
        ).delete()
        for item in (
            session.query(TutorSession).filter(TutorSession.learner_id == learner_id).all()
        ):
            session.delete(item)
        session.commit()

        point = session.get(KnowledgePoint, kp_id)
        if point is not None:
            document = session.get(Document, point.document_id)
            if document is not None:
                session.delete(document)
                session.commit()


def make_runtime(llm, learner_id: str) -> TutorRuntime:
    return TutorRuntime(db=None, llm=llm, embedder=MockEmbeddingProvider(), learner_id=learner_id)  # type: ignore[arg-type]


async def run_session_a(kp_id: int, learner_id: str) -> tuple[int, list]:
    """会话一：连错几次，留下明确的薄弱记录。"""
    llm = None if args.live else ScriptedLLM([(False, 0.2, "not_mastered")] * 3)
    turns: list = []
    with SessionLocal() as db:
        runtime = TutorRuntime(db, llm=llm, embedder=MockEmbeddingProvider(), learner_id=learner_id)
        turns.append(await runtime.start(knowledge_point_id=kp_id))
    for index in range(3):
        with SessionLocal() as db:
            runtime = TutorRuntime(
                db, llm=llm, embedder=MockEmbeddingProvider(), learner_id=learner_id
            )
            turns.append(
                await runtime.submit_answer(
                    session_id=turns[0].session_id,
                    user_answer=BAD_ANSWER if not args.live else f"我不太确定，大概是第 {index + 1} 种说法吧",
                )
            )
    return turns[0].session_id, turns


async def main() -> int:
    c = Checker()
    print("=" * 78)
    print("  Learning Buddy · P5 长期记忆演示与回归")
    print("=" * 78)
    print(f"  模式：{'live（真实 DeepSeek）' if args.live else 'mock（脚本化，零消耗）'}")

    curve = memory_service.CURVE
    print("  遗忘曲线（简化）：")
    for upper, seconds in curve.tiers:
        print(f"    掌握度 < {upper:<5} → {seconds // 60:>5} 分钟后复习")
    print(
        f"    答错时间隔 ×{curve.wrong_factor}；连错 {curve.wrong_streak_threshold} 次"
        f"压到 {curve.min_seconds // 60} 分钟"
    )
    print()

    kp_id, learner_id = seed()
    print(f"  学习者 {learner_id} · 知识点《{KP_TITLE}》\n")

    try:
        # ================================================ 遗忘曲线（纯函数）
        print("[1/5] 简化遗忘曲线")
        weak_gap = memory_service.review_interval_seconds(0.30, correct=True)
        strong_gap = memory_service.review_interval_seconds(0.90, correct=True)
        c.check(weak_gap < strong_gap, "掌握得越牢，下次看得越晚",
                f"{weak_gap // 60} 分钟 vs {strong_gap // 60} 分钟")
        wrong_gap = memory_service.review_interval_seconds(0.70, correct=False, consecutive_wrong=1)
        right_gap = memory_service.review_interval_seconds(0.70, correct=True)
        c.check(wrong_gap < right_gap, "答错就尽快再见", f"{wrong_gap // 60} 分钟 vs {right_gap // 60} 分钟")
        streak_gap = memory_service.review_interval_seconds(0.90, correct=False, consecutive_wrong=2)
        c.check(streak_gap == curve.min_seconds, "连错两次压到最短间隔",
                f"{streak_gap // 60} 分钟")

        # ============================================ 会话一：留下薄弱记录
        print("\n[2/5] 会话一：连错三次，留下薄弱记录")
        session_a, turns_a = await run_session_a(kp_id, learner_id)
        actions_a = [t.action for t in turns_a]
        print(f"     动作序列：{' → '.join(actions_a)}")

        last = turns_a[-1]
        c.check(last.state_after["consecutive_wrong"] >= 2, "连错计数已累计",
                f"{last.state_after['consecutive_wrong']} 次")
        c.check(last.state_after["status"] == "weak", "状态标记为薄弱", last.state_after["status"])
        c.check(not last.state_updated or last.state_after["mastery"] < 0.3,
                "掌握度处于低位", f"{last.state_after['mastery']:.3f}")

        with SessionLocal() as db:
            state = learner_service.get_learner_state(db, kp_id, learner_id=learner_id)
            c.check(state.next_review_at is not None, "已排定下次复习时间",
                    str(state.next_review_at))
            c.check(state.last_error_type is not None, "记录了上次错因", str(state.last_error_type))
            mastery_after_a = float(state.mastery)

        # ============================== 模拟"关掉浏览器，隔一段时间再回来"
        print("\n[3/5] 模拟时间流逝（把复习时间拨到过去）")
        with SessionLocal() as db:
            state = learner_service.get_learner_state(db, kp_id, learner_id=learner_id)
            state.next_review_at = datetime.now() - timedelta(hours=3)
            db.commit()
        c.check(True, "复习时间已到期", "等价于用户隔天再回来")

        # ======================================= 会话二：新会话，应当主动回顾
        print("\n[4/5] 会话二：全新会话 —— 验收核心")
        llm_b = None if args.live else ScriptedLLM()
        with SessionLocal() as db:
            runtime = TutorRuntime(
                db, llm=llm_b, embedder=MockEmbeddingProvider(), learner_id=learner_id
            )
            turn_b = await runtime.start(knowledge_point_id=kp_id)

        c.check(turn_b.session_id != session_a, "确实是新会话（不是同一会话的下一轮）",
                f"#{session_a} → #{turn_b.session_id}")
        c.check(turn_b.memory["recalled"] is True, "**首轮主动回顾**")
        c.check(bool(turn_b.memory["recall_note"]), "给出了回顾提示")
        c.check("连续答错" in turn_b.memory["recall_note"], "提到了上次连错的次数",
                turn_b.memory["recall_note"][:60])
        c.check("概念混淆" in turn_b.memory["recall_note"], "提到了上次的错因")
        c.check(turn_b.memory["recall_note"] in turn_b.content, "回顾提示进了发给学习者的正文")
        c.check(turn_b.memory["is_due"] is True, "**到期信号生效（优先复习）**")
        c.check("该回顾了" in turn_b.memory["recall_note"], "提示里点明了该复习")
        c.check(
            abs(turn_b.state_before["mastery"] - mastery_after_a) < 1e-6,
            "掌握度继承自上次（没有归零）",
            f"{turn_b.state_before['mastery']:.3f}",
        )
        c.check(RuntimeState_has_memory(turn_b.trace), "状态轨迹含 LOAD_MEMORY",
                "→".join(turn_b.trace[:3]))

        print("\n  ── 会话二首轮的实际内容 ──")
        for line in turn_b.content.splitlines():
            print(f"  {line}")

        # ========================================== 讲法偏好跨会话一致
        print("\n[5/5] 讲法偏好跨会话一致 + 首屏看板")
        with SessionLocal() as db:
            profile = memory_service.refresh_profile(db, learner_id=learner_id)
            style = profile.preferred_style
            evidence = profile.style_evidence or {}

        c.check(style == "contrast", "三次概念混淆推演出对比式讲法", style)
        c.check("概念混淆" in str(evidence.get("_note", "")), "留下了推导依据",
                str(evidence.get("_note", "")))

        instruction = memory_service.style_instruction(style)
        c.check(
            turn_b.memory["style_instruction"] == instruction,
            "会话二注入的指令与画像一致（前后端同一段文字）",
        )

        # 再开一个会话，讲法必须还是同一个
        with SessionLocal() as db:
            runtime = TutorRuntime(
                db,
                llm=None if args.live else ScriptedLLM(),
                embedder=MockEmbeddingProvider(),
                learner_id=learner_id,
            )
            turn_c = await runtime.start(knowledge_point_id=kp_id)
        c.check(turn_c.memory["preferred_style"] == style, "再开一个会话，讲法仍然一致", style)

        # 看板
        with SessionLocal() as db:
            board = memory_service.build_dashboard(db, learner_id=learner_id)
        c.check(board["overview"]["tracked"] >= 1, "看板记录了已学的知识点",
                f"已学 {board['overview']['tracked']} 个")
        c.check(
            any(item["knowledge_point_id"] == kp_id for item in board["due_reviews"]),
            "待复习清单包含这个知识点",
        )
        c.check(
            any(item["knowledge_point_id"] == kp_id for item in board["weak_points"]),
            "薄弱点清单包含这个知识点",
        )
        c.check(board["profile"]["style_label"] == "对比式讲法", "看板展示讲法标签",
                board["profile"]["style_label"])
        c.check(
            board["profile"]["style_instruction"] == instruction,
            "看板返回的指令与注入 Prompt 的逐字一致",
        )

        print(f"\n  ── 首屏看板 ──")
        print(f"     掌握度概览：{board['overview']}")
        print(f"     待复习：{[i['title'] for i in board['due_reviews']]}")
        print(f"     薄弱点：{[i['title'] for i in board['weak_points']]}")
        print(f"     讲法：{board['profile']['style_label']}（{board['profile']['style_source']}）")
        print(f"     紧迫度：{board['weak_points'][0]['urgency'] if board['weak_points'] else '-'}")

        # 手动设定不被覆盖
        with SessionLocal() as db:
            memory_service.set_manual_style(db, "stepwise", learner_id=learner_id)
            again = memory_service.refresh_profile(db, learner_id=learner_id)
        c.check(again.preferred_style == "stepwise", "手动设定的讲法不被自动推导覆盖",
                f"{again.preferred_style}（{again.style_source}）")

    finally:
        cleanup(kp_id, learner_id)
        print("\n  测试数据已清理")

    print("\n" + "=" * 78)
    if c.failed:
        print(f"  结果：{c.total - c.failed}/{c.total} 项通过，{c.failed} 项失败 ✘")
    else:
        print(f"  结果：全部 {c.total} 项通过 ✔")
    print("=" * 78)
    return 1 if c.failed else 0


def RuntimeState_has_memory(trace: list[str]) -> bool:
    """轨迹里是否出现过 LOAD_MEMORY。"""
    from app.agent.runtime import RuntimeState

    return RuntimeState.LOAD_MEMORY.value in trace


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
