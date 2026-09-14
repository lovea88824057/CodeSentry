"""结果解析、行号校验与合并测试。

这一层面对的是"不受控的模型输出"，所以测试重点全在异常输入上：
围栏、前缀说明、键名不一致、YAML 语法错误、越界行号。
正常输入反而是最不容易出问题的那一类。
"""

from codesentry.models import FilePatchInfo, KeyIssue, ReviewResult
from codesentry.report.merge import merge_results
from codesentry.report.schema import (
    build_file_index_map,
    cap_issues,
    load_yaml,
    parse_review,
    sort_issues,
    strip_code_fences,
    validate_line_numbers,
)

# --------------------------------------------------------------------------- #
# 文本清洗与 YAML 解析
# --------------------------------------------------------------------------- #

def test_strip_code_fences_with_language_tag():
    text = "```yaml\nsummary: hi\n```"
    assert "summary: hi" in strip_code_fences(text)
    assert "```" not in strip_code_fences(text)


def test_strip_code_fences_without_closing():
    """模型偶尔会忘记闭合围栏，这时也要能提取出内容。"""
    text = "```yaml\nsummary: hi"
    assert strip_code_fences(text).strip() == "summary: hi"


def test_load_yaml_plain():
    data = load_yaml("summary: ok\nscore: 8")
    assert data == {"summary": "ok", "score": 8}


def test_load_yaml_with_fences():
    data = load_yaml("```yaml\nsummary: ok\n```")
    assert data["summary"] == "ok"


def test_load_yaml_with_prose_prefix_and_suffix():
    """模型加了解释性前后语时，应能截取出 YAML 主体。"""
    text = (
        "Here is my review of the change set.\n\n"
        "summary: 这次改动修正了缓存逻辑\n"
        "score: 7\n"
        "key_issues: []\n"
        "\n"
        "Let me know if you want more detail.\n"
    )
    data = load_yaml(text)
    assert data is not None
    assert data["score"] == 7


def test_load_yaml_with_tabs():
    """YAML 不允许 tab 缩进，但模型偶尔会写。"""
    data = load_yaml("summary: ok\nkey_issues:\n\t- relevant_file: a.py\n")
    assert data is not None


def test_load_yaml_returns_none_for_garbage():
    assert load_yaml("这不是 YAML，只是一句中文。") is None
    assert load_yaml("") is None
    assert load_yaml("   ") is None


def test_load_yaml_handles_single_item_list_wrapper():
    data = load_yaml("- summary: wrapped\n  score: 5\n")
    assert data is not None


# --------------------------------------------------------------------------- #
# 结构化解析
# --------------------------------------------------------------------------- #

FULL_YAML = """
summary: 这次改动引入了重试工具并调整了导出逻辑。
score: 6
risk_level: medium
merge_recommendation: merge_with_caution
effort_to_review: 15 minutes
suggested_tests: 建议补充 rows 为空时的用例。
key_issues:
  - relevant_file: app/report.py
    issue_content: 文件句柄没有关闭，会泄漏。
    start_line: 6
    end_line: 8
    severity: high
  - relevant_file: app/service.py
    issue_content: 未处理查询结果为 None 的情况。
    start_line: 16
    end_line: 16
    severity: critical
file_summaries:
  - relevant_file: app/retry.py
    changes_summary: 新增重试工具，逻辑简单。
"""


def test_parse_review_full_payload():
    result = parse_review(FULL_YAML)
    assert result.score == 6
    assert result.risk_level == "medium"
    assert result.merge_recommendation == "merge_with_caution"
    assert result.effort_to_review == "15 minutes"
    assert len(result.key_issues) == 2
    assert len(result.file_summaries) == 1
    assert result.key_issues[0].severity == "high"
    assert result.key_issues[1].start_line == 16


def test_parse_review_unparseable_falls_back_to_raw_text():
    """完全解析不出来时，必须保留原文而不是抛异常。"""
    raw = "我觉得这段代码还行，但有几处可以改进。"
    result = parse_review(raw)
    assert result.raw_text == raw
    assert result.warnings
    assert result.score is None


def test_parse_review_accepts_score_formats():
    assert parse_review("summary: x\nscore: 7/10").score == 7
    assert parse_review("summary: x\nscore: '8'").score == 8
    assert parse_review("summary: x\nscore: N/A").score is None


def test_parse_review_normalizes_severity():
    yaml_text = """
summary: x
key_issues:
  - relevant_file: a.py
    issue_content: 严重问题
    severity: Critical
  - relevant_file: b.py
    issue_content: 小问题
    severity: 低
  - relevant_file: c.py
    issue_content: 未知级别
    severity: whatever
"""
    result = parse_review(yaml_text)
    severities = [i.severity for i in result.key_issues]
    assert severities == ["critical", "low", "medium"]


