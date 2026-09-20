"""造一批真实的学习记录，让学习看板有内容可看。

跑法：python scripts/seed_demo_state.py            # 默认学习者
      python scripts/seed_demo_state.py --actor demo

做的事：在样例资料的主知识点上故意答错几次，制造出「待复习」与「薄弱点」，
再把其中一条的复习时间拨到过去，让它进入"该复习了"。

**不新增业务逻辑** —— 全部走产品自己的接口，和真人操作产生的数据一模一样。
演示前跑一次，看板就是"有历史"的状态，而不是空看板。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "api"))

parser = argparse.ArgumentParser(description="造演示用的学习记录")
parser.add_argument("--base", default="http://127.0.0.1:8000", help="后端地址")
parser.add_argument("--learner", default="local", help="学习者标识")
args = parser.parse_args()

BASE = args.base.rstrip("/")


def post(path: str, payload: dict):
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return json.loads(urllib.request.urlopen(request, timeout=180).read())


def put(path: str, payload: dict):
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    return json.loads(urllib.request.urlopen(request, timeout=60).read())


def get(path: str):
    return json.loads(urllib.request.urlopen(BASE + path, timeout=60).read())


#: 答得含糊、明显没理解 —— 用来制造薄弱
VAGUE = "我记得好像是跟资源分配有关吧，具体说不清楚，可能和进程不太一样？"

#: 答得完整 —— 用来制造"学会了"的对照
GOOD = (
    "进程是系统进行资源分配的基本单位，它由程序段、数据段和进程控制块组成；"
    "线程是进程内部的执行单元，是处理机调度的基本单位。"
    "两者的核心区别在于：进程负责资源分配，线程负责处理机调度；"
    "同一进程内的线程共享地址空间和打开的文件，所以线程切换不需要换地址空间，开销比进程切换小。"
)


def main() -> int:
    print("=" * 74)
    print("  造演示用的学习记录")
    print("=" * 74)
    print(f"  后端：{BASE}   学习者：{args.learner}")
    print()

    documents = get("/api/documents?limit=100&offset=0")["items"]
    textbook = next(
        (d for d in documents if "操作系统原理" in d["file_name"] and d["kp_count"] > 0),
        None,
    )
    if textbook is None:
        print("  找不到样例教材。先跑：python scripts/load_samples.py")
        return 2

    points = get(f"/api/documents/{textbook['id']}/knowledge-points?limit=100")["items"]
    print(f"  资料：《{textbook['file_name']}》{len(points)} 个知识点")

    # 找到适合演示的三个知识点：一个用来答错、一个用来答对
    weak_target = next((p for p in points if "进程" in p["title"]), points[0])
    good_target = next(
        (p for p in points if p["id"] != weak_target["id"] and "线程" in p["title"]),
        points[1] if len(points) > 1 else points[0],
    )
    print(f"  制造薄弱：{weak_target['title']}")
    print(f"  制造掌握：{good_target['title']}")
    print()

    # ------------------------------------------------ 薄弱：连错三次
    turn = post("/api/tutor/start", {"knowledge_point_id": weak_target["id"]})
    session_id = turn["session_id"]
    print(f"  会话 #{session_id}")
    for index in range(3):
        turn = post(
            "/api/tutor/answer", {"session_id": session_id, "answer": VAGUE}
        )
        assessment = turn["assessment"]
        print(
            f"    第 {index + 1} 次：{'对' if assessment['correct'] else '错'} "
            f"得分 {assessment['score']:.2f}  "
            f"掌握度 {turn['state_before']['mastery']:.3f} → {turn['state_after']['mastery']:.3f}  "
            f"动作 {turn['action']}"
        )

    # ------------------------------------------------ 对照：答对两次
    turn2 = post("/api/tutor/start", {"knowledge_point_id": good_target["id"]})
    print(f"  会话 #{turn2['session_id']}")
    for index in range(2):
        turn2 = post(
            "/api/tutor/answer", {"session_id": turn2["session_id"], "answer": GOOD}
        )
        assessment = turn2["assessment"]
        print(
            f"    第 {index + 1} 次：{'对' if assessment['correct'] else '错'} "
            f"得分 {assessment['score']:.2f}  "
            f"掌握度 {turn2['state_before']['mastery']:.3f} → {turn2['state_after']['mastery']:.3f}  "
            f"动作 {turn2['action']}"
        )

    # ------------------------------------ 把薄弱那条拨到过去，进入"待复习"
    from datetime import datetime, timedelta

    import app.db.base  # noqa: F401
    from app.db.session import SessionLocal
    from app.models.learner_kp_state import LearnerKpState

    with SessionLocal() as db:
        state = (
            db.query(LearnerKpState)
            .filter(
                LearnerKpState.learner_id == args.learner,
                LearnerKpState.knowledge_point_id == weak_target["id"],
            )
            .first()
        )
        if state is not None:
            state.next_review_at = datetime.now() - timedelta(hours=2)
            db.commit()
            print(f"\n  已把「{weak_target['title']}」的复习时间拨到 2 小时前（进入待复习）")

    board = get("/api/tutor/dashboard")
    print()
    print("  学习看板现状：")
    print(f"    概览：{board['overview']}")
    print(f"    待复习：{[i['title'] for i in board['due_reviews']]}")
    print(f"    薄弱点：{[i['title'] for i in board['weak_points']]}")
    print(f"    讲法：{board['profile']['style_label']}（{board['profile']['style_source']}）")
    print()
    print("  打开前端「学习」页即可看到这套状态。")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
