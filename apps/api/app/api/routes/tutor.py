"""P4 路由：Tutor 教学闭环。

新增 5 个路径，**P0–P3 的 26 个路径一个都没有改动**
（`/api/chat` 与 `/api/rag/ask` 保持原样，Tutor 是独立的能力入口）。

关于 `async def`：TutorRuntime 内部要做模型调用与向量检索，必须是异步路由。

P6 追加两个**流式**入口（`/start/stream`、`/answer/stream`）：
入参与原有接口完全一致，返回的最后一帧也与原响应体一致，
只是中间会实时推送执行阶段。原有接口保持原样、一行未改。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import policy
from app.api.deps import current_learner_id, require_knowledge_point
from app.agent.runtime import TutorError, TutorRuntime
from app.agent.tools.registry import DEFAULT_MAX_SECONDS, DEFAULT_MAX_TOOL_CALLS
from app.core.llm import llm_gateway
from app.core.logging import get_logger
from app.db.session import get_db
from app.models.answer_evaluation import AnswerEvaluation
from app.models.knowledge_point import KnowledgePoint
from app.models.message import ALL_ACTIONS, ActionType, Message, MessageRole
from app.models.session import Session as TutorSession
from app.schemas.tutor import (
    LearnerProfileSummary,
    ProfileUpdateRequest,
    TutorAnswerRequest,
    TutorAssessment,
    TutorCapabilities,
    TutorDashboardResponse,
    TutorEvaluationItem,
    TutorLearnerState,
    TutorMessageItem,
    TutorSessionResponse,
    TutorStartRequest,
    TutorTurnResponse,
)
from app.services import learner_service, memory_service

logger = get_logger(__name__)

router = APIRouter(prefix="/tutor", tags=["tutor"])

#: 动作的中文标签。放在接口层返回，前端不必再抄一份。
ACTION_LABELS: dict[str, str] = {
    ActionType.PROBE: "追问",
    ActionType.EXPLAIN: "讲解",
    ActionType.REPHRASE: "换讲法",
    ActionType.HARDER: "升难度",
    ActionType.EASIER: "降难度",
    ActionType.SUMMARIZE: "总结",
}


# --------------------------------------------------------------------------- #
# 能力探测
# --------------------------------------------------------------------------- #
@router.get("/capabilities", response_model=TutorCapabilities, summary="教学策略探测")
def tutor_capabilities() -> TutorCapabilities:
    """返回动作集合与阈值。

    阈值来自 `policy.py`（唯一权威），前端据此渲染规则说明，不必硬编码数字 ——
    这样改策略时前端不需要跟着改。
    """
    return TutorCapabilities(
        actions=list(ALL_ACTIONS),
        action_labels=ACTION_LABELS,
        thresholds=policy.thresholds_snapshot(),
        max_tool_calls=DEFAULT_MAX_TOOL_CALLS,
        max_seconds=DEFAULT_MAX_SECONDS,
        llm_mode=llm_gateway.mode,
        llm_model=llm_gateway.model,
    )


# --------------------------------------------------------------------------- #
# 开始学习 / 提交作答
# --------------------------------------------------------------------------- #
@router.post("/start", response_model=TutorTurnResponse, summary="开始或继续一次学习")
async def tutor_start(
    payload: TutorStartRequest,
    db: Session = Depends(get_db),
    learner_id: str = Depends(current_learner_id),
) -> TutorTurnResponse:
    """产出第一轮教学：读取学习状态 → 检索资料 → 决定动作 → 生成内容。

    **不传 `session_id` 会新建会话，但学习状态是跨会话累积的** ——
    新会话不会让掌握度归零，这是"长期记忆"的关键。
    """
    # 授权放路由层，不放运行时 —— 运行时的职责是教学；
    # 而且运行时也被测试与脚本直接调用（那边没有账号上下文）。
    require_knowledge_point(db, payload.knowledge_point_id, learner_id)

    runtime = TutorRuntime(db, learner_id=learner_id)
    try:
        turn = await runtime.start(
            knowledge_point_id=payload.knowledge_point_id,
            document_id=payload.document_id,
            session_id=payload.session_id,
        )
    except TutorError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return TutorTurnResponse(**turn.as_dict())


@router.post("/answer", response_model=TutorTurnResponse, summary="提交作答并获取下一轮")
async def tutor_answer(
    payload: TutorAnswerRequest,
    db: Session = Depends(get_db),
    learner_id: str = Depends(current_learner_id),
) -> TutorTurnResponse:
    """评估作答 → 更新学习状态 → 决定下一动作。

    返回里同时包含：本轮评估、状态变化（mastery 前后值）、下一动作与决策理由。
    """
    runtime = TutorRuntime(db, learner_id=learner_id)
    try:
        turn = await runtime.submit_answer(
            session_id=payload.session_id, user_answer=payload.answer
        )
    except TutorError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return TutorTurnResponse(**turn.as_dict())


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 流式版本（阶段反馈）
# --------------------------------------------------------------------------- #
def _sse(event: str, data: dict) -> str:
    """SSE 帧。data 用 JSON，前端按事件名分派。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _stream_call(factory, opening: str) -> AsyncIterator[str]:
    """把一个会跑好几秒的教学回合，变成"阶段与正文都实时可见"的流。

    推三种帧：
      · `stage`   —— 后台现在在干嘛（来自运行时状态机，**不是假进度**）
      · `content` —— 正文的文字增量（边生成边显示，从第一个字起就能读）
      · `done`    —— 最终响应体，与同步接口**完全一致**

    阶段的意义：前端看到"正在翻你的资料"时，后台确实正在检索资料。
    内容增量的意义：生成通常是最慢的一段（可到 7 秒），
    等生成完再显示就是干等；边生成边显示，阅读时间与生成时间重叠。

    `factory(on_stage, on_content)` 返回一个 awaitable，产出最终的响应体 dict。
    """
    # 两类事件共用一个队列，弹出顺序即真实发生顺序
    queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()

    def on_stage(label: str) -> None:
        queue.put_nowait(("stage", label))

    def on_content(piece: str) -> None:
        queue.put_nowait(("content", piece))

    async def worker() -> dict:
        try:
            return await factory(on_stage, on_content)
        finally:
            # 无论成功、失败还是抛错，都要放哨兵，否则外层会一直等下去
            queue.put_nowait(None)

    task = asyncio.create_task(worker())
    # 先给一帧，避免第一个真实阶段到来之前界面空着
    yield _sse("stage", {"label": opening})

    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            kind, value = item
            yield _sse(kind, {"label": value} if kind == "stage" else {"text": value})
        yield _sse("done", await task)
    except TutorError as exc:
        yield _sse("error", {"message": exc.message, "status": exc.status_code})
    except asyncio.CancelledError:
        task.cancel()
        raise
    except Exception as exc:  # noqa: BLE001 - 流里必须兜住，否则前端只看到连接断开
        logger.exception("流式教学失败")
        yield _sse("error", {"message": f"这一轮没能完成：{exc}", "status": 500})
    finally:
        if not task.done():
            task.cancel()


