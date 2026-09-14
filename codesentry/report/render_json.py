"""JSON 渲染。

给 CI / 其他工具消费用。设计目标是"稳定且自描述"：
字段名一旦发布就不改，新增字段只追加；同时把 warnings 一并输出，
让下游能感知到"这份结果是在降级状态下产生的"。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from codesentry.models import ReviewResult


def render_json(
    result: ReviewResult,
    title: str = "",
    source_ref: str = "",
    total_files: int = 0,
    active_files: int = 0,
) -> str:
    """输出缩进过的 JSON 字符串。"""
    payload = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "title": title,
        "source_ref": source_ref,
        "scope": {
            "total_files": total_files,
            "analyzed_files": active_files,
        },
        "result": result.model_dump(mode="json"),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def render_json_compact(result: ReviewResult) -> str:
    """单行 JSON，便于日志系统按行摄取。"""
    return json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
