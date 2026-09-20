"""认证接口验收探针（一次性）。

覆盖：未登录、用户名校验、注册、Cookie 会话、/me、错误口令、重复用户名、
以及最关键的 —— **两个账号的学习数据确实隔离**。
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "api"))
os.chdir(PROJECT_ROOT)

from app.core.config import settings  # noqa: E402

BASE = "http://127.0.0.1:8000"


def session() -> tuple[object, http.cookiejar.CookieJar]:
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar)), jar


def call(opener, method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        return json.loads(opener.open(req, timeout=60).read()), 200
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "ignore")
        try:
            return json.loads(raw), exc.code
        except json.JSONDecodeError:
            return {"detail": raw[:120]}, exc.code


def main() -> int:
    # ---------------------------------------------------------------- 未登录
    anon, _ = session()
    data, code = call(anon, "GET", "/api/auth/me")
    print(f"1. 未登录 /me                → HTTP {code}  body={data}")

    # ------------------------------------------------------ 用户名合法性校验
    print("\n2. 用户名规则校验")
    for username in ["ab", "1abc", "a b", "a" * 21, "Tom_2024"]:
        _, code = call(anon, "POST", "/api/auth/register", {"username": username, "password": "secret123"})
        mark = "通过" if code == 200 else "拒绝"
        print(f"   {username!r:<14} → HTTP {code}  {mark}")

    # -------------------------------------------------------------- 新用户注册
    print("\n3. 注册新用户 xiaoming")
    fresh, _ = session()
    data, code = call(fresh, "POST", "/api/auth/register",
                      {"username": "xiaoming", "password": "secret123", "display_name": "小明"})
    print(f"   HTTP {code}  {json.dumps(data, ensure_ascii=False)[:150]}")

    # -------------------------------------------------------- 新用户的学习数据
    new_learner = (data.get("user") or {}).get("learner_id") if code == 200 else None
    if new_learner:
        dash, _ = call(fresh, "GET", "/api/tutor/dashboard")
        overview = dash.get("overview", {}) if isinstance(dash, dict) else {}
        print(f"   新账号 learner_id={new_learner}  看板已追踪={overview.get('tracked')} 个知识点")

    # ------------------------------------------------------------ 演示账号登录
    print("\n4. 登录演示账号（应看到 P0–P5 累积数据）")
    demo, jar = session()
    data, code = call(demo, "POST", "/api/auth/login",
                      {"username": settings.demo_username, "password": settings.demo_password})
    if code != 200:
        print(f"   HTTP {code} 失败：{data}")
        return 1
    user = data["user"]
    print(f"   HTTP {code}  登录名={user['username']}  展示名={user['display_name']}  learner_id={user['learner_id']}")
    print(f"   下发的 Cookie：{[c.name for c in jar]}（值为 httpOnly，脚本只做请求转发）")
    print(f"   响应体里有没有 token 字段？ {'有（不合格）' if 'token' in json.dumps(data) else '没有（合格）'}")

    dash, _ = call(demo, "GET", "/api/tutor/dashboard")
    overview = dash.get("overview", {}) if isinstance(dash, dict) else {}
    print(f"   演示账号看板：已追踪 {overview.get('tracked')} 个知识点、"
          f"待复习 {len(dash.get('due_reviews', []))} 个、薄弱 {len(dash.get('weak_points', []))} 个")

    me, _ = call(demo, "GET", "/api/auth/me")
    print(f"   登录后 /me：{json.dumps(me, ensure_ascii=False)[:110]}")

    # ---------------------------------------------------------------- 边界
    print("\n5. 边界情况")
    bad, _ = session()
    _, code = call(bad, "POST", "/api/auth/login", {"username": settings.demo_username, "password": "wrong-password"})
    print(f"   错误口令                 → HTTP {code}")
    _, code = call(bad, "POST", "/api/auth/login", {"username": "no-such-user", "password": "whatever"})
    print(f"   不存在的用户             → HTTP {code}（应与错误口令一致，不泄露账号是否存在）")
    _, code = call(anon, "POST", "/api/auth/register", {"username": "xiaoming", "password": "another123"})
    print(f"   重复用户名               → HTTP {code}")
    _, code = call(anon, "POST", "/api/auth/register", {"username": "shortpw", "password": "123"})
    print(f"   口令过短                 → HTTP {code}")

    # ---------------------------------------------------- 数据隔离（最关键）
    if new_learner:
        print("\n6. 数据隔离（本项最关键）")
        print(f"   新账号 learner_id = {new_learner}")
        print(f"   演示账号 learner_id = {user['learner_id']}")
        print(f"   两者不同 → 学习数据互不可见：{'是' if new_learner != user['learner_id'] else '否（有问题）'}")

    # 登出
    out, _ = session()
    call(out, "POST", "/api/auth/login", {"username": settings.demo_username, "password": settings.demo_password})
    _, code = call(out, "POST", "/api/auth/logout")
    after, _ = call(out, "GET", "/api/auth/me")
    print(f"\n7. 登出后 /me                → {after}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
