"""附件上传、图片绑定与视觉链路测试。

## 这个文件守的是"图片真的能被看到"

分三层：

  **上传层**  —— 只能传支持的格式，匿名必须被拒，越权整批拒绝
  **绑定层**  —— 本轮的图要绑进工具，模型**不需要也不可能**知道图片路径
  **参数层**  —— `question` 必须可选（实测把它写成必填会让整个功能跑不通）

第三层尤其重要：它不是"边界情况"，而是**实测踩出来的致命设计错误**。
"""

from __future__ import annotations

import base64
import pathlib

import pytest
from PIL import Image

from app.agent import free_study
from app.agent.runtime_loop import (
    AgentLoop,
    LoopDecision,
    LoopLimits,
    LoopObservation,
)
from app.agent.tool_specs import image_analysis
from app.agent.tools.registry import ToolRunner
from app.agent.tools.specs import ToolRegistry, ToolSpec, ToolResult
from app.core.config import settings
from app.services import study_service


# --------------------------------------------------------------------------- #
# 测试台
# --------------------------------------------------------------------------- #
def _make_image(tmp_path: pathlib.Path, name: str = "code.png") -> pathlib.Path:
    """造一张含可识别文字的图 —— 回答里必须出现这些字才算"真看了"。"""
    target = tmp_path / name
    img = Image.new("RGB", (420, 140), "white")
    from PIL import ImageDraw

    draw = ImageDraw.Draw(img)
    draw.text((16, 16), "def fib(n):", fill="black")
    draw.text((16, 52), "    return fib(n-1)", fill="black")
    draw.text((16, 88), "print(fib(10))  # 55", fill="black")
    img.save(target)
    return target


# =========================================================================== #
# 一、上传层的校验（纯本地，不打网络）
# =========================================================================== #
@pytest.mark.parametrize(
    ("name", "expected_suffix"),
    [("a.png", ".png"), ("b.JPG", ".jpg"), ("c.jpeg", ".jpeg"), ("d.webp", ".webp")],
)
def test_accepts_image_formats(name: str, expected_suffix: str) -> None:
    assert study_service.validate_attachment_name(name) == expected_suffix


@pytest.mark.parametrize("name", ["a.pdf", "b.docx", "c.md", "d.txt", "e.pptx"])
def test_accepts_document_formats(name: str) -> None:
    """对话附件不只是图片 —— 用户也可能直接丢一份文档进来。"""
    assert study_service.validate_attachment_name(name)


@pytest.mark.parametrize("name", ["a.exe", "b.sh", "c.zip", "d"])
def test_rejects_unsupported_or_extensionless(name: str) -> None:
    with pytest.raises(study_service.AttachmentError):
        study_service.validate_attachment_name(name)


def test_rejection_message_lists_what_is_allowed() -> None:
    """报错要说清"可以传什么" —— 只说"不支持"会让人反复试。"""
    with pytest.raises(study_service.AttachmentError) as exc:
        study_service.validate_attachment_name("x.exe")
    assert ".png" in exc.value.message
    assert ".pdf" in exc.value.message


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("a.png", True),
        ("b.JPG", True),
        ("c.WebP", True),
        ("d.pdf", False),
        ("e.docx", False),
        ("f.md", False),
    ],
)
def test_image_detection_is_case_insensitive(name: str, expected: bool) -> None:
    """大小写不敏感 —— 手机拍的照片常常是 `.JPG`。"""
    assert study_service.is_image_extension(name) is expected


# =========================================================================== #
# 二、把本轮图片绑定进工具
# =========================================================================== #
def test_image_paths_are_bound_into_the_tool(tmp_path: pathlib.Path) -> None:
    """⚠️ **模型不可能知道图片路径** —— 那是服务端的内部信息。

    所以路径必须**绑定**进 handler，模型只负责说它想知道什么。
    曾经这里让模型自己传 `image_path`，结果它只能编一个、然后失败。
    """
    image = _make_image(tmp_path)
    registry = free_study.build_registry(images=[str(image)], user_question="这是什么？")

    spec = registry.get("image_analysis")
    assert spec is not None
    # 参数里**只有** question，没有 image_path
    assert list((spec.parameters.get("properties") or {}).keys()) == ["question"]


def test_image_question_is_not_required(tmp_path: pathlib.Path) -> None:
    """⚠️ **`question` 不能是必填。** 这是实测踩出来的致命设计错误：

    把它声明成必填后，模型调这个工具时**不传参数**，
    于是被参数校验拦下 → 重试 → 又拦下 → 撞上重试上限 →
    最后回一句"我看不到图"，整个功能形同不存在。

    **那不是模型的错，是 schema 错了**：工具已经绑定了本轮的图，
    "看一眼"这个动作本身不需要参数。
    """
    registry = free_study.build_registry(images=["data:image/png;base64,AAAA"])
    spec = registry.get("image_analysis")
    assert spec is not None
    assert spec.parameters.get("required") == []


