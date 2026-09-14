"""diff 解析测试。

解析层是全项目最需要"钉死"的一块：解析错一个字节，后面所有行号都会错。
所以这里的断言写得比较细，包括具体的行号值，而不只是文件个数。
"""

from pathlib import Path

import pytest

from codesentry.config import IgnoreSection
from codesentry.diff.parse import (
    count_changed_lines,
    guess_language,
    iter_hunks,
    parse_unified_diff,
    split_file_blocks,
)
from codesentry.models import EditType
from codesentry.utils.filter import apply_ignore_rules, matches_any

FIXTURE = Path(__file__).parent / "fixtures" / "sample.diff"


@pytest.fixture
def diff_text() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture
def files(diff_text):
    return parse_unified_diff(diff_text)


def test_block_splitting(diff_text):
    """每个文件应该被切成独立的块。"""
    blocks = split_file_blocks(diff_text)
    assert len(blocks) == 8
    assert all(b for b in blocks)


def test_filenames_and_order(files):
    """文件名按 diff 顺序解析，且路径前缀 a/ b/ 被正确剥掉。"""
    names = [f.filename for f in files]
    assert names[:7] == [
        "app/service.py",
        "app/report.py",
        "app/retry.py",
        "legacy/old_util.py",
        "docs/handbook.md",
        "package-lock.json",
        "assets/logo.png",
    ]


def test_edit_types(files):
    """新增 / 删除 / 重命名 / 修改四类都要识别正确。"""
    by_name = {f.filename: f for f in files}
    assert by_name["app/service.py"].edit_type is EditType.MODIFIED
    assert by_name["app/retry.py"].edit_type is EditType.ADDED
    assert by_name["legacy/old_util.py"].edit_type is EditType.DELETED
    assert by_name["docs/handbook.md"].edit_type is EditType.RENAMED
    # 重命名同时要记住旧路径，否则后续取 base 内容会找错文件
    assert by_name["docs/handbook.md"].base_path == "docs/guide.md"


def test_binary_file_marked_skipped(files):
    """二进制文件应在解析阶段就被标记跳过，不该浪费 token。"""
    by_name = {f.filename: f for f in files}
    logo = by_name["assets/logo.png"]
    assert logo.skipped is True
    assert "二进制" in logo.skip_reason


def test_rename_without_content_marked_skipped(files):
    """纯重命名没有内容改动，也没有审查价值。"""
    by_name = {f.filename: f for f in files}
    renamed = by_name["docs/handbook.md"]
    assert renamed.skipped is True
    assert "无内容改动" in renamed.skip_reason


def test_language_guessing(files):
    by_name = {f.filename: f for f in files}
    assert by_name["app/service.py"].language == "Python"
    assert by_name["package-lock.json"].language == "JSON"


def test_guess_language_edge_cases():
    assert guess_language("Dockerfile") == "Dockerfile"
    assert guess_language("src/Makefile") == "Makefile"
    assert guess_language("a/b/c.pyi") == "Python"
    assert guess_language("no_extension") is None
    assert guess_language("kernel.cu") == "CUDA"


def test_changed_line_counts(files):
    by_name = {f.filename: f for f in files}
    added, removed = count_changed_lines(by_name["app/service.py"].patch)
    assert added > 0 and removed > 0


# --------------------------------------------------------------------------- #
# 行号渲染 —— 这是最关键的一组断言
# --------------------------------------------------------------------------- #

def test_hunk_iterator_reports_new_start_line(files):
    """hunk 迭代器要给出正确的新文件起始行号。"""
    service = next(f for f in files if f.filename == "app/service.py")
    hunks = list(iter_hunks(service.patch))
    assert len(hunks) == 1
    header, old_start, new_start, body = hunks[0]
    assert header.startswith("@@ -8,10 +8,13 @@")
    assert old_start == 8
    assert new_start == 8
    assert body


def test_line_number_rendering_matches_new_file_positions(files):
    """渲染出的行号必须与"按新文件逐行推进"的结果一致。

    构造方式：手工模拟一遍行号推进规则，再和渲染结果比对。
    这是防止"渲染层与解析层各写一套逻辑导致错位"的守门测试。
    """
    from codesentry.context.hunk import render_hunk_body

    service = next(f for f in files if f.filename == "app/service.py")
    rendered = render_hunk_body(service.patch)
    lines = rendered.splitlines()

    # 逐行重建期望的行号，只对新文件的上下文行/新增行计数
    expected_numbers: list[int | None] = []
    _, _, new_start, body = list(iter_hunks(service.patch))[0]
    current = new_start
    for raw in body:
        if raw.startswith("-"):
            expected_numbers.append(None)
        else:
            expected_numbers.append(current)
            current += 1

    # 渲染结果里第一行是 hunk 头，之后逐行对应
    assert lines[0].startswith("@@")
    rendered_numbers = []
    for line in lines[1:]:
        prefix = line[:5].strip()
        rendered_numbers.append(int(prefix) if prefix else None)
    assert rendered_numbers == expected_numbers


def test_added_file_line_numbers_start_at_one(files):
    """新增文件的第一行应该是第 1 行。"""
    from codesentry.context.hunk import render_hunk_body

    retry = next(f for f in files if f.filename == "app/retry.py")
    rendered = render_hunk_body(retry.patch)
    lines = [l for l in rendered.splitlines() if not l.startswith("@@")]
    assert lines[0].strip().startswith("1|")
    assert '"""重试工具。"""' in lines[0]


def test_deleted_lines_have_no_new_line_number(files):
    """被删除的行不应该带新文件行号（它们只存在于旧文件里）。"""
    from codesentry.context.hunk import render_hunk_body

    service = next(f for f in files if f.filename == "app/service.py")
    rendered = render_hunk_body(service.patch)
    deleted = [l for l in rendered.splitlines() if "|-" in l]
    assert deleted, "应当存在被删除的行"
    for line in deleted:
        assert line[:5].strip() == "", f"删除行的行号列应为空：{line!r}"


# --------------------------------------------------------------------------- #
# 过滤规则
# --------------------------------------------------------------------------- #

def test_glob_matching_semantics():
    # `**` 跨目录
    assert matches_any("a/b/c/node_modules/x/y.js", ["**/node_modules/**"])
    # 不带斜杠的模式也能命中 basename
    assert matches_any("deep/nested/uv.lock", ["*.lock"])
    # `*` 不跨目录
    assert matches_any("dist/app.min.js", ["dist/*.js"])
    assert matches_any("dist/app.min.js", ["**/*.min.js"])


def test_glob_does_not_over_match():
    assert matches_any("src/node_modules_helper.py", ["**/node_modules/**"]) is None
    assert matches_any("src/app.py", ["**/*.lock"]) is None


def test_apply_ignore_rules_marks_but_keeps_all(files):
    """过滤只做标记、不删元素 —— 后续报告要能列出"跳过了什么"。"""
    ignore = IgnoreSection(paths=["**/package-lock.json"], extensions=[".md"], max_file_tokens=99999)
    result = apply_ignore_rules(files, ignore)
    assert len(result) == len(files)                    # 一个都没丢
    by_name = {f.filename: f for f in result}
    assert by_name["package-lock.json"].skipped is True
    assert "忽略规则" in by_name["package-lock.json"].skip_reason
    # 未命中规则的文件不受影响
    assert by_name["app/service.py"].skipped is False
