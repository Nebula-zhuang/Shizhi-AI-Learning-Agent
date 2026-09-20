"""浏览器验收：阶段 2（一次性脚本）。"""

from __future__ import annotations

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


def main() -> int:
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

    print()
    print("=== 场景 A：普通问题 ===")
    js(
        "(()=>{const t=document.querySelector('textarea');"
        "const set=Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype,'value').set;"
        "set.call(t,'什么是 JVM？');t.dispatchEvent(new Event('input',{bubbles:true}));return 1;})()"
    )
    wait(1)
    js("[...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='发送')?.click()")
    wait(22)
    print("回答片段:", js(
        "(()=>{const t=document.body.innerText.replace(/\\n+/g,' | ');"
        "const i=t.lastIndexOf('JVM'); return t.slice(Math.max(0,i-40), i+120);})()"
    )[:220])

    print()
    print("=== 场景 B：资料不足再联网（关键）===")
    js(
        "(()=>{const t=document.querySelector('textarea');"
        "const set=Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype,'value').set;"
        "set.call(t,'根据我的资料讲讲虚拟线程，资料里没有的再联网查');"
        "t.dispatchEvent(new Event('input',{bubbles:true}));return 1;})()"
    )
    wait(1)
    js("[...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='发送')?.click()")

    # 生成中途看一次状态行，证明流式阶段在推
    wait(6)
    print("中途状态:", js(
        "(()=>{const t=document.body.innerText;"
        "const m=t.match(/正在[^\\n|]{2,16}/g); return m?m.join(' / '):'（本刻无状态行）';})()"
    ))
    wait(32)

    js("[...document.querySelectorAll('button')].find(b=>b.textContent.includes('参考')&&!b.textContent.includes('收起'))?.click()")
    wait(3)

    print()
    print("右侧面板（它做了什么）:", js(
        "(()=>{const t=document.body.innerText;"
        "const i=t.indexOf('它做了什么'); if(i<0) return '（未展开）';"
        "return t.slice(i, i+150).replace(/\\n+/g,' | ');})()"
    )[:240])

    print()
    print("=== 通道标识（要求的「UI 显示经由 MCP」）===")
    print("页面上是否出现「经由 MCP」:", js(
        "(()=>document.body.innerText.includes('经由 MCP') ? '有 ✔' : '没有 ✘（可能这一轮没联网）')()"
    ))
    print("是否出现「备用通道」:", js(
        "(()=>document.body.innerText.includes('备用通道') ? '有（说明发生了回退）' : '没有（未回退）')()"
    ))

    print()
    print("引用数:", js(
        "(()=>{const a=[...document.querySelectorAll('a[target=_blank]')]; return String(a.length);})()"
    ))
    print("页面是否暴露内部术语:", js(
        "(()=>{const t=document.body.innerText;"
        "const bad=['retrieve_knowledge','image_analysis','web_search','ToolRunner','RAG','embedding']"
        ".filter(w=>t.includes(w)); return bad.length?('有 ✘ '+bad.join('/')):'无 ✔';})()"
    ))

    ab(["screenshot", str(SHOTS / "stage2-agent.png")])
    print()
    print("控制台:", ab(["console"])[:300] or "（无输出）")
    print("截图:", SHOTS / "stage2-agent.png")
    return 0


if __name__ == "__main__":
    import os

    os.chdir(ROOT)
    raise SystemExit(main())
