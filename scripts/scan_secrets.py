"""凭据泄漏扫描。

项目约定：**每次涉及凭据的改动后必须复跑一次**。
凭据只允许存在于项目根目录的 `.env`（已 gitignore），
不得出现在源码、文档、报告、脚本、日志或测试文件里。

用法：
    python scripts/scan_secrets.py          # 扫描，命中即非零退出
    python scripts/scan_secrets.py -v       # 额外打印被扫描的文件数

它只输出**命中文件的路径与行号**，绝不回显凭据内容 ——
扫描工具的日志本身也是一种泄漏渠道。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT_ROOT / ".env"

parser = argparse.ArgumentParser(description="凭据泄漏扫描")
parser.add_argument("-v", "--verbose", action="store_true", help="打印扫描统计")
args = parser.parse_args()

#: 只扫文本类文件。二进制（向量库 .bin、图片、PDF）不可能"泄漏"成可读凭据。
TEXT_SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".md", ".txt", ".yml", ".yaml",
    ".toml", ".ini", ".cfg", ".sh", ".sql", ".html", ".css", ".env", ".example",
}
#: 跳过的目录：依赖、构建产物、运行时数据、版本库
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
    "data", "dist", "dist-p3", "dist-p4", "dist-p4final", "dist-verify",
    ".venv", "venv", ".learnbuddy",
}
#: 允许出现凭据值的文件 —— 只有它自己
ALLOWED = {ENV_PATH.resolve()}

#: 太短的值（如 "1"）会命中一切，不参与扫描
MIN_SECRET_LEN = 8


def read_secrets() -> list[tuple[str, str]]:
    """从 .env 读出真实凭据。返回 [(变量名, 值)]。"""
    if not ENV_PATH.exists():
        return []
    secrets: list[tuple[str, str]] = []
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        # 只关心看起来像密钥的项：名字里有 KEY / TOKEN / SECRET / PASSWORD
        looks_secret = any(k in name.upper() for k in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
        if looks_secret and len(value) >= MIN_SECRET_LEN:
            secrets.append((name, value))
    return secrets


def iter_files() -> list[Path]:
    files: list[Path] = []
    for root, dirs, names in os.walk(PROJECT_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            path = Path(root) / name
            if path.suffix.lower() in TEXT_SUFFIXES or name.startswith(".env"):
                files.append(path)
    return files


def env_is_ignored() -> tuple[bool, str]:
    """确认 .env 确实不会被提交。

    用**退出码**判断，不要看输出内容：`git check-ignore -v` 对否定规则（`!`）
    也会打印，只看输出会误判。

    三种退出码要分开处理：
      - `0`   → 被忽略 ✓
      - `1`   → **没被忽略** ✗
      - `128` → 这里根本不是 git 仓库（或没有 git）

    128 不能当成"没被忽略"—— 实测踩过：项目当时还没 `git init`，
    脚本于是报出"凭据会进版本库"的假警报。安全工具一旦误报，
    人就会开始忽略它，比不报还糟。
    这种情况退化为**静态检查 `.gitignore` 的内容**。
    """
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", ".env"],
            cwd=PROJECT_ROOT,
            capture_output=True,
        )
    except FileNotFoundError:
        return _static_gitignore_check("（本机没有 git，改为静态检查 .gitignore）")

    if result.returncode == 0:
        return True, ".env 已被 git 忽略"
    if result.returncode == 1:
        return False, "★ .env 未被 git 忽略！凭据会进版本库"
    return _static_gitignore_check("（当前不是 git 仓库，改为静态检查 .gitignore）")


def _static_gitignore_check(prefix: str) -> tuple[bool, str]:
    """没有 git 可用时，退化为读 `.gitignore` 文本。

    只做基本判断：存在一条覆盖 `.env` 的规则，且没有被后面的 `!` 规则否定。
    """
    path = PROJECT_ROOT / ".gitignore"
    if not path.exists():
        return False, f"{prefix} ★ 没有 .gitignore 文件，.env 会被提交"
    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    covered = False
    for line in lines:
        if line.startswith("!"):
            if line[1:].strip() in {".env", ".env*", "*.env"}:
                covered = False
            continue
        if line in {".env", ".env*", "*.env"} or line.rstrip("/") == ".env":
            covered = True
    if covered:
        return True, f"{prefix} .gitignore 中有覆盖 .env 的规则"
    return False, f"{prefix} ★ .gitignore 中没有覆盖 .env 的规则"


def main() -> int:
    secrets = read_secrets()
    print("=" * 74)
    print("  凭据泄漏扫描")
    print("=" * 74)
    print(f"  从 .env 读到 {len(secrets)} 个凭据项：{', '.join(n for n, _ in secrets) or '（无）'}")
    print(f"  每个凭据只检查是否出现在文本文件中；**本工具不打印凭据内容**")
    print()

    ignored_ok, ignore_note = env_is_ignored()
    print(f"  {'✔' if ignored_ok else '✘'} {ignore_note}")

    files = iter_files()
    if args.verbose:
        print(f"  扫描 {len(files)} 个文本文件…")

    hits: list[tuple[Path, str, int]] = []
    for path in files:
        if path.resolve() in ALLOWED:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for name, value in secrets:
            if value and value in text:
                line_no = text[: text.index(value)].count("\n") + 1
                hits.append((path, name, line_no))

    print()
    if hits:
        print(f"  ✘ 发现 {len(hits)} 处泄漏：")
        seen: set[Path] = set()
        for path, name, line_no in hits:
            rel = path.relative_to(PROJECT_ROOT)
            if path not in seen:
                seen.add(path)
            print(f"      {rel}:{line_no}  （{name}）")
        print()
        print("  处置：把凭据从这些文件里删掉，只留在 .env。")
        print("        注意 —— 已经提交过的凭据要视为已泄漏，应当轮换。")
    else:
        print("  ✔ 未在任何文本文件中发现凭据")

    print("=" * 74)
    return 1 if hits or not ignored_ok else 0


if __name__ == "__main__":
    raise SystemExit(main())
