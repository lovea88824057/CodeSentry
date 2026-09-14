"""把 ReviewResult 渲染成 Markdown。

为什么最终产物选 Markdown？
—— 它同时满足三个约束：① 终端里能直接看（配合 rich 渲染）；② 落盘后能被
GitHub / GitLab / 各种笔记工具原样渲染；③ 结构清晰到能被 diff 和 grep。
JSON 虽然机器友好，但没人愿意在终端里读 JSON；纯文本又丢了结构。

排版上刻意保持"结论先行"：**摘要 → 指标 → 关键问题 → 细节**。
读者如果只有 10 秒，看完前两段就应该知道"这个改动能不能合"。
"""

from __future__ import annotations

from codesentry.context.hunk import render_file_index
from codesentry.models import ReviewResult
from codesentry.utils.filter import render_skipped_summary

_SEVERITY_ICON = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}
_SEVERITY_LABEL = {"critical": "严重", "high": "高", "medium": "中", "low": "低"}
_RISK_ICON = {"low": "🟢", "medium": "🟡", "high": "🔴"}
_RISK_LABEL = {"low": "低", "medium": "中", "high": "高"}
_MERGE_LABEL = {
    "safe_to_merge": "✅ 可以直接合并",
    "merge_with_caution": "⚠️ 建议谨慎合并",
    "changes_required": "⛔ 需要修改后再合并",
}


def _line_ref(issue) -> str:
    """把行号渲染成 `L42-L48` / `L42` / `（无行号）`。"""
    if issue.start_line is None:
        return "（无行号）"
    if issue.end_line and issue.end_line != issue.start_line:
        return f"L{issue.start_line}-L{issue.end_line}"
    return f"L{issue.start_line}"


def _verification_mark(issue) -> str:
    """标注行号的可信度。

    三种状态必须区分清楚，因为它们的含义完全不同：
        ✅          行号已与文件真实内容比对通过
        ⚠️ 行号未校验  拿不到文件全文（纯 diff 模式），无法判断真伪
        （空）       行号已被判定为越界并移除，此时 _line_ref 会显示"（无行号）"

    把"未校验"也显式写出来，是为了让读者知道"这个位置可能是准的，
    但我们没有依据" —— 沉默地展示一个未经核实的行号是在制造虚假的可信感。
    """
    if issue.line_verified:
        return " ✅"
    if issue.start_line is not None:
        return " ⚠️ 行号未校验"
    return ""


