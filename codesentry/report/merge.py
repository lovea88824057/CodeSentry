"""多块结果的合并（map-reduce 里的 reduce 阶段）。

分块带来了一个必须解决的问题：**同一份报告由 N 次独立调用拼出来**。
如果简单地把 N 份结果首尾相接，会出现：
    - 摘要重复 3 遍
    - 同一个问题被 2 个块各报一次（文件在块边界上被拆开时尤其常见）
    - 分数取了最后一个块的值，而不是整体最差值

这一层就是把这些"合并语义"显式定义下来。每种字段的合并规则不同，
所以不用通用的 merge，而是逐字段写清楚 —— 合并策略是业务决策，
不该藏在某个通用的深合并函数里。
"""

from __future__ import annotations

import hashlib
import re

from codesentry.models import FileSummary, KeyIssue, ReviewResult, RunDetails
from codesentry.utils.logger import get_logger

#: 风险等级的"取最差值"顺序
_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}

#: 合并建议的"取最保守值"顺序
_MERGE_ORDER = {"safe_to_merge": 0, "merge_with_caution": 1, "changes_required": 2}


def _normalize(text: str) -> str:
    """归一化文本用于去重比较：折叠空白、去掉标点差异。"""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _issue_identity(issue: KeyIssue) -> str:
    """给一条问题算去重指纹。

    指纹只用"文件 + 内容前 120 字符"，刻意**不含行号**：
    同一段代码在块边界被切两次时，模型给出的行号可能不同，
    但问题本身是同一个。含行号会导致去重失效。
    """
    payload = f"{issue.relevant_file}|{_normalize(issue.issue_content)[:120]}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _worst_risk(values: list[str]) -> str | None:
    candidates = [v for v in values if v in _RISK_ORDER]
    if not candidates:
        return None
    return max(candidates, key=lambda v: _RISK_ORDER[v])


def _worst_merge_advice(values: list[str]) -> str | None:
    candidates = [v for v in values if v in _MERGE_ORDER]
    if not candidates:
        return None
    return max(candidates, key=lambda v: _MERGE_ORDER[v])


def _join_texts(values: list[str], separator: str = "\n\n") -> str | None:
    """把多段文本拼起来，去掉完全重复的段落。"""
    seen: set[str] = set()
    parts: list[str] = []
    for value in values:
        text = (value or "").strip()
        if not text:
            continue
        key = _normalize(text)
        if key in seen:
            continue
        seen.add(key)
        parts.append(text)
    return separator.join(parts) if parts else None


def merge_results(results: list[ReviewResult], chunk_labels: list[str] | None = None) -> ReviewResult:
    """把多个分块的结果合并成一份。

    单块时直接返回，不做任何加工 —— 保持"最常见路径零副作用"，
    这样单块场景的行为完全可预测，不会被合并逻辑影响。
    """
    results = [r for r in results if r is not None]
    if not results:
        return ReviewResult(warnings=["所有分块的模型调用均失败，未产生任何结果"])
    if len(results) == 1:
        return results[0]

    get_logger().info(f"正在合并 {len(results)} 个分块的审查结果")

    merged = ReviewResult()

    # --- 摘要：拼接并标注来源块，而不是只取第一个 ---
    # 只取第一个会丢掉其他块发现的整体性问题；全丢也不行。
    # 折中：给每段加上"（第 N 部分）"的前缀，读者能感知到这是拼的。
    summary_parts: list[str] = []
    for idx, result in enumerate(results):
        text = (result.summary or "").strip()
        if not text:
            continue
        label = ""
        if chunk_labels and idx < len(chunk_labels):
            label = f"（{chunk_labels[idx]}）"
        summary_parts.append(f"{label}{text}" if label else text)
    merged.summary = _join_texts(summary_parts) or ""

    # --- 分数：取最小值 ---
    # 理由：分块审查时，任何一个块发现严重问题都应该拉低整体评价。
    # 取平均会让严重问题被其他"大而干净"的块稀释掉，这不符合审查的保守性原则。
    scores = [r.score for r in results if r.score is not None]
    merged.score = min(scores) if scores else None

    # --- 风险：取最差 ---
    merged.risk_level = _worst_risk([r.risk_level for r in results if r.risk_level])

    # --- 合并建议：取最保守 ---
    merged.merge_recommendation = _worst_merge_advice(
        [r.merge_recommendation for r in results if r.merge_recommendation]
    )

    # --- 问题：并集 + 去重 + 重新排序 ---
    seen: set[str] = set()
    issues: list[KeyIssue] = []
    duplicates = 0
    for result in results:
        for issue in result.key_issues:
            fingerprint = _issue_identity(issue)
            if fingerprint in seen:
                duplicates += 1
                continue
            seen.add(fingerprint)
            issues.append(issue)
    merged.key_issues = issues
    if duplicates:
        get_logger().info(f"去重合并：移除 {duplicates} 条跨块重复的问题")

    # --- 文件要点：按文件去重，保留信息量更大的那条 ---
    by_file: dict[str, FileSummary] = {}
    for result in results:
        for summary in result.file_summaries:
            existing = by_file.get(summary.relevant_file)
            if existing is None or len(summary.changes_summary) > len(existing.changes_summary):
                by_file[summary.relevant_file] = summary
    merged.file_summaries = list(by_file.values())

    # --- 测试建议 / 工作量：拼接 ---
    merged.suggested_tests = _join_texts([r.suggested_tests or "" for r in results], "\n")
    # 工作量估算不做累加：多个块的时间不是简单相加（人工看代码是整体视图），
    # 取最长的那条通常最接近真实值（保守估计）
    efforts = [r.effort_to_review for r in results if r.effort_to_review]
    merged.effort_to_review = max(efforts, key=len) if efforts else None

    # --- 告警与原文本：汇总 ---
    warnings: list[str] = []
    for idx, result in enumerate(results):
        for warning in result.warnings:
            warnings.append(f"第 {idx + 1} 部分：{warning}")
    merged.warnings = warnings

    raw_texts = [r.raw_text for r in results if r.raw_text]
    if raw_texts:
        merged.raw_text = "\n\n".join(f"--- 第 {i + 1} 部分原始输出 ---\n{t}" for i, t in enumerate(raw_texts))

    # --- 运行详情：累加调用次数与 token，耗时取最大值（并行执行，不是相加）---
    details = [r.run_details for r in results if r.run_details is not None]
    if details:
        base = details[0].model_copy(deep=True)
        base.num_chunks = len(results)
        base.num_llm_calls = sum(d.num_llm_calls for d in details)
        base.prompt_tokens = sum(d.prompt_tokens for d in details)
        base.completion_tokens = sum(d.completion_tokens for d in details)
        base.elapsed_seconds = max(d.elapsed_seconds for d in details)
        merged.run_details = base

    return merged
