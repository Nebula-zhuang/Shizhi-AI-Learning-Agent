"""对照实验：把「作答评估」这一步换到更快的模型，判定会不会变差。

为什么单测这一项：评估是教学闭环的命门 —— 它判错，后面的
「换讲法 / 降难度 / 升难度」全跟着错。生成内容可以慢（要质量），
但评估和决策是**判断类任务**，输出短、模式固定，理论上更适合轻量模型。

做法：拿真实知识点素材 + 三类回答（答对 / 答得含糊 / 答错），
让两个模型各判一次，比对 level、error_type、correct 是否一致。

跑法：python scripts/ab_assessment_model.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "api"))
os.chdir(PROJECT_ROOT)

from openai import AsyncOpenAI  # noqa: E402

from app.core.config import settings  # noqa: E402

MODELS = ["qwen-plus", "qwen-flash", "qwen-turbo"]

#: 三类回答：完整 / 含糊 / 跑偏。用真实教材的知识点做素材。
CASES = [
    {
        "kp": "线程与进程的区别",
        "reference": (
            "进程是资源分配的基本单位，它拥有独立的地址空间、文件表等资源。"
            "线程是处理机调度的基本单位，同一进程内的多个线程共享该进程的地址空间与资源。"
            "线程的创建、撤销与切换开销都比进程小。"
        ),
        "question": "进程和线程，谁负责分配资源、谁负责被CPU调度？",
        "answers": [
            ("完整", "进程是资源分配的基本单位，线程是处理机调度的基本单位。同一进程内的线程共享进程的地址空间。"),
            ("含糊", "进程比较重，线程比较轻，线程切换快一些。"),
            ("跑偏", "进程是静态的代码，线程是动态执行的程序。"),
        ],
    },
    {
        "kp": "死锁的四个必要条件",
        "reference": (
            "死锁的产生必须同时满足四个条件：互斥条件、请求与保持条件、"
            "不可剥夺条件、循环等待条件。四个条件缺一不可，"
            "因此破坏其中任意一个即可预防死锁。"
        ),
        "question": "产生死锁需要同时满足哪几个条件？破坏其中一个会怎样？",
        "answers": [
            ("完整", "需要互斥、请求与保持、不可剥夺、循环等待四个条件同时成立。破坏任意一个就不会死锁，所以预防死锁就是破坏其中之一。"),
            ("含糊", "需要好几个条件同时满足，好像有互斥和循环等待。"),
            ("跑偏", "死锁是因为内存不够，进程抢不到资源就卡住了。"),
        ],
    },
]

SYSTEM = """你是学习助手的作答评估模块。判断学生的回答是否真的答到了点子上。

依据只有给定的资料素材。学生没说的就是没说。
区分"没答对"和"没答透"：方向错→not_mastered；方向对但只说了一半→vague；完整准确→mastered。

只输出 JSON，不要解释、不要 Markdown 围栏：
{"correct": true, "score": 0.9, "confidence": 0.9, "level": "not_mastered|vague|mastered",
 "error_type": "concept_confusion|memory_gap|reasoning_break|misread|none",
 "feedback": "不超过 150 字，具体指出对在哪、缺在哪"}"""


async def judge(client: AsyncOpenAI, model: str, case: dict, answer: str) -> tuple[dict | None, float]:
    prompt = (
        f"## 资料素材（判断的唯一依据）\n\n{case['reference']}\n\n"
        f"## 提出的问题\n\n{case['question']}\n\n"
        f"## 学生的回答\n\n{answer}\n\n请按 Schema 输出 JSON："
    )
    started = time.perf_counter()
    try:
        res = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt},
            ],
            max_tokens=400,
            temperature=0.2,
        )
        text = (res.choices[0].message.content or "").strip()
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(text), (time.perf_counter() - started) * 1000
    except Exception as exc:  # noqa: BLE001
        return {"__error__": f"{type(exc).__name__}: {exc}"}, (time.perf_counter() - started) * 1000


async def main() -> int:
    client = AsyncOpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url)

    timings: dict[str, list[float]] = {m: [] for m in MODELS}
    agreements: dict[str, int] = {m: 0 for m in MODELS}
    total = 0

    print("=" * 84)
    print("  评估环节的模型对照（基准 = qwen-plus）")
    print("=" * 84)

    for case in CASES:
        print(f"\n【{case['kp']}】")
        for label, answer in case["answers"]:
            verdicts: dict[str, dict] = {}
            for model in MODELS:
                verdict, ms = await judge(client, model, case, answer)
                verdicts[model] = verdict or {}
                timings[model].append(ms)

            base = verdicts["qwen-plus"]
            print(f"  · {label:<4} {answer[:34]}…")
            for model in MODELS:
                v = verdicts[model]
                if "__error__" in v:
                    print(f"      {model:<12} 调用失败：{v['__error__'][:50]}")
                    continue
                level = v.get("level", "?")
                err = v.get("error_type", "?")
                mark = ""
                if model != "qwen-plus":
                    total += 1
                    # 判定一致性：level 与 error_type 都对得上才算一致
                    same = level == base.get("level") and err == base.get("error_type")
                    if same:
                        agreements[model] += 1
                        mark = "  与基准一致 ✔"
                    else:
                        mark = f"  与基准不同 ✘（基准 {base.get('level')}/{base.get('error_type')}）"
                print(
                    f"      {model:<12} level={level:<13} err={err:<17}"
                    f" conf={v.get('confidence', 0):<5}{mark}"
                )

    print()
    print("=" * 84)
    print("  汇总")
    print("=" * 84)
    for model in MODELS:
        ts = timings[model]
        avg = sum(ts) / len(ts) if ts else 0
        line = f"  {model:<12} 平均 {avg:>6.0f} ms"
        if model != "qwen-plus" and total:
            rate = agreements[model] / total * 100
            line += f"    与基准一致 {agreements[model]}/{total}（{rate:.0f}%）"
        print(line)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
