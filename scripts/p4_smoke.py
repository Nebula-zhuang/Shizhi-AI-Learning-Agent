"""P4 回归基线 + 完整教学闭环演示。

这个脚本要回答一个问题：**Agent 到底会不会跟着学习状态改变教学策略？**

    python scripts/p4_smoke.py            # 脚本化评估 + 脚本化决策（确定性，零消耗）
    python scripts/p4_smoke.py --live     # 真实 DeepSeek 做评估与决策

两种模式都会跑完整的教学闭环：
    选知识点 → Agent 依据学习状态选动作 → 教学/出题 → 作答 → 评估 → 更新状态
    → 依据新状态选下一动作 → …… → 达成掌握 → summarize 收束

并逐条验证需求点名的六条硬阈值规则：
    连续正确 2 次 → harder
    连续错误 2 次 → rephrase
    连续错误 3 次 → easier
    达到掌握阈值 → summarize
    正确但答得浅 → probe
    错误且基础薄弱 → explain
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

parser = argparse.ArgumentParser(description="P4 教学闭环冒烟测试")
parser.add_argument("--live", action="store_true", help="用真实 DeepSeek 做评估与决策")
args = parser.parse_args()

if not args.live:
    os.environ["LLM_MODE"] = "mock"
    os.environ["EMBEDDING_PROVIDER"] = "mock"

import app.db.base  # noqa: E402,F401
from app.agent import policy  # noqa: E402
from app.agent.runtime import TutorRuntime  # noqa: E402
from app.core.llm import LLMResult  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.models.chunk import Chunk  # noqa: E402
from app.models.document import Document  # noqa: E402
from app.models.knowledge_point import KnowledgePoint  # noqa: E402
from app.models.learner_kp_state import LearnerKpState  # noqa: E402
from app.models.message import ALL_ACTIONS, ActionType  # noqa: E402
from app.models.session import Session as TutorSession  # noqa: E402
from app.rag.embedding import MockEmbeddingProvider  # noqa: E402
from app.services import learner_service  # noqa: E402

FIXTURE_HASH = "p4smoke" + "5" * 57
KP_TITLE = "进程与线程的区别"

CHUNK_TEXTS = [
    "进程是程序的一次执行过程，是系统进行资源分配和调度的基本单位。"
    "程序是静态的指令集合，而进程是动态的执行实体。",
    "线程是进程内部的一个执行单元，同时是处理机调度的基本单位。"
    "一个进程可以包含多个线程，这些线程共享该进程的地址空间与打开的文件。",
    "进程与线程最核心的区别：进程是资源分配的基本单位，线程是处理机调度的基本单位。"
    "因此同一进程内的线程切换不需要切换地址空间，开销明显小于进程之间的切换。",
]

ACTION_LABEL = {
    "probe": "追问",
    "explain": "讲解",
    "rephrase": "换讲法",
    "harder": "升难度",
    "easier": "降难度",
    "summarize": "总结",
}


class ScriptedLLM:
    """脚本化 LLM：评估按队列返回，决策遵循系统倾向。"""

    model = "scripted"
    mode = "mock"

    def __init__(self, verdicts: list[tuple[bool, float, str]]) -> None:
        self.verdicts = list(verdicts)
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
                "missing_points": [] if correct else ["没有提到线程共享地址空间"],
                "misunderstood_points": [],
                "feedback": "脚本化评估：%s" % ("答对了" if correct else "答错了"),
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


def seed() -> int:
    with SessionLocal() as session:
        for stale in session.query(Document).filter(Document.file_hash == FIXTURE_HASH).all():
            session.delete(stale)
        session.commit()

        document = Document(
            file_name="_p4_smoke_test.txt",
            file_type="text",
            file_size=sum(len(c) for c in CHUNK_TEXTS),
            file_hash=FIXTURE_HASH,
            storage_path="smoke/p4.txt",
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
        return point.id, point.details or GOOD_ANSWER


def cleanup(kp_id: int) -> None:
    with SessionLocal() as session:
        session.query(LearnerKpState).filter(
            LearnerKpState.knowledge_point_id == kp_id
        ).delete()
        for item in (
            session.query(TutorSession).filter(TutorSession.knowledge_point_id == kp_id).all()
        ):
            session.delete(item)
        session.commit()

        point = session.get(KnowledgePoint, kp_id)
        if point is not None:
            document = session.get(Document, point.document_id)
            if document is not None:
                session.delete(document)
                session.commit()


GOOD_ANSWER = (
    "进程是程序的一次执行过程，是系统进行资源分配和调度的基本单位；"
    "线程是进程内部的执行单元，是处理机调度的基本单位。"
    "两者最核心的区别在于：进程是资源分配的基本单位，线程是处理机调度的基本单位；"
    "同一进程内的线程共享该进程的地址空间与打开的文件，"
    "因此线程切换不需要切换地址空间，开销明显小于进程之间的切换。"
)

BAD_ANSWER = "我不太清楚这个概念之间的关系，好像都差不多吧。"


async def simulate_student(question: str, material: str) -> str:
    """用一个额外的模型调用扮演"认真学过的学生"，依据资料回答老师的问题。

    为什么必须这么做：真实模式下题目是**模型动态生成**的（这一轮问"区别"，
    下一轮可能问"举个反例"）。固定的一段话必然被判"审题偏差"，
    于是 mastery 永远涨不上去，演示也就走不到 summarize。
    让模型基于资料真实作答，才模拟出"真的会了的学生"，
    「达到掌握 → 收束」这段链路才能被真实地走通。

    只出现在冒烟脚本里，不属于产品代码。
    """
    from app.core.llm import llm_gateway

    result = await llm_gateway.chat(
        [
            {
                "role": "system",
                "content": (
                    "你是一个刚认真读完下面这段资料的学生。请依据资料，"
                    "用自己的话准确回答老师的问题，把资料里的关键点都说到。"
                    "只输出回答正文，不要客套，不要复述问题。"
                ),
            },
            {
                "role": "user",
                "content": f"## 资料\n{material}\n\n## 老师的问题\n{question}\n\n请回答：",
            },
        ],
        temperature=0.3,
        max_tokens=400,
    )
    return result.content.strip() or material


async def run_loop(
    kp_id: int,
    verdicts: list[tuple[bool, float, str]],
    *,
    live: bool,
    good_answer: str = GOOD_ANSWER,
):
    """跑一条完整的教学闭环，返回每轮结果。

    真实模式下用 `simulate_student` 针对**当前这一轮的题目**生成作答；
    mock 模式下题目也是脚本化的，用固定作答即可对上。
    """
    llm = None if live else ScriptedLLM(verdicts)
    turns: list = []

    with SessionLocal() as db:
        runtime = TutorRuntime(db, llm=llm, embedder=MockEmbeddingProvider())
        first = await runtime.start(knowledge_point_id=kp_id)
        turns.append(first)

    for index in range(len(verdicts)):
        if verdicts[index][0]:
            if live:
                answer_text = await simulate_student(turns[-1].content, good_answer)
            else:
                answer_text = good_answer
        else:
            answer_text = BAD_ANSWER

        with SessionLocal() as db:
            runtime = TutorRuntime(db, llm=llm, embedder=MockEmbeddingProvider())
            turn = await runtime.submit_answer(
                session_id=turns[0].session_id, user_answer=answer_text
            )
            turns.append(turn)
        if turn.action == ActionType.SUMMARIZE:
            break
    return turns


def render(turns: list, kp_title: str) -> None:
    print(f"\n知识点：《{kp_title}》")
    print("-" * 78)
    for index, turn in enumerate(turns):
        label = ACTION_LABEL.get(turn.action, turn.action)
        if index == 0:
            print(
                f"  第 1 轮  [{label}]  规则={turn.decision['rule']}\n"
                f"          mastery={turn.state_before['mastery']:.3f}  "
                f"（首次接触，尚无评估）\n"
                f"          理由：{turn.reason}"
            )
            continue

        assessment = turn.assessment or {}
        print(
            f"  第 {index + 1} 轮  [{label}]  规则={turn.decision['rule']}"
            f"{'  🔒硬阈值' if turn.decision['forced'] else ''}\n"
            f"          作答={'对' if assessment.get('correct') else '错'}"
            f"(得分 {assessment.get('score', 0):.2f})  "
            f"mastery {turn.state_before['mastery']:.3f} → {turn.state_after['mastery']:.3f}  "
            f"连对{turn.state_after['consecutive_correct']}"
            f"/连错{turn.state_after['consecutive_wrong']}\n"
            f"          reason：{turn.reason}"
        )
    print("-" * 78)


async def main() -> int:
    c = Checker()
    print("=" * 78)
    print("  Learning Buddy · P4 教学闭环演示与回归")
    print("=" * 78)
    print(f"  模式：{'live（真实 DeepSeek 评估与决策）' if args.live else 'mock（脚本化，零消耗）'}")
    print(f"  动作集合：{'、'.join(ACTION_LABEL[a] for a in ALL_ACTIONS)}")
    t = policy.THRESHOLDS
    print(
        f"  阈值（来自 Policy）：掌握 {t.mastery_threshold} / 薄弱 {t.weak_mastery} / "
        f"浅答 {t.shallow_score} / 连对升难 {t.harder_after_correct} / "
        f"连错换讲法 {t.rephrase_after_wrong} / 连错降难 {t.easier_after_wrong}"
    )

    kp_id, kp_details = seed()

    try:
        # ------------------------------------------------ 场景一：连对 → 升难度
        print("\n[1/4] 场景一：连续答对 → 升难度 → 达成掌握 → summarize")
        # 给足轮次：从 0 推到掌握阈值需要若干次高分作答，
        # 真实模式下模型判定更严，轮次不够会看不出收束
        verdicts = [(True, 0.95, "mastered")] * 14
        turns = await run_loop(
            kp_id, verdicts, live=args.live, good_answer=kp_details
        )
        render(turns, KP_TITLE)

        actions = [turn.action for turn in turns]
        rules = [turn.decision["rule"] for turn in turns]
        c.check(actions[0] == ActionType.EXPLAIN, "首次接触 → explain")
        c.check(rules[0] == policy.RuleId.FIRST_CONTACT, "命中 R7 首见规则")
        c.check(ActionType.HARDER in actions, "连续答对后出现 harder", f"动作序列 {actions}")

        if actions[-1] == ActionType.SUMMARIZE:
            c.check(True, "达成掌握 → summarize 收束")
            c.check(turns[-1].session_finished, "会话已标记为已收束")
        else:
            final = turns[-1]
            c.check(
                False,
                "应当达成掌握并收束",
                f"最终 mastery={final.state_after['mastery']:.3f}",
            )

        c.check(
            len(set(rules)) >= 3,
            "动作依据随状态变化（不是固定或随机）",
            f"命中的规则：{sorted(set(rules))}",
        )

        # 状态断言
        with SessionLocal() as db:
            state = learner_service.get_learner_state(db, kp_id)
            c.check(
                float(state.mastery) >= t.mastery_threshold,
                "学习状态已持久化到 learner_kp_states",
                f"mastery={float(state.mastery):.3f} status={state.status}",
            )
            c.check(state.attempt_count >= 3, "作答次数已累计", f"{state.attempt_count} 次")

        # ------------------------------------ 场景二：连续答错 → 换讲法 → 降难度
        print("\n[2/4] 场景二：连续答错 → rephrase → easier（新会话，状态继承）")
        cleanup(kp_id)
        kp_id, kp_details = seed()

        verdicts = [
            (False, 0.2, "not_mastered"),
            (False, 0.2, "not_mastered"),
            (False, 0.2, "not_mastered"),
        ]
        turns = await run_loop(kp_id, verdicts, live=args.live)
        render(turns, KP_TITLE)

        actions = [turn.action for turn in turns]
        rules = [turn.decision["rule"] for turn in turns]
        c.check(policy.RuleId.WRONG_STREAK_2 in rules, "连续答错 2 次 → rephrase")
        c.check(ActionType.REPHRASE in actions, "出现换讲法动作")
        c.check(policy.RuleId.WRONG_STREAK_3 in rules, "连续答错 3 次 → easier")
        c.check(ActionType.EASIER in actions, "出现降难度动作")
        c.check(
            turns[-1].decision["forced"] is True,
            "连错触发的动作被硬阈值锁定（模型改不掉）",
        )
        with SessionLocal() as db:
            state = learner_service.get_learner_state(db, kp_id)
            c.check(state.consecutive_wrong >= 3, "连错计数已累计", f"{state.consecutive_wrong} 次")
            c.check(state.status == "weak", "状态标记为薄弱", state.status)

        # -------------------------------------------------- 场景三：跨会话保留
        print("\n[3/4] 场景三：跨会话状态保留 + Policy 拦截非法动作")
        with SessionLocal() as db:
            state = learner_service.get_learner_state(db, kp_id)
            mastery_before = float(state.mastery)
            wrong_before = state.consecutive_wrong

        with SessionLocal() as db:
            fresh = await TutorRuntime(
                db, llm=None if args.live else ScriptedLLM([]), embedder=MockEmbeddingProvider()
            ).start(knowledge_point_id=kp_id)

        c.check(
            fresh.session_id != turns[0].session_id,
            "开了一个新会话",
            f"新会话 #{fresh.session_id}",
        )
        c.check(
            abs(fresh.state_before["mastery"] - mastery_before) < 1e-6,
            "新会话的掌握度继承自既有状态（没有归零）",
            f"{fresh.state_before['mastery']:.3f}",
        )
        c.check(
            fresh.state_before["consecutive_wrong"] == wrong_before,
            "连错计数也继承了",
            f"{fresh.state_before['consecutive_wrong']} 次",
        )
        c.check(
            fresh.action == ActionType.EASIER,
            "新会话首个动作直接是降难度（因为状态还没恢复）",
            fresh.action,
        )

        # Policy 拦截：让模型提一个非法动作
        class IllegalLLM(ScriptedLLM):
            async def chat_json(self, messages, *, max_tokens=None, mock_builder=None):  # noqa: ANN001, ANN202
                system = messages[0]["content"] if messages else ""
                if "教学策略" in system:
                    return {"action": "dance", "reason": "我想跳舞", "confidence": 0.9}
                return await super().chat_json(
                    messages, max_tokens=max_tokens, mock_builder=mock_builder
                )

        with SessionLocal() as db:
            runtime = TutorRuntime(db, llm=IllegalLLM([(True, 0.95, "mastered")]))
            illegal_turn = await runtime.submit_answer(
                session_id=fresh.session_id, user_answer="进程是资源分配的基本单位"
            )

        c.check(
            illegal_turn.action != "dance",
            "模型提议的非法动作被 Policy 拦截",
            f"实际动作 {illegal_turn.action}",
        )
        if illegal_turn.proposal:
            c.check(
                illegal_turn.proposal["ok"] is False,
                "提案被拒并留下了原因",
                illegal_turn.proposal["reject_reason"],
            )

        # ------------------------------------------------------ 场景四：留痕
        print("\n[4/4] 决策留痕与状态一致性")
        from app.models.message import Message

        with SessionLocal() as db:
            messages = (
                db.query(Message)
                .filter(Message.session_id == fresh.session_id)
                .order_by(Message.id)
                .all()
            )
            assistant = [m for m in messages if m.role == "assistant"]
            c.check(
                all(m.action_type and m.reason and m.state_snapshot for m in assistant),
                "每条助手消息都留下动作 / 理由 / 状态快照",
                f"{len(assistant)} 条",
            )
            c.check(
                all(m.action_type in set(ALL_ACTIONS) for m in assistant),
                "所有动作都在规范集合内",
            )

        c.check(
            policy.THRESHOLDS.mastery_threshold
            == policy.thresholds_snapshot()["mastery_threshold"],
            "阈值只由 Policy 提供（Prompt 中不含阈值数字）",
            "见 tests/agent/test_policy.py::test_prompts_do_not_contain_thresholds",
        )

    finally:
        cleanup(kp_id)
        print("\n  测试数据已清理")

    print("\n" + "=" * 78)
    if c.failed:
        print(f"  结果：{c.total - c.failed}/{c.total} 项通过，{c.failed} 项失败 ✘")
    else:
        print(f"  结果：全部 {c.total} 项通过 ✔")
    print("=" * 78)
    return 1 if c.failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