def render_markdown(
    result: ReviewResult,
    title: str = "",
    source_ref: str = "",
    file_index: str = "",
    skipped_summary: str = "",
    total_files: int = 0,
    active_files: int = 0,
) -> str:
    """渲染完整报告。"""
    lines: list[str] = []
    lines.append("# 🛡️ CodeSentry 审查报告")
    lines.append("")

    if title or source_ref:
        meta = " · ".join(x for x in (f"**{title}**" if title else "", source_ref) if x)
        lines.append(f"> {meta}")
        lines.append("")

    # ---------------- 摘要 ----------------
    if result.summary:
        lines.append("## 摘要")
        lines.append("")
        lines.append(result.summary)
        lines.append("")

    # ---------------- 指标表 ----------------
    metrics: list[tuple[str, str]] = []
    if result.score is not None:
        # 用图形化进度条代替干巴巴的数字，扫一眼就有量感
        filled = max(0, min(10, result.score))
        bar = "█" * filled + "░" * (10 - filled)
        metrics.append(("质量评分", f"`{bar}` **{result.score}/10**"))
    if result.risk_level:
        icon = _RISK_ICON.get(result.risk_level, "")
        label = _RISK_LABEL.get(result.risk_level, result.risk_level)
        metrics.append(("风险等级", f"{icon} **{label}**"))
    if result.merge_recommendation:
        metrics.append(("合并建议", _MERGE_LABEL.get(result.merge_recommendation,
                                                result.merge_recommendation)))
    if result.effort_to_review:
        metrics.append(("建议审查工作量", result.effort_to_review))
    if total_files:
        scope = f"{active_files} / {total_files} 个文件"
        if result.run_details and result.run_details.num_chunks > 1:
            scope += f" · 分 {result.run_details.num_chunks} 批分析"
        metrics.append(("本次范围", scope))

    if metrics:
        lines.append("## 结论指标")
        lines.append("")
        lines.append("| 指标 | 值 |")
        lines.append("|---|---|")
        for name, value in metrics:
            lines.append(f"| {name} | {value} |")
        lines.append("")

    # ---------------- 关键问题 ----------------
    lines.append(f"## 关键问题（{len(result.key_issues)}）")
    lines.append("")
    if not result.key_issues:
        lines.append("未发现需要优先处理的问题。")
        lines.append("")
    else:
        for idx, issue in enumerate(result.key_issues, start=1):
            icon = _SEVERITY_ICON.get(issue.severity, "🟡")
            label = _SEVERITY_LABEL.get(issue.severity, issue.severity)
            location = _line_ref(issue)
            lines.append(
                f"### {idx}. {icon} [{label}] `{issue.relevant_file}` {location}{_verification_mark(issue)}"
            )
            lines.append("")
            # 引用块让问题描述与章节标题在视觉上分开，长报告里更好定位
            for content_line in issue.issue_content.splitlines():
                lines.append(f"> {content_line}" if content_line.strip() else ">")
            lines.append("")

    # ---------------- 文件要点 ----------------
    if result.file_summaries:
        lines.append("## 文件逐项")
        lines.append("")
        lines.append("| 文件 | 要点 |")
        lines.append("|---|---|")
        for summary in result.file_summaries:
            text = summary.changes_summary.replace("\n", " ").replace("|", "\\|") or "—"
            lines.append(f"| `{summary.relevant_file}` | {text} |")
        lines.append("")

    # ---------------- 测试建议 ----------------
    if result.suggested_tests:
        lines.append("## 建议补充的测试")
        lines.append("")
        lines.append(result.suggested_tests)
        lines.append("")

    # ---------------- 全貌索引（大 diff 被裁时特别有用）----------------
    if file_index:
        lines.append("<details>")
        lines.append("<summary>本次变更全貌（点击展开）</summary>")
        lines.append("")
        lines.append("```")
        lines.append(file_index)
        lines.append("```")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    # ---------------- 跳过的文件 ----------------
    if skipped_summary:
        lines.append("<details>")
        lines.append("<summary>未纳入审查的文件（点击展开）</summary>")
        lines.append("")
        lines.append(skipped_summary)
        lines.append("")
        lines.append("</details>")
        lines.append("")

    # ---------------- 告警 ----------------
    if result.warnings:
        lines.append("## ⚠️ 本次运行的注意事项")
        lines.append("")
        for warning in result.warnings:
            lines.append(f"- {warning}")
        lines.append("")

    # ---------------- 原始输出（解析失败时的兜底展示）----------------
    if result.raw_text:
        lines.append("## 模型原始输出")
        lines.append("")
        lines.append("> 结构化解析未成功，以下为模型返回的原文。")
        lines.append("")
        lines.append("```text")
        lines.append(result.raw_text)
        lines.append("```")
        lines.append("")

    # ---------------- 运行详情 ----------------
    details = result.run_details
    if details is not None:
        lines.append("<details>")
        lines.append("<summary>运行详情</summary>")
        lines.append("")
        lines.append("| 项 | 值 |")
        lines.append("|---|---|")
        lines.append(f"| 模型 | `{details.model}` |")
        if details.fallback_models:
            lines.append(f"| 备用模型 | {', '.join(f'`{m}`' for m in details.fallback_models)} |")
        lines.append(f"| 数据来源 | {details.provider} · {details.source_ref} |")
        lines.append(f"| 文件数 | {details.num_files}（跳过 {details.num_skipped_files}） |")
        lines.append(f"| 模型调用 | {details.num_llm_calls} 次（{details.num_chunks} 批） |")
        if details.prompt_tokens or details.completion_tokens:
            lines.append(f"| Token 用量 | 输入 {details.prompt_tokens:,} · 输出 {details.completion_tokens:,} |")
        lines.append(f"| 耗时 | {details.elapsed_seconds:.1f} 秒 |")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("*由 [CodeSentry](https://github.com/) 生成 · 本地优先的 AI 代码审查 Agent*")
    lines.append("")
    return "\n".join(lines)


def render_summary_line(result: ReviewResult) -> str:
    """一行式结论，用于终端里的即时反馈（不落盘）。"""
    parts: list[str] = []
    if result.score is not None:
        parts.append(f"评分 {result.score}/10")
    if result.risk_level:
        parts.append(f"风险 {_RISK_LABEL.get(result.risk_level, result.risk_level)}")
    parts.append(f"问题 {len(result.key_issues)} 条")
    return " · ".join(parts)
