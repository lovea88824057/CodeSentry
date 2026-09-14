"""文件过滤：在花钱调模型之前，先把不值得看的文件剔掉。

为什么这一步很重要？—— 一次审查的成本几乎完全正比于送进去的 token 数。
一个前端仓库的 diff 里，`package-lock.json` 能占掉 80% 的体积，
而它对代码审查的价值是零。先过滤再分块，往往能把成本降一个数量级。

过滤规则刻意做成"可解释"的：每个被跳过的文件都记录原因，
并（可选）出现在报告末尾。用户看到"跳过了 12 个文件"时，
必须能知道是哪 12 个、为什么 —— 否则他会怀疑漏看了关键改动。
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from codesentry.config import IgnoreSection
from codesentry.models import FilePatchInfo, EditType
from codesentry.utils.logger import get_logger

_GLOB_CACHE: dict[str, re.Pattern[str]] = {}


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """把 gitignore 风格的 glob 转成正则。

    为什么不用 fnmatch / Path.match？
    —— 它们对 `**` 的处理各不相同，且 Path.match 在 Windows 上还有
    大小写和分隔符的坑。自己转正则只需 20 行，行为完全可控可测。

    规则：
        **  匹配任意层级（含 /）
        *   匹配单层内任意字符（不含 /）
        ?   匹配单个字符（不含 /）
    """
    cached = _GLOB_CACHE.get(pattern)
    if cached is not None:
        return cached

    out: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if i + 1 < len(pattern) and pattern[i + 1] == "*":
                # `**/` 或 `**` —— 允许跨越目录分隔符
                out.append(".*")
                i += 2
                if i < len(pattern) and pattern[i] == "/":
                    i += 1
                continue
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
        i += 1

    # 没有以 ** 开头的模式，允许匹配任意前缀路径（等价于 gitignore 的"任意层级"语义）
    prefix = "" if pattern.startswith("**") else "(?:.*/)?"
    regex = re.compile(f"^{prefix}{''.join(out)}$")
    _GLOB_CACHE[pattern] = regex
    return regex


def matches_any(path: str, patterns: list[str]) -> str | None:
    """返回第一个命中的模式，没命中返回 None。"""
    normalized = path.replace("\\", "/")
    for pattern in patterns:
        if _glob_to_regex(pattern).match(normalized):
            return pattern
        # 便捷语义：不带 / 的模式也匹配 basename，让 "*.lock" 能命中 a/b/c.lock
        if "/" not in pattern and _glob_to_regex(pattern).match(PurePosixPath(normalized).name):
            return pattern
    return None


def apply_ignore_rules(files: list[FilePatchInfo], ignore: IgnoreSection) -> list[FilePatchInfo]:
    """就地标记应跳过的文件，返回全部文件（含被跳过的）。

    就地标记而不是过滤掉，是为了后续报告能列出"跳过了什么"。
    """
    extensions = {e.lower() for e in ignore.extensions}
    for info in files:
        if info.skipped:
            continue  # 解析阶段已判定（二进制 / 无内容改动）

        hit = matches_any(info.filename, ignore.paths)
        if hit:
            info.skipped = True
            info.skip_reason = f"命中忽略规则 `{hit}`"
            continue

        suffix = PurePosixPath(info.filename).suffix.lower()
        if suffix and suffix in extensions:
            info.skipped = True
            info.skip_reason = f"扩展名 {suffix} 不在审查范围内"
            continue

        if not info.patch.strip():
            info.skipped = True
            info.skip_reason = "无可审查的 diff 内容"
            continue

        if info.edit_type is EditType.DELETED and not info.patch.strip():
            info.skipped = True
            info.skip_reason = "纯删除且无内容"
            continue

    skipped = [f for f in files if f.skipped]
    if skipped:
        get_logger().info(f"已跳过 {len(skipped)} 个文件（{len(files) - len(skipped)} 个进入审查）")
        for f in skipped:
            get_logger().debug(f"  跳过 {f.filename}：{f.skip_reason}")
    return files


def render_skipped_summary(files: list[FilePatchInfo]) -> str:
    """把被跳过的文件渲染成报告末尾的一段说明。"""
    skipped = [f for f in files if f.skipped]
    if not skipped:
        return ""
    lines = [f"- `{f.filename}` —— {f.skip_reason}" for f in skipped]
    return "\n".join(lines)