def test_parse_review_handles_alternative_key_names():
    """模型有时用 issues/problems 表达同一件事，不能因此丢掉结论。"""
    yaml_text = """
overview: 概览文字
rating: 9
issues:
  - file: src/a.py
    description: 这里有问题
    line: 12
    priority: high
"""
    result = parse_review(yaml_text)
    assert result.summary == "概览文字"
    assert result.score == 9
    assert len(result.key_issues) == 1
    issue = result.key_issues[0]
    assert issue.relevant_file == "src/a.py"
    assert issue.severity == "high"
    assert issue.start_line == 12


def test_parse_review_unwraps_single_key_wrapper():
    yaml_text = """
Review:
  summary: 被包了一层
  score: 4
"""
    result = parse_review(yaml_text)
    assert result.summary == "被包了一层"
    assert result.score == 4


def test_parse_review_file_summary_as_mapping():
    yaml_text = """
summary: x
file_summaries:
  a.py: 改了缓存
  b.py: 新增工具
"""
    result = parse_review(yaml_text)
    assert {s.relevant_file for s in result.file_summaries} == {"a.py", "b.py"}


def test_parse_review_drops_issue_without_content():
    yaml_text = """
summary: x
key_issues:
  - relevant_file: a.py
"""
    result = parse_review(yaml_text)
    assert result.key_issues == []


# --------------------------------------------------------------------------- #
# 行号校验
# --------------------------------------------------------------------------- #

def make_real_file(name: str, content: str) -> FilePatchInfo:
    info = FilePatchInfo(filename=name)
    info.head_file = content
    return info


def test_validate_line_numbers_accepts_in_range():
    content = "\n".join(f"line{i}" for i in range(1, 21))
    files = [make_real_file("a.py", content)]
    result = ReviewResult(key_issues=[
        KeyIssue(relevant_file="a.py", issue_content="x", start_line=5, end_line=8)
    ])
    validate_line_numbers(result, files)
    assert result.key_issues[0].line_verified is True
    assert result.key_issues[0].start_line == 5


def test_validate_line_numbers_clears_out_of_range():
    """越界行号一定要清掉 —— 错误行号比没有行号更伤信任。"""
    content = "\n".join(f"line{i}" for i in range(1, 11))
    files = [make_real_file("a.py", content)]
    result = ReviewResult(key_issues=[
        KeyIssue(relevant_file="a.py", issue_content="x", start_line=999, end_line=1000)
    ])
    validate_line_numbers(result, files)
    issue = result.key_issues[0]
    assert issue.start_line is None
    assert issue.end_line is None
    assert issue.line_verified is False
    assert result.warnings and "越界" in result.warnings[0]


def test_validate_line_numbers_marks_unverifiable_without_file_content():
    """拿不到文件全文（纯 diff 模式）时不做判断，只标未验证。"""
    files = [FilePatchInfo(filename="a.py")]        # head_file 为空
    result = ReviewResult(key_issues=[
        KeyIssue(relevant_file="a.py", issue_content="x", start_line=3, end_line=3)
    ])
    validate_line_numbers(result, files)
    issue = result.key_issues[0]
    assert issue.line_verified is False
    assert issue.start_line == 3                    # 行号保留，不做无依据的否定
    assert not result.warnings


def test_validate_line_numbers_rewrites_model_path_variants():
    """模型常把路径写得不一致，校验时应纠正成 diff 里的真实路径。"""
    content = "\n".join(f"line{i}" for i in range(1, 21))
    files = [make_real_file("src/deep/module.py", content)]
    result = ReviewResult(key_issues=[
        KeyIssue(relevant_file="module.py", issue_content="x", start_line=4, end_line=4),
        KeyIssue(relevant_file="./src/deep/module.py", issue_content="y", start_line=6, end_line=6),
    ])
    validate_line_numbers(result, files)
    assert all(i.relevant_file == "src/deep/module.py" for i in result.key_issues)
    assert all(i.line_verified for i in result.key_issues)


def test_validate_line_numbers_handles_missing_file():
    files = [make_real_file("a.py", "x")]
    result = ReviewResult(key_issues=[
        KeyIssue(relevant_file="nonexistent.py", issue_content="x", start_line=1, end_line=1)
    ])
    validate_line_numbers(result, files)
    assert result.key_issues[0].line_verified is False


