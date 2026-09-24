"""待办清单接口（Phase 5D）。

## 这一组守什么

| 不变量 | 为什么必须守 |
|---|---|
| **看不到、改不了、删不掉别人的待办** | 待办是私人内容；而且它是**唯一的**约束 —— 前端不做归属判断 |
| **不属于自己时返回 404，不是 403** | 403 等于确认"这个 id 存在"，那是一个枚举接口 |
| **PATCH 不因为缺字段而清空 due_date** | 「没提供」和「主动设成 null」在模型上一样，只有"有没有出现"能区分 |
| **标题清洗后再判空** | 否则 `"   "` 会通过 `min_length=1` 然后变空串落库 |

## 造数据用真实 HTTP

与 `test_study_learn_target.py` 同一范式：`client.post('/api/auth/register')` 拿
真实账号，**cookie 由 TestClient 自己带** —— 归属隔离只有在真的用两个身份
打过请求之后才算验证过。
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app

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
    return _register(f"todo{uuid4().hex[:10]}")


def _create(title: str = "复习 Java 线程", due_date: str | None = None) -> dict:
    body: dict = {"title": title}
    if due_date is not None:
        body["due_date"] = due_date
    res = client.post("/api/todos", json=body)
    assert res.status_code == 201, res.text
    return dict(res.json())


def _list(**params) -> dict:
    res = client.get("/api/todos", params=params)
    assert res.status_code == 200, res.text
    return dict(res.json())


# --------------------------------------------------------------------------- #
# 1. 创建成功
# --------------------------------------------------------------------------- #
def test_create_returns_the_new_todo() -> None:
    _fresh_user()

    todo = _create("复习 Java 线程", due_date="2026-09-30")

    assert todo["id"] > 0
    assert todo["title"] == "复习 Java 线程"
    assert todo["completed"] is False, "新建的待办默认未完成"
    assert todo["due_date"] == "2026-09-30"
    assert todo["created_at"] and todo["updated_at"]


def test_create_without_due_date() -> None:
    _fresh_user()
    todo = _create("没有截止日的待办")
    assert todo["due_date"] is None


# --------------------------------------------------------------------------- #
# 2 + 3. 空标题 422；标题首尾空格
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", ["", "   ", "\t", "\n  \n"])
def test_blank_title_is_rejected(bad: str) -> None:
    _fresh_user()
    res = client.post("/api/todos", json={"title": bad})
    assert res.status_code == 422, f"{bad!r} 应当被拒"


def test_title_is_trimmed() -> None:
    _fresh_user()
    todo = _create("   带空格的标题   ")
    assert todo["title"] == "带空格的标题", "首尾空格必须被清掉"


def test_missing_title_is_rejected() -> None:
    _fresh_user()
    assert client.post("/api/todos", json={}).status_code == 422


# --------------------------------------------------------------------------- #
# 4. 列表只返回当前 learner
# --------------------------------------------------------------------------- #
def test_list_returns_only_my_todos() -> None:
    alice = _fresh_user()
    _create("甲的待办")
    client.cookies.clear()
    _fresh_user()
    _create("乙的待办")

    items = _list()["items"]

    assert [t["title"] for t in items] == ["乙的待办"], "看到了别人的待办"
    assert alice  # 用掉变量，同时说明两个身份确实不同


def test_list_is_empty_for_a_new_user() -> None:
    _fresh_user()
    got = _list()
    assert got["items"] == []
    assert got["total"] == 0


# --------------------------------------------------------------------------- #
# 5. completed 过滤
# --------------------------------------------------------------------------- #
def test_completed_filter() -> None:
    _fresh_user()
    first = _create("已完成的事")
    _create("未完成的事")
    client.patch(f"/api/todos/{first['id']}", json={"completed": True})

    done = _list(completed=True)
    todo = _list(completed=False)

    assert [t["title"] for t in done["items"]] == ["已完成的事"]
    assert [t["title"] for t in todo["items"]] == ["未完成的事"]


# --------------------------------------------------------------------------- #
# 6 + 7. 完成 / 取消完成
# --------------------------------------------------------------------------- #
def test_complete_and_uncomplete() -> None:
    _fresh_user()
    todo_id = _create("打勾试试")["id"]

    done = client.patch(f"/api/todos/{todo_id}", json={"completed": True})
    assert done.status_code == 200
    assert done.json()["completed"] is True

    undone = client.patch(f"/api/todos/{todo_id}", json={"completed": False})
    assert undone.status_code == 200
    assert undone.json()["completed"] is False


# --------------------------------------------------------------------------- #
# 8. 编辑 title
# --------------------------------------------------------------------------- #
def test_update_title() -> None:
    _fresh_user()
    todo_id = _create("旧标题")["id"]

    res = client.patch(f"/api/todos/{todo_id}", json={"title": "  新标题  "})

    assert res.status_code == 200
    assert res.json()["title"] == "新标题", "编辑时也要清洗"


def test_update_to_blank_title_is_rejected() -> None:
    _fresh_user()
    todo_id = _create("原标题")["id"]

    assert client.patch(f"/api/todos/{todo_id}", json={"title": "   "}).status_code == 422
    # 拒绝之后原值不能被动过
    assert _list()["items"][0]["title"] == "原标题"


# --------------------------------------------------------------------------- #
# 9 + 10. 编辑 due_date / 清空 due_date
# --------------------------------------------------------------------------- #
def test_update_due_date() -> None:
    _fresh_user()
    todo_id = _create("设个截止日")["id"]

    res = client.patch(f"/api/todos/{todo_id}", json={"due_date": "2026-10-01"})

    assert res.status_code == 200
    assert res.json()["due_date"] == "2026-10-01"


def test_clear_due_date_explicitly() -> None:
    _fresh_user()
    todo_id = _create("有截止日", due_date="2026-10-01")["id"]

    res = client.patch(f"/api/todos/{todo_id}", json={"due_date": None})

    assert res.status_code == 200
    assert res.json()["due_date"] is None


def test_patch_without_due_date_does_not_clear_it() -> None:
    """⚠️ 本轮最容易写错的一条。

    `{"title": "x"}` 里没有 due_date —— 改标题**绝不能**顺手把截止日抹掉。
    「没提供」与「主动设成 null」在模型上长得一样，只有"有没有出现"能区分。
    """
    _fresh_user()
    todo_id = _create("别被清空", due_date="2026-10-01")["id"]

    res = client.patch(f"/api/todos/{todo_id}", json={"title": "改了标题"})

    assert res.status_code == 200
    assert res.json()["due_date"] == "2026-10-01", "截断日被意外清空了"
    assert res.json()["title"] == "改了标题"


def test_patch_completed_does_not_touch_other_fields() -> None:
    _fresh_user()
    todo_id = _create("只打勾", due_date="2026-10-01")["id"]

    res = client.patch(f"/api/todos/{todo_id}", json={"completed": True})

    body = res.json()
    assert body["completed"] is True
    assert body["title"] == "只打勾"
    assert body["due_date"] == "2026-10-01"


def test_empty_patch_changes_nothing() -> None:
    _fresh_user()
    todo_id = _create("什么都不改", due_date="2026-10-01")["id"]

    res = client.patch(f"/api/todos/{todo_id}", json={})

    assert res.status_code == 200
    assert res.json()["title"] == "什么都不改"
    assert res.json()["due_date"] == "2026-10-01"


# --------------------------------------------------------------------------- #
# 11. 删除
# --------------------------------------------------------------------------- #
def test_delete() -> None:
    _fresh_user()
    todo_id = _create("待删")["id"]

    res = client.delete(f"/api/todos/{todo_id}")

    assert res.status_code == 200
    assert res.json() == {"deleted": True, "todo_id": todo_id}
    assert _list()["items"] == []


def test_delete_twice_is_404() -> None:
    _fresh_user()
    todo_id = _create("删两次")["id"]
    assert client.delete(f"/api/todos/{todo_id}").status_code == 200
    assert client.delete(f"/api/todos/{todo_id}").status_code == 404


# --------------------------------------------------------------------------- #
# 12 + 13 + 14. 其他 learner 读 / 改 / 删
# --------------------------------------------------------------------------- #
def test_other_learner_cannot_see_it() -> None:
    _fresh_user()
    _create("甲的机密待办")

    client.cookies.clear()
    _fresh_user()

    assert _list()["items"] == [], "看到了别人的待办"


def test_other_learner_cannot_update_it() -> None:
    _fresh_user()
    todo_id = _create("甲的待办")["id"]

    client.cookies.clear()
    _fresh_user()
    res = client.patch(f"/api/todos/{todo_id}", json={"title": "我改了"})

    assert res.status_code == 404, "改别人的待办必须 404"


def test_other_learner_cannot_delete_it() -> None:
    _fresh_user()
    todo_id = _create("甲的待办")["id"]

    client.cookies.clear()
    _fresh_user()
    res = client.delete(f"/api/todos/{todo_id}")

    assert res.status_code == 404, "删别人的待办必须 404"


def test_404_not_403_so_existence_is_not_leaked() -> None:
    """**必须是 404 而不是 403** —— 403 等于确认这个 id 存在。

    同时验证"不存在"与"不属于自己"返回**完全一样**的响应体。
    """
    _fresh_user()
    others_id = _create("别人的")["id"]

    client.cookies.clear()
    _fresh_user()
    not_owned = client.patch(f"/api/todos/{others_id}", json={"title": "x"})
    missing = client.patch("/api/todos/99999999", json={"title": "x"})

    assert not_owned.status_code == missing.status_code == 404
    assert not_owned.json() == missing.json(), "两者响应不同 → 泄露了 id 是否存在"


# --------------------------------------------------------------------------- #
# 15. 不存在的 Todo
# --------------------------------------------------------------------------- #
def test_missing_todo_is_404_on_every_single_resource_verb() -> None:
    _fresh_user()
    assert client.patch("/api/todos/99999999", json={"title": "x"}).status_code == 404
    assert client.delete("/api/todos/99999999").status_code == 404


# --------------------------------------------------------------------------- #
# 16. 非法 due_date
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bad",
    ["2026-13-45", "13/45/2026", "2026/10/01", "明天", "20261001", "2026-10-01T10:00:00"],
)
def test_invalid_due_date_is_rejected_on_create(bad: str) -> None:
    _fresh_user()
    res = client.post("/api/todos", json={"title": "x", "due_date": bad})
    assert res.status_code == 422, f"{bad!r} 应当被拒"


def test_invalid_due_date_is_rejected_on_patch() -> None:
    _fresh_user()
    todo_id = _create("x")["id"]
    res = client.patch(f"/api/todos/{todo_id}", json={"due_date": "不是日期"})
    assert res.status_code == 422


# --------------------------------------------------------------------------- #
# 补充：排序与分页（列表的默认口径）
# --------------------------------------------------------------------------- #
def test_unfinished_first_newest_on_top() -> None:
    """未完成在前；同组内新的在上 —— 与前端 `todoState.ts` 的排序一致。"""
    _fresh_user()
    first = _create("先建的")
    second = _create("后建的")

    assert [t["title"] for t in _list()["items"]] == ["后建的", "先建的"]

    client.patch(f"/api/todos/{second['id']}", json={"completed": True})

    assert [t["title"] for t in _list()["items"]] == ["先建的", "后建的"], "完成的不该排在前"


def test_limit_and_offset() -> None:
    _fresh_user()
    for i in range(5):
        _create(f"第 {i} 条")

    page = _list(limit=2)
    assert len(page["items"]) == 2
    assert page["total"] == 5, "total 是全量，不受 limit 影响"

    first_page = {t["id"] for t in _list(limit=2)["items"]}
    second_page = {t["id"] for t in _list(limit=2, offset=2)["items"]}
    assert not (first_page & second_page), "分页出现了重复项"


def test_limit_is_bounded() -> None:
    _fresh_user()
    assert client.get("/api/todos", params={"limit": 0}).status_code == 422
    assert client.get("/api/todos", params={"limit": 9999}).status_code == 422


# --------------------------------------------------------------------------- #
# 未登录：待办是私人内容，**必须登录**
# --------------------------------------------------------------------------- #
def test_anonymous_cannot_use_todos() -> None:
    """`require_learner_id` 会拒绝未登录；用 `current_learner_id` 就会掉进
    `local`（= demo 账号），那等于谁都能看 demo 的待办。"""
    client.cookies.clear()
    assert client.get("/api/todos").status_code == 401
    assert client.post("/api/todos", json={"title": "x"}).status_code == 401


# --------------------------------------------------------------------------- #
# 计时（Phase 5E+）
#
# 计时是一**对**字段（timer_mode + timer_minutes），必须一起给：
#   countdown 必须给分钟数；countup / none 必须不给。
# 这一组把"成对"这条规则钉死。
# --------------------------------------------------------------------------- #
def _create_with(**body) -> dict:
    payload = {"title": "计时任务", **body}
    res = client.post("/api/todos", json=payload)
    assert res.status_code == 201, res.text
    return dict(res.json())


def test_default_is_no_timer() -> None:
    _fresh_user()
    todo = _create_with()
    assert todo["timer_mode"] == "none"
    assert todo["timer_minutes"] is None
    assert todo["spent_seconds"] == 0


def test_create_countup_timer() -> None:
    _fresh_user()
    todo = _create_with(timer_mode="countup")
    assert todo["timer_mode"] == "countup"
    assert todo["timer_minutes"] is None, "正计时不该有目标分钟数"


def test_create_countdown_timer() -> None:
    _fresh_user()
    todo = _create_with(timer_mode="countdown", timer_minutes=25)
    assert todo["timer_mode"] == "countdown"
    assert todo["timer_minutes"] == 25


@pytest.mark.parametrize("mode,minutes", [("countup", 25), ("none", 10)])
def test_countup_and_none_must_not_carry_minutes(mode: str, minutes: int) -> None:
    """给了分钟数会让人以为它会响 —— 挡掉。"""
    _fresh_user()
    res = client.post(
        "/api/todos", json={"title": "x", "timer_mode": mode, "timer_minutes": minutes}
    )
    assert res.status_code == 422


def test_countdown_requires_minutes() -> None:
    _fresh_user()
    res = client.post("/api/todos", json={"title": "x", "timer_mode": "countdown"})
    assert res.status_code == 422, "不给分钟数就不知道从哪儿往下数"


@pytest.mark.parametrize("bad", [0, -1, 601, 99999])
def test_countdown_minutes_range(bad: int) -> None:
    _fresh_user()
    res = client.post(
        "/api/todos", json={"title": "x", "timer_mode": "countdown", "timer_minutes": bad}
    )
    assert res.status_code == 422


def test_unknown_timer_mode_is_rejected() -> None:
    _fresh_user()
    res = client.post("/api/todos", json={"title": "x", "timer_mode": "pomodoro"})
    assert res.status_code == 422


def test_patch_switch_countdown_to_countup() -> None:
    """把倒计时改成正计时 = 显式把分钟数设成 null（与 due_date 的清空同一种语义）。"""
    _fresh_user()
    todo_id = _create_with(timer_mode="countdown", timer_minutes=25)["id"]

    res = client.patch(
        f"/api/todos/{todo_id}", json={"timer_mode": "countup", "timer_minutes": None}
    )

    assert res.status_code == 200
    assert res.json()["timer_mode"] == "countup"
    assert res.json()["timer_minutes"] is None


def test_patch_timer_requires_the_mode_to_come_along() -> None:
    """只给分钟数不给方式 —— 表达不了意图，拒绝。"""
    _fresh_user()
    todo_id = _create_with()["id"]
    res = client.patch(f"/api/todos/{todo_id}", json={"timer_minutes": 30})
    assert res.status_code == 422


def test_patch_spent_seconds() -> None:
    """计时暂停时前端回写累计秒数。"""
    _fresh_user()
    todo_id = _create_with(timer_mode="countup")["id"]

    res = client.patch(f"/api/todos/{todo_id}", json={"spent_seconds": 754})

    assert res.status_code == 200
    assert res.json()["spent_seconds"] == 754
    assert res.json()["timer_mode"] == "countup", "回写秒数不该改动计时方式"


def test_patch_negative_spent_seconds_is_rejected() -> None:
    _fresh_user()
    todo_id = _create_with(timer_mode="countup")["id"]
    assert client.patch(f"/api/todos/{todo_id}", json={"spent_seconds": -1}).status_code == 422


def test_patching_title_does_not_touch_the_timer() -> None:
    """⚠️ 与 due_date 同一条规矩：改标题不该顺手把计时配置抹掉。"""
    _fresh_user()
    todo_id = _create_with(timer_mode="countdown", timer_minutes=25)["id"]
    client.patch(f"/api/todos/{todo_id}", json={"spent_seconds": 300})

    res = client.patch(f"/api/todos/{todo_id}", json={"title": "改了标题"})

    body = res.json()
    assert body["title"] == "改了标题"
    assert body["timer_mode"] == "countdown"
    assert body["timer_minutes"] == 25
    assert body["spent_seconds"] == 300, "累计时长被意外清空"


def test_timer_does_not_survive_into_other_learners() -> None:
    """计时配置也受归属隔离 —— 别人改不了我的计时。"""
    _fresh_user()
    todo_id = _create_with(timer_mode="countup")["id"]

    client.cookies.clear()
    _fresh_user()
    res = client.patch(
        f"/api/todos/{todo_id}", json={"timer_mode": "countdown", "timer_minutes": 5}
    )

    assert res.status_code == 404
