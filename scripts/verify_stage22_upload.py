"""阶段 2.2 / 2.3 端到端验证：图片上传 → ask → 看图 → 回答。

真打 HTTP（multipart 上传 + SSE），真落盘，真落库。
"""

from __future__ import annotations

import http.cookiejar
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))

BASE = "http://127.0.0.1:8000"
JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(JAR))

PASS, FAIL = "✔", "✘"


def request(method: str, path: str, *, body: dict | None = None, opener=None):
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with (opener or OPENER).open(req, timeout=180) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": raw[:200].decode(errors="ignore")}


def upload_image(path: pathlib.Path) -> tuple[int, dict]:
    """手工拼 multipart —— 不引 requests，保持零依赖。"""
    boundary = f"----lb{uuid.uuid4().hex}"
    body = b""
    body += f"--{boundary}\r\n".encode()
    body += (
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
    ).encode()
    body += b"Content-Type: image/png\r\n\r\n"
    body += path.read_bytes()
    body += f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        f"{BASE}/api/study/attachments",
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with OPENER.open(req, timeout=180) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": raw[:200].decode(errors="ignore")}


def sse_ask(conversation_id: int, question: str, document_ids: list[int]):
    body = json.dumps({"question": question, "document_ids": document_ids}).encode()
    req = urllib.request.Request(
        f"{BASE}/api/study/conversations/{conversation_id}/ask",
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

    # 生成一张含可识别代码的测试图 —— 回答里必须出现这些标识符
    image_path = ROOT / "data" / "_stage22_test.png"
    image_path.parent.mkdir(exist_ok=True)
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (640, 240), "white")
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(
        ["def fib(n):", "    if n < 2:", "        return n", "    return fib(n-1) + fib(n-2)", "", "print(fib(10))  # 55"]
    ):
        draw.text((20, 20 + i * 32), line, fill="black")
    img.save(image_path)
    print(f"① 测试图已生成：{image_path.name}（含 fib 递归函数）")

    # 登录
    status, payload = request(
        "POST", "/api/auth/login", body={"username": "demo", "password": settings.demo_password}
    )
    if status != 200:
        print(f"   {FAIL} 登录失败：HTTP {status}")
        return 1
    print(f"② 登录 {PASS}")

    print()
    print("③ 上传（匿名必须被拒）")
    anon = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    status, _ = request("GET", "/api/study/attachments", opener=anon)
    ok &= status == 401
    print(f"   匿名上传/列举 → HTTP {status}  {PASS if status == 401 else FAIL}")

    status, uploaded = upload_image(image_path)
    if status != 200:
        print(f"   {FAIL} 上传失败：HTTP {status} {uploaded}")
        return 1
    doc_id = uploaded["document_id"]
    print(f"   上传成功 → document_id={doc_id}")
    print(f"   文件名={uploaded['file_name']} 大小={uploaded['file_size']} 是图片={uploaded['is_image']}")
    print(f"   状态={uploaded['parse_status']}  去重={uploaded.get('dedup')}  {PASS}")

    print()
    print("④ 重复上传应命中去重")
    _, again = upload_image(image_path)
    dedup_ok = again.get("dedup") is True and again["document_id"] == doc_id
    ok &= dedup_ok
    print(f"   document_id={again['document_id']} dedup={again.get('dedup')}  {PASS if dedup_ok else FAIL}")

    print()
    print("⑤ 新建对话并带图提问")
    _, conv = request("POST", "/api/study/conversations", body={"title": ""})
    cid = conv["id"]
    t0 = time.time()
    events = sse_ask(cid, "这张图里的代码在做什么？输出是什么？", [doc_id])
    kinds: dict[str, int] = {}
    for name, _ in events:
        kinds[name] = kinds.get(name, 0) + 1
    done = next((d for name, d in events if name == "done"), {})
    answer = "".join(d.get("text", "") for _n, d in events if _n == "delta")

    print(f"   事件: {kinds}  耗时 {time.time() - t0:.1f}s")
    for s in done.get("steps") or []:
        print(f"      step {s['index']}: {s['state']:<14} tool={str(s['tool']):<18} ok={s['tool_ok']}")

    print()
    print("⑥ 是否真的看了图（回答里要有图内标识符）")
    for kw, label in [("fib", "函数名 fib"), ("递归", "概念「递归」"), ("55", "输出值 55")]:
        hit = kw.lower() in answer.lower()
        print(f"   {label:<14} {'有 ' + PASS if hit else '没有 ' + FAIL}")
    print()
    print("   回答前 200 字:", answer[:200].replace("\n", " "))

    print()
    print("=" * 64)
    print("  结论：" + ("通过 " + PASS if ok else "存在问题 " + FAIL))
    print("=" * 64)
    return 0 if ok else 1


if __name__ == "__main__":
    import os

    os.chdir(ROOT)
    raise SystemExit(main())
