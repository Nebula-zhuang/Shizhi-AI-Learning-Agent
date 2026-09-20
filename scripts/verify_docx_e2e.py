"""端到端验证：上传一份真实的 .docx，走完整条解析链路。

不是"能收下"就算过 —— 要验证标题层级、表格、正文都被真正读出来，
并且后台抽取真的产出了知识点。
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "data" / "_e2e_sample.docx"
BASE = "http://127.0.0.1:8000"


def build_sample() -> pathlib.Path:
    """造一份结构完整的 Word：三级标题 / 段落 / 表格。"""
    from docx import Document

    document = Document()
    document.add_heading("操作系统原理 · 进程与线程", level=1)
    document.add_paragraph("本章讨论进程与线程的区别，以及进程的状态转换。")

    document.add_heading("3.1 进程", level=2)
    document.add_paragraph("进程是资源分配的基本单位，拥有独立的地址空间、文件表等资源。")
    document.add_paragraph("进程的创建、撤销与切换开销都比较小。")

    document.add_heading("3.1.1 进程的三种基本状态", level=3)
    document.add_paragraph("下表列出进程的三种基本状态及其含义。")
    table = document.add_table(rows=4, cols=2)
    table.cell(0, 0).text = "状态"
    table.cell(0, 1).text = "说明"
    table.cell(1, 0).text = "就绪"
    table.cell(1, 1).text = "已获得除处理机之外的所有资源"
    table.cell(2, 0).text = "运行"
    table.cell(2, 1).text = "正占用处理机执行"
    table.cell(3, 0).text = "阻塞"
    table.cell(3, 1).text = "等待某个事件完成而暂停"

    document.add_heading("3.2 线程", level=2)
    document.add_paragraph(
        "线程是处理机调度的基本单位，同一进程内的多个线程共享该进程的地址空间。"
    )
    document.add_paragraph("线程的切换开销比进程小，因为不需要切换地址空间。")

    document.add_heading("3.3 两者的区别", level=2)
    document.add_paragraph(
        "进程是资源分配的基本单位，线程是处理机调度的基本单位，这是最核心的区别。"
    )

    document.save(str(SAMPLE))
    return SAMPLE


def post_file(path: pathlib.Path, field: str = "file") -> tuple[int, dict]:
    boundary = "----e2e" + str(int(time.time()))
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{path.name}"\r\n'
        f"Content-Type: application/vnd.openxmlformats-officedocument."
        f"wordprocessingml.document\r\n\r\n"
    ).encode()
    body = head + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()

    request = urllib.request.Request(
        f"{BASE}/api/documents",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def get(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=60) as response:
        return json.loads(response.read())


def main() -> int:
    sample = build_sample()
    print(f"样本：{sample.name}  {sample.stat().st_size / 1024:.1f}KB")

    print("\n① 上传 .docx")
    status, body = post_file(sample)
    print(f"   HTTP {status}")
    if status != 202:
        print("   ✘ 上传被拒：", body)
        return 1
    doc_id = body["document"]["id"]
    print(f"   文档 id={doc_id}  已接受解析")

    print("\n② 等待解析（最多 180s）")
    detail = {}
    for _ in range(60):
        time.sleep(3)
        detail = get(f"/api/documents/{doc_id}")
        state = detail.get("parse_status")
        print(f"   [{detail.get('progress'):>3}%] {state}  {detail.get('stage_detail') or ''}")
        if state in {"ready", "failed"}:
            break

    if detail.get("parse_status") != "ready":
        print(f"   ✘ 解析未成功：{detail.get('parse_error')}")
        return 1

    print("\n③ 校验解析结果")
    points = get(f"/api/documents/{doc_id}/knowledge-points?limit=100")
    print(f"   知识点数：{points['total']}")
    for item in points["items"][:6]:
        print(f"     · {item['title']}")

    structure = get(f"/api/documents/{doc_id}/structure")
    pages = structure.get("pages") or []
    kinds: dict[str, int] = {}
    for page in pages:
        for block in page.get("blocks") or []:
            kinds[block["type"]] = kinds.get(block["type"], 0) + 1
    print(f"   块类型统计：{kinds}")

    ok = True
    if points["total"] <= 0:
        print("   ✘ 没有抽出任何知识点")
        ok = False
    if kinds.get("heading", 0) < 5:
        print(f"   ✘ 标题块偏少（{kinds.get('heading', 0)}），层级可能没读到")
        ok = False
    if kinds.get("table", 0) < 1:
        print("   ✘ 表格没被识别")
        ok = False
    if kinds.get("text", 0) < 5:
        print(f"   ✘ 正文段落偏少（{kinds.get('text', 0)}）")
        ok = False

    print("\n④ 清理")
    request = urllib.request.Request(f"{BASE}/api/documents/{doc_id}", method="DELETE")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            print(f"   删除 → HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        print(f"   删除 → HTTP {exc.code}")
        ok = False

    sample.unlink(missing_ok=True)
    print("\n" + ("=" * 60))
    print("  结论：" + ("docx 全链路通过 ✔" if ok else "docx 链路有问题 ✘"))
    print("=" * 60)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
