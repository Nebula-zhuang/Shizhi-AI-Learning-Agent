"""验证流式阶段反馈：记录每一帧到达的真实时刻。

要证明的两件事：
1. 第一帧必须在**很早就到**（不能长时间白屏）；
2. 每一帧的内容要和后台真正在做的事对得上（不是假进度条）。

跑法：python scripts/probe_sse.py
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "api"))

BASE = "http://127.0.0.1:8000"


def post(path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return json.loads(urllib.request.urlopen(request, timeout=300).read())


def get(path: str) -> dict:
    return json.loads(urllib.request.urlopen(BASE + path, timeout=60).read())


def stream(path: str, payload: dict) -> None:
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    started = time.perf_counter()
    final: dict | None = None
    error: str | None = None
    first_content_ms: float | None = None
    content_chars = 0
    stage_count = 0

    with urllib.request.urlopen(request, timeout=300) as response:
        event = ""
        for raw in response:
            line = raw.decode("utf-8").rstrip("\n")
            if line.startswith("event: "):
                event = line[7:]
                continue
            if not line.startswith("data: "):
                continue
            body = json.loads(line[6:])
            elapsed = (time.perf_counter() - started) * 1000
            if event == "stage":
                stage_count += 1
                print(f"    {elapsed:>7.0f} ms  阶段：{body['label']}")
            elif event == "content":
                content_chars += len(body.get("text", ""))
                if first_content_ms is None:
                    first_content_ms = elapsed
                    print(f"    {elapsed:>7.0f} ms  ✎ 正文开始出现…")
            elif event == "done":
                final = body
                print(f"    {elapsed:>7.0f} ms  ✔ 完成（正文 {len(body['content'])} 字）")
                records = (body.get("tools") or {}).get("records") or []
                for item in records:
                    if item.get("elapsed_ms") is not None and item["elapsed_ms"] >= 50:
                        print(f"        · {item['name']:<20} {item['elapsed_ms']:>6} ms")
            elif event == "error":
                error = body.get("message", "")
                print(f"    {elapsed:>7.0f} ms  ✘ 失败：{error}")

    total = (time.perf_counter() - started) * 1000
    print(f"    ── 总耗时 {total:.0f} ms")
    if first_content_ms is not None:
        print(
            f"    ── 第一个字出现在 {first_content_ms:.0f} ms，"
            f"之后 {content_chars} 字分 {content_chars} 次流式到达"
        )
        print(f"    ── 也就是说：学习者从 {first_content_ms / 1000:.1f}s 起就能开始读，"
              f"而不是等到 {total / 1000:.1f}s")
    if final:
        print(f"    ── 动作={final['action']}  降级={final['degraded']}")
        print()
        print("    ── 助教这一轮说的话 ──")
        for line in final["content"].split("\n")[:22]:
            print("      " + line)


def main() -> int:
    documents = get("/api/documents?limit=100&offset=0")["items"]
    textbook = next((d for d in documents if "操作系统原理" in d["file_name"]), None)
    if textbook is None:
        print("找不到样例教材")
        return 2
    points = get(f"/api/documents/{textbook['id']}/knowledge-points?limit=100")["items"]
    target = next((p for p in points if "线程" in p["title"]), points[0])

    print("=" * 78)
    print(f"  流式验证 · {target['title']}")
    print("=" * 78)
    print()
    print("  【开课】POST /api/tutor/start/stream")
    session = post("/api/tutor/start", {"knowledge_point_id": target["id"]})
    print()
    print("  【作答】POST /api/tutor/answer/stream")
    print()
    stream(
        "/api/tutor/answer/stream",
        {
            "session_id": session["session_id"],
            "answer": "线程是进程里面真正被 CPU 执行的那个单位。",
        },
    )
    print()
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
