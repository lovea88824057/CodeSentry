"""把模型返回的文本解析成结构化 ReviewResult。

**这一层的价值全在"兜底"上。** 让模型输出 YAML 已经很稳了，但"很稳"不等于
"每次都行"：小模型会漏引号、会多写一句"以上是我的分析"、会在 YAML 后面
再附一段解释。如果解析失败就让整次审查白费，用户体验会非常糟糕。

所以这里设计了三层降级，按"信息损失从小到大"：
    1. 直接 safe_load
    2. 清理噪声（代码围栏、前后缀说明文字）后重试
    3. 只截取像 YAML 的那一段再试
    4. 全失败 -> 保留原文作为 raw_text，报告里原样展示 + 给出告警

另外还负责一件容易被忽略但很重要的事：**校验模型给出的行号**。
本地模式下我们手里有文件的完整内容，于是可以拿行号去核对 ——
对不上就说明模型在编，这时宁可不给行号，也不能给出错误行号。
"""

from __future__ import annotations

import re
from typing import Any, Optional

import yaml

from codesentry.models import FilePatchInfo, FileSummary, KeyIssue, ReviewResult
from codesentry.utils.logger import get_logger

# 模型偶尔会用别的键名表达同一个概念。做一层宽松映射，
# 避免因为一个键名不匹配就丢掉整份有价值的结论。
_KEY_ISSUE_ALIASES = ("key_issues", "issues", "problems", "findings", "critical_issues")
_FILE_SUMMARY_ALIASES = ("file_summaries", "files", "file_changes", "changed_files")
_SUMMARY_ALIASES = ("summary", "overview", "description", "pr_summary", "review_summary")
_SCORE_ALIASES = ("score", "rating", "quality_score", "overall_score")
_RISK_ALIASES = ("risk_level", "risk", "risk_assessment")
_TESTS_ALIASES = ("suggested_tests", "tests", "test_recommendations", "recommended_tests")
_EFFORT_ALIASES = ("effort_to_review", "review_effort", "effort", "time_to_review")
_MERGE_ALIASES = ("merge_recommendation", "recommendation", "merge_advice")

#: 可能是"包装层"的顶层键。模型有时会返回 {Review: {...}} 而不是 {...}。
_WRAPPER_KEYS = ("review", "pr_review", "result", "output", "review_result", "response")

_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

