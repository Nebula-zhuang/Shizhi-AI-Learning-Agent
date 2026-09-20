"""播种演示账号。

## 为什么需要它

P0–P5 攒下来的样例学习数据（作答状态、讲法偏好、会话记录）全部挂在
`learner_id = "local"` 这个标识上。引入账号体系之后，**没有账号指向它**，
于是登录进去会看到一片空白 —— 演示效果全没了。

这个脚本建一个演示账号，把它的 `learner_id` **显式指定为 `local`**，
于是登录演示账号就能看到之前积累的全部学习记录。

## 口令从哪来

从 `.env` 读 `DEMO_USERNAME` / `DEMO_PASSWORD`，**不写死在代码里**。
没配就报错退出，不猜默认值 —— 默认口令是账号体系里最典型的失守点。

跑法：python scripts/seed_demo_user.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "api"))

# 让 config 能在没有显式环境变量时也读到项目根 .env（脚本直接跑时不会被 uvicorn 加载）
os.chdir(PROJECT_ROOT)

import app.db.base  # noqa: F401,E402  —— 注册全部模型
from app.core.config import settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services import auth_service, learner_service  # noqa: E402


def main() -> int:
    username = (os.environ.get("DEMO_USERNAME") or getattr(settings, "demo_username", "") or "").strip()
    password = (os.environ.get("DEMO_PASSWORD") or getattr(settings, "demo_password", "") or "").strip()

    if not username or not password:
        print("✘ 未配置演示账号。请在项目根 .env 中设置：")
        print("    DEMO_USERNAME=demo")
        print("    DEMO_PASSWORD=<你自己的口令>")
        return 2

    with SessionLocal() as db:
        existing = auth_service.get_user_by_username(db, username)
        if existing is not None:
            print(f"· 演示账号 {existing.username} 已存在（learner_id={existing.learner_id}）")
            if existing.learner_id != learner_service.DEFAULT_LEARNER_ID:
                print(
                    f"  ⚠ 注意：它的 learner_id 不是 {learner_service.DEFAULT_LEARNER_ID}，"
                    "看不到 P0–P5 的样例学习数据。"
                )
            return 0

        user = auth_service.create_user(
            db,
            username=username,
            password=password,
            display_name="演示同学",
            # 关键：绑到既有的本地档案上，样例数据因此可见
            learner_id=learner_service.DEFAULT_LEARNER_ID,
        )
        print(f"✔ 已创建演示账号：{user.username}（learner_id={user.learner_id}）")
        print("  登录名与口令见项目根 .env 的 DEMO_USERNAME / DEMO_PASSWORD。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
