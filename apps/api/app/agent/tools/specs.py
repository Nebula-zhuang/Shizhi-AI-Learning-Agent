"""工具的**声明**层：名字、Schema、可用性、执行函数。

## 与 `registry.py` 的分工

    registry.py  「怎么调」—— 预算、超时、异常包装（ToolRunner / ToolBudget）
    specs.py     「有哪些、叫什么、什么时候能用」—— 声明与查表（ToolRegistry）

分开是因为它们变化的理由不同：预算规则很少动，
而工具集合每个阶段都在加。混在一起会让"加一个工具"变成"改一个 300 行的文件"。

## 为什么需要"按名字查表"

原来的 `ToolRunner.call(name, func, **kwargs)` 要求**调用方把函数传进来** ——
那是"我知道要调哪个函数"。

而 Agent 恰恰相反：**模型说"我要用 web_search"，代码去查表执行**。
名字到实现的映射必须有一张表，否则模型说什么都得写一个 `if name == "...":`。

## ⚠️ 工具名不许硬编码在提示词之外的任何地方

这不是洁癖。实测踩过：Tavily MCP 的官方 README 写的是 `tavily-search`（连字符），
而 `tools/list` 实际返回的是 `tavily_search`（下划线）。
**照文档硬编码，功能会静默不工作**，而且报错是一句难懂的 "tool not found"。

所以对外部服务的工具名，一律**运行时从对方的能力清单里读**，
再用读到的名字去调 —— 见 `app/search/mcp_client.py`。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ToolResult:
    """一次工具执行的结果。

    ## `content` 是**文本**而不是结构化 JSON，这是刻意的

    模型读结构化 JSON 时容易把字段名当内容讲出来 ——
    实测拿到 `{"url": "...", "title": "..."}` 之后，它会写出
    "根据 url 字段和 title 字段…" 这种句子。

    给一段排版好的文本，它讲出来的东西才像人话。
    结构化的部分放 `display`，那是**给界面看的**，不给模型看。
    """

    ok: bool
    #: 给模型看的文本。失败时也要写清"为什么失败" ——
    #: 模型据此可以换个策略重试，比一句"调用失败"有用得多。
    content: str = ""
    #: 给界面看的结构化摘要（右侧面板）
    display: dict[str, Any] = field(default_factory=dict)
    #: 引用来源（网页链接 / 文档出处）
    citations: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    #: 是否值得重试。超时/限流可重试；参数错误不该重试（重试也是错）。
    retryable: bool = False
    elapsed_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "content": self.content[:2000],
            "display": self.display,
            "citations": self.citations,
            "error": self.error,
            "retryable": self.retryable,
            "elapsed_ms": self.elapsed_ms,
        }


#: 可用性检查：返回 (是否可用, 不可用原因)
AvailabilityCheck = Callable[[], tuple[bool, str]]


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的声明。"""

    name: str
    #: **给模型看的**说明。"什么时候该用它"比"它是什么"更重要 ——
    #: 模型不缺理解力，缺的是"此刻该不该调用"的判断依据。
    description: str
    #: JSON Schema。模型据此填 arguments。
    parameters: dict[str, Any]
    handler: Callable[..., Awaitable[ToolResult]]
    #: 是否计入 ToolBudget。调外部服务的计入；纯本地的不计。
    counted: bool = True
    #: 覆盖默认单次超时
    timeout: float | None = None
    #: 运行时可用性。不可用的工具**不进**给模型的清单 ——
    #: 让模型看见一个调不通的工具，只会浪费它一次决策。
    available: AvailabilityCheck | None = None

    def availability(self) -> tuple[bool, str]:
        if self.available is None:
            return True, ""
        try:
            return self.available()
        except Exception as exc:  # noqa: BLE001
            logger.warning("工具 %s 的可用性检查抛异常：%s", self.name, exc)
            return False, f"可用性检查失败：{exc}"

    def as_llm_schema(self) -> dict[str, Any]:
        """给模型的工具描述（OpenAI tools 风格的子集，但我们走 JSON 通道）。"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


class ToolRegistry:
    """名字 → ToolSpec 的表。

    ## 为什么 `names()` 与实际可用的工具要分开

    注册表里可以有若干工具，但"此刻能给模型看哪些"取决于运行时
    （联网没配 Key 时 `web_search` 就不该出现）。
    分开之后，提示词里列出的工具**永远是当下真的能用的**，
    模型不会把一次决策浪费在一个必然失败的工具上。
    """

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._specs:
            raise ValueError(f"工具名重复注册：{spec.name}")
        self._specs[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec | None:
        """按名字取。**大小写不敏感，且容忍连字符/下划线混用。**

        容忍这两种写法是因为外部服务的工具名格式不统一
        （Tavily MCP 实际是下划线，而不少文档写连字符）。
        在查表这一层做归一化，比让每个调用点都记得转换安全。
        """
        if name in self._specs:
            return self._specs[name]
        normalized = name.strip().lower().replace("-", "_")
        for key, spec in self._specs.items():
            if key.lower().replace("-", "_") == normalized:
                return spec
        return None

    def all(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def available_specs(self) -> list[ToolSpec]:
        """当下真的能用的工具。**给模型看的清单只从这里出。**"""
        return [spec for spec in self._specs.values() if spec.availability()[0]]

    def describe_for_llm(self) -> list[dict[str, Any]]:
        return [spec.as_llm_schema() for spec in self.available_specs()]

    def names(self) -> list[str]:
        return sorted(self._specs)

    def available_names(self) -> list[str]:
        return sorted(spec.name for spec in self.available_specs())

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.get(name) is not None

    def __len__(self) -> int:
        return len(self._specs)


# --------------------------------------------------------------------------- #
# 全局注册表
# --------------------------------------------------------------------------- #
#: 进程内唯一的工具表。
#:
#: 用模块级单例而不是依赖注入：工具是**无状态的声明**，
#: 没有"每个请求一份"的必要；而多份会让"某个接口看到的工具集不一样"
#: 变成一类难查的 bug。
registry = ToolRegistry()


def register_tool(spec: ToolSpec) -> ToolSpec:
    return registry.register(spec)
