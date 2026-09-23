"""学习报告接口（Phase 5E）。

## 这一组守什么

| 不变量 | 为什么必须守 |
|---|---|
| **结构化数据绝不依赖 LLM** | 模型挂了报告页照样要有数据 —— 为一段文字让整页打不开是把增强项当前置条件 ✗ |
| **只看得到自己的数据** | 报告里有"你哪里卡住、错了几次"，是私人内容 ✗ 越权 = 把别人的学习记录摊开 |
| **只读** | 报告不改任何状态；调一次接口不该在库里留下痕迹 |
| **坏快照不许搞垮整页** | `state_snapshot` 是 JSON 列，形状可能不对 → 跳过它，不是 500 |

## 关于 LLM

用 monkeypatch 替掉 `llm_gateway.chat` 来测三条路：成功 / 抛异常 / 返回空。
**不打真实模型** —— 既省额度，也让"失败也必须有数据"这条能被稳定验证 ✓
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.llm import llm_gateway
from app.db.session import SessionLocal
from app.main import app
from app.models.answer_evaluation import AnswerEvaluation, ErrorType
from app.models.document import ParseStatus
from app.models.knowledge_point import KnowledgePoint
from app.models.learner_kp_state import LearnerKpState, LearnerStatus
from app.models.message import ActionType, Message, MessageRole
from app.models.session import Session as TutorSession
from app.services import document_service, knowledge_service, report_service

client = TestClient(app)
PASSWORD = "Shizhi#2026"


# --------------------------------------------------------------------------- #
# 助手
# --------------------------------------------------------------------------- #
def _register(username: str) -> str:
    res = client.post("/api/auth/register", json={"username": username, "password": PASSWORD})
    assert res.status_code == 200, res.text
    return str(res.json()["user"]["learner_id"])


def _fresh_user() -> str:
    return _register(f"rep{uuid4().hex[:10]}")


def _seed(learner_id: str, titles: list[str]) -> list[int]:
    """造一份 READY 资料 + 若干知识点，返回 kp_id 列表。"""
    with SessionLocal() as db:
        doc = document_service.create_document(
            db,
            file_name=f"报告-{uuid4().hex[:6]}.pdf",
            file_size=1,
            file_hash=f"h{uuid4().hex}",
            storage_path="/tmp/x",
            owner_learner_id=learner_id,
        )
        doc.parse_status = ParseStatus.READY
        ids: list[int] = []
        for title in titles:
            point = KnowledgePoint(
                document_id=doc.id,
                title=title,
                title_norm=knowledge_service.normalize_title(title),
                summary="",
            )
            db.add(point)
            db.flush()
            ids.append(int(point.id))
        db.commit()
        return ids


def _seed_state(
    learner_id: str,
    kp_id: int,
    *,
    mastery: float = 0.3,
    status: str = LearnerStatus.LEARNING,
    attempt_count: int = 3,
    consecutive_wrong: int = 1,
    next_review_at: datetime | None = None,
    last_error_type: str | None = None,
) -> None:
    with SessionLocal() as db:
        db.add(
            LearnerKpState(
                learner_id=learner_id,
                knowledge_point_id=kp_id,
                mastery=mastery,
                status=status,
                attempt_count=attempt_count,
                consecutive_wrong=consecutive_wrong,
                next_review_at=next_review_at,
                last_error_type=last_error_type,
            )
        )
        db.commit()


def _seed_session_with_history(
    learner_id: str,
    kp_id: int,
    *,
    snapshots: list[dict | None],
    created_at: datetime | None = None,
    action_count: int | None = None,
) -> int:
    """造一次学习会话 + 若干条带状态快照的 assistant 消息 + 一条答错记录。

    返回 session_id。`snapshots` 里可以故意塞坏形状（测抗坏数据）。
    """
    moment = created_at or datetime.now(UTC)
    with SessionLocal() as db:
        session = TutorSession(
            learner_id=learner_id,
            knowledge_point_id=kp_id,
            title="报告用会话",
            action_count=action_count if action_count is not None else len(snapshots),
            created_at=moment,
            last_active_at=moment,
        )
        db.add(session)
        db.flush()
        for snapshot in snapshots:
            db.add(
                Message(
                    session_id=session.id,
                    role=MessageRole.ASSISTANT,
                    content="讲解",
                    action_type=ActionType.EXPLAIN,
                    state_snapshot=snapshot,
                    created_at=moment,
                )
            )
        # 一条答错记录 —— error_types 是从这里统计的
        db.add(
            AnswerEvaluation(
                session_id=session.id,
                knowledge_point_id=kp_id,
                question="这是什么",
                user_answer="不知道",
                correct=False,
                score=0,
                error_type=ErrorType.CONCEPT_CONFUSION,
                created_at=moment,
            )
        )
        db.commit()
        return int(session.id)


def _snap(mastery: float, kp_id: int, learner_id: str) -> dict:
    return {
        "learner_id": learner_id,
        "knowledge_point_id": kp_id,
        "mastery": mastery,
        "attempt_count": 1,
        "consecutive_wrong": 0,
        "status": "learning",
    }


def _report(days: int | None = None) -> dict:
    params = {} if days is None else {"days": days}
    res = client.get("/api/reports/learning", params=params)
    assert res.status_code == 200, res.text
    return dict(res.json())


# --------------------------------------------------------------------------- #
# 一、空数据（新用户）—— 不许报错、不许空白
# --------------------------------------------------------------------------- #
def test_empty_report_is_well_formed() -> None:
    _fresh_user()

    got = _report()

    assert got["window_days"] == 30, "默认窗口应为近 30 天"
    assert got["overview"]["tracked"] == 0
    assert got["overview"]["mastered"] == 0
    assert got["weak_points"] == []
    assert got["due_reviews"] == []
    assert got["error_types"] == {}
    assert got["sessions"] == {"count": 0, "turns": 0}
    assert got["trajectory"] == []
    assert got["narrative"]["text"] == "" or got["narrative"]["available"] is True


def test_empty_report_does_not_touch_anything() -> None:
    """报告是只读的 —— 调它不该在库里留下任何东西。"""
    learner = _fresh_user()

    def counts() -> tuple[int, int]:
        with SessionLocal() as db:
            states = db.execute(
                text("select count(*) from learner_kp_states where learner_id = :lid"),
                {"lid": learner},
            ).scalar_one()
            sessions = db.execute(
                text("select count(*) from sessions where learner_id = :lid"),
                {"lid": learner},
            ).scalar_one()
            return int(states), int(sessions)

    assert counts() == (0, 0)
    _report()
    assert counts() == (0, 0), "报告接口不该产生任何写入"


# --------------------------------------------------------------------------- #
# 二、正常数据
# --------------------------------------------------------------------------- #
def test_report_reflects_real_learning_data() -> None:
    learner = _fresh_user()
    kp_ids = _seed(learner, ["Java 线程", "Java 内存模型"])
    _seed_state(
        learner,
        kp_ids[0],
        mastery=0.31,
        status=LearnerStatus.WEAK,
        attempt_count=4,
        consecutive_wrong=2,
        last_error_type=ErrorType.CONCEPT_CONFUSION,
        next_review_at=datetime.now(UTC) - timedelta(hours=1),  # 已到期
    )
    _seed_state(learner, kp_ids[1], mastery=0.85, status=LearnerStatus.MASTERED)
    _seed_session_with_history(
        learner,
        kp_ids[0],
        snapshots=[_snap(0.1, kp_ids[0], learner), _snap(0.31, kp_ids[0], learner)],
    )

    got = _report()

    # ⚠️ `knowledge_point_total` / `untouched` 是**全库**口径（见
    # `test_global_point_total_is_not_used_anywhere`），所以这里只断言
    # 真正按 learner 限定的那几个字段 ✓
    assert got["overview"]["tracked"] == 2
    assert got["overview"]["mastered"] == 1
    assert got["overview"]["weak"] == 1
    assert got["overview"]["learning"] == 0, "只种了 weak 和 mastered 两种状态"
    # 薄弱点：只列作答过的，且带知识点信息
    assert [p["title"] for p in got["weak_points"]] == ["Java 线程"]
    # 待复习：next_review_at 已过期 → 到期
    assert [p["title"] for p in got["due_reviews"]] == ["Java 线程"]
    assert got["due_reviews"][0]["due"] is True
    # 错因：来自真实的答错记录
    assert got["error_types"] == {"concept_confusion": 1}
    assert got["error_types_are_all_time"] is True
    # 学习次数：本次会话
    assert got["sessions"]["count"] == 1
    assert got["sessions"]["turns"] == 2


def test_global_point_total_is_not_used_anywhere() -> None:
    """⚠️ 钉住一个**既有缺陷**（本轮边界不允许改 `memory_service`，所以只钉不改）：

    `mastery_overview` 的 `knowledge_point_total` 统计的是**全库所有知识点**
    （`select(count()).select_from(KnowledgePoint)`，没有按 learner 限定），
    所以对单个用户来说它是错的 —— 新用户会看到 `untouched` = 全部人的知识点数。

    处理方式：payload 里**如实保留**（不改既有函数行为 ✓），
    但**提示词与界面都不使用它** ✓。
    """
    learner = _fresh_user()
    _seed(learner, ["只属于我的一个点"])

    got = _report()

    assert got["overview"]["tracked"] == 0, "还没学过任何东西"
    # 这个数字是全库的，不等于"我资料里的知识点数" —— 记录事实，不去改它
    assert got["overview"]["knowledge_point_total"] >= 1
    assert got["overview"]["untouched"] == got["overview"]["knowledge_point_total"]

    # 提示词里**绝不能**出现这个数字（模型会照着它写进正文 ✗）
    data = report_service.collect(db=_db(), learner_id=learner, days=30)
    rendered = "\n".join(report_service.render_prompt_input(data).values())
    assert str(got["overview"]["knowledge_point_total"]) not in rendered, "全局总数漏进了提示词"
    assert "一共有" not in rendered, "不许把全局总数写成'你资料里一共有 N 个'"
    assert "还没碰过" not in rendered


def test_trajectory_is_recovered_from_state_snapshots() -> None:
    """掌握度轨迹从 `messages.state_snapshot` 还原 —— 顺序必须是时间顺序。"""
    learner = _fresh_user()
    kp_ids = _seed(learner, ["三次握手"])
    _seed_session_with_history(
        learner,
        kp_ids[0],
        snapshots=[
            _snap(0.1, kp_ids[0], learner),
            _snap(0.4, kp_ids[0], learner),
            _snap(0.7, kp_ids[0], learner),
        ],
    )

    got = _report()

    assert len(got["trajectory"]) == 1
    line = got["trajectory"][0]
    assert line["title"] == "三次握手"
    assert [round(p["mastery"], 2) for p in line["points"]] == [0.1, 0.4, 0.7]


def test_bad_snapshot_is_skipped_not_fatal() -> None:
    """⚠️ 一条坏快照不该毁掉整份报告 —— 最差退化成少一条轨迹。"""
    learner = _fresh_user()
    kp_ids = _seed(learner, ["坏数据测试"])
    good = _snap(0.5, kp_ids[0], learner)
    _seed_session_with_history(
        learner,
        kp_ids[0],
        snapshots=[
            good,
            {"mastery": "不是数字"},  # 类型错
            {"mastery": None},  # 空
            {"没有 mastery 这个键": 1},  # 缺字段
            "整个不是 dict",  # 形状全错
            {"mastery": True},  # 布尔混进来（bool 是 int 的子类，得挡住）
            _snap(0.6, kp_ids[0], learner),
        ],
    )

    got = _report()

    assert len(got["trajectory"]) == 1, "好的那两条应当仍然在"
    assert [round(p["mastery"], 2) for p in got["trajectory"][0]["points"]] == [0.5, 0.6]


@pytest.mark.parametrize(
    "snapshot",
    [
        None,
        "不是 dict",
        123,
        [],
        {},
        {"mastery": "0.5"},
        {"mastery": None},
        {"mastery": True},
        {"mastery": float("nan")},
        {"mastery": float("inf")},
    ],
)
def test_read_mastery_rejects_anything_unreadable(snapshot) -> None:
    """`_read_mastery` 是纯函数 —— 这些形状没法塞进 MySQL 的 JSON 列，
    所以直接测它（NaN / inf 都会被 JSON 编码拒绝，只能在这里验）。"""
    assert report_service._read_mastery(snapshot) is None


def test_read_mastery_clamps_to_range() -> None:
    assert report_service._read_mastery({"mastery": 0.42}) == 0.42
    assert report_service._read_mastery({"mastery": 0}) == 0.0
    assert report_service._read_mastery({"mastery": 1}) == 1.0
    assert report_service._read_mastery({"mastery": -0.5}) == 0.0, "负数夹到 0"
    assert report_service._read_mastery({"mastery": 9.9}) == 1.0, "超过 1 夹到 1"


def test_trajectory_respects_the_window() -> None:
    """窗口外的快照不进来。"""
    learner = _fresh_user()
    kp_ids = _seed(learner, ["窗口测试"])
    long_ago = datetime.now(UTC) - timedelta(days=90)
    _seed_session_with_history(
        learner, kp_ids[0], snapshots=[_snap(0.2, kp_ids[0], learner)], created_at=long_ago
    )

    assert _report(30)["trajectory"] == [], "90 天前的记录不该出现在近 30 天里"
    assert _report(365)["trajectory"] != [], "窗口放宽后就该看到"


# --------------------------------------------------------------------------- #
# 三、归属隔离
# --------------------------------------------------------------------------- #
def test_other_learners_data_is_invisible() -> None:
    other = _fresh_user()
    kp_ids = _seed(other, ["别人独有的知识点"])
    _seed_state(other, kp_ids[0], mastery=0.2, status=LearnerStatus.WEAK)
    _seed_session_with_history(other, kp_ids[0], snapshots=[_snap(0.2, kp_ids[0], other)])

    _fresh_user()  # 换身份
    got = _report()

    assert got["overview"]["tracked"] == 0, "看到了别人的学习状态"
    assert got["weak_points"] == []
    assert got["sessions"]["count"] == 0
    assert got["trajectory"] == [], "看到了别人的学习轨迹"


def test_anonymous_is_rejected() -> None:
    client.cookies.clear()
    assert client.get("/api/reports/learning").status_code == 401


# --------------------------------------------------------------------------- #
# 四、days 边界
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [0, -1, -30, 366, 99999])
def test_out_of_range_days_is_rejected(bad: int) -> None:
    """越界直接 422 —— 与其悄悄夹到边界、让人以为参数生效了，不如当场说清。"""
    _fresh_user()
    assert client.get("/api/reports/learning", params={"days": bad}).status_code == 422


@pytest.mark.parametrize("ok_days", [1, 7, 30, 365])
def test_valid_days_are_accepted(ok_days: int) -> None:
    _fresh_user()
    assert _report(ok_days)["window_days"] == ok_days


def test_clamp_days_is_defense_in_depth() -> None:
    """路由层已经拦住了越界；service 层的夹取是给内部调用方的第二道。"""
    assert report_service.clamp_days(None) == report_service.DEFAULT_DAYS
    assert report_service.clamp_days(0) == report_service.MIN_DAYS
    assert report_service.clamp_days(99999) == report_service.MAX_DAYS
    assert report_service.clamp_days(7) == 7


# --------------------------------------------------------------------------- #
# 五、LLM：失败不影响结构化数据（本组最重要）
# --------------------------------------------------------------------------- #
def test_llm_failure_still_returns_full_report(monkeypatch) -> None:
    """⚠️ 模型挂了：接口**仍然 200**，`narrative.available=false`，数据一个不少。"""
    learner = _fresh_user()
    kp_ids = _seed(learner, ["模型挂了也要能看到我"])
    _seed_state(learner, kp_ids[0], mastery=0.4, status=LearnerStatus.WEAK)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("模型额度用尽")

    monkeypatch.setattr(llm_gateway, "chat", boom)

    got = _report()

    assert got["narrative"] == {"available": False, "text": ""}
    # 结构化数据完好
    assert got["overview"]["tracked"] == 1
    assert [p["title"] for p in got["weak_points"]] == ["模型挂了也要能看到我"]


def test_llm_returning_empty_is_also_unavailable(monkeypatch) -> None:
    async def empty(*_args, **_kwargs):
        class _R:
            content = "   "

        return _R()

    monkeypatch.setattr(llm_gateway, "chat", empty)

    got = _report()

    assert got["narrative"]["available"] is False
    assert got["overview"]["knowledge_point_total"] >= 0, "数据仍在"


def test_llm_success_marks_narrative_available(monkeypatch) -> None:
    async def fake(*_args, **_kwargs):
        class _R:
            content = "你学过 1 个知识点，先把最不稳的那个过一遍。"

        return _R()

    monkeypatch.setattr(llm_gateway, "chat", fake)

    got = _report()

    assert got["narrative"]["available"] is True
    assert "知识点" in got["narrative"]["text"]


# --------------------------------------------------------------------------- #
# 六、提示词与渲染（不依赖模型）
# --------------------------------------------------------------------------- #
def test_prompt_template_has_both_sections() -> None:
    system, user = report_service.load_prompt_template()
    assert system and user
    assert "{{overview_block}}" in user
    assert "{{trajectory_block}}" in user


def test_prompt_input_never_contains_raw_mastery() -> None:
    """⚠️ 数字一旦进了提示词，模型就会把它写进正文 —— 所以这里也不许有。"""
    learner = _fresh_user()
    kp_ids = _seed(learner, ["别把数字喂给模型"])
    _seed_state(learner, kp_ids[0], mastery=0.31, status=LearnerStatus.WEAK)
    _seed_session_with_history(
        learner, kp_ids[0], snapshots=[_snap(0.31, kp_ids[0], learner)]
    )

    data = report_service.collect(db=_db(), learner_id=learner, days=30)
    rendered = "\n".join(report_service.render_prompt_input(data).values())

    assert "0.31" not in rendered, "原始掌握度漏进了提示词"
    assert "mastery" not in rendered
    assert "还不太稳" in rendered, "应当用人话分级"


def _db():
    """拿一个 session 给 service 直调用。"""
    return SessionLocal()


# --------------------------------------------------------------------------- #
# 七、窗口只影响该影响的部分
# --------------------------------------------------------------------------- #
def test_window_does_not_change_snapshots() -> None:
    """`overview` / `weak_points` / `due_reviews` 是当前快照，与窗口无关。"""
    learner = _fresh_user()
    kp_ids = _seed(learner, ["快照不受窗口影响"])
    _seed_state(
        learner,
        kp_ids[0],
        mastery=0.3,
        status=LearnerStatus.WEAK,
        next_review_at=datetime.now(UTC) - timedelta(hours=2),
    )

    short = _report(1)
    long = _report(365)

    assert short["overview"] == long["overview"]
    assert short["weak_points"] == long["weak_points"]
    assert short["due_reviews"] == long["due_reviews"]
