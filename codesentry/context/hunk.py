"""把 diff 渲染成"带行号的结构化文本"。

这是喂给模型的最终形态，也是整个项目里对审查质量影响最大的一处设计。

**为什么不直接把 unified diff 原样丢给模型？**
unified diff 的行号只出现在 hunk 头（`@@ -10,6 +12,8 @@`），
模型要引用"第 42 行"就必须自己做加法。实测这条链路上模型出错率很高：
不是加了忘记减，就是把旧行号当新行号用。既然这是纯粹的机械劳动，
就让程序替它算好 —— 每一行前面直接标出新文件中的绝对行号，
模型只需"照抄"，准确率立刻上一个台阶。

**格式约定**（同一套约定在 prompt 里同步说明）：

    行号列 | 标记 | 内容
    -------|------|------
      12   | 空格 | 上下文行（新旧文件都有）
      13   |  +   | 新增行
    (空)   |  -   | 删除行（只存在于旧文件，没有新文件行号）

行号一律是**新文件中的绝对行号**（1-based）。
"""

from __future__ import annotations

from codesentry.diff.parse import iter_hunks
from codesentry.models import EditType, FilePatchInfo

#: 注入 prompt 的格式说明。与上面的表保持一致 —— 改格式必须同时改这里。
HUNK_FORMAT_LEGEND = """\
每个文件的 diff 会以下面的结构给出（行号是新文件中的绝对行号，1-based）：

### 文件 `<路径>` (变更类型 · 语言)
@@ -旧起始,旧行数 +新起始,新行数 @@ 可能的函数/类名
   12|   上下文行（新旧文件共有）
   13|+  新增的行
     |-  被删除的行（仅存在于旧文件，因此没有新文件行号）

说明：
- 行号列显示的是该行在**新文件**里的绝对行号，请直接引用它，不要自己计算。
- `+` 开头的行是新增内容，`-` 开头的行是被删除内容，其余是未改动的上下文。
- hunk 头 `@@` 后面的文字（如函数名）也能帮你定位所在位置。"""


def render_hunk_body(patch: str) -> str:
    """把单文件的 patch 渲染成带行号的文本。

    实现要点：hunk 体内的行号必须"按内容类型分别推进" ——
    上下文行和新增行会让新文件行号 +1，删除行不会。
    这是唯一容易写错的地方，所以这里刻意不做任何"聪明"的合并或去重。
    """
    output: list[str] = []
    for header, _old_start, new_start, body in iter_hunks(patch):
        # hunk 头原样保留：`@@ -10,6 +12,8 @@ def foo():` 里的函数名
        # 是模型定位"这段代码属于哪个函数"的重要线索，不能丢。
        if header:
            output.append(header)
        new_no = new_start
        for raw in body:
            if not raw:
                # 空行：diff 里真正的空行是单个空格或空串，统一按上下文行处理
                output.append(f"{new_no:>5}| ")
                new_no += 1
                continue
            marker = raw[0]
            content = raw[1:]
            if marker == "+":
                output.append(f"{new_no:>5}|+{content}")
                new_no += 1
            elif marker == "-":
                # 删除行没有新文件行号，留空格占位以保持列对齐
                output.append(f"{'':>5}|-{content}")
            else:
                output.append(f"{new_no:>5}| {content}")
                new_no += 1
    return "\n".join(output)


def render_file_block(info: FilePatchInfo, include_header: bool = True) -> str:
    """渲染单个文件的完整块。"""
    lines: list[str] = []
    if include_header:
        traits = [info.edit_type.value]
        if info.language:
            traits.append(info.language)
        # 新增/删除的数量对模型判断"这是新功能还是小修补"有帮助
        added, removed = _count_changes(info.patch)
        if added or removed:
            traits.append(f"+{added}/-{removed}")
        lines.append(f"### 文件 `{info.filename}` ({' · '.join(traits)})")
        if info.edit_type is EditType.RENAMED and info.base_path:
            lines.append(f"（由 `{info.base_path}` 重命名而来）")
    body = render_hunk_body(info.patch)
    if body:
        lines.append(body)
    return "\n".join(lines)


def render_files_for_prompt(files: list[FilePatchInfo]) -> str:
    """把多个文件拼成一段完整的 diff 文本。

    文件之间用一条醒目的分隔线隔开：模型对长上下文里"哪里结束、哪里开始"
    比较敏感，明确的分隔能显著降低把 A 文件的问题归到 B 文件头上的概率。
    """
    if not files:
        return "（无内容）"
    blocks = [render_file_block(f) for f in files if not f.skipped]
    blocks = [b for b in blocks if b.strip()]
    if not blocks:
        return "（无内容）"
    separator = "\n\n" + "=" * 72 + "\n\n"
    return separator.join(blocks)


def _count_changes(patch: str) -> tuple[int, int]:
    """统计增删行数，仅用于展示。"""
    added = removed = 0
    for line in patch.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


def render_file_index(files: list[FilePatchInfo]) -> str:
    """渲染"文件清单"，用于超大 diff 被裁时至少让模型知道全貌。

    这是从 pr-agent 的 `pr_generate_compressed_diff` 学来的：
    当 token 不够放全部 hunk 时，保留一份文件清单比什么都不给有价值得多 ——
    模型至少能说出"这次改动涉及 20 个文件，但我只看到了其中 3 个的细节"。
    """
    added = [f.filename for f in files if f.edit_type is EditType.ADDED]
    modified = [f.filename for f in files if f.edit_type is EditType.MODIFIED]
    renamed = [f.filename for f in files if f.edit_type is EditType.RENAMED]
    deleted = [f.filename for f in files if f.edit_type is EditType.DELETED]
    parts: list[str] = []
    for label, items in (("新增文件", added), ("修改文件", modified),
                         ("重命名文件", renamed), ("删除文件", deleted)):
        if items:
            parts.append(f"{label}（{len(items)}）：" + "、".join(items))
    return "\n".join(parts)
