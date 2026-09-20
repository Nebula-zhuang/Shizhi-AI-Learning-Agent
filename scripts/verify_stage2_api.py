"""自由学习空间 API 端到端验证（阶段 2）。

覆盖：
  · 登录门（匿名必须 401）
  · 对话 CRUD
  · 场景 A：普通问题 → 无工具 → 直接回答
  · 场景 B：资料不足再联网 → **循环里出现 ≥2 次工具调用**
  · MCP provider 标识出现在引用里
  · SSE 事件完整性

真打 HTTP、真走 SSE、真落库。
"""

from __future__ import annotations

import http.cookiejar
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))

BASE = "http://127.0.0.1:8000"

JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(JAR))

PASS = "✔"
FAIL = "✘"


def request(method: str, path: str, *, body: dict | None = None, opener=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    client = opener or OPENER
    try:
        with client.open(req, timeout=180) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": raw[:200].decode(errors="ignore")}


def sse_ask(path: str, question: str) -> list[tuple[str, dict]]:
    body = json.dumps({"question": question}).encode()
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )
    events: list[tuple[str, dict]] = []
    with OPENER.open(req, timeout=300) as resp:
        name = ""
        for raw in resp:
            line = raw.decode("utf-8", errors="ignore").rstrip("\n")
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                try:
                    events.append((name, json.loads(line[5:].strip())))
                except json.JSONDecodeError:
                    pass
    return events


def main() -> int:
    from app.core.config import settings  # noqa: PLC0415

    ok = True

    # ────────────────────────────────────────── ① 登录门
    print("① 登录门（阶段 2 新增的隐私边界）")
    anon = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    status, _ = request("GET", "/api/study/conversations", opener=anon)
    gate = status == 401
    ok &= gate
    print(f"   匿名访问（清空 cookie）→ HTTP {status}  {PASS if gate else FAIL}")

    status, payload = request(
        "POST", "/api/auth/login", body={"username": "demo", "password": settings.demo_password}
    )
    if status != 200 or "user" not in payload:
        print(f"   {FAIL} 登录失败：HTTP {status}")
        return 1
    print(f"   登录后 → HTTP {status}  {PASS}")

    # ────────────────────────────────────────── ② 能力自检（含 MCP 真实状态）
    print()
    print("② 能力自检（MCP 的真实身份，不是「配了个 Provider」）")
    _, caps = request("GET", "/api/study/capabilities")
    mcp = caps.get("mcp") or {}
    print(f"   后端策略: {caps.get('web_search_backend')}")
    print(f"   MCP 配置: {mcp.get('configured')}")
    print(f"   服务端自报身份: {mcp.get('server_info')}")
    print(f"   运行时发现的工具: {mcp.get('discovered_tools')}")
    print(f"   已注册工具: {caps.get('tools')}")

    # ────────────────────────────────────────── ③ 新建对话
    print()
    print("③ 新建对话")
    status, conv = request("POST", "/api/study/conversations", body={"title": ""})
    if status != 200:
        print(f"   {FAIL} HTTP {status} {conv}")
        return 1
    cid = conv["id"]
    print(f"   对话 id={cid}")

    # ────────────────────────────────────────── ④ 场景 A
    print()
    print("④ 场景 A：普通问题 → 无工具 → 直接回答")
    t0 = time.time()
    events = sse_ask(f"/api/study/conversations/{cid}/ask", "什么是 JVM？")
    kinds: dict[str, int] = {}
    for name, _ in events:
        kinds[name] = kinds.get(name, 0) + 1
    tool_starts = kinds.get("tool_start", 0)
    done = next((d for name, d in events if name == "done"), {})
    scenario_a = tool_starts == 0 and kinds.get("delta", 0) > 5
    ok &= scenario_a
    print(f"   事件: {kinds}")
    print(f"   工具调用 {tool_starts} 次（应为 0）  {PASS if tool_starts == 0 else FAIL}")
    print(f"   增量帧 {kinds.get('delta', 0)} 个  耗时 {time.time() - t0:.1f}s")
    print(f"   步骤 {len(done.get('steps') or [])}  | provider={done.get('provider') or '（未联网）'}")

    # ────────────────────────────────────────── ⑤ 场景 B（核心）
    print()
    print("⑤ 场景 B：资料不足再联网 → **循环里出现 ≥2 次工具调用**")
    t0 = time.time()
    events = sse_ask(
        f"/api/study/conversations/{cid}/ask",
        "根据我的资料讲讲虚拟线程，资料里没有的再联网查",
    )
    done = next((d for name, d in events if name == "done"), {})
    steps = done.get("steps") or []
    tool_steps = [s for s in steps if s.get("tool")]
    tools_used = [s["tool"] for s in tool_steps]

    scenario_b = len(tool_steps) >= 2
    ok &= scenario_b
    print(f"   step trace（这就是「确实是 Agent」的证据）：")
    for s in steps:
        print(f"      step {s['index']}: {s['state']:<14} tool={str(s['tool']):<20} ok={s['tool_ok']}")
    print(f"   工具调用序列: {tools_used}")
    print(f"   调用 ≥2 次（流水线做不到）  {PASS if scenario_b else FAIL}")
    print(f"   provider={done.get('provider')} | fell_back={done.get('fell_back')}")

    # ────────────────────────────────────────── ⑥ MCP 证据
    print()
    print("⑥ 「确实经过 MCP」的证据")
    citations = done.get("citations") or []
    web_cites = [c for c in citations if c.get("kind") == "web"]
    providers = {c.get("provider") for c in web_cites}
    print(f"   引用总数 {len(citations)}（网页 {len(web_cites)}）")
    if web_cites:
        sample = web_cites[0]
        print(f"   实际 provider = {sample.get('provider')}")
        print(f"   provider_detail = {sample.get('provider_detail')}")
        print(f"   fell_back = {sample.get('fell_back')}")
        mcp_used = sample.get("provider") == "mcp"
        ok &= mcp_used
        print(f"   引用确实来自 MCP  {PASS if mcp_used else FAIL}")
    else:
        print(f"   （这一轮没有联网引用）")

    # ────────────────────────────────────────── ⑦ 落库与隔离
    print()
    print("⑦ 落库与越权防护")
    _, msgs = request("GET", f"/api/study/conversations/{cid}/messages")
    items = msgs.get("items", [])
    print(f"   消息数 {len(items)}（应为 4：两轮各一问一答）")
    ok &= len(items) == 4
    for m in items:
        print(f"      · {m['role']:<9} {len(m['content']):>4} 字  引用 {len(m.get('citations') or [])}")

    status, _ = request("GET", "/api/study/conversations/999999/messages")
    print(f"   访问不存在的对话 → HTTP {status}  {PASS if status == 404 else FAIL}")
    ok &= status == 404

    # ────────────────────────────────────────── 收尾
    print()
    print("=" * 64)
    print("  结论：" + ("阶段 2 全链路通过 " + PASS if ok else "存在问题 " + FAIL))
    print("=" * 64)
    return 0 if ok else 1


if __name__ == "__main__":
    import os

    os.chdir(ROOT)
    raise SystemExit(main())