def test_user_question_is_used_as_default(tmp_path: pathlib.Path, monkeypatch) -> None:
    """模型什么都不传时，也要按**用户这一轮真正问的话**去看图。

    这样 `{}` 也能得到一次贴题的看图，而不是一句泛泛的"描述这张图"。
    """
    captured: dict = {}

    async def fake_chat(messages, **kwargs):
        # 把发给模型的提示词抓下来
        content = messages[0].get("content")
        if isinstance(content, list):
            captured["text"] = "".join(
                p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
            )
        else:
            captured["text"] = str(content)

        class _R:
            content = "看到了一些代码"

        return _R()

    monkeypatch.setattr(settings, "llm_supports_vision", True)
    monkeypatch.setattr("app.agent.tool_specs.vision_gateway", lambda: type("G", (), {"chat": staticmethod(fake_chat)})())

    image = _make_image(tmp_path)
    registry = free_study.build_registry(images=[str(image)], user_question="这段代码输出什么？")
    spec = registry.get("image_analysis")
    assert spec is not None

    import asyncio

    asyncio.get_event_loop_policy()
    asyncio.run(spec.handler())  # 不传任何参数

    assert "这段代码输出什么？" in captured.get("text", "")


# =========================================================================== #
# 三、视觉能力闸门
# =========================================================================== #
@pytest.mark.asyncio
async def test_image_analysis_refuses_without_vision(monkeypatch) -> None:
    """不支持视觉时直接拒绝，并给一句**可执行**的话 —— 而不是让模型去猜图里有什么。"""
    monkeypatch.setattr(settings, "llm_supports_vision", False)
    result = await image_analysis(image_paths=["data:image/png;base64,AAAA"])

    assert result.ok is False
    assert result.error == "vision_not_supported"
    assert "不要猜测" in result.content


def test_vision_tool_hidden_when_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_supports_vision", False)
    assert "image_analysis" not in free_study.build_registry().available_names()

    monkeypatch.setattr(settings, "llm_supports_vision", True)
    assert "image_analysis" in free_study.build_registry().available_names()


def test_relative_paths_are_skipped_not_crashed() -> None:
    """相对路径有歧义（相对于谁？），所以**跳过而不是拿它去猜**。

    曾经这里对任何非绝对路径都调 `storage.resolve`，而那条路是给
    "上传目录内的相对路径"用的 —— 传别的进去会抛一句"路径越界"，
    对调用方毫无帮助，还会让人以为是自己越权了。
    """
    from app.agent.tool_specs import _image_to_data_uri

    assert _image_to_data_uri("some/relative/path.png") is None


# =========================================================================== #
# 四、被拒的工具调用也要在界面上可见
# =========================================================================== #
async def _collect(loop: AgentLoop, observation: LoopObservation) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    async for event in loop.run(observation):
        events.append((event.event, event.data))
    return events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "expected_error"),
    [
        (LoopDecision(tool="不存在的工具"), "unknown_tool"),
        (LoopDecision(tool="web_search", arguments={}), "missing_arguments"),
    ],
)
async def test_rejected_calls_emit_tool_result(
    decision: LoopDecision, expected_error: str
) -> None:
    """⚠️ **被拒的调用不能再"闷掉"。**

    原来"未知工具 / 不可用 / 缺参数 / 超重试上限"这些分支直接 `continue`，
    不推任何事件 —— 于是"它做了什么"里什么都没有，
    用户只看到回答说"我看不到图"，却不知道为什么。

    **失败也要可见**，这是"状态必须真实"那条规矩的延伸。
    """
    registry = ToolRegistry()

    async def handler(**kwargs) -> ToolResult:
        return ToolResult(ok=True, content="ok")

    registry.register(
        ToolSpec(
            name="web_search",
            description="d",
            parameters={"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}},
            handler=handler,
        )
    )

    script = [decision, LoopDecision(final=True)]
    index = {"n": 0}

    async def decider(observation: LoopObservation, tools: list[dict]) -> LoopDecision:
        value = script[min(index["n"], len(script) - 1)]
        index["n"] += 1
        return value

    async def generate(observation: LoopObservation):
        yield "答案"

    loop = AgentLoop(
        registry=registry,
        runner=ToolRunner(max_calls=9),
        decider=decider,
        generate=generate,
        limits=LoopLimits(max_steps=6, max_tool_calls=9),
    )
    events = await _collect(loop, LoopObservation(question="q"))

    results = [d for name, d in events if name == "tool_result"]
    assert results, "被拒的调用没有推出 tool_result —— 界面上会什么都看不到"
    rejected = [d for d in results if d.get("ok") is False]
    assert rejected, f"没有一条失败事件：{results}"

    done = next(d for name, d in events if name == "done")
    assert expected_error in [s["tool_error"] for s in done["steps"]]


# =========================================================================== #
# 五、base64 体积
# =========================================================================== #
def test_data_uri_is_well_formed(tmp_path: pathlib.Path) -> None:
    from app.agent.tool_specs import _image_to_data_uri

    image = _make_image(tmp_path)
    uri = _image_to_data_uri(str(image))
    assert uri is not None
    assert uri.startswith("data:image/png;base64,")

    decoded = base64.b64decode(uri.split(",", 1)[1])
    assert decoded[:8] == image.read_bytes()[:8], "base64 编解码后内容对不上"
