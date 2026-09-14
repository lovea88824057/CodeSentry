"""报告层：结构化结论的解析、校验、合并与渲染。"""

from codesentry.report.merge import merge_results
from codesentry.report.render_json import render_json, render_json_compact
from codesentry.report.render_md import render_markdown, render_summary_line
from codesentry.report.schema import (
    cap_issues,
    load_yaml,
    parse_review,
    sort_issues,
    validate_line_numbers,
)

__all__ = [
    "merge_results",
    "render_json", "render_json_compact",
    "render_markdown", "render_summary_line",
    "cap_issues", "load_yaml", "parse_review", "sort_issues", "validate_line_numbers",
]
