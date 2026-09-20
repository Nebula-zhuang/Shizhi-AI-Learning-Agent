"""生成大文件测试样本（一次性脚本）。

为什么写成文件而不是一行 `python -c`：内容里有中文与转义换行，
在 bash 里嵌套引号会被吃掉 —— 这个坑在本项目已经踩过多次。
"""

from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]

# 35MB 纯文本：证明"超过旧上限 30MB 的文件现在能收"
LINE = "操作系统中，进程是资源分配的基本单位，线程是处理机调度的基本单位。\n"
TARGET = 35 * 1024 * 1024

mid = ROOT / "data" / "_midtest.md"
with mid.open("w", encoding="utf-8") as handle:
    written = 0
    while written < TARGET:
        handle.write(LINE)
        written += len(LINE.encode("utf-8"))

print(f"已生成 {mid.name}：{mid.stat().st_size / 1024 / 1024:.1f}MB（旧上限 30MB）")

# 320MB 超限样本：稀疏写，瞬间完成
big = ROOT / "data" / "_bigtest.bin"
with big.open("wb") as handle:
    handle.seek(320 * 1024 * 1024 - 1)
    handle.write(b"\0")

print(f"已生成 {big.name}：{big.stat().st_size / 1024 / 1024:.0f}MB（超限）")