def test_file_index_map_builds_multiple_keys():
    info = FilePatchInfo(filename="src/a/b.py", base_path="src/a/old_b.py")
    index = build_file_index_map([info])
    assert index["src/a/b.py"] is info
    assert index["b.py"] is info
    assert index["src/a/old_b.py"] is info


# --------------------------------------------------------------------------- #
# 排序与截断
# --------------------------------------------------------------------------- #

def test_sort_issues_by_severity():
    result = ReviewResult(key_issues=[
        KeyIssue(relevant_file="a.py", issue_content="low", severity="low"),
        KeyIssue(relevant_file="b.py", issue_content="crit", severity="critical"),
        KeyIssue(relevant_file="c.py", issue_content="med", severity="medium"),
    ])
    sort_issues(result)
    assert [i.severity for i in result.key_issues] == ["critical", "medium", "low"]


def test_cap_issues_records_warning():
    result = ReviewResult(key_issues=[
        KeyIssue(relevant_file=f"{i}.py", issue_content="x", severity="low") for i in range(10)
    ])
    cap_issues(result, 3)
    assert len(result.key_issues) == 3
    assert result.warnings and "截断" in result.warnings[0]


def test_cap_issues_noop_when_under_limit():
    result = ReviewResult(key_issues=[
        KeyIssue(relevant_file="a.py", issue_content="x")
    ])
    cap_issues(result, 5)
    assert len(result.key_issues) == 1
    assert not result.warnings


# --------------------------------------------------------------------------- #
# 合并
# --------------------------------------------------------------------------- #

def test_merge_single_result_is_passthrough():
    original = ReviewResult(summary="唯一结果", score=5)
    assert merge_results([original]) is original


def test_merge_takes_worst_score_and_risk():
    a = ReviewResult(summary="A", score=9, risk_level="low")
    b = ReviewResult(summary="B", score=3, risk_level="high")
    merged = merge_results([a, b])
    assert merged.score == 3
    assert merged.risk_level == "high"


def test_merge_deduplicates_identical_issues():
    """同一个问题被两个块各报一次时，只能留一条。"""
    issue_a = KeyIssue(relevant_file="x.py", issue_content="缓存没有失效", severity="high")
    issue_b = KeyIssue(relevant_file="x.py", issue_content="缓存没有失效", severity="high")
    merged = merge_results([
        ReviewResult(summary="A", key_issues=[issue_a]),
        ReviewResult(summary="B", key_issues=[issue_b]),
    ])
    assert len(merged.key_issues) == 1


def test_merge_keeps_distinct_issues():
    merged = merge_results([
        ReviewResult(summary="A", key_issues=[KeyIssue(relevant_file="x.py", issue_content="问题一")]),
        ReviewResult(summary="B", key_issues=[KeyIssue(relevant_file="y.py", issue_content="问题二")]),
    ])
    assert len(merged.key_issues) == 2


def test_merge_union_of_file_summaries_keeps_richer_text():
    from codesentry.models import FileSummary

    merged = merge_results([
        ReviewResult(summary="A", file_summaries=[FileSummary(relevant_file="a.py", changes_summary="短")]),
        ReviewResult(summary="B", file_summaries=[
            FileSummary(relevant_file="a.py", changes_summary="更详细的一段描述"),
            FileSummary(relevant_file="b.py", changes_summary="新文件"),
        ]),
    ])
    by_file = {s.relevant_file: s.changes_summary for s in merged.file_summaries}
    assert by_file["a.py"] == "更详细的一段描述"
    assert len(merged.file_summaries) == 2


def test_merge_accumulates_run_details():
    from codesentry.models import RunDetails

    merged = merge_results([
        ReviewResult(summary="A", run_details=RunDetails(model="m", num_llm_calls=1,
                                                         prompt_tokens=100, completion_tokens=50,
                                                         elapsed_seconds=3.0)),
        ReviewResult(summary="B", run_details=RunDetails(model="m", num_llm_calls=1,
                                                         prompt_tokens=200, completion_tokens=80,
                                                         elapsed_seconds=5.0)),
    ])
    details = merged.run_details
    assert details.num_llm_calls == 2
    assert details.prompt_tokens == 300
    assert details.completion_tokens == 130
    # 并行执行下耗时不能相加，取最大值
    assert details.elapsed_seconds == 5.0


def test_merge_aggregates_warnings_with_chunk_labels():
    merged = merge_results([
        ReviewResult(summary="A", warnings=["第一块的告警"]),
        ReviewResult(summary="B", warnings=["第二块的告警"]),
    ])
    assert len(merged.warnings) == 2
    assert all(w.startswith("第") for w in merged.warnings)


def test_merge_all_empty_results():
    merged = merge_results([])
    assert merged.warnings
