"""Tutor Runtime：教学闭环的状态机。

```
LOAD_STATE → RETRIEVE → DECIDE_ACTION → EXECUTE_ACTION → WAIT_USER
                                                              ↓
                    NEXT_TURN ← UPDATE_STATE ← ASSESS ← ───────┘
```

## 状态机跨两次请求

- `POST /api/tutor/start`  跑 `LOAD_STATE … EXECUTE_ACTION → WAIT_USER`
- `POST /api/tutor/answer` 跑 `ASSESS → UPDATE_STATE → NEXT_TURN`（即再走一遍到 EXECUTE_ACTION）

**运行态不落库** —— 它完全由 `learner_kp_states`（长期状态）与 `messages`（过程留痕）
推导得出。这样不存在"存的运行态与实际数据不一致"这种经典问题。
但每轮的流转轨迹会随响应返回（`trace`），便于演示与测试断言状态机真的按序走了。

## 三层降级（任何一层失败都不让整轮失败）

| 环节 | 失败时 |
|---|---|
| `RETRIEVE` | 不带资料继续（内容生成靠知识点自身素材） |
| `DECIDE_ACTION` 的 LLM 调用 | 用 Policy 的动作（它本来就有默认值） |
| `EXECUTE_ACTION` 的内容生成 | 用该动作的兜底模板 |
| `ASSESS` | 启发式评估，`engine=heuristic` |
| `UPDATE_STATE` | 如实报告 `state_updated=false`，**不假装成功** |

## 预算是硬的

每轮 ≤3 次计入预算的 Tool Call、总时长 ≤30 秒（见 `tools/registry.py` 的说明）。
预算耗尽后 `RETRIEVE` 会被跳过，保证"无论如何都能给用户一个回复"。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Sequence

from sqlalchemy.orm import Session

from app.agent import policy
from app.agent.tools.registry import (
    DEFAULT_MAX_SECONDS,
    DEFAULT_MAX_TOOL_CALLS,
    ToolRunner,
)
from app.core.llm import LLMGateway, fast_llm_gateway, llm_gateway

#: 启动时的主网关引用。用来识别「模块级网关是否被替换过」（测试打桩），
#: 见 `_pick_fast_gateway` 的说明。
_BOOT_GATEWAY = llm_gateway
from app.core.logging import get_logger
from app.models.knowledge_check import KnowledgeCheck
from app.models.knowledge_point import KnowledgePoint
from app.models.message import ActionType, Message, MessageRole
from app.models.session import Session, SessionStatus
from app.rag.embedding import EmbeddingProvider
from app.rag.vectorstore import VectorStore
from app.services import (
    assessment_service,
    learner_service,
    memory_service,
    rag_service,
    verify_voice,
)
from app.core.config import settings

logger = get_logger(__name__)

DECISION_PROMPT = (
    Path(__file__).resolve().parent / "prompts" / "tutor_decision.md"
)


class RuntimeState(StrEnum):
    """状态机的全部状态。顺序即执行顺序。"""

    LOAD_STATE = "LOAD_STATE"
    #: P5：装载长期记忆（讲法偏好 / 该知识点的复习状态 / 跨知识点薄弱点）
    LOAD_MEMORY = "LOAD_MEMORY"
    RETRIEVE = "RETRIEVE"
    DECIDE_ACTION = "DECIDE_ACTION"
    EXECUTE_ACTION = "EXECUTE_ACTION"
    WAIT_USER = "WAIT_USER"
    ASSESS = "ASSESS"
    UPDATE_STATE = "UPDATE_STATE"
    NEXT_TURN = "NEXT_TURN"


#: 阶段回调：进入某个状态时把"现在在干嘛"报出去（同步调用，通常只往队列里塞一条）。
StageHook = Callable[[str], None]

#: 内容回调：生成正文时逐块收到文字增量（同步调用）。
ContentHook = Callable[[str], None]

#: 状态 → 给学习者看的阶段话术。
#:
#: 只挑**真的会让人等**的那几步。读状态 / 读记忆只要几毫秒，报出去只会闪一下，
#: 反而让界面显得不稳 —— 所以它们不在这张表里，等于不报。
#:
#: 话术是"我（助教）正在做什么"，不是"系统正在处理"。这正是流式反馈的意义：
#: 让等待变成"有人正在为你忙"，而不是"页面卡住了"。
STAGE_LABEL: dict[RuntimeState, str] = {
    RuntimeState.ASSESS: "正在看你的回答…",
    RuntimeState.UPDATE_STATE: "正在记下这次的结果…",
    RuntimeState.RETRIEVE: "正在翻你的资料…",
    RuntimeState.DECIDE_ACTION: "正在想下一步怎么教…",
    RuntimeState.EXECUTE_ACTION: "正在组织讲解…",
}


@dataclass
class TurnResult:
    """一轮的完整结果。字段刻意做全，前端与测试都不需要再查库。"""

    session_id: int
    knowledge_point_id: int
    title: str
    action: str
    content: str
    reason: str
    #: 本轮进入了哪些状态，按顺序
    trace: list[str] = field(default_factory=list)
    #: Policy 的决策基线（含命中的规则、是否强制、允许集合）
    decision: dict[str, Any] = field(default_factory=dict)
    #: LLM 提案的校验结果（含是否被 Policy 拦截）
    proposal: dict[str, Any] | None = None
    #: 本轮评估（作答轮才有）
    assessment: dict[str, Any] | None = None
    #: 更新前后的学习状态
    state_before: dict[str, Any] = field(default_factory=dict)
    state_after: dict[str, Any] = field(default_factory=dict)
    state_updated: bool = False
    #: RAG 来源
    sources: list[dict[str, Any]] = field(default_factory=list)
    #: Tool 调用留痕与预算占用
    tools: dict[str, Any] = field(default_factory=dict)
    #: 本轮是否发生过降级
    degraded: bool = False
    #: 降级的具体原因，供排障与演示展示
    notes: list[str] = field(default_factory=list)
    #: P5 长期记忆：讲法偏好 / 是否到期 / 主动回顾提示
    memory: dict[str, Any] = field(default_factory=dict)
    #: 会话是否已收束（summarize 后置为 finished）
    session_finished: bool = False
    thresholds: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "knowledge_point_id": self.knowledge_point_id,
            "title": self.title,
            "action": self.action,
            "content": self.content,
            "reason": self.reason,
            "trace": self.trace,
            "decision": self.decision,
            "proposal": self.proposal,
            "assessment": self.assessment,
            "state_before": self.state_before,
            "state_after": self.state_after,
            "state_updated": self.state_updated,
            "sources": self.sources,
            "tools": self.tools,
            "degraded": self.degraded,
            "notes": self.notes,
            "memory": self.memory,
            "session_finished": self.session_finished,
            "thresholds": self.thresholds,
        }


class TutorError(RuntimeError):
    """对用户可读的运行时错误。`status_code` 供路由直接使用。"""

    def __init__(self, message: str, *, status_code: int = 400, code: str = "tutor_error") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


# --------------------------------------------------------------------------- #
# 提示词
# --------------------------------------------------------------------------- #
def _load_text_prompt(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    if "--- USER ---" not in text:
        raise RuntimeError(f"提示词缺少 '--- USER ---' 分隔符：{path}")
    system_part, user_part = text.split("--- USER ---", 1)
    return system_part.replace("--- SYSTEM ---", "", 1).strip(), user_part.strip()


def render_decision_prompt(
    *,
    point: KnowledgePoint,
    state_summary: str,
    assessment_summary: str,
    decision: policy.PolicyDecision,
    style_instruction: str = "",
) -> list[dict[str, str]]:
    """渲染决策提示词。

    **把「允许的动作集合」明确写进提示词**，让模型知道自己的选择空间 ——
    它只负责在空间内择优，不负责划定空间。

    同时**必须把知识点的真实 id 告诉模型**：不给的话它只能猜（实测会编出 123、0
    这种数字），而 kp_id 越权校验会把整个提案拒掉 —— 于是每一轮都退回 Policy 的默认动作，
    Agent 的决策权完全失效。护栏不能变成墙。

    `style_instruction`（P5）是这位学习者跨会话稳定的讲法偏好，从历史错因推导而来，
    见 `memory_service`。它只影响"怎么讲"，不影响"用什么动作"。
    """
    system, template = _load_text_prompt(DECISION_PROMPT)
    forced_note = (
        f"注意：系统已根据学习状态把动作锁定为 `{decision.action}`，你只能选它。"
        if decision.forced
        else "系统没有锁定动作，你可以从上面的集合里自由选择。"
    )
    point_line = f"id={point.id}，标题：{point.title}，摘要：{point.summary or '（无）'}"
    user = (
        template.replace("{{knowledge_point}}", point_line)
        .replace("{{state}}", state_summary or "（新学习者）")
        .replace("{{last_assessment}}", assessment_summary or "（首次接触，暂无评估）")
        .replace("{{allowed_actions}}", "、".join(f"`{a}`" for a in decision.allowed))
        # 只给"情境"，不给阈值数字 —— 阈值由 Policy 掌握，模型不需要也不该知道
        .replace(
            "{{policy_hint}}",
            f"系统倾向 `{decision.action}`，因为{policy.hint_text(decision)}。",
        )
        .replace("{{style_instruction}}", style_instruction or "（暂无偏好，用常规讲法）")
        .replace("{{forced_note}}", forced_note)
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# --------------------------------------------------------------------------- #
# Runtime
# --------------------------------------------------------------------------- #
class TutorRuntime:
    """一次教学会话的运行时。每处理一个请求创建一个实例。"""

    def __init__(
        self,
        db: Session,
        *,
        llm: LLMGateway | None = None,
        embedder: EmbeddingProvider | None = None,
        store: VectorStore | None = None,
        learner_id: str = learner_service.DEFAULT_LEARNER_ID,
        max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
        max_seconds: float = DEFAULT_MAX_SECONDS,
        on_stage: StageHook | None = None,
        on_content: ContentHook | None = None,
    ) -> None:
        self.db = db
        self.llm = llm or llm_gateway
        #: 判断类任务（作答评估、决策）用的网关。
        #: 这两步输出短、模式固定，轻量模型实测快 2.5–3.4 倍；
        #: 而教学内容生成仍走主模型 —— 那里质量比速度重要。
        self.llm_fast = self._pick_fast_gateway(llm)
        self.embedder = embedder
        self.store = store
        self.learner_id = learner_id
        self.runner = ToolRunner(max_calls=max_tool_calls, max_seconds=max_seconds)
        self._trace: list[str] = []
        #: 工具内部的降级原因。工具自己兜住的失败不会体现在 runner 留痕里，
        #: 由这里收集，最终汇总进 TurnResult.notes 与 degraded。
        self._notes: list[str] = []
        #: 可选的阶段回调。流式接口用它把"现在在干嘛"实时推给前端，
        #: 不传就是纯同步行为 —— 既有调用方一行都不用改。
        self._stage_hook = on_stage
        #: 可选的内容回调。给了它，正文就会**边生成边吐出去**，
        #: 学习者从第一个字起就能开始读，不必等整段生成完。
        self._content_hook = on_content

    def _pick_fast_gateway(self, explicit: LLMGateway | None) -> LLMGateway:
        """判断类任务用哪个网关。三条规则，从紧到松：

          1. 调用方**显式传了** `llm`（构造参数）→ 两处都用它
          2. **模块级 `llm_gateway` 被替换过**（测试打桩的常见做法）→ 两处都用替换后的
          3. 否则用 `fast_llm_gateway`（未配置轻量模型时它本身就是主网关）

        规则 2 必须存在：测试经常 monkeypatch 模块级网关来避免真实调用。
        不看这一点的话，判断类任务会绕过打桩直接打到线上模型 ——
        测试会变慢、不稳定，而且是真的在花钱。
        """
        if explicit is not None:
            return explicit
        if llm_gateway is not _BOOT_GATEWAY:
            return self.llm
        return fast_llm_gateway or self.llm

    # ------------------------------------------------------------ 状态机
    def _enter(self, state: RuntimeState) -> None:
        """记录进入某个状态。状态机的可观测性全靠它。"""
        self._trace.append(state.value)
        # 只把**真的会让人等**的那几步报出去。读状态/读记忆只要几毫秒，
        # 报出去只会闪一下，反而更烦。
        if self._stage_hook is not None:
            label = STAGE_LABEL.get(state)
            if label:
                self._stage_hook(label)

    # ------------------------------------------------------------ 对外入口
    async def start(
        self,
        *,
        knowledge_point_id: int,
        document_id: int | None = None,
        session_id: int | None = None,
    ) -> TurnResult:
        """开始（或继续）一个会话，产出第一轮教学内容。"""
        point = self.db.get(KnowledgePoint, knowledge_point_id)
        if point is None:
            raise TutorError(
                f"知识点 {knowledge_point_id} 不存在。", status_code=404, code="kp_not_found"
            )

        if session_id is not None:
            session = self.db.get(Session, session_id)
            if session is None:
                raise TutorError(
                    f"会话 {session_id} 不存在。", status_code=404, code="session_not_found"
                )
        else:
            session = Session(
                learner_id=self.learner_id,
                document_id=document_id if document_id is not None else point.document_id,
                knowledge_point_id=point.id,
                title=f"学习：{point.title}"[:255],
                status=SessionStatus.ACTIVE,
            )
            self.db.add(session)
            self.db.commit()

        return await self._run_decision_cycle(
            session=session, point=point, assessment=None, state_update=None
        )

    async def submit_answer(self, *, session_id: int, user_answer: str) -> TurnResult:
        """提交一次作答：评估 → 更新状态 → 决策下一动作。"""
        session = self.db.get(Session, session_id)
        if session is None:
            raise TutorError(f"会话 {session_id} 不存在。", status_code=404, code="session_not_found")
        if session.knowledge_point_id is None:
            raise TutorError("会话没有关联知识点。", status_code=409, code="session_without_kp")

        point = self.db.get(KnowledgePoint, session.knowledge_point_id)
        if point is None:
            raise TutorError(
                f"会话关联的知识点 {session.knowledge_point_id} 已不存在。",
                status_code=409,
                code="kp_missing",
            )

        answer = (user_answer or "").strip()
        if not answer:
            raise TutorError("回答不能为空。", status_code=422, code="empty_answer")

        # 上一轮的提问即为本次评估的问题
        question = self._last_question(session.id)

        user_message = Message(
            session_id=session.id,
            role=MessageRole.USER,
            content=answer[:4096],
            engine="user",
        )
        self.db.add(user_message)
        self.db.commit()

        # ------------------------------------------------------------ ASSESS
        # 资料检索的入参只有知识点（标题+摘要），与"评估作答"互不依赖 ——
        # 所以提前并发出去：评估要 3.6s、检索只要 0.4s，整段检索被完全遮住。
        #
        # 注意：这里**不调用 `_enter(RETRIEVE)`**，状态的记录位置仍在下面的决策周期里 ——
        # 状态序列（trace）是给排障与演示看的契约，不因为并发而改变顺序。
        # `_retrieve_sources` 内部会自行处理预算与失败降级。
        retrieve_task = asyncio.create_task(self._retrieve_sources(point))

        self._enter(RuntimeState.ASSESS)
        assessment_outcome = await self.runner.call(
            "evaluate_answer",
            assessment_service.evaluate_answer,
            point=point,
            question=question,
            user_answer=answer,
            # 判断类任务 → 轻量模型（生成内容仍走主模型）
            llm=self.llm_fast,
        )
        if assessment_outcome.ok:
            assessment = assessment_outcome.value
        else:
            # 评估工具本身异常（不是模型异常）时的最后一道兜底
            logger.warning("评估工具调用失败，使用启发式：%s", assessment_outcome.error)
            assessment = assessment_service.heuristic_assessment(
                user_answer=answer,
                reference=assessment_service.reference_text(point),
                engine="heuristic(tool_error)",
            )

        self.db.add(
            assessment_service_to_row(
                session_id=session.id,
                message_id=user_message.id,
                knowledge_point_id=point.id,
                question=question,
                user_answer=answer,
                assessment=assessment,
            )
        )
        self.db.commit()

        if assessment.is_fallback:
            self._notes.append(f"作答评估降级为启发式（{assessment.engine}）")

        # ------------------------------------------------------- UPDATE_STATE
        self._enter(RuntimeState.UPDATE_STATE)
        state = learner_service.get_or_create_learner_state(
            self.db, point.id, learner_id=self.learner_id, for_update=True
        )
        before = state.snapshot()
        update_outcome = await self.runner.state_op(
            "update_learning_state",
            _run_update,
            db=self.db,
            state=state,
            correct=assessment.correct,
            score=assessment.score,
            error_type=assessment.error_type,
        )
        update_result = update_outcome.value if update_outcome.ok else None
        state_updated = bool(update_result and update_result.ok)

        if not state_updated:
            reason = update_outcome.error or (update_result.error if update_result else "未知原因")
            logger.error("学习状态更新失败（session=%s）：%s", session.id, reason)

        # ---------------------------------------------------------- NEXT_TURN
        self._enter(RuntimeState.NEXT_TURN)
        # 取回在评估之前就并发出去的检索结果（此时它早已完成，不会阻塞）
        sources = await retrieve_task
        return await self._run_decision_cycle(
            session=session,
            point=point,
            assessment=assessment,
            state_update=(update_result.as_dict() if update_result else None),
            state_before=before,
            state_updated=state_updated,
            prefetched_sources=sources,
        )

    # ------------------------------------------------------------ 决策循环
    async def _retrieve_sources(self, point: KnowledgePoint) -> list[Any]:
        """检索这个知识点相关的资料片段。

        **检索的入参只有知识点本身**（标题 + 摘要），不依赖评估结果、也不依赖决策结果。
        正因为如此，它可以被提前到"评估作答"之前并发跑 ——
        单独抽成方法是让调用方能按需并发，而不是把检索固定在决策周期里串行等待。
        """
        query = f"{point.title} {point.summary or ''}".strip()
        if self.runner.budget.exhausted:
            logger.info("预算已耗尽，跳过资料检索：%s", self.runner.budget.exhausted_reason())
            return []
        outcome = await self.runner.call(
            "retrieve_knowledge",
            rag_service.retrieve_knowledge,
            question=query,
            document_ids=[point.document_id] if point.document_id else None,
            provider=self.embedder,
            store=self.store,
        )
        if outcome.ok:
            return outcome.value or []
        # 检索不到资料不影响教学 —— 内容生成会用知识点自身的素材兜底
        logger.info("资料检索失败，无来源继续：%s", outcome.error)
        return []

    async def _run_decision_cycle(
        self,
        *,
        session: Session,
        point: KnowledgePoint,
        assessment: Any | None,
        state_update: dict[str, Any] | None,
        state_before: dict[str, Any] | None = None,
        state_updated: bool = False,
        prefetched_sources: list[Any] | None = None,
    ) -> TurnResult:
        # -------------------------------------------------------- LOAD_STATE
        self._enter(RuntimeState.LOAD_STATE)
        load_outcome = await self.runner.state_op(
            "get_learner_state",
            _load_state,
            db=self.db,
            knowledge_point_id=point.id,
            learner_id=self.learner_id,
        )
        state = load_outcome.value if load_outcome.ok else None
        if state is None:
            # 读不到状态时按"全新学习者"处理 —— 教学照常进行，不中断
            logger.warning("读取学习状态失败，按新学习者处理：%s", load_outcome.error)
            from app.models.learner_kp_state import LearnerKpState

            state = LearnerKpState(
                learner_id=self.learner_id, knowledge_point_id=point.id, mastery=0
            )
        before_snapshot = state_before if state_before is not None else state.snapshot()

        # -------------------------------------------------------- LOAD_MEMORY
        # P5：装载长期记忆。放在 RETRIEVE 之前，是因为"讲法偏好"要跟着后面的
        # 决策与内容生成一路传下去；放在 LOAD_STATE 之后，是因为它要用到刚读出来的状态。
        #
        # 读不到记忆也照常教学（`load_memory` 内部已保证不抛）——
        # 长期记忆是增强项，不该成为"能不能开始学习"的前置条件。
        self._enter(RuntimeState.LOAD_MEMORY)
        memory = await self.runner.state_op(
            "load_memory",
            memory_service.load_memory,
            db=self.db,
            learner_id=self.learner_id,
            knowledge_point_id=point.id,
        )
        memory_context = (
            memory.value
            if memory.ok and memory.value is not None
            else memory_service.MemoryContext(learner_id=self.learner_id)
        )
        style_instruction = memory_service.style_instruction(memory_context.preferred_style)

        # ----------------------------------------------------------- RETRIEVE
        self._enter(RuntimeState.RETRIEVE)
        # 两种并发策略，取决于调用方：
        #   · 作答轮：检索已被提前到"评估作答"之前并发跑（评估要 3.6s，检索只有 0.4s，
        #     整段被遮住），这里直接取结果；
        #   · 开课轮：前面没有耗时步骤可遮，就在这里与"决策"并发 ——
        #     检索只看知识点，决策只看学习状态，两者互不依赖。
        retrieve_task: asyncio.Task[list[Any]] | None = None
        if prefetched_sources is not None:
            sources: list[Any] = prefetched_sources
        elif self.runner.budget.exhausted:
            logger.info("预算已耗尽，跳过资料检索：%s", self.runner.budget.exhausted_reason())
            sources = []
        else:
            retrieve_task = asyncio.create_task(self._retrieve_sources(point))
            sources = []

        # ------------------------------------------------------ DECIDE_ACTION
        self._enter(RuntimeState.DECIDE_ACTION)
        context = policy.DecisionContext.from_state(state, assessment)
        decision = policy.decide(context)

        proposal_result: dict[str, Any] | None = None
        final_action = decision.action
        final_reason = decision.reason
        decision_confidence = decision.confidence

        # 每轮都让 Agent 提案、都由 Policy 校验 —— 即使动作已被硬阈值锁定。
        #
        # 为什么不跳过：跳过会让"Agent 决策 + Policy 校验"这条链路在部分轮次上不存在，
        # 于是"阈值不可被绕过"就只被验证了一半。统一走一遍的好处是
        # 每轮都有提案、每轮都过闸门，拦截行为在留痕里始终可见。
        # （决策调用不计入 Tool 预算，它是 Agent 自身的推理步骤。）
        llm_outcome = await self.runner.call(
            "decide_action",
            self._decide_with_llm,
            point=point,
            state_summary=assessment_service.summarize_state_for_prompt(context.snapshot()),
            assessment_summary=assessment_service.summarize_assessment_for_prompt(assessment),
            decision=decision,
            style_instruction=style_instruction,
            counted=False,
        )
        if llm_outcome.ok and isinstance(llm_outcome.value, dict):
            validation = policy.validate(llm_outcome.value, decision, allowed_kp_ids=[point.id])
            proposal_result = validation.as_dict()
            if validation.ok:
                final_action = validation.action
                final_reason = validation.reason
                decision_confidence = validation.confidence
            else:
                # 被 Policy 拦截 —— 用 Policy 的动作，理由里写明拦截原因，
                # 让"闸门真的在工作"这件事在界面上也看得见。
                logger.info(
                    "模型提案被 Policy 拦截（%s，提案 %s），回退到 %s",
                    validation.reject_reason,
                    llm_outcome.value.get("action"),
                    decision.action,
                )
                final_reason = validation.reason
        else:
            proposal_result = {
                "ok": False,
                "reject_reason": "llm_unavailable",
                "detail": llm_outcome.error or "决策模型未返回可用结果",
            }
            if decision.forced:
                final_reason = decision.reason
            else:
                # 自由区里模型不可用 → 用 Policy 的推荐动作，理由里说明这是降级
                self._notes.append("决策模型不可用，采用策略默认动作")
                final_reason = f"{decision.reason}（决策模型不可用，采用策略默认选择）"

        # 收拢与"决策"并发跑的那次检索。
        # 放在这里而不是更早，是为了让检索真正与决策重叠 ——
        # 它只依赖知识点，等决策做完再取结果即可。
        if retrieve_task is not None:
            sources = await retrieve_task

        # ----------------------------------------------------- EXECUTE_ACTION
        self._enter(RuntimeState.EXECUTE_ACTION)
        context_text = _format_sources(sources)
        # 把核查结论翻成助教能用的话。**为空是常态** ——
        # 大多数知识点没什么需要特别说明的，这时助教会正常讲课，
        # 而不是每轮加一句"这个我也核实过了"。
        credibility = _credibility_note(self.db, point.id)
        if credibility.silent and not credibility.may_claim_web:
            # 没联网能力是个需要留痕的事实：出问题时能一眼看出
            # "助教没说谎，是这一层本来就没开"
            self._notes.append("本轮无联网核验能力，已禁止声称查过外部资料")
        # 有内容回调时用流式生成：正文边生成边吐出去。
        # 注意仍然走 runner.call —— 工具契约（返回 GeneratedContent）、
        # 预算计数与耗时留痕**一个都没变**，只是生成过程中会回调。
        generator = (
            assessment_service.generate_question_streaming
            if self._content_hook is not None
            else assessment_service.generate_question
        )
        extra = {"on_piece": self._content_hook} if self._content_hook is not None else {}
        content_outcome = await self.runner.call(
            "generate_question",
            generator,
            point=point,
            action=final_action,
            state_summary=assessment_service.summarize_state_for_prompt(context.snapshot()),
            assessment_summary=assessment_service.summarize_assessment_for_prompt(assessment),
            context=context_text,
            style_instruction=style_instruction,
            credibility=credibility.text,
            llm=self.llm,
            **extra,
        )
        generated: assessment_service.GeneratedContent | None = (
            content_outcome.value if content_outcome.ok else None
        )
        if generated is not None and generated.content.strip():
            content = generated.content.strip()
            if generated.degraded:
                # 降级发生在工具内部（模型返回空/模型抛错被工具自己兜住），
                # 光看 runner 的留痕看不出来，必须由工具如实上报
                self._notes.append(f"教学内容使用兜底模板：{generated.error or '模型未返回可用内容'}")
        else:
            content = assessment_service.fallback_content(final_action, point)
            self._notes.append("教学内容生成失败，使用兜底模板")

        # ---------------------------------------------------------- WAIT_USER
        self._enter(RuntimeState.WAIT_USER)
        after_snapshot = state.snapshot() if state_updated else before_snapshot

        # ---------------------------------------------------- P5：主动回顾
        # **验收的核心就在这几行**：新会话的第一轮，如果这个知识点有历史
        # 且曾经卡住过，就把"上次你在这里……"放在教学内容之前主动说出来。
        #
        # 为什么只在首轮：后续轮次学习者刚做完题、注意力正在当前问题上，
        # 每轮都提"上次"会变成噪音。首轮是"重新开始学"的时机，最适合回顾。
        #
        # 为什么提示语是拼出来的而不是让模型自由发挥：
        # 验收要求"能**主动提到**上次的薄弱点" —— 主动的前提是这句话必须来自真实记录。
        # 模型可以润色教学内容，但"上次错在哪"这件事不该由它编。
        is_first_turn = int(session.action_count or 0) == 0
        recalled = bool(is_first_turn and memory_context.should_recall)
        if recalled:
            content = f"{memory_context.recall_note}\n\n---\n\n{content}"
            logger.info(
                "会话 %s 首轮主动回顾：kp=%s 上次错因=%s",
                session.id,
                point.id,
                memory_context.last_error_type,
            )

        assistant_message = Message(
            session_id=session.id,
            role=MessageRole.ASSISTANT,
            content=content[:4096],
            action_type=final_action,
            reason=final_reason[:512],
            state_snapshot=context.snapshot(),
            kp_ids=[point.id],
            refs=[_source_ref(s) for s in sources],
            engine=self.llm.model if self.llm.mode != "mock" else "mock",
        )
        self.db.add(assistant_message)

        session.action_count = int(session.action_count or 0) + 1
        session.last_active_at = datetime.now()
        if final_action == ActionType.SUMMARIZE:
            session.status = SessionStatus.FINISHED
        self.db.commit()

        return TurnResult(
            session_id=session.id,
            knowledge_point_id=point.id,
            title=point.title,
            action=final_action,
            content=content,
            reason=final_reason,
            trace=list(self._trace),
            decision=decision.as_dict(),
            proposal=proposal_result,
            assessment=assessment.as_dict() if assessment is not None else None,
            state_before=before_snapshot,
            state_after=after_snapshot,
            state_updated=state_updated,
            sources=[_source_ref(s) for s in sources],
            tools={**self.runner.as_dict(), "state_update": state_update},
            # runner 的降级（工具调用失败/超时）+ 工具内部的降级（兜底模板/启发式评估）
            degraded=self.runner.degraded or bool(self._notes),
            notes=list(self._notes),
            memory={**memory_context.as_dict(), "recalled": recalled},
            session_finished=session.status == SessionStatus.FINISHED,
            thresholds=policy.thresholds_snapshot(),
        )

    # ------------------------------------------------------------ 内部工具
    async def _decide_with_llm(
        self,
        *,
        point: KnowledgePoint,
        state_summary: str,
        assessment_summary: str,
        decision: policy.PolicyDecision,
        style_instruction: str = "",
    ) -> dict[str, Any]:
        """让 Agent 在允许集合内做选择。失败时由调用方回退到 Policy 的动作。"""
        return await self.llm_fast.chat_json(
            render_decision_prompt(
                point=point,
                state_summary=state_summary,
                assessment_summary=assessment_summary,
                decision=decision,
                style_instruction=style_instruction,
            ),
            max_tokens=400,
            mock_builder=lambda _messages: {
                "action": decision.action,
                "knowledge_point_id": point.id,
                "reason": decision.reason,
                "confidence": decision.confidence,
            },
        )

    def _last_question(self, session_id: int) -> str:
        """取最近一条助手消息的正文作为"提出的问题"。

        用小角标而不是 ORM 关系，避免把整个消息列表拉进内存。
        """
        row = (
            self.db.query(Message)
            .filter(Message.session_id == session_id, Message.role == MessageRole.ASSISTANT)
            .order_by(Message.id.desc())
            .first()
        )
        return (row.content if row else "") or ""


# --------------------------------------------------------------------------- #
# 供 ToolRunner 调用的普通函数（避免把 ORM 会话捕获进闭包）
# --------------------------------------------------------------------------- #
def _load_state(*, db: Session, knowledge_point_id: int, learner_id: str):  # noqa: ANN202
    return learner_service.get_or_create_learner_state(
        db, knowledge_point_id, learner_id=learner_id
    )


def _run_update(*, db: Session, state: Any, correct: bool, score: float, error_type: str | None):  # noqa: ANN202
    return learner_service.update_learning_state(
        db, state, correct=correct, score=score, error_type=error_type
    )


def assessment_service_to_row(  # noqa: ANN201
    *,
    session_id: int,
    message_id: int | None,
    knowledge_point_id: int,
    question: str,
    user_answer: str,
    assessment: Any,
):
    """把 Assessment 落成 `answer_evaluations` 行。"""
    from app.models.answer_evaluation import AnswerEvaluation

    return AnswerEvaluation(
        session_id=session_id,
        message_id=message_id,
        knowledge_point_id=knowledge_point_id,
        question=(question or "")[:2048],
        user_answer=(user_answer or "")[:4096],
        correct=assessment.correct,
        score=assessment.score,
        confidence=assessment.confidence,
        level=assessment.level,
        error_type=assessment.error_type,
        missing_points=assessment.missing_points or None,
        misunderstood_points=assessment.misunderstood_points or None,
        feedback=(assessment.feedback or "")[:2000],
        engine=assessment.engine,
    )


def _source_ref(source: Any) -> dict[str, Any]:
    """RAG 来源 → 前端要的扁平结构。"""
    return {
        "document_id": getattr(source, "document_id", None),
        "file_name": getattr(source, "file_name", ""),
        "chunk_index": getattr(source, "chunk_index", None),
        "page_label": getattr(source, "page_label", ""),
        "page_start": getattr(source, "page_start", None),
        "heading_path": getattr(source, "heading_path", []) or [],
        "distance": round(float(getattr(source, "distance", 0) or 0), 4),
        "content": (getattr(source, "content", "") or "")[:200],
    }


def _credibility_note(db: Session, knowledge_point_id: int) -> verify_voice.CredibilityNote:
    """读这个知识点的核查记录，翻成助教能用的话。

    **失败一律降级为空注记。** 核查结论是"锦上添花"的信息，
    读不到它不该让整轮教学失败 —— 而乱说一句"我查过了"比不说严重得多，
    所以这里的兜底是"什么都不说"，不是"猜一个"。
    """
    try:
        checks = (
            db.query(KnowledgeCheck)
            .filter(KnowledgeCheck.kp_id == knowledge_point_id)
            .order_by(KnowledgeCheck.created_at.asc(), KnowledgeCheck.id.asc())
            .all()
        )
        return verify_voice.build_credibility_note(
            list(checks), web_effective=settings.effective_web_verify
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取核查结论失败（kp=%s），本轮不涉及可信度说明：%s", knowledge_point_id, exc)
        return verify_voice.EMPTY_NOTE


def _format_sources(sources: Sequence[Any]) -> str:
    """把来源片段拼成提示词里的资料段落。"""
    if not sources:
        return ""
    blocks: list[str] = []
    for index, source in enumerate(sources, start=1):
        header = f"[{index}] 《{getattr(source, 'file_name', '')}》{getattr(source, 'page_label', '')}"
        heading = getattr(source, "heading_path", None)
        if heading:
            header += f" · {' / '.join(str(h) for h in heading)}"
        blocks.append(f"{header}\n{(getattr(source, 'content', '') or '')[:1000]}")
    return "\n\n".join(blocks)
