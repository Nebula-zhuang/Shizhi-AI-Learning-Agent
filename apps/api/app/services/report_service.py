"""学习报告服务（Phase 5E）。

把已有的学习状态数据聚合成一份**可读的学习回顾**。

## 三条铁律

### 一、结构化数据**绝不依赖 LLM**

`collect()` 里的每一个字段都来自**纯查询**（大部分直接复用 `memory_service`
已经算好的东西）。LLM 只负责最后那一段 `narrative`。
模型挂了、超时了、额度没了 —— 报告页**照样有完整的数据**，只是少了那段人话。

所以分工很硬：`collect()` 同步、确定、可离线测试；
`build_narrative()` 异步、可能失败、**失败就返回空串**，绝不让异常传出去。

### 二、不新造指标

能用现成的就用现成的：
`mastery_overview` / `aggregate_weak_points` / `due_reviews` / `error_type_counts`
一行都不重写。只有两样现成没有、需要新查：

- **sessions** —— 按 learner + 时间窗统计学习次数与教学轮次
- **trajectory** —— 从 `messages.state_snapshot` 还原掌握度轨迹
  （`LearnerKpState` 只存**当前值**，变化过程只存在于每条教学轮次的快照里）

### 三、只读

本模块**没有任何写操作** —— 不改 `LearnerKpState`、不改 profile、不改任何表。

## 关于时间窗口

窗口**只影响** `trajectory` 与 `sessions` ✓。

`overview` / `weak_points` / `due_reviews` 是**当前快照**，按定义与窗口无关；
`error_types` 沿用 `memory_service.error_type_counts` 的既有口径 ——
统计**全部历史**错因、**不受窗口影响**。这一点在响应里用
`error_types_are_all_time=true` 如实带出去，由前端说明，
而不是悄悄按窗口截断（那会让用户以为"我只错过这几次"）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.llm import llm_gateway
from app.core.logging import get_logger
from app.models.knowledge_point import KnowledgePoint
from app.models.message import Message, MessageRole
from app.models.session import Session as TutorSession
from app.services import memory_service

logger = get_logger(__name__)

PROMPT_PATH = Path(__file__).resolve().parent.parent / "agent" / "prompts" / "learning_report.md"

#: 默认窗口：近 30 天。
DEFAULT_DAYS = 30
#: 窗口下限 / 上限。上限防"一次拉一整年的快照"，下限防 `days=0` 这种退化输入。
MIN_DAYS = 1
MAX_DAYS = 365

#: 薄弱点 / 待复习在报告里各列几条。
WEAK_LIMIT = 5
DUE_LIMIT = 5
#: 轨迹最多还原几个知识点，每个知识点最多保留几个采样点。
TRAJECTORY_LIMIT = 5
TRAJECTORY_POINTS = 20

#: 错因键 → 人话。**必须是 `ErrorType` 的真实取值**（见 `models/answer_evaluation.py`）。
#: 未知键原样带出 —— 宁可显示得生硬，也不要猜一个错的标签。
ERROR_LABEL: dict[str, str] = {
    "concept_confusion": "概念混了",
    "memory_gap": "没记住",
    "reasoning_break": "推理断了",
    "misread": "看错了题",
    "none": "说不上来哪里错",
}


def clamp_days(days: int | None) -> int:
    """把窗口夹到合理范围。`None` 取默认值。"""
    if days is None:
        return DEFAULT_DAYS
    return max(MIN_DAYS, min(int(days), MAX_DAYS))


def _level(mastery: float) -> str:
    """掌握度 → 人话分级。

    ⚠️ **只用于喂给模型描述趋势**，不直接进界面（界面用 `LearnerStatus` 映射）。
    这里不能出现数字 —— 一旦数字进了提示词，模型就会把它写进正文 ✗
    """
    if mastery >= 0.8:
        return "差不多了"
    if mastery >= 0.5:
        return "在学"
    if mastery > 0:
        return "还不太稳"
    return "刚起步"


# --------------------------------------------------------------------------- #
# 结构化数据（纯只读、确定、可离线测）
# --------------------------------------------------------------------------- #
def collect_sessions(db: Session, *, learner_id: str, since: datetime) -> dict[str, int]:
    """窗口内**这位学习者自己**的学习次数与教学轮次。

    按 `learner_id` 过滤 —— 不加这一层，报告里会出现"别人学了多少次" ✗
    """
    row = db.execute(
        select(
            func.count(TutorSession.id),
            func.coalesce(func.sum(TutorSession.action_count), 0),
        ).where(
            TutorSession.learner_id == learner_id,
            TutorSession.created_at >= since,
        )
    ).one()
    return {"count": int(row[0] or 0), "turns": int(row[1] or 0)}


def collect_trajectory(
    db: Session, *, learner_id: str, since: datetime
) -> list[dict[str, Any]]:
    """从 `messages.state_snapshot` 还原每个知识点的掌握度轨迹。

    ## 为什么必须 join 到 sessions

    `messages` 上没有 `learner_id`（它挂在 session 上），归属要顺着
    `messages.session_id → sessions.learner_id` 判 ——
    不判就会把别人的学习轨迹画进你的报告 ✗

    ## 为什么只取 assistant 消息

    `state_snapshot` 只在教学轮次落库（`runtime.py` 写 assistant 消息时）✓
    用户消息没有快照 ✓

    ## 为什么知识点从**会话**取，不从消息取

    `messages` 上没有 `knowledge_point_id`（表里只有 `kp_ids` 这个 JSON 列表，
    见 `models/message.py` 的注释 —— 那是给检索用的，形状会变）。
    而一次 Tutor 会话**从始至终就属于一个知识点**（`sessions.knowledge_point_id`
    是真实外键 ✓），所以"这一轮在讲什么"取会话上的那个字段最准、也最稳 ✓

    ## 抗坏数据

    `state_snapshot` 是 JSON 列，历史数据、手工改库、将来字段变动都可能让它
    形状不对 ✗。**任何一条读不懂就跳过它**，绝不让一条坏数据毁掉整份报告 ——
    「最差退化成没有这条轨迹，而不是整页打不开」
    （与 `taskCard.ts` 的降级原则一致 ✓）。
    """
    rows = db.execute(
        select(
            Message.state_snapshot,
            Message.created_at,
            TutorSession.knowledge_point_id,
            KnowledgePoint.title,
        )
        .join(TutorSession, TutorSession.id == Message.session_id)
        .join(KnowledgePoint, KnowledgePoint.id == TutorSession.knowledge_point_id)
        .where(
            TutorSession.learner_id == learner_id,
            TutorSession.knowledge_point_id.is_not(None),
            Message.role == MessageRole.ASSISTANT,
            Message.created_at >= since,
            Message.state_snapshot.is_not(None),
        )
        .order_by(Message.created_at.asc(), Message.id.asc())
    ).all()

    grouped: dict[int, dict[str, Any]] = {}
    for snapshot, created_at, kp_id, title in rows:
        mastery = _read_mastery(snapshot)
        if mastery is None:
            continue  # 形状不对 → 跳过这一条，其他照常
        entry = grouped.setdefault(
            int(kp_id),
            {"knowledge_point_id": int(kp_id), "title": str(title), "points": []},
        )
        entry["points"].append({"at": created_at.isoformat(), "mastery": mastery})

    # 记录最多的排前面（学过的最值得看），再等距抽稀到 TRAJECTORY_POINTS 个点
    ordered = sorted(grouped.values(), key=lambda item: len(item["points"]), reverse=True)
    trimmed = ordered[:TRAJECTORY_LIMIT]
    for item in trimmed:
        points = item["points"]
        if len(points) > TRAJECTORY_POINTS:
            step = len(points) / TRAJECTORY_POINTS
            picked = [points[int(i * step)] for i in range(TRAJECTORY_POINTS - 1)]
            picked.append(points[-1])  # 保留最新的那一点
            item["points"] = picked
    return trimmed


def _read_mastery(snapshot: Any) -> float | None:
    """从快照里读掌握度。**读不懂就返回 None**（调用方跳过这一条）。

    ⚠️ `bool` 要单独挡：Python 里 `True` 是 `int` 的子类，
    不挡的话 `{"mastery": true}` 会变成 `1.0`（= 已掌握）✗

    ⚠️ `inf` / `nan` 也要挡，而且**不是夹取而是拒绝** ——
    把它们悄悄夹成 1.0 等于把一条坏数据说成"已掌握"，比丢掉它更糟 ✗
    """
    if not isinstance(snapshot, dict):
        return None
    raw = snapshot.get("mastery")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    if value != value or value in (float("inf"), float("-inf")):  # NaN / ±inf
        return None
    return max(0.0, min(1.0, value))


def collect(
    db: Session, *, learner_id: str, days: int, now: datetime | None = None
) -> dict[str, Any]:
    """组装报告的**全部结构化字段**。同步、确定、不碰 LLM。"""
    moment = now or datetime.now(UTC)
    since = moment - timedelta(days=days)

    overview = memory_service.mastery_overview(db, learner_id=learner_id)
    weak = memory_service.aggregate_weak_points(db, learner_id=learner_id, limit=WEAK_LIMIT)
    due = memory_service.due_reviews(db, learner_id=learner_id, limit=DUE_LIMIT)
    errors = memory_service.error_type_counts(db, learner_id=learner_id)

    return {
        "generated_at": moment.isoformat(),
        "window_days": days,
        "window_since": since.isoformat(),
        # ⚠️ error_types 是**全量历史**口径，不随窗口变化 —— 如实带出去
        "error_types_are_all_time": True,
        "overview": overview,
        "weak_points": [item.as_dict() for item in weak],
        "due_reviews": [item.as_dict() for item in due],
        "error_types": errors,
        "sessions": collect_sessions(db, learner_id=learner_id, since=since),
        "trajectory": collect_trajectory(db, learner_id=learner_id, since=since),
    }


# --------------------------------------------------------------------------- #
# 人话那一段（唯一依赖 LLM 的地方）
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def load_prompt_template() -> tuple[str, str]:
    """加载提示词，返回 `(system, user_template)`。与知识服务的同款约定。"""
    text = PROMPT_PATH.read_text(encoding="utf-8")
    if "--- USER ---" not in text:
        raise RuntimeError(f"提示词模板缺少 '--- USER ---' 分隔符：{PROMPT_PATH}")
    system_part, user_part = text.split("--- USER ---", 1)
    system = system_part.replace("--- SYSTEM ---", "", 1).strip()
    return system, user_part.strip()


def _render_points(items: list[dict[str, Any]]) -> str:
    if not items:
        return "（暂无）"
    lines = []
    for item in items:
        title = item.get("title") or "未命名"
        # 卡了几次 / 连错几次，比 mastery 更能说明"哪里不稳"
        lines.append(f"- {title}（作答 {item.get('attempt_count', 0)} 次，连错 {item.get('consecutive_wrong', 0)} 次）")
    return "\n".join(lines)


def render_prompt_input(data: dict[str, Any]) -> dict[str, str]:
    """把结构化数据渲染成提示词里的几个块。

    ⚠️ **只放事实，不放结论** —— 结论由模型根据事实写，不是我们喂给它。
    且**不放 mastery 数字**：数字进了提示词，模型就会把它写进正文 ✗
    """
    overview = data["overview"]
    sessions = data["sessions"]
    errors = data["error_types"]

    error_lines = "\n".join(
        f"- {ERROR_LABEL.get(key, key)}：{count} 次" for key, count in sorted(errors.items())
    )

    trajectory_lines: list[str] = []
    for item in data["trajectory"]:
        points = item["points"]
        if not points:
            continue
        trajectory_lines.append(
            f"- {item['title']}：{len(points)} 次记录，"
            f"从「{_level(points[0]['mastery'])}」到「{_level(points[-1]['mastery'])}」"
        )

    return {
        "window_days": str(data["window_days"]),
        "weak_limit": str(WEAK_LIMIT),
        # ⚠️ **刻意不用 `knowledge_point_total` / `untouched`。**
        #
        # `memory_service.mastery_overview` 的这两个字段统计的是**全库所有知识点**
        # （`select(count()).select_from(KnowledgePoint)`，没有按 learner 限定）——
        # 对单个用户来说那个数字是**别人的知识点也算进来的**，写进报告就是错的 ✗
        #
        # 本轮边界不允许改 `memory_service` ✗，所以这里只喂**真正按 learner 限定**的
        # 那几个字段（`tracked` / `new` / `learning` / `weak` / `mastered` 都来自
        # `where(learner_id == …)` 的那条查询 ✓）。
        # 那两个全局字段仍按原样出现在 API 响应里（不改既有函数的行为），
        # 但**提示词与界面都不使用它们** ✓
        "overview_block": (
            f"- 开始学过的知识点：{overview['tracked']} 个\n"
            f"- 其中已达到掌握：{overview['mastered']} 个\n"
            f"- 还不太稳：{overview['weak']} 个\n"
            f"- 在学中：{overview['learning']} 个"
        ),
        "weak_block": _render_points(data["weak_points"]),
        "due_block": _render_points(data["due_reviews"]),
        "error_block": error_lines or "（还没有答错的记录）",
        "sessions_block": (
            f"- 学习次数：{sessions['count']} 次\n- 教学轮次：{sessions['turns']} 轮"
            if sessions["count"]
            else "（这段时间还没有学习记录）"
        ),
        "trajectory_block": "\n".join(trajectory_lines) or "（这段时间还没有掌握变化的记录）",
    }


async def build_narrative(data: dict[str, Any]) -> str:
    """让模型把数据写成一段人话。**失败返回空串**，由调用方标 `available=false`。

    刻意**不让异常传出去**：报告的价值在结构化数据上，那段文字是锦上添花。
    为了一段文字让整页打不开，是把增强项当成了前置条件 ✗
    """
    try:
        system, user_template = load_prompt_template()
        filled = render_prompt_input(data)
        user = user_template
        for key, value in filled.items():
            user = user.replace(f"{{{{{key}}}}}", value)
        result = await llm_gateway.chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.4,
            max_tokens=500,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("学习报告的文字部分生成失败，只返回结构化数据：%s", exc)
        return ""
    return (result.content or "").strip()


async def build_report(
    db: Session, *, learner_id: str, days: int | None = None
) -> dict[str, Any]:
    """报告全量：结构化数据 + 可选的那段人话。"""
    window = clamp_days(days)
    data = collect(db, learner_id=learner_id, days=window)
    text = await build_narrative(data)
    data["narrative"] = {"available": bool(text), "text": text}
    return data
