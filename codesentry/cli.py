"""CodeSentry 命令行入口。

设计要点：

1. **通用覆盖语法** `--section.key=value`（如 `--review.num_max_findings=3`）。
   高频参数（model / base / output）给显式 flag，其余一律走通用语法。
   好处是配置文件里的任何一个键都能从命令行临时改一次，而 CLI 不需要
   为每个键维护一个 flag —— 这个设计直接抄自 pr-agent 的
   `update_settings_from_args`，实测在调试配置时极其顺手。

2. **退出码有语义**，便于在 CI / 脚本里判断：
       0  成功
       1  运行错误（配置错、模型调用失败等）
       2  没有可审查的改动（不是错误，但与"审出了问题"要能区分）
       3  参数用法错误

3. 所有异常都在最外层兜住并翻译成人话。用户不该看到 Python traceback，
   除非加 `-vv` 主动要调试信息。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import traceback
from pathlib import Path
from typing import Optional, Sequence

from codesentry import __version__
from codesentry.config import describe_settings, load_settings
from codesentry.diff.base import DiffProviderError
from codesentry.diff.from_file import FromFileProvider
from codesentry.diff.git_local import GitLocalProvider
from codesentry.llm import build_handler, model_chain
from codesentry.llm.base import LLMError
from codesentry.output.console import ConsolePublisher
from codesentry.output.file import FilePublisher
from codesentry.prompts.loader import PromptError
from codesentry.tools.registry import commands
from codesentry.tools.review import ReviewTool
from codesentry.utils.logger import get_logger, setup_logger

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NO_CHANGES = 2
EXIT_USAGE = 3

_EPILOG = """\
示例：
  # 审查当前仓库里所有未提交的改动
  codesentry review

  # 只审已经 git add 的内容（适合放进 pre-commit）
  codesentry review --staged

  # 与 main 分支对比（等价于看一个 PR 的 diff）
  codesentry review --base main

  # 审一个现成的 diff 文件，并同时保存 Markdown 与 JSON
  codesentry review --diff-file changes.diff --output review.md --json-output review.json

  # 从管道读取 diff（不需要在 git 仓库里）
  git diff main | codesentry review --stdin

  # 只看将要发生什么：打印提示词与 token 预算，不调用模型、不花钱
  codesentry review --dry-run

  # 临时改一个配置项（配置里任意一个键都能这样覆盖）
  codesentry review --model deepseek/deepseek-reasoner --review.num_max_findings=3

  # 查看最终生效的配置以及每一项的来源
  codesentry config
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codesentry",
        description="CodeSentry —— 本地优先的 AI 代码审查 Agent",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"codesentry {__version__}")

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    # ---------------- review ----------------
    review = subparsers.add_parser(
        "review",
        help="审查一个 diff 并生成结构化报告",
        description="审查本地改动或外部 diff 文件，输出结构化 Review 报告。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    review.add_argument("path", nargs="?", default=".", help="仓库或目录路径（默认当前目录）")

    source = review.add_argument_group("diff 来源（默认：未提交的改动）")
    source.add_argument("--staged", action="store_true", help="只审查已 git add 的改动")
    source.add_argument("--base", metavar="REF", help="与指定分支/引用对比，例如 --base main")
    source.add_argument("--commit-range", metavar="A..B", help="审查指定提交区间，例如 HEAD~1..HEAD")
    source.add_argument("--diff-file", metavar="PATH", help="审查一个现成的 unified diff 文件")
    source.add_argument("--stdin", action="store_true", help="从标准输入读取 diff")

    output = review.add_argument_group("输出")
    output.add_argument("--output", metavar="PATH", help="把 Markdown 报告写入该文件")
    output.add_argument("--json-output", metavar="PATH", help="把结构化结果写成 JSON")
    output.add_argument("--no-color", action="store_true", help="禁用终端彩色输出")

    behavior = review.add_argument_group("行为")
    behavior.add_argument("--model", metavar="MODEL", help="临时指定模型，例如 deepseek/deepseek-chat")
    behavior.add_argument("--extra-instructions", metavar="TEXT", help="追加给模型的额外要求")
    behavior.add_argument("--dry-run", action="store_true", help="只打印将要发送的提示词，不调用模型")
    behavior.add_argument("--config", dest="config_path", metavar="PATH", help="指定配置文件路径")
    behavior.add_argument("-v", "--verbose", action="count", default=0, help="增加日志详细度（可重复）")
    behavior.add_argument("-q", "--quiet", action="store_true", help="静默模式，只输出错误")

    # ---------------- config ----------------
    config_cmd = subparsers.add_parser(
        "config", help="打印最终生效的配置及其来源",
        description="显示合并后的配置，以及每个键的值来自哪里（默认值/配置文件/环境变量/CLI）。",
    )
    config_cmd.add_argument("--config", dest="config_path", metavar="PATH", help="指定配置文件路径")
    config_cmd.add_argument("path", nargs="?", default=".", help="用于查找配置文件的起始目录")

    return parser


# --------------------------------------------------------------------------- #
# 参数处理
# --------------------------------------------------------------------------- #

def split_overrides(unknown: list[str]) -> tuple[list[str], list[str]]:
    """把无法识别的参数分成"合法的配置覆盖"和"真正的用法错误"。

    合法形式：`--section.key=value`
    其余（例如 `--typox`、多写的裸参数）一律视为用法错误 ——
    静默忽略拼错的参数是 CLU 里最危险的错误类型之一：
    用户以为某个设置生效了，实际没有。
    """
    overrides: list[str] = []
    errors: list[str] = []
    for item in unknown:
        if item.startswith("-") and "=" in item and "." in item.split("=", 1)[0]:
            overrides.append(item.lstrip("-"))
        else:
            errors.append(item)
    return overrides, errors


def resolve_verbosity(args: argparse.Namespace) -> int:
    if getattr(args, "quiet", False):
        return 0
    # 默认 1（常规），-v 到 2（调试并打印提示词）
    return 1 + int(getattr(args, "verbose", 0))


def build_provider(args: argparse.Namespace):
    """按参数决定 diff 来源。"""
    if args.stdin and args.diff_file:
        raise DiffProviderError("--stdin 与 --diff-file 不能同时使用")
    if args.diff_file or args.stdin:
        # repo 路径仍然传进去：能从磁盘读到文件全文，就能开启行号校验
        return FromFileProvider(
            diff_file=args.diff_file,
            use_stdin=args.stdin,
            repo_path=args.path,
        )
    if args.staged and (args.base or args.commit_range):
        raise DiffProviderError("--staged 不能与 --base / --commit-range 同时使用")

    mode = "worktree"
    if args.staged:
        mode = "staged"
    elif args.base:
        mode = "base"
    elif args.commit_range:
        mode = "commit_range"
    return GitLocalProvider(
        repo_path=args.path,
        mode=mode,
        base_ref=args.base,
        commit_range=args.commit_range,
    )


# --------------------------------------------------------------------------- #
# 命令实现
# --------------------------------------------------------------------------- #

def cmd_config(args: argparse.Namespace, overrides: list[str]) -> int:
    settings = load_settings(cli_overrides=overrides, config_path=args.config_path,
                             cwd=Path(args.path))
    print(describe_settings(settings))
    return EXIT_OK


def cmd_review(args: argparse.Namespace, overrides: list[str]) -> int:
    # --extra-instructions 是高频参数，映射到配置键后与通用覆盖走同一条路径，
    # 保证优先级规则只有一套（CLI > env > 文件 > 默认）
    if args.extra_instructions:
        overrides.append(f"review.extra_instructions={args.extra_instructions}")
    if args.model:
        overrides.append(f"config.model={args.model}")

    settings = load_settings(cli_overrides=overrides, config_path=args.config_path, cwd=Path.cwd())
    setup_logger(verbosity=settings.config.verbosity, use_color=not args.no_color)
    log = get_logger()

    log.debug(f"配置文件：{settings.config_file or '(未找到，使用默认值)'}")
    log.debug(f"模型链：{' -> '.join(model_chain(settings))}")

    provider = build_provider(args)
    log.info(f"审查范围：{provider.describe_target()}")

    handler = build_handler(settings)
    tool = ReviewTool(settings=settings, handler=handler, provider=provider, dry_run=args.dry_run)

    try:
        result = asyncio.run(tool.run())
    except LLMError as exc:
        log.error(f"模型调用失败：{exc}")
        return EXIT_ERROR

    if not result.has_content:
        ConsolePublisher(use_color=not args.no_color).publish(_publish_context(args, result))
        return EXIT_NO_CHANGES

    # 终端始终输出（用户跑这条命令就是为了看结果）
    ConsolePublisher(use_color=not args.no_color).publish(_publish_context(args, result))

    if args.output or args.json_output:
        FilePublisher(
            output_path=args.output,
            json_output_path=args.json_output,
            timestamped=False,
        ).publish(_publish_context(args, result))
    else:
        log.info("提示：加 --output <path> 可把这份报告保存成 Markdown 文件")

    for notice in result.notices:
        log.warning(notice)
    return EXIT_OK


def _publish_context(args: argparse.Namespace, result):
    from codesentry.output.base import PublishContext

    return PublishContext(
        markdown=result.markdown,
        json_text=result.json_text,
        result=result.result,
    )


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)

    # 允许 `--section.key=value` 这样的通用覆盖：它们不是 parser 注册过的参数，
    # 所以用 parse_known_args 收集，再自行校验。
    args, unknown = parser.parse_known_args(argv)
    overrides, unknown_errors = split_overrides(unknown)

    if unknown_errors:
        parser.error("无法识别的参数：" + " ".join(unknown_errors)
                     + "\n（若要临时覆盖配置项，请使用 --section.key=value 形式）")

    if not args.command:
        parser.print_help()
        return EXIT_USAGE

    try:
        if args.command == "config":
            return cmd_config(args, overrides)
        if args.command == "review":
            return cmd_review(args, overrides)
        parser.error(f"未知命令：{args.command}")
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return EXIT_ERROR
    except DiffProviderError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return EXIT_ERROR
    except PromptError as exc:
        print(f"[错误] 提示词加载失败：{exc}", file=sys.stderr)
        return EXIT_ERROR
    except FileNotFoundError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:                       # noqa: BLE001 - 最外层兜底
        print(f"[错误] {type(exc).__name__}: {exc}", file=sys.stderr)
        # 只有用户明确要调试（-vv，verbosity>=3）时才打完整栈，
        # 否则 traceback 只会淹没真正有用的那行错误信息
        if getattr(args, "verbose", 0) >= 2:
            traceback.print_exc()
        else:
            print("提示：加 -vv 可查看完整调用栈。", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":                          # pragma: no cover
    sys.exit(main())
