"""诊断：慢在哪 —— 模型本身，还是调用链。

做三组对照，把"模型慢"和"链路长"分开：

  A. 模型裸延迟：给它一个极短的请求，测它自己的往返时间。
     这是**这个模型的下限**，跟我们的架构无关。
  B. 单次真实调用的延迟：评估 / 决策 / 生成，各自的输入规模与耗时。
  C. 一轮的端到端：以及各段占比。

跑法：python scripts/diagnose_latency.py
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "api"))
os.chdir(PROJECT_ROOT)

from app.core.config import settings  # noqa: E402
from app.core.llm import llm_gateway  # noqa: E402

BASE = "http://127.0.0.1:8000"


def post(path: str, payload: dict) -> tuple[dict, float]:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    body = json.loads(urllib.request.urlopen(req, timeout=300).read())
    return body, (time.perf_counter() - started) * 1000


def get(path: str) -> dict:
    return json.loads(urllib.request.urlopen(BASE + path, timeout=60).read())


async def probe_model_floor() -> list[float]:
    """A. 模型裸延迟。

    两个请求：一个几乎不用思考（回一个词），一个需要一点推理。
    两者的差值说明"思考"要花多少钱，绝对值说明"往返"要花多少钱。
    """
    cases = [
        ("极短输出（1 个词）", "只回答一个词：好", 8),
        ("短推理（1 句话）", "用一句话说明进程和线程的区别。", 60),
    ]
    results: list[float] = []
    for label, prompt, max_tokens in cases:
        samples = []
        for _ in range(3):
            started = time.perf_counter()
            text = ""
            async for piece in llm_gateway.stream_chat(
                [{"role": "user", "content": prompt}], max_tokens=max_tokens
            ):
                text += piece
            samples.append((time.perf_counter() - started) * 1000)
        median = statistics.median(samples)
        results.append(median)
        print(f"    {label:<20} 中位 {median:>7.0f} ms   样本 {[round(s) for s in samples]}")
    return results


def main() -> int:
    print("=" * 78)
    print("  延迟诊断")
    print("=" * 78)
    print(f"  模型：{settings.llm_model}    网关：{settings.llm_base_url}")
    print()

    # ---------------------------------------------------------------- A
    print("A. 模型裸延迟（不含我们的任何逻辑）")
    asyncio.run(probe_model_floor())
    print()

    # ---------------------------------------------------------------- B/C
    documents = get("/api/documents?limit=100&offset=0")["items"]
    textbook = next((d for d in documents if "操作系统原理" in d["file_name"]), documents[0])
    points = get(f"/api/documents/{textbook['id']}/knowledge-points?limit=100")["items"]
    target = next((p for p in points if "线程" in p["title"]), points[0])
    print(f"B. 一轮真实教学（知识点：{target['title']}）")
    print()

    payload, ms = post("/api/tutor/start", {"knowledge_point_id": target["id"]})
    print(f"   开课           端到端 {ms:>7.0f} ms")
    for name, cost in _rows(payload):
        print(f"     · {name:<22} {cost:>6} ms")
    print()

    session_id = payload["session_id"]
    totals: list[float] = []
    phases: dict[str, list[int]] = {}
    for index in range(3):
        payload, ms = post(
            "/api/tutor/answer",
            {"session_id": session_id, "answer": "线程是进程里被调度的单位，进程是资源分配的单位。"},
        )
        totals.append(ms)
        print(f"   作答 #{index + 1}      端到端 {ms:>7.0f} ms   动作={payload['action']}")
        for name, cost in _rows(payload):
            phases.setdefault(name, []).append(cost)
            print(f"     · {name:<22} {cost:>6} ms")
    print()

    print("C. 汇总")
    if totals:
        print(f"   作答轮端到端中位   {statistics.median(totals):>7.0f} ms")
    llm_total = 0.0
    for name, costs in sorted(phases.items(), key=lambda kv: -statistics.median(kv[1])):
        median = statistics.median(costs)
        print(f"     · {name:<22} {median:>7.0f} ms   （{len(costs)} 次）")
        if name in {"evaluate_answer", "decide_action", "generate_question"}:
            llm_total += median
    if totals:
        median_total = statistics.median(totals)
        share = (llm_total / median_total) * 100 if median_total else 0
        print()
        print(f"   三次串行 LLM 合计   {llm_total:>7.0f} ms，占端到端 {share:.0f}%")
    print()
    print("=" * 78)
    return 0


def _rows(payload: dict) -> list[tuple[str, int]]:
    records = (payload.get("tools") or {}).get("records") or []
    return [
        (str(r.get("name")), int(r["elapsed_ms"]))
        for r in records
        if isinstance(r, dict) and r.get("elapsed_ms") is not None
    ]


if __name__ == "__main__":
    raise SystemExit(main())
