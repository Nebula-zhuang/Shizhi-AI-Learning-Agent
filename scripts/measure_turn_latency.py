"""真实调用链耗时打点。

跑法：python scripts/measure_turn_latency.py [--rounds 3]

做两件事：
1. 端到端计时：start / answer 各花多久（用户真正感受到的等待）
2. 拆解每一段：从返回体的 tools 留痕里读出 RAG / 决策 / 生成各占多少

**只读不改** —— 打的是产品自己的接口，和真人操作完全一致。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "api"))

parser = argparse.ArgumentParser(description="测量 Tutor 一轮的耗时分布")
parser.add_argument("--base", default="http://127.0.0.1:8000")
parser.add_argument("--rounds", type=int, default=3, help="作答轮数")
args = parser.parse_args()

BASE = args.base.rstrip("/")

#: 答得含糊 —— 逼出「换个讲法」这类动作，顺便验证反馈是否具体
WEAK = "应该是跟资源分配有关系吧，具体我也说不太清楚。"


def post(path: str, payload: dict) -> tuple[dict, float]:
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    body = json.loads(urllib.request.urlopen(request, timeout=300).read())
    return body, (time.perf_counter() - started) * 1000


def get(path: str) -> dict:
    return json.loads(urllib.request.urlopen(BASE + path, timeout=60).read())


def rows(payload: dict) -> list[tuple[str, int]]:
    """从 tools 留痕里取出每一段的耗时。

    运行时已经把每一步的 elapsed_ms 记在 `tools.records` 里
    （键名是 records，不是 trace —— 第一版脚本取错了，导致"看起来没有留痕"）。
    """
    out: list[tuple[str, int]] = []
    tools = payload.get("tools") or {}
    records = tools.get("records") if isinstance(tools, dict) else None
    if isinstance(records, list):
        for item in records:
            if isinstance(item, dict) and item.get("elapsed_ms") is not None:
                out.append((str(item.get("name")), int(item["elapsed_ms"])))
    return out


def turn_total(payload: dict) -> int | None:
    tools = payload.get("tools") or {}
    if isinstance(tools, dict) and tools.get("elapsed_ms") is not None:
        return int(tools["elapsed_ms"])
    return None


def main() -> int:
    print("=" * 78)
    print("  Tutor 真实调用链耗时")
    print("=" * 78)

    documents = get("/api/documents?limit=100&offset=0")["items"]
    textbook = next((d for d in documents if "操作系统原理" in d["file_name"]), None)
    if textbook is None:
        print("  找不到样例教材，先跑 python scripts/load_samples.py")
        return 2
    points = get(f"/api/documents/{textbook['id']}/knowledge-points?limit=100")["items"]
    target = next((p for p in points if "进程" in p["title"]), points[0])
    print(f"  知识点：{target['title']}")
    print()

    payload, ms = post("/api/tutor/start", {"knowledge_point_id": target["id"]})
    session_id = payload["session_id"]
    print(f"  ── start（开课）{ms:.0f} ms")
    for name, cost in rows(payload):
        print(f"       {name:<22} {cost:>6} ms")
    if not rows(payload):
        print("       （返回体里没有分阶段留痕）")
    print()

    totals: list[float] = []
    phases: dict[str, list[int]] = {}
    for index in range(args.rounds):
        payload, ms = post(
            "/api/tutor/answer", {"session_id": session_id, "answer": WEAK}
        )
        totals.append(ms)
        print(
            f"  ── answer #{index + 1}  端到端 {ms:.0f} ms   "
            f"动作={payload['action']}  降级={payload['degraded']}"
        )
        for name, cost in rows(payload):
            phases.setdefault(name, []).append(cost)
            print(f"       {name:<22} {cost:>6} ms")
        assessment = payload.get("assessment")
        if assessment:
            print(f"       ↳ 反馈：{assessment['feedback'][:88]}")
        print()

    print("=" * 78)
    print("  汇总")
    print("=" * 78)
    if totals:
        print(f"  作答轮端到端：中位 {statistics.median(totals):.0f} ms  "
              f"最慢 {max(totals):.0f} ms  最快 {min(totals):.0f} ms")
    for name, costs in sorted(phases.items(), key=lambda kv: -statistics.median(kv[1])):
        print(f"    {name:<24} 中位 {statistics.median(costs):>6.0f} ms  （{len(costs)} 次）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
