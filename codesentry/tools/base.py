"""工具层基类。

对齐 pr-agent 的 Tool 设计：每个"命令"是一个类，对外只暴露 `run()`。
构造函数负责把所有依赖（配置、模型 handler、diff 来源、输出目标）注入进来，
run() 负责流程编排。这样每个工具都是可单测的：注入 FakeLLMHandler 和
一个内存里的 DiffProvider 就能跑完整流程。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from codesentry.config import Settings
from codesentry.llm.base import BaseLLMHandler
from codesentry.models import ReviewResult


@dataclass
class ToolResult:
    """工具的产出。

    同时携带 markdown / json / 结构化对象三种形态，由调用方决定用哪种、
    用几个。这样 CLI 只负责"决定去哪"，不需要重新组织数据。
    """

    markdown: str = ""
    json_text: str = ""
    result: ReviewResult | None = None
    # 是否需要用户注意的非致命问题（渲染到终端末尾）
    notices: list[str] = field(default_factory=list)
    # 有内容需要展示吗（完全没有改动时为 False，CLI 直接友好退出）
    has_content: bool = False


class BaseTool(ABC):
    """所有审查工具的基类。"""

    #: 命令名，与 registry 里的键对应
    name: str = "base"

    def __init__(self, settings: Settings, handler: BaseLLMHandler) -> None:
        self.settings = settings
        self.handler = handler

    @abstractmethod
    async def run(self) -> ToolResult:
        """执行工具。"""
        raise NotImplementedError
