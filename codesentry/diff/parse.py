"""unified diff 解析。

这是整个项目最需要"抠细节"的一块：解析错一个字节，后面的行号就会全错，
而模型的行号可信度直接决定了报告质量。

支持的输入形态：
1. `git diff` 的标准输出（含 `diff --git a/x b/x` 头）
2. 剥掉 `diff --git` 头的裸 patch（只有 `--- / +++ / @@`），常见于手工粘贴

刻意不支持二进制 patch 内容解析，识别到就标记跳过。
"""

from __future__ import annotations

import re
from typing import Iterator, Optional

from codesentry.models import EditType, FilePatchInfo

_DIFF_GIT_RE = re.compile(r'^diff --git (?:"(?P<a>[^"]+)"|(?P<a2>\S+)) (?:"(?P<b>[^"]+)"|(?P<b2>\S+))\s*$')
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")
_STRIP_PREFIX = ("a/", "b/")


def _unquote(path: str) -> str:
    """去掉 git 加在特殊路径外的双引号，并还原常见转义。"""
    path = path.strip()
    if len(path) >= 2 and path.startswith('"') and path.endswith('"'):
        path = path[1:-1]
        path = path.replace('\\"', '"').replace("\\\\", "\\")
        path = path.replace("\\t", "\t").replace("\\n", "\n")
    return path


def _strip_ab_prefix(path: str) -> str:
    """把 `a/foo.py` / `b/foo.py` 还原成 `foo.py`。

    只剥一层且只在确实是前缀时剥，避免把真的叫 "a/" 的目录名改掉。
    """
    for prefix in _STRIP_PREFIX:
        if path.startswith(prefix):
            return path[len(prefix):]
    return path


def _clean_header_path(raw: str) -> Optional[str]:
    """处理 `--- a/foo.py` 这一行的值。

    返回 None 表示是 /dev/null（新增或删除文件的标志）。
    非 git 的 diff 可能带制表符+时间戳（`--- foo.py\t2024-01-01 00:00:00`），
    这里按制表符切一刀取第一段。
    """
    value = raw.strip()
    if not value:
        return None
    value = value.split("\t", 1)[0].strip()
    value = _unquote(value)
    if value == "/dev/null":
        return None
    return _strip_ab_prefix(value)


def split_file_blocks(diff_text: str) -> list[list[str]]:
    """把整段 diff 拆成"每个文件一个块"。

    优先按 `diff --git` 头切分；如果整段都没有这个头（裸 patch），
    退化为按 `--- ` 行切分。这种降级很实用：用户从 issue/邮件里
    复制出来的 patch 往往就是裸的。
    """
    lines = diff_text.splitlines()
    blocks: list[list[str]] = []
    current: list[str] = []
    has_git_header = any(ln.startswith("diff --git ") for ln in lines)

    for line in lines:
        if has_git_header:
            is_boundary = line.startswith("diff --git ")
        else:
            # 裸 patch：`--- ` 既可能是文件边界，也可能是删除行（`-` 开头）
            # 所以要求它后面紧跟 `+++ ` 才算边界，用回看实现
            is_boundary = line.startswith("--- ") and current and any(
                l.startswith("+++ ") for l in current[-3:]
            )
        if is_boundary and current:
            blocks.append(current)
            current = []
        current.append(line)
    if current:
        # 丢掉纯空白尾巴
        if any(l.strip() for l in current):
            blocks.append(current)
    return blocks


