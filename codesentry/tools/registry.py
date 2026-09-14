"""命令注册表。

形式与 pr-agent 的 `command2class` 完全一致：一个字典，命令名 -> 工具类。
多个命令名可以指向同一个类（别名），这让"改名"和"加别名"变得零成本 ——
`/review` 和 `/review_pr` 是同一件事，没必要写两份。

为什么用显式字典而不是装饰器自动注册？
—— 显式列表让人一眼就能看到"这个程序支持哪些命令"。自动注册虽然看起来
更优雅，但会导致"改了个文件名，命令就消失了"这类难以排查的问题。
在只有十几个命令的规模下，显式 > 隐式。
"""

from __future__ import annotations

from codesentry.tools.base import BaseTool, ToolResult
from codesentry.tools.review import ReviewTool

#: 命令名 -> 工具类。新增命令只需在这里加一行。
command2class: dict[str, type[BaseTool]] = {
    "review": ReviewTool,
    "review_pr": ReviewTool,          # 别名：习惯 pr-agent 写法的用户可以直接用
    "auto_review": ReviewTool,        # 别名：给未来"无参数自动审查"留的入口
}

#: 供 CLI / 帮助信息使用
commands: list[str] = sorted(command2class.keys())

__all__ = ["BaseTool", "ToolResult", "ReviewTool", "command2class", "commands"]
