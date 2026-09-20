"""学习状态服务：读写 `learner_kp_states`。

分工要点（与 assessment_service 的边界）：

- 本模块只管**状态**：读出来、按 Policy 的公式更新、写回去；
- 它**不判断教学动作**（那是 `agent/policy.py` 的事），
  也**不做作答评估**（那是 `assessment_service` 的事）。

`mastery` 的更新公式来自 Policy，本模块只是执行者 —— 这样"阈值在哪"这个问题
永远只有一个答案：`policy.py`。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.agent import policy
from app.core.logging import get_logger
from app.models.learner_kp_state import LearnerKpState, LearnerStatus

logger = get_logger(__name__)

#: 默认学习者。用户体系属 P5，P4 用固定标识先跑通闭环。
# 统一定义在 app/core/identity.py，这里重导出 —— 既有 import 全部继续可用，
# 且保证模型层与业务层用的是同一个值。
from app.core.identity import DEFAULT_LEARNER_ID  # noqa: E402

#: mastery 的数据库精度是 DECIMAL(4,3)，写库前按此量化 ——
#: 不量化的话浮点尾数会在读回来时"看起来变了"，造成没必要的困惑。
_MASTERY_QUANT = Decimal("0.001")


def _quantize(value: float) -> Decimal:
    return Decimal(str(round(max(0.0, min(1.0, value)), 3))).quantize(_MASTERY_QUANT)


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #
def get_learner_state(
    db: Session,
    knowledge_point_id: int,
    *,
    learner_id: str = DEFAULT_LEARNER_ID,
) -> LearnerKpState | None:
    """读取学习状态。不存在时返回 None（调用方按"全新学习者"处理）。"""
    return db.execute(
        select(LearnerKpState).where(
            LearnerKpState.learner_id == learner_id,
            LearnerKpState.knowledge_point_id == int(knowledge_point_id),
        )
    ).scalars().first()


def get_or_create_learner_state(
    db: Session,
    knowledge_point_id: int,
    *,
    learner_id: str = DEFAULT_LEARNER_ID,
    for_update: bool = False,
) -> LearnerKpState:
    """读取学习状态，不存在则创建一条全零记录。

    `for_update=True` 时加行锁。学习状态的更新是"读-改-写"，两个请求并发时
    不加锁会丢更新（后写的覆盖先写的），表现为"答对了但 mastery 没涨"这种
    极难排查的问题。
    """
    query = select(LearnerKpState).where(
        LearnerKpState.learner_id == learner_id,
        LearnerKpState.knowledge_point_id == int(knowledge_point_id),
    )
    if for_update:
        query = query.with_for_update()

    state = db.execute(query).scalars().first()
    if state is not None:
        return state

    state = LearnerKpState(
        learner_id=learner_id,
        knowledge_point_id=int(knowledge_point_id),
        mastery=_quantize(0.0),
        attempt_count=0,
        correct_count=0,
        consecutive_correct=0,
        consecutive_wrong=0,
        status=LearnerStatus.NEW,
    )
    db.add(state)
    db.flush()
    return state


# --------------------------------------------------------------------------- #
# 更新
# --------------------------------------------------------------------------- #
@dataclass
class StateUpdateResult:
    """一次状态更新的结果。

    `ok=False` 时调用方**必须如实告诉用户"状态没更新成功"**，
    不能假装成功 —— 学习状态是后续所有决策的依据，静默失败会让整个教学策略失真。
    """

    ok: bool
    mastery_before: float = 0.0
    mastery_after: float = 0.0
    status: str = ""
    consecutive_correct: int = 0
    consecutive_wrong: int = 0
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "mastery_before": round(self.mastery_before, 4),
            "mastery_after": round(self.mastery_after, 4),
            "mastery_delta": round(self.mastery_after - self.mastery_before, 4),
            "status": self.status,
            "consecutive_correct": self.consecutive_correct,
            "consecutive_wrong": self.consecutive_wrong,
            "error": self.error,
        }


def update_learning_state(
    db: Session,
    state: LearnerKpState,
    *,
    correct: bool,
    score: float,
    error_type: str | None = None,
) -> StateUpdateResult:
    """按一次作答结果更新学习状态。

    **公式来自 Policy**（`policy.apply_mastery` / `next_streaks` / `status_for`），
    本函数只负责把结果落库并提交。

    失败时回滚并返回 `ok=False` —— 不抛异常，因为"状态没更新成功"这一个环节出问题
    不应该让用户这一轮的学习完全白费（他至少应该看到评估结果）。
    """
    before = float(state.mastery or 0)

    mastery_after = policy.apply_mastery(before, correct=correct, score=score)
    streak_correct, streak_wrong = policy.next_streaks(
        correct=correct,
        consecutive_correct=state.consecutive_correct,
        consecutive_wrong=state.consecutive_wrong,
    )
    new_status = policy.status_for(
        mastery=mastery_after,
        attempt_count=state.attempt_count + 1,
        consecutive_wrong=streak_wrong,
    )

    try:
        state.mastery = _quantize(mastery_after)
        state.attempt_count += 1
        state.review_count += 1
        if correct:
            state.correct_count += 1
        state.consecutive_correct = streak_correct
        state.consecutive_wrong = streak_wrong
        if error_type:
            state.last_error_type = error_type
        state.status = new_status
        now = datetime.now()
        state.last_attempt_at = now
        # 简化遗忘曲线（P5）：安排下次复习时间。
        #
        # 为什么放在这里而不是让调用方再单独调一次：技术方案把"掌握度增量"与
        # "遗忘曲线"并列在 `update_learning_state` 名下 —— 它们本来就是同一件事的两面：
        # "这次学得怎么样"与"下次什么时候再碰"。分开写迟早会漏掉一个。
        #
        # 函数级导入是为了打破循环：memory_service 需要本模块的 DEFAULT_LEARNER_ID，
        # 模块级导入会成环。曲线本身是纯函数，延迟导入没有副作用。
        from app.services.memory_service import schedule_next_review

        state.next_review_at = schedule_next_review(
            float(state.mastery), correct=correct, consecutive_wrong=streak_wrong, now=now
        )
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception("学习状态更新失败（kp=%s）：%s", state.knowledge_point_id, exc)
        return StateUpdateResult(
            ok=False,
            mastery_before=before,
            mastery_after=before,  # 回滚了，如实报告没变
            status=str(state.status or ""),
            error=f"{type(exc).__name__}: {exc}",
        )

    return StateUpdateResult(
        ok=True,
        mastery_before=before,
        mastery_after=float(state.mastery),
        status=state.status,
        consecutive_correct=state.consecutive_correct,
        consecutive_wrong=state.consecutive_wrong,
    )


# --------------------------------------------------------------------------- #
# 展示
# --------------------------------------------------------------------------- #
def state_to_dict(state: LearnerKpState | None, *, knowledge_point_id: int | None = None) -> dict[str, Any]:
    """给接口用的扁平结构。状态不存在时返回一份"全新学习者"的零值。"""
    if state is None:
        return {
            "learner_id": DEFAULT_LEARNER_ID,
            "knowledge_point_id": knowledge_point_id,
            "exists": False,
            "mastery": 0.0,
            "attempt_count": 0,
            "correct_count": 0,
            "consecutive_correct": 0,
            "consecutive_wrong": 0,
            "status": LearnerStatus.NEW,
            "last_error_type": None,
            "last_attempt_at": None,
            "next_review_at": None,
            "review_count": 0,
            "updated_at": None,
            "accuracy": 0.0,
        }
    attempts = int(state.attempt_count or 0)
    return {
        "learner_id": state.learner_id,
        "knowledge_point_id": state.knowledge_point_id,
        "exists": True,
        "mastery": float(state.mastery or 0),
        "attempt_count": attempts,
        "correct_count": int(state.correct_count or 0),
        "consecutive_correct": int(state.consecutive_correct or 0),
        "consecutive_wrong": int(state.consecutive_wrong or 0),
        "status": state.status,
        "last_error_type": state.last_error_type,
        "last_attempt_at": state.last_attempt_at.isoformat() if state.last_attempt_at else None,
        "next_review_at": state.next_review_at.isoformat() if state.next_review_at else None,
        "review_count": int(state.review_count or 0),
        "updated_at": state.updated_at.isoformat() if state.updated_at else None,
        "accuracy": round(int(state.correct_count or 0) / attempts, 3) if attempts else 0.0,
    }


def list_learner_states(
    db: Session, *, learner_id: str = DEFAULT_LEARNER_ID, limit: int = 100
) -> list[LearnerKpState]:
    return list(
        db.execute(
            select(LearnerKpState)
            .where(LearnerKpState.learner_id == learner_id)
            .order_by(LearnerKpState.updated_at.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
