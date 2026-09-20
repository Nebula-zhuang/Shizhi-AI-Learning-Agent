"""浏览器验收：阶段 2.1 / 2.2 / 2.3（一次性脚本）。

覆盖三个子阶段各自最关键的那条：
  2.1  "今天几号" 必须得到**真实日期**（不能靠模型记忆）
  2.2  从前端**真的能选图上传**，附件卡片出现
  2.3  带图提问得到**含图内标识符**的回答
"""

from __future__ import annotations

import base64
import json
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(r"C:\Users\Lenovo\Desktop\Ai coding")
NODE = r"C:/Users/Lenovo/.workbuddy/binaries/node/versions/22.22.2/node.exe"
AB = r"node_modules/agent-browser/bin/agent-browser.js"
WS = pathlib.Path(r"C:/Users/Lenovo/.workbuddy/binaries/node/workspace")
SHOTS = ROOT / "docs" / "screenshots"

sys.path.insert(0, str(ROOT / "apps" / "api"))


def ab(args: list[str]) -> str:
    proc = subprocess.run(
        [NODE, AB, *args], cwd=str(WS), capture_output=True, text=True, errors="ignore"
    )
    return (proc.stdout or "").strip()


def js(code: str) -> str:
    return ab(["eval", code])


def wait(seconds: float) -> None:
    time.sleep(seconds)


def ask(text: str, settle: float = 24) -> None:
    js(
        "(()=>{const t=document.querySelector('textarea');"
        "const set=Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype,'value').set;"
        f"set.call(t,{json.dumps(text)});t.dispatchEvent(new Event('input',{{bubbles:true}}));return 1;}})()"
    )
    wait(1)
    js("[...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='发送')?.click()")
    wait(settle)


def main() -> int:
    from datetime import datetime

    from app.core.config import settings  # noqa: PLC0415

    ab(["console", "--clear"])
    js("location.reload()")
    wait(18)

    password = settings.demo_password
    print("登录:", js(
        "(()=>{const u=document.querySelector('input[name=username]');"
        "if(!u) return '已登录';"
        "const set=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
        "set.call(u,'demo');u.dispatchEvent(new Event('input',{bubbles:true}));"
        "const p=document.querySelector('input[name=password]');"
        f"set.call(p,'{password}');p.dispatchEvent(new Event('input',{{bubbles:true}}));"
        "[...document.querySelectorAll('button')].find(x=>x.type==='submit').click();"
        "return '登录中';})()"
    ))
    wait(12)

    js("[...document.querySelectorAll('button')].find(b=>b.textContent.includes('自由学习'))?.click()")
    wait(6)

    # ─────────────────────────────── 2.1
    print()
    print("══ 2.1 实时能力触发：今天几号")
    today = datetime.now()
    ask("今天几号？", settle=22)
    text = js("(()=>document.body.innerText.replace(/\\n+/g,' | '))()")
    wants = [str(today.year), f"{today.month}月", f"{today.day}日"]
    hits = [w for w in wants if w in text]
    print(f"   期望包含：{wants}")
    print(f"   实际命中：{hits}  {'✔' if len(hits) >= 2 else '✘'}")
    print(f"   是否误答其他年份：{'是 ✘' if ('2024年' in text or '2025年' in text) else '否 ✔'}")

    # ─────────────────────────────── 2.2
    print()
    print("══ 2.2 图片上传：前端能选图")
    print("   ＋ 按钮存在：", js(
        "(()=>{const b=[...document.querySelectorAll('button')].find(x=>x.title==='添加图片或文件');"
        "return b?'是 ✔':'没有 ✘';})()"
    ))
    print("   文件选择器存在：", js(
        "(()=>{const i=document.querySelector('input[type=file]'); return i?('是 ✔ accept='+i.accept.slice(0,30)):'没有 ✘';})()"
    ))

    # 造一张图并通过 input.files 塞进去（模拟用户选文件）
    image_path = ROOT / "data" / "_browser_upload.png"
    image_path.parent.mkdir(exist_ok=True)
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (560, 180), "white")
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(["def fib(n):", "    return fib(n-1)+fib(n-2)", "print(fib(10))  # 55"]):
        draw.text((18, 20 + i * 44), line, fill="black")
    img.save(image_path)
    uri = "data:image/png;base64," + base64.b64encode(image_path.read_bytes()).decode()

    print("   塞入文件：", js(
        "(()=>{const i=document.querySelector('input[type=file]');"
        "const b=atob('" + uri.split(",", 1)[1] + "');"
        "const arr=new Uint8Array(b.length);"
        "for(let k=0;k<b.length;k++)arr[k]=b.charCodeAt(k);"
        "const f=new File([arr],'code.png',{type:'image/png'});"
        "const dt=new DataTransfer();dt.items.add(f);i.files=dt.files;"
        "i.dispatchEvent(new Event('change',{bubbles:true}));"
        "return '已触发 change';})()"
    ))
    wait(9)
    print("   附件卡片出现：", js(
        "(()=>{const t=document.body.innerText; return t.includes('code.png')?'是 ✔':'没有 ✘';})()"
    ))

    # ─────────────────────────────── 2.3
    print()
    print("══ 2.3 带图提问：回答要含图内标识符")
    ask("这张图里的代码在做什么？输出是什么？", settle=32)
    text = js("(()=>document.body.innerText.replace(/\\n+/g,' | '))()")
    for kw, label in [("fib", "函数名 fib"), ("递归", "递归"), ("55", "输出 55")]:
        print(f"   {label:<12} {'有 ✔' if kw.lower() in text.lower() else '没有 ✘'}")

    print()
    print("   右侧面板：", js(
        "(()=>{const t=document.body.innerText; const i=t.indexOf('它做了什么');"
        "if(i<0) return '（未展开）'; return t.slice(i,i+120).replace(/\\n+/g,' | ');})()"
    )[:200])

    print()
    print("   暴露内部术语：", js(
        "(()=>{const t=document.body.innerText;"
        "const bad=['retrieve_knowledge','image_analysis','web_search','current_time','ToolRunner','RAG']"
        ".filter(w=>t.includes(w)); return bad.length?('有 ✘ '+bad.join('/')):'无 ✔';})()"
    ))

    ab(["screenshot", str(SHOTS / "stage2_1_2_3.png")])
    print()
    print("   控制台错误:", js(
        "(()=>{return '见下方';})()"
    ))
    print(ab(["console"])[:260] or "（无输出）")
    print("   截图:", SHOTS / "stage2_1_2_3.png")
    return 0


if __name__ == "__main__":
    import os

    os.chdir(ROOT)
    raise SystemExit(main())