def parse_file_block(block: list[str]) -> Optional[FilePatchInfo]:
    """把单个文件块解析成 FilePatchInfo（不含全文，全文由 provider 事后填充）。"""
    if not block:
        return None

    base_path: Optional[str] = None
    head_path: Optional[str] = None
    rename_from: Optional[str] = None
    rename_to: Optional[str] = None
    new_file = False
    deleted_file = False
    binary = False

    for line in block:
        if line.startswith("diff --git "):
            m = _DIFF_GIT_RE.match(line)
            if m:
                # 记录候选路径；后续 --- / +++ 会给出更权威的值
                base_path = _strip_ab_prefix(_unquote(m.group("a") or m.group("a2") or ""))
                head_path = _strip_ab_prefix(_unquote(m.group("b") or m.group("b2") or ""))
        elif line.startswith("new file mode"):
            new_file = True
        elif line.startswith("deleted file mode"):
            deleted_file = True
        elif line.startswith("rename from "):
            rename_from = _strip_ab_prefix(_unquote(line[len("rename from "):]))
        elif line.startswith("rename to "):
            rename_to = _strip_ab_prefix(_unquote(line[len("rename to "):]))
        elif line.startswith("copy from "):
            rename_from = _strip_ab_prefix(_unquote(line[len("copy from "):]))
        elif line.startswith("copy to "):
            rename_to = _strip_ab_prefix(_unquote(line[len("copy to "):]))
        elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            binary = True
        elif line.startswith("--- "):
            parsed = _clean_header_path(line[4:])
            if parsed is not None:
                base_path = parsed
            elif "--- /dev/null" in line:
                new_file = True
        elif line.startswith("+++ "):
            parsed = _clean_header_path(line[4:])
            if parsed is not None:
                head_path = parsed
            elif "+++ /dev/null" in line:
                deleted_file = True

    # 路径定案：rename 信息最权威，其次 +++/---，最后 diff --git 头
    if rename_to:
        head_path = rename_to
    if rename_from:
        base_path = rename_from
    filename = head_path or base_path
    if not filename:
        return None

    # 变更类型判定。注意顺序：新增/删除优先于 rename，
    # 因为 rename 同时伴随内容改写时 git 会同时给出 rename 与 ---/+++。
    if new_file or base_path is None:
        edit_type = EditType.ADDED
    elif deleted_file:
        edit_type = EditType.DELETED
    elif rename_from and rename_to and rename_from != rename_to:
        edit_type = EditType.RENAMED
    else:
        edit_type = EditType.MODIFIED

    # patch 正文只保留 hunk 部分，去掉 diff --git / index / mode 等元信息行。
    # 这样后续渲染行号时不用反复跳过噪声行。
    hunks: list[str] = []
    in_hunk = False
    for line in block:
        if _HUNK_RE.match(line):
            in_hunk = True
        if in_hunk:
            hunks.append(line)
    patch = "\n".join(hunks)

    info = FilePatchInfo(
        filename=filename,
        base_path=base_path,
        patch=patch,
        edit_type=edit_type,
        language=guess_language(filename),
    )
    if binary:
        # 二进制内容无法审查，也不该浪费 token
        info.skipped = True
        info.skip_reason = "二进制文件"
    elif not patch and edit_type in (EditType.MODIFIED, EditType.RENAMED):
        # 纯 rename / 纯 mode change，没有内容改动。
        # 注意：RENAMED 且 patch 非空 = "重命名 + 改内容"，那种情况要正常审查，
        # 所以这里必须同时判断 patch 为空。
        info.skipped = True
        info.skip_reason = "无内容改动（仅重命名或权限变更）"
    return info


def parse_unified_diff(diff_text: str) -> list[FilePatchInfo]:
    """解析整段 diff，返回文件列表（保持 diff 中的原始顺序）。"""
    files: list[FilePatchInfo] = []
    for block in split_file_blocks(diff_text):
        info = parse_file_block(block)
        if info is not None:
            files.append(info)
    return files


