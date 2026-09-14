"""终端输出。

用 rich 渲染 Markdown 而不是自己拼 ANSI 颜色：rich 的 Markdown 渲染器
能正确处理标题层级、表格、代码块和引用块，输出的排版在终端里相当接近
网页效果。自己实现这些要几百行，而且边界情况（超长表格换行、宽字符对齐）
会一路踩坑。

降级策略：rich 输出失败（比如被重定向到文件、终端不支持）时回退到
纯文本 print。审查结果永远不能被"显示层的问题"吞掉。
"""

from __future__ import annotations

from codesentry.output.base import Publisher, PublishContext
from codesentry.utils.logger import get_logger


class ConsolePublisher(Publisher):
    """把报告渲染到终端。"""

    def __init__(self, use_color: bool = True) -> None:
        self.use_color = use_color

    def publish(self, context: PublishContext) -> None:
        markdown = context.markdown or ""
        if not markdown:
            get_logger().warning("报告内容为空，终端未输出任何内容")
            return

        if not self.use_color:
            print(markdown)
            return

        try:
            from rich.console import Console
            from rich.markdown import Markdown
            from rich.padding import Padding

            console = Console()
            # 左右各留 2 格边距：贴边显示在终端里阅读体验很差
            console.print(Padding(Markdown(markdown), (0, 2)))
        except Exception as exc:      # noqa: BLE001 - 显示层任何问题都不该影响主流程
            get_logger().debug(f"rich 渲染失败，回退为纯文本输出：{exc}")
            print(markdown)
