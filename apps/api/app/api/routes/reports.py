"""学习报告接口（Phase 5E）。

```
GET /api/reports/learning?days=30
```

## 一个端点，纯只读

报告是**读**出来的，不是算出来存下来的 —— 没有任何写操作、没有新表、
没有 migration ✓。数据全部来自已经存在的 `learner_kp_states` / `sessions` /
`messages` / `answer_evaluations`。

## 为什么用 `require_learner_id`

与 `study` / `todos` 同一把门：**必须登录**。

`current_learner_id` 在未登录时会回退到 `DEFAULT_LEARNER_ID = "local"`，
而演示账号 demo 的 `learner_id` **也是 `local`** —— 于是"未登录访客 = demo 账号"。
学习报告是**私人内容**（他学了什么、哪里卡住、错过几次），
用 `current_learner_id` 就意味着任何人都能读到 demo 的学习记录 ✗

## LLM 失败不是错误

那段人话（`narrative`）由模型生成，但**报告不依赖它**：
生成失败时接口**照样返回 200**，只是 `narrative.available=false`。
详见 `report_service.build_narrative`。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import require_learner_id
from app.db.session import get_db
from app.schemas.report import LearningReportResponse
from app.services import report_service

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get(
    "/learning",
    response_model=LearningReportResponse,
    summary="我的学习报告",
)
async def learning_report(
    days: int = Query(
        report_service.DEFAULT_DAYS,
        ge=report_service.MIN_DAYS,
        le=report_service.MAX_DAYS,
        description="时间窗口（天）。只影响学习轨迹与学习次数。",
    ),
    db: Session = Depends(get_db),
    learner_id: str = Depends(require_learner_id),
) -> LearningReportResponse:
    """把已有的学习状态聚合成一份学习回顾。

    ⚠️ **窗口只影响 `trajectory` 与 `sessions`** ——
    `overview` / `weak_points` / `due_reviews` 是当前快照，与窗口无关；
    `error_types` 沿用既有口径，统计**全部历史**（响应里用
    `error_types_are_all_time` 如实带出，由前端说明）。

    越界的 `days` 直接 **422**（`ge=1, le=365`）—— 与其悄悄夹到边界、
    让调用方以为参数生效了，不如当场说清楚 ✗
    """
    data = await report_service.build_report(db, learner_id=learner_id, days=days)
    return LearningReportResponse.model_validate(data)