def iter_hunks(patch: str) -> Iterator[tuple[str, int, int, list[str]]]:
    """遍历 patch 里的每个 hunk。

    yield: (hunk 头原文, 旧起始行, 新起始行, hunk 体行列表)
    hunk 体包含 ` ` / `+` / `-` 前缀的原始行（不含 `\\ No newline` 这类元行）。

    单独抽出来是为了让"渲染带行号文本"和"统计增删行数"共用同一套解析，
    两处逻辑一旦分家，行号错位几乎必然发生。
    """
    old_start = new_start = 0
    body: list[str] = []
    header = ""
    in_hunk = False

    def flush():
        return (header, old_start, new_start, list(body))

    for line in patch.splitlines():
        if _HUNK_RE.match(line):
            if in_hunk:
                yield flush()
            header = line
            old_start, new_start = _parse_hunk_header(line)
            body = []
            in_hunk = True
            continue
        if not in_hunk:
            continue
        # `\ No newline at end of file` 是元信息，混进正文会让行号计算错位
        if line.startswith("\\"):
            continue
        body.append(line)
    if in_hunk:
        yield flush()


def _parse_hunk_header(header: str) -> tuple[int, int]:
    """从 `@@ -10,6 +12,8 @@` 提取新旧起始行号。"""
    try:
        spec = header.split("@@")[1].strip()
    except IndexError:
        return (0, 0)
    old_part, _, new_part = spec.partition(" ")
    old_start = int(old_part[1:].split(",")[0])
    new_start = int(new_part[1:].split(",")[0])
    return (old_start, new_start)


def count_changed_lines(patch: str) -> tuple[int, int]:
    """统计新增/删除行数，用于报告里的 diff 规模展示。"""
    added = removed = 0
    for line in patch.splitlines():
        if _HUNK_RE.match(line):
            continue
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


# --------------------------------------------------------------------------- #
# 语言识别
# --------------------------------------------------------------------------- #

_EXT_LANGUAGE = {
    ".py": "Python", ".pyi": "Python", ".ipynb": "Jupyter",
    ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript",
    ".java": "Java", ".kt": "Kotlin", ".scala": "Scala",
    ".c": "C", ".h": "C", ".cc": "C++", ".cpp": "C++", ".cxx": "C++",
    ".hpp": "C++", ".hh": "C++", ".cu": "CUDA", ".cuh": "CUDA",
    ".cs": "C#", ".go": "Go", ".rs": "Rust", ".rb": "Ruby", ".php": "PHP",
    ".swift": "Swift", ".m": "Objective-C", ".mm": "Objective-C++",
    ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell", ".ps1": "PowerShell",
    ".bat": "Batch", ".cmd": "Batch",
    ".sql": "SQL", ".proto": "Protobuf", ".graphql": "GraphQL",
    ".yaml": "YAML", ".yml": "YAML", ".json": "JSON", ".toml": "TOML",
    ".ini": "INI", ".cfg": "INI", ".xml": "XML", ".html": "HTML", ".htm": "HTML",
    ".css": "CSS", ".scss": "SCSS", ".less": "Less", ".vue": "Vue", ".svelte": "Svelte",
    ".r": "R", ".jl": "Julia", ".lua": "Lua", ".dart": "Dart", ".ex": "Elixir",
    ".tf": "Terraform", ".dockerfile": "Dockerfile", ".cmake": "CMake",
    ".md": "Markdown", ".rst": "reStructuredText", ".txt": "Text",
}

_SPECIAL_NAMES = {
    "dockerfile": "Dockerfile",
    "makefile": "Makefile",
    "cmakelists.txt": "CMake",
    ".gitignore": "GitIgnore",
    ".dockerignore": "DockerIgnore",
    "requirements.txt": "Text",
    "setup.py": "Python",
    "pyproject.toml": "TOML",
}


def guess_language(filename: str) -> Optional[str]:
    """按文件名猜语言，用于在报告里标注、也方便给模型提示代码类型。

    只是"锦上添花"的信息，猜不中就返回 None，绝不因此中断流程。
    """
    lower = filename.lower()
    name = lower.rsplit("/", 1)[-1]
    if name in _SPECIAL_NAMES:
        return _SPECIAL_NAMES[name]
    # 处理 `.tar.gz` 这类多段扩展名：只取最后一段即可，够用了
    if "." not in name:
        return None
    ext = "." + name.rsplit(".", 1)[-1]
    return _EXT_LANGUAGE.get(ext)
