"""探针：验证 .env 里的凭据能否调用对话模型。

存在意义：P6 的比赛演示需要真实模型输出 —— mock 模式的知识点标题会带
`[MOCK]` 前缀、教学内容是模板生成的，演示效果没法看。
而 DeepSeek 账户余额不足。embedding 用的是阿里云百炼的 Key，
百炼同一个 OpenAI 兼容端点也提供 Qwen 对话模型，所以先探一下这个 Key 能不能对话。

**不打印任何凭据内容。**
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "api"))

from app.core.config import settings  # noqa: E402

CANDIDATES = [
    ("阿里云百炼 · qwen-plus", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    ("阿里云百炼 · qwen-turbo", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-turbo"),
    ("阿里云百炼 · qwen-max", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-max"),
]


async def probe(label: str, base_url: str, model: str, api_key: str) -> tuple[bool, str]:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=30.0)
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "回复两个字：可以"}],
            max_tokens=16,
        )
        text = (response.choices[0].message.content or "").strip()
        return True, f"✔ 可用  返回：{text[:20]!r}"
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        if "insufficient" in message.lower() or "402" in message:
            message = "余额不足（402）"
        elif "401" in message or "invalid" in message.lower():
            message = "凭据无效或无权调用（401）"
        elif "model" in message.lower() and "not" in message.lower():
            message = f"无此模型权限：{message[:60]}"
        return False, f"✘ 不可用  {message[:110]}"
    finally:
        await client.close()


async def main() -> int:
    print("=" * 74)
    print("  对话模型可用性探针")
    print("=" * 74)

    embedding_key = settings.embedding_api_key
    llm_key = settings.llm_api_key
    print(f"  embedding Key（百炼）：{'已配置' if embedding_key else '缺失'}"
          f"{f'，长度 {len(embedding_key)}' if embedding_key else ''}")
    print(f"  LLM Key（DeepSeek）：{'已配置' if llm_key else '缺失'}"
          f"{f'，长度 {len(llm_key)}' if llm_key else ''}")
    print(f"  当前 LLM 配置：base_url={settings.llm_base_url}  model={settings.llm_model}")
    print()

    if not embedding_key:
        print("  没有可用的百炼 Key，退出。")
        return 2

    ok_models: list[tuple[str, str, str]] = []
    for label, base_url, model in CANDIDATES:
        ok, detail = await probe(label, base_url, model, embedding_key)
        print(f"  {label:<26} {detail}")
        if ok:
            ok_models.append((base_url, model, label))

    print()
    print("=" * 74)
    if ok_models:
        base_url, model, label = ok_models[0]
        print(f"  结论：百炼 Key 可以当对话模型用（{label}）")
        print()
        print("  想用它跑真实演示，把项目根 .env 改成：")
        print(f"      LLM_MODE=auto")
        print(f"      LLM_BASE_URL={base_url}")
        print(f"      LLM_MODEL={model}")
        print(f"      LLM_API_KEY=<与 EMBEDDING_API_KEY 相同的值>")
        print()
        print("  改完重启后端，再跑：")
        print("      python scripts/load_samples.py --reset")
        print("  重新抽取出不带 [MOCK] 前缀的知识点。")
    else:
        print("  结论：这个 Key 没有对话模型权限，演示仍需真实 LLM 凭据。")
    print("=" * 74)
    return 0 if ok_models else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