_FENCE_RE = re.compile(r"^\s*```(?:ya?ml|json)?\s*$", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# 文本清洗
# --------------------------------------------------------------------------- #

def strip_code_fences(text: str) -> str:
    """去掉 ```yaml ... ``` 围栏。

    模型被要求"不要用围栏"，但实测仍有相当比例会加。与其在报告里
    因为三个反引号而丢结构，不如主动剥掉。
    """
    lines = text.splitlines()
    if not lines:
        return text
    # 找到第一行围栏和最后一行围栏，取中间
    start = None
    end = None
    for i, line in enumerate(lines):
        if _FENCE_RE.match(line):
            if start is None:
                start = i
            else:
                end = i
                break
    if start is not None:
        inner = lines[start + 1: end] if end is not None else lines[start + 1:]
        if any(l.strip() for l in inner):
            return "\n".join(inner)
    return text


def _extract_yaml_region(text: str) -> str:
    """从混杂文本里截取最像 YAML 的那一段。

    判据：从第一个"看起来像顶层键"的行（`key:` 或 `- `）开始，
    到最后一个"仍属于 YAML"的行为止。这样"以上是我的分析"这类
    前后缀说明文字会被裁掉。
    """
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if re.match(r"^[A-Za-z_][\w-]*\s*:", stripped) or stripped.startswith("- "):
            start = i
            break
    if start is None:
        return text

    end = len(lines)
    # 从尾部往回找最后一个非空且不像是"总结性自然语言"的行
    for i in range(len(lines) - 1, start - 1, -1):
        stripped = lines[i].strip()
        if not stripped:
            continue
        # 明显是散文的行（以句号/逗号结尾且不含 `:`），不算 YAML 内容
        if stripped.endswith(("。", "！", "？", ".", "!", "?")) and ":" not in stripped:
            continue
        end = i + 1
        break
    return "\n".join(lines[start:end])


def load_yaml(text: str) -> Optional[dict[str, Any]]:
    """尽力把文本解析成 dict。全失败返回 None。"""
    if not text or not text.strip():
        return None

    candidates: list[str] = []
    candidates.append(text.strip())
    cleaned = strip_code_fences(text).strip()
    if cleaned != text.strip():
        candidates.append(cleaned)
    region = _extract_yaml_region(cleaned).strip()
    if region and region not in candidates:
        candidates.append(region)

    for candidate in candidates:
        # YAML 里出现 tab 缩进会直接解析失败，而模型偶尔会混用 tab
        normalized = candidate.replace("\t", "  ")
        try:
            data = yaml.safe_load(normalized)
        except yaml.YAMLError as exc:
            get_logger().debug(f"YAML 解析失败（继续尝试下一种清洗方式）：{exc}")
            continue
        if isinstance(data, dict):
            return data
        # 顶层是列表：可能是 [{"summary": ...}] 这种包法
        if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
            return data[0]
    return None


# --------------------------------------------------------------------------- #
# 字段提取
# --------------------------------------------------------------------------- #

def _unwrap(data: dict[str, Any]) -> dict[str, Any]:
    """剥掉可能的包装层，例如 {"Review": {...}}。"""
    if len(data) == 1:
        only_key, only_value = next(iter(data.items()))
        if isinstance(only_value, dict) and str(only_key).lower() in _WRAPPER_KEYS:
            return only_value
    return data


def _first_present(data: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    """按键名别名表取值。大小写不敏感。"""
    lowered = {str(k).lower(): v for k, v in data.items()}
    for alias in aliases:
        if alias in lowered:
            return lowered[alias]
    return None


def _as_text(value: Any) -> str:
    """把任意值转成展示用文本。列表会被拼成多行，方便直接放进报告。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return "\n".join(_as_text(v) for v in value if v is not None)
    if isinstance(value, dict):
        return "\n".join(f"{k}: {_as_text(v)}" for k, v in value.items())
    return str(value).strip()


def _coerce_issue(raw: Any) -> Optional[KeyIssue]:
    """把一条原始 issue 记录转成 KeyIssue。缺关键字段则丢弃。"""
    if isinstance(raw, str):
        # 模型偶尔把 issue 写成纯文本行，尽力提取文件路径
        match = re.search(r"[`'\"]?([\w./\\-]+\.\w+)[`'\"]?", raw)
        if not match:
            return None
        raw = {"relevant_file": match.group(1), "issue_content": raw}
    if not isinstance(raw, dict):
        return None

    lowered = {str(k).lower(): v for k, v in raw.items()}
    filename = _as_text(
        lowered.get("relevant_file") or lowered.get("file") or lowered.get("path")
        or lowered.get("filename") or ""
    )
    content = _as_text(
        lowered.get("issue_content") or lowered.get("content") or lowered.get("description")
        or lowered.get("message") or lowered.get("issue") or ""
    )
    if not content:
        return None
    # 文件路径缺失时用占位符，保证这条问题仍然被展示出来（有内容就有价值）
    if not filename:
        filename = "(未指明文件)"

    return KeyIssue(
        relevant_file=filename,
        issue_content=content,
        start_line=lowered.get("start_line") or lowered.get("line") or lowered.get("relevant_line_start"),
        end_line=lowered.get("end_line") or lowered.get("relevant_line_end"),
        severity=lowered.get("severity") or lowered.get("level") or lowered.get("priority") or "medium",
    )


def _coerce_file_summary(raw: Any) -> Optional[FileSummary]:
    if isinstance(raw, str):
        return FileSummary(relevant_file=raw, changes_summary="")
    if not isinstance(raw, dict):
        return None
    lowered = {str(k).lower(): v for k, v in raw.items()}
    filename = _as_text(
        lowered.get("relevant_file") or lowered.get("file") or lowered.get("path")
        or lowered.get("filename") or ""
    )
    if not filename:
        return None
    content = _as_text(
        lowered.get("changes_summary") or lowered.get("summary") or lowered.get("changes")
        or lowered.get("description") or ""
    )
    return FileSummary(relevant_file=filename, changes_summary=content)


def parse_review(text: str) -> ReviewResult:
    """把模型输出解析成 ReviewResult。

    解析失败不抛异常：返回一个只带 raw_text 的结果，让报告层原样展示。
    "看到原始输出"远好于"程序报错，什么也没拿到"。
    """
    data = load_yaml(text)
    if data is None:
        get_logger().warning("模型输出无法解析为 YAML，已降级为原文展示")
        return ReviewResult(raw_text=text.strip(), warnings=["模型输出未能解析为结构化 YAML，已展示原文"])

    data = _unwrap(data)

    result = ReviewResult(
        summary=_as_text(_first_present(data, _SUMMARY_ALIASES)),
        score=_first_present(data, _SCORE_ALIASES),
        risk_level=_first_present(data, _RISK_ALIASES),
        merge_recommendation=_as_text(_first_present(data, _MERGE_ALIASES)) or None,
        effort_to_review=_as_text(_first_present(data, _EFFORT_ALIASES)) or None,
        suggested_tests=_as_text(_first_present(data, _TESTS_ALIASES)) or None,
    )

    raw_issues = _first_present(data, _KEY_ISSUE_ALIASES) or []
    if isinstance(raw_issues, dict):
        raw_issues = [raw_issues]
    if isinstance(raw_issues, list):
        for item in raw_issues:
            issue = _coerce_issue(item)
            if issue is not None:
                result.key_issues.append(issue)

    raw_files = _first_present(data, _FILE_SUMMARY_ALIASES) or []
    if isinstance(raw_files, dict):
        # {"a.py": "改了 x", "b.py": "改了 y"} 这种写法也接受
        raw_files = [{"relevant_file": k, "changes_summary": v} for k, v in raw_files.items()]
    if isinstance(raw_files, list):
        for item in raw_files:
            summary = _coerce_file_summary(item)
            if summary is not None:
                result.file_summaries.append(summary)

    if not result.summary and not result.key_issues and not result.file_summaries:
        # 解析出 dict 但一个有用字段都没提取到 —— 说明结构完全不是我们期待的
        result.warnings.append("模型返回的 YAML 结构与预期不符，未能提取到有效字段")
        result.raw_text = text.strip()
    return result


# --------------------------------------------------------------------------- #
# 行号校验
# --------------------------------------------------------------------------- #

def build_file_index_map(files: list[FilePatchInfo]) -> dict[str, FilePatchInfo]:
    """构建"多种可能的路径写法 -> FilePatchInfo"的查找表。

    为什么需要多键映射？模型经常把路径写得不一致：
    diff 里是 `src/a.py`，它写成 `a.py` 或 `./src/a.py`。
    如果严格匹配，这些问题的行号校验会全部失效（我们以为是"文件没找到"，
    其实只是路径写法不同）。宁可宽松匹配，也不要白白丢掉校验能力。
    """
    index: dict[str, FilePatchInfo] = {}
    for info in files:
        name = info.filename.replace("\\", "/")
        index[name] = info
        index[name.lstrip("./")] = info
        index[name.rsplit("/", 1)[-1]] = info          # 只看 basename
        if info.base_path:
            base = info.base_path.replace("\\", "/")
            index.setdefault(base, info)
    return index


def _lookup(path: str, index: dict[str, FilePatchInfo]) -> Optional[FilePatchInfo]:
    """按"精确 -> 去掉前缀 -> basename -> 后缀匹配"的顺序找文件。"""
    if not path:
        return None
    normalized = path.replace("\\", "/").lstrip("./")
    if normalized in index:
        return index[normalized]
    basename = normalized.rsplit("/", 1)[-1]
    if basename in index:
        return index[basename]
    # 末段路径匹配：模型可能少写了前几层目录
    for key, info in index.items():
        if key.endswith("/" + normalized) or normalized.endswith("/" + key):
            return info
    return None


def validate_line_numbers(result: ReviewResult, files: list[FilePatchInfo]) -> ReviewResult:
    """校验每条问题引用的行号是否真实存在于新文件中。

    三种结果：
      1. 文件内容可得 且 行号在范围内  -> line_verified = True
      2. 文件内容可得 但 行号越界      -> 立即清空行号并记告警（这个行号一定是错的）
      3. 文件内容不可得（纯 diff 模式）-> 保留行号但标记未验证（无法判断，不妄断）

    为什么要清空而不是保留？—— 一个指向错误位置的行号会让读者去翻一段
    无关的代码，然后开始怀疑整份报告。没有行号只是少了便利，
    错误行号是主动误导。两者严重性完全不同。
    """
    index_map = build_file_index_map(files)
    out_of_range: list[str] = []
    unverifiable = 0

    for issue in result.key_issues:
        info = _lookup(issue.relevant_file, index_map)
        if info is None:
            issue.line_verified = False
            unverifiable += 1
            continue

        # 把模型可能写错的路径改写成 diff 里的真实路径，避免报告里出现找不到的文件名
        issue.relevant_file = info.filename

        if not info.has_head_context:
            issue.line_verified = False
            unverifiable += 1
            continue

        if issue.start_line is None:
            issue.line_verified = False
            continue

        end = issue.end_line if issue.end_line and issue.end_line >= issue.start_line else issue.start_line
        if info.get_head_lines(issue.start_line, end):
            issue.line_verified = True
            if issue.end_line is None:
                issue.end_line = end
        else:
            out_of_range.append(f"{info.filename}:{issue.start_line}")
            issue.start_line = None
            issue.end_line = None
            issue.line_verified = False

    if out_of_range:
        result.warnings.append(
            "以下行号越界（文件里不存在该行），已从报告中移除以免误导：" + "、".join(out_of_range[:10])
        )
    if unverifiable:
        get_logger().debug(f"{unverifiable} 条问题无法做行号校验（缺少文件全文）")
    return result


def sort_issues(result: ReviewResult) -> ReviewResult:
    """按严重程度排序，同级保持原顺序（稳定排序）。"""
    result.key_issues.sort(key=lambda i: _SEVERITY_ORDER.get(i.severity, 9))
    return result


def cap_issues(result: ReviewResult, max_findings: int) -> ReviewResult:
    """兜底截断。

    prompt 里已经要求"最多 N 条"，但模型并不总是遵守。这里再截一次，
    同时把被截掉的数量记进告警 —— 静默丢弃是不可接受的。
    """
    if max_findings > 0 and len(result.key_issues) > max_findings:
        dropped = len(result.key_issues) - max_findings
        result.key_issues = result.key_issues[:max_findings]
        result.warnings.append(f"问题数量超过上限 {max_findings}，已截断 {dropped} 条低优先级问题")
    return result