@router.post("/answer/stream", summary="提交作答（流式阶段反馈）")
async def tutor_answer_stream(
    payload: TutorAnswerRequest,
    db: Session = Depends(get_db),
    learner_id: str = Depends(current_learner_id),
):
    """与 `/answer` **同样的入参、同样的最终结果**，区别只是中间会推阶段与正文增量。

    为什么要它：一轮要 6-13 秒（三次串行模型调用 + 生成结构化提问说明），
    前端如果只有"加载中"，学习者会以为页面死了。流式把真实阶段摊开，
    正文也边生成边显示 —— 阅读时间与生成时间重叠。

    原有 `/answer` 一行未改，前端可以按需切换。
    """

    async def factory(on_stage, on_content):
        runtime = TutorRuntime(
            db,
            learner_id=learner_id,
            on_stage=on_stage,
            on_content=on_content,
        )
        turn = await runtime.submit_answer(
            session_id=payload.session_id, user_answer=payload.answer
        )
        return turn.as_dict()

    return StreamingResponse(
        _stream_call(factory, "正在看你的回答…"),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/start/stream", summary="开始学习（流式阶段反馈）")
async def tutor_start_stream(
    payload: TutorStartRequest,
    db: Session = Depends(get_db),
    learner_id: str = Depends(current_learner_id),
):
    """与 `/start` 同样的入参和最终结果，中间推送真实阶段与正文增量。"""

    require_knowledge_point(db, payload.knowledge_point_id, learner_id)

    async def factory(on_stage, on_content):
        runtime = TutorRuntime(
            db,
            learner_id=learner_id,
            on_stage=on_stage,
            on_content=on_content,
        )
        turn = await runtime.start(
            knowledge_point_id=payload.knowledge_point_id,
            document_id=payload.document_id,
            session_id=payload.session_id,
        )
        return turn.as_dict()

    return StreamingResponse(
        _stream_call(factory, "正在翻你的资料…"),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------------- #
# 会话详情
# --------------------------------------------------------------------------- #
@router.get(
    "/sessions/{session_id}", response_model=TutorSessionResponse, summary="会话全貌"
)
def tutor_session(
    session_id: int,
    db: Session = Depends(get_db),
    learner_id: str = Depends(current_learner_id),
) -> TutorSessionResponse:
    """消息 + 每轮决策 + 每次评估。

    `action_sequence` 是从助手消息里提取的动作序列 —— 演示时一眼就能看出
    "教学策略确实随状态在变"，而不是每轮随机。

    ⚠️ **必须按 learner 校验归属。** 这个接口返回的是完整对话内容、
    学习者的作答与评分 —— 曾经它只依赖 `get_db`、按主键直取会话，
    于是任何人（甚至未登录）枚举 `session_id` 就能读到别人的会话。
    """
    session = db.get(TutorSession, session_id)
    # 不存在与不属于当前 learner **都返回 404** ——
    # 403 等于确认"这个 id 确实存在"，那就是一个用来枚举他人会话的接口。
    if session is None or session.learner_id != learner_id:
        raise HTTPException(status_code=404, detail=f"会话 {session_id} 不存在。")

    messages = list(
        db.execute(
            select(Message).where(Message.session_id == session_id).order_by(Message.id)
        )
        .scalars()
        .all()
    )
    evaluations = list(
        db.execute(
            select(AnswerEvaluation)
            .where(AnswerEvaluation.session_id == session_id)
            .order_by(AnswerEvaluation.id)
        )
        .scalars()
        .all()
    )

    state = None
    if session.knowledge_point_id is not None:
        state = learner_service.get_learner_state(
            db, session.knowledge_point_id, learner_id=session.learner_id
        )

    return TutorSessionResponse(
        session_id=session.id,
        learner_id=session.learner_id,
        document_id=session.document_id,
        knowledge_point_id=session.knowledge_point_id,
        title=session.title,
        status=session.status,
        action_count=int(session.action_count or 0),
        created_at=session.created_at.isoformat(),
        last_active_at=session.last_active_at.isoformat(),
        messages=[
            TutorMessageItem(
                id=m.id,
                role=m.role,
                content=m.content,
                action_type=m.action_type,
                reason=m.reason or "",
                state_snapshot=m.state_snapshot,
                refs=m.refs,
                engine=m.engine or "",
                created_at=m.created_at.isoformat(),
            )
            for m in messages
        ],
        evaluations=[
            TutorEvaluationItem(
                id=e.id,
                message_id=e.message_id,
                knowledge_point_id=e.knowledge_point_id,
                question=e.question or "",
                user_answer=e.user_answer or "",
                correct=bool(e.correct),
                score=float(e.score or 0),
                confidence=float(e.confidence or 0),
                level=e.level,
                error_type=e.error_type,
                missing_points=e.missing_points or [],
                misunderstood_points=e.misunderstood_points or [],
                feedback=e.feedback or "",
                engine=e.engine or "",
                created_at=e.created_at.isoformat(),
            )
            for e in evaluations
        ],
        action_sequence=[m.action_type for m in messages if m.action_type],
        state=TutorLearnerState(
            **learner_service.state_to_dict(state, knowledge_point_id=session.knowledge_point_id)
        ),
    )


# --------------------------------------------------------------------------- #
# 学习状态
# --------------------------------------------------------------------------- #
@router.get(
    "/learner-state/{knowledge_point_id}",
    response_model=TutorLearnerState,
    summary="某个知识点的学习状态",
)
def tutor_learner_state(
    knowledge_point_id: int,
    db: Session = Depends(get_db),
    learner_id: str = Depends(current_learner_id),
) -> TutorLearnerState:
    """学习状态。不存在时返回一份"全新学习者"的零值而不是 404 ——
    前端不必为一个还没学过的知识点写额外分支。"""
    require_knowledge_point(db, knowledge_point_id, learner_id)

    state = learner_service.get_learner_state(
        db, knowledge_point_id, learner_id=learner_id
    )
    return TutorLearnerState(
        **learner_service.state_to_dict(state, knowledge_point_id=knowledge_point_id)
    )


# --------------------------------------------------------------------------- #
# P5：学习看板与画像
# --------------------------------------------------------------------------- #
@router.get("/dashboard", response_model=TutorDashboardResponse, summary="学习看板")
def tutor_dashboard(
    learner_id: str = Depends(current_learner_id),
    weak_limit: int = Query(default=20, ge=1, le=100),
    due_limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> TutorDashboardResponse:
    """首屏要的全部东西：掌握度概览 + 待复习清单 + 薄弱点清单 + 讲法画像。

    这是"跨会话记得住"的对外出口 —— 用户关掉浏览器再回来，
    看到的就是上次留下的这些状态，而不是一片空白。
    """
    data = memory_service.build_dashboard(
        db,
        learner_id=learner_id,
        weak_limit=weak_limit,
        due_limit=due_limit,
    )
    return TutorDashboardResponse(**data)


@router.put("/profile", response_model=LearnerProfileSummary, summary="设定讲法偏好")
def update_profile(
    payload: ProfileUpdateRequest,
    db: Session = Depends(get_db),
    learner_id: str = Depends(current_learner_id),
) -> LearnerProfileSummary:
    """手动设定讲法偏好。

    设定后 `style_source` 变成 `manual`，**自动推导不再覆盖** ——
    用户明确表达过的偏好优先于系统从错因里猜出来的。

    身份同样只来自会话：请求体里的 `learner_id` 不再参与，
    否则改一下请求体就能改别人的讲法偏好。
    """
    try:
        profile = memory_service.set_manual_style(
            db,
            payload.preferred_style,
            learner_id=learner_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return LearnerProfileSummary(
        preferred_style=profile.preferred_style,
        style_label=memory_service.STYLE_LABEL.get(
            profile.preferred_style, profile.preferred_style
        ),
        style_source=profile.style_source,
        style_instruction=memory_service.style_instruction(profile.preferred_style),
        style_evidence=profile.style_evidence or {},
    )


__all__ = ["router", "ACTION_LABELS", "MessageRole"]
