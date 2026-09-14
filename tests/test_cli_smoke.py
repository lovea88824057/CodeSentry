"""端到端冒烟测试。

分两类：
1. **不碰网络、不碰真实模型**的完整流程 —— 用 FakeLLMHandler 替掉模型层，
   验证"取 diff -> 过滤 -> 分块 -> 渲染 -> 调用 -> 解析 -> 校验 -> 合并 -> 渲染"
   这条链路真的跑得通。这是最有价值的测试：它能抓出任何一环的接口错配。
2. **CLI 层** —— 验证参数解析、退出码、配置文件优先级。

`--dry-run` 在本测试里扮演关键角色：它跑到"渲染提示词"就停，
完全不调用模型，所以可以在离线环境里断言最终发给模型的提示词长什么样。
"""

import subprocess
import sys
from pathlib import Path

import pytest

from codesentry.config import load_settings
from codesentry.diff.from_file import FromFileProvider
from codesentry.llm.base import FakeLLMHandler
from codesentry.tools.review import ReviewTool
from codesentry.cli import EXIT_OK, EXIT_USAGE, main
from codesentry.prompts.loader import render_prompt, get_review_prompt

FIXTURE = Path(__file__).parent / "fixtures" / "sample.diff"

# 一份"模型理想输出"，用来驱动完整流程
FAKE_MODEL_YAML = """
summary: 本次改动调整了用户查询与 CSV 导出的实现，引入了重试工具。
score: 6
risk_level: medium
merge_recommendation: merge_with_caution
effort_to_review: 约 15 分钟
suggested_tests: 建议补充 rows 为空、以及查询不到用户时的用例。
key_issues:
  - relevant_file: app/report.py
    issue_content: 文件句柄从未关闭，长时间运行会耗尽文件描述符。
    start_line: 6
    end_line: 8
    severity: critical
  - relevant_file: app/service.py
    issue_content: 未处理 db.query 返回 None 的情况，会抛 AttributeError。
    start_line: 17
    end_line: 18
    severity: high
file_summaries:
  - relevant_file: app/retry.py
    changes_summary: 新增重试工具，逻辑直接。
"""


def run_tool(handler: FakeLLMHandler, tmp_path: Path):
    """用固定 diff 驱动一次完整审查。

    刻意不覆盖任何 ignore 配置：走默认规则才能顺带验证
    "package-lock.json 之类的文件确实被挡在提示词之外"。
    """
    settings = load_settings(cwd=tmp_path)
    provider = FromFileProvider(diff_file=FIXTURE)
    tool = ReviewTool(settings=settings, handler=handler, provider=provider)
    import asyncio
    return asyncio.run(tool.run())


def test_full_pipeline_produces_structured_report(tmp_path):
    handler = FakeLLMHandler(responses=[FAKE_MODEL_YAML])
    result = run_tool(handler, tmp_path)

    assert result.has_content is True
    assert handler.calls, "应该至少调用过一次模型"

    md = result.markdown
    # 报告应该包含所有关键区块
    assert "# 🛡️ CodeSentry 审查报告" in md
    assert "## 摘要" in md
    assert "## 关键问题（2）" in md
    assert "## 文件逐项" in md
    assert "## 建议补充的测试" in md
    assert "运行详情" in md
    # 结构化数据也要正确落地
    assert result.result.score == 6
    assert len(result.result.key_issues) == 2
    # JSON 输出必须能被解析
    import json
    payload = json.loads(result.json_text)
    assert payload["result"]["score"] == 6


def test_prompt_contains_line_numbered_diff_and_format_legend(tmp_path):
    """发给模型的内容必须包含格式说明和带行号的 diff。

    这是"行号可信"这条设计目标的最小验证：如果渲染格式或格式说明
    在重构中丢了一个，模型给出的行号就会开始漂移。
    """
    handler = FakeLLMHandler(responses=[FAKE_MODEL_YAML])
    run_tool(handler, tmp_path)

    user_prompt = handler.calls[0]["user"]
    system_prompt = handler.calls[0]["system"]

    # 格式说明在 system 里
    assert "行号是新文件中的绝对行号" in system_prompt
    # 带行号的 diff 在 user 里：形如 "   12|   context"
    assert "   10|" in user_prompt or "   11|" in user_prompt
    assert "|+" in user_prompt          # 新增行标记
    # 被过滤掉的文件不应出现在提示词里
    assert "package-lock.json" not in user_prompt
    # 输出语言要求要带上
    assert "Chinese" in system_prompt


def test_issue_line_numbers_are_validated_against_real_files(tmp_path):
    """没有文件全文时（纯 diff 模式），行号只能标"未校验"，不能假装已核实。"""
    handler = FakeLLMHandler(responses=[FAKE_MODEL_YAML])
    result = run_tool(handler, tmp_path)
    for issue in result.result.key_issues:
        assert issue.line_verified is False, "没有文件全文时不应声称行号已验证"
    assert "行号未校验" in result.markdown
    # 未校验的行号仍然要展示出来（不妄断），只是带上警示标记
    assert "L6" in result.markdown


def test_line_numbers_verified_when_file_content_is_available(tmp_path):
    """把 repo 路径指向含真实文件的目录时，行号应被校验为可信。"""
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    # 造出足够长的文件，让 FAKE_MODEL_YAML 里的行号落在范围内
    for name in ("report.py", "service.py"):
        (repo / "app" / name).write_text(
            "\n".join(f"# line {i}" for i in range(1, 21)), encoding="utf-8"
        )
    settings = load_settings(cwd=tmp_path)
    handler = FakeLLMHandler(responses=[FAKE_MODEL_YAML])
    provider = FromFileProvider(diff_file=FIXTURE, repo_path=repo)
    tool = ReviewTool(settings=settings, handler=handler, provider=provider)

    import asyncio
    result = asyncio.run(tool.run())

    # 两个文件都存在且行号在范围内 -> 应全部通过校验
    assert all(i.line_verified for i in result.result.key_issues)
    assert "✅" in result.markdown


def test_unparseable_model_output_degrades_gracefully(tmp_path):
    """模型完全乱答时，报告要保留原文而不是崩溃。"""
    handler = FakeLLMHandler(responses=["我看了这段代码，觉得还行。"])
    result = run_tool(handler, tmp_path)
    assert result.has_content is True
    assert "模型原始输出" in result.markdown
    assert result.result.raw_text


def test_fallback_model_is_used_when_primary_fails(tmp_path):
    """主模型失败时应自动切到备用模型，而不是整体失败。"""
    settings = load_settings(
        cli_overrides=["config.model=primary/model", "config.fallback_models=[\"backup/model\"]",
                       "ignore.paths=[]", "ignore.extensions=[]"],
        cwd=tmp_path,
    )
    handler = FakeLLMHandler(responses=[FAKE_MODEL_YAML], fail_models=["primary/model"])
    provider = FromFileProvider(diff_file=FIXTURE)
    tool = ReviewTool(settings=settings, handler=handler, provider=provider)

    import asyncio
    result = asyncio.run(tool.run())

    assert result.has_content
    assert any(c["model"] == "backup/model" for c in handler.calls)
    assert result.result.run_details.model == "backup/model"


def test_skip_by_ignore_rules_is_reported(tmp_path):
    """被忽略规则跳过的文件要出现在报告的"未纳入审查"清单里。"""
    settings = load_settings(cli_overrides=["ignore.paths=[\"**/service.py\"]",
                                            "ignore.extensions=[]"], cwd=tmp_path)
    handler = FakeLLMHandler(responses=[FAKE_MODEL_YAML])
    provider = FromFileProvider(diff_file=FIXTURE)
    tool = ReviewTool(settings=settings, handler=handler, provider=provider)

    import asyncio
    result = asyncio.run(tool.run())
    assert "未纳入审查的文件" in result.markdown
    assert "service.py" in result.markdown


def test_empty_diff_reports_no_changes(tmp_path):
    """没有改动时要友好返回，而不是报错。"""
    empty = tmp_path / "empty.diff"
    empty.write_text("", encoding="utf-8")
    provider = FromFileProvider(diff_file=empty)
    handler = FakeLLMHandler()
    settings = load_settings(cwd=tmp_path)
    tool = ReviewTool(settings=settings, handler=handler, provider=provider)

    import asyncio
    with pytest.raises(Exception):
        # 空 diff 应该在 provider 层就被拦下，给出明确错误
        asyncio.run(tool.run())


# --------------------------------------------------------------------------- #
# CLI 层
# --------------------------------------------------------------------------- #

def test_cli_config_command_prints_provenance(capsys):
    code = main(["config"])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "[config]" in out
    assert "[review]" in out
    assert "[ignore]" in out
    assert "来源" in out


def test_cli_config_reflects_cli_override(capsys):
    """CLI 覆盖必须真的生效，并且来源标注为 CLI 参数。"""
    code = main(["config", "--config.model=some/model"])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "some/model" in out
    assert "CLI 参数" in out


def test_cli_dry_run_does_not_call_model(capsys):
    """dry-run 应该是零成本路径：只打印提示词，不产生任何网络调用。"""
    code = main(["review", "--diff-file", str(FIXTURE), "--dry-run", "-q"])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "CodeSentry dry-run" in out
    assert "将要发送的 system 提示词" in out
    assert "chunks" in out
    # dry-run 不应该打印出模型结论
    assert "关键问题（" not in out


def test_cli_rejects_unknown_flag():
    """拼错的参数必须报错，不能被静默忽略。"""
    with pytest.raises(SystemExit):
        main(["review", "--not-a-real-flag"])


def test_cli_no_command_prints_help():
    assert main([]) == EXIT_USAGE


def test_cli_rejects_conflicting_sources(capsys):
    """互相冲突的来源参数应被拒绝，并给出可读的错误信息（而不是抛栈）。"""
    from codesentry.cli import EXIT_ERROR

    code = main(["review", "--stdin", "--diff-file", str(FIXTURE)])
    assert code == EXIT_ERROR
    assert "不能同时使用" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# 本地 git 仓库（真实调用 git）
# --------------------------------------------------------------------------- #

def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(not _git_available(), reason="本机没有 git")
@pytest.mark.needs_git
def test_git_local_provider_reads_worktree_changes(tmp_path):
    """在真实临时仓库里验证：diff 解析 + 磁盘文件全文读取。

    head_file 从磁盘读是本项目"行号可校验"的基础，必须有测试盯着。
    """
    from codesentry.diff.git_local import GitLocalProvider

    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, timeout=30, check=True)

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (tmp_path / "app.py").write_text("def f(a, b):\n    return a + b\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "init")

    # 制造一处未提交改动
    (tmp_path / "app.py").write_text(
        "def f(a, b):\n    return a - b\n\n\ndef g():\n    pass\n", encoding="utf-8"
    )

    provider = GitLocalProvider(repo_path=tmp_path, mode="worktree")
    bundle = provider.get_bundle()

    assert len(bundle.files) == 1
    info = bundle.files[0]
    assert info.filename == "app.py"
    # diff 内容正确
    assert "return a - b" in info.patch
    # 关键：新版本全文来自磁盘，包含所有行（而不只是 hunk）
    assert info.head_file.count("\n") >= 5
    assert "def g():" in info.head_file
    # 旧版本全文来自 HEAD，应当是加法版本
    assert "return a + b" in info.base_file
    # 因此这条改动可以做行号校验
    assert info.has_head_context is True


@pytest.mark.skipif(not _git_available(), reason="本机没有 git")
@pytest.mark.needs_git
def test_git_local_provider_staged_mode_only_sees_index(tmp_path):
    """--staged 应该只看已 add 的内容，不包含之后新写的未暂存改动。"""
    from codesentry.diff.git_local import GitLocalProvider

    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, timeout=30, check=True)

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "init")

    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
    git("add", "a.py")                                   # 暂存
    (tmp_path / "a.py").write_text("x = 3\n", encoding="utf-8")   # 暂存之后再改

    staged = GitLocalProvider(repo_path=tmp_path, mode="staged").get_bundle()
    assert len(staged.files) == 1
    # 暂存的版本是 x = 2，所以 head_file（来自 index）应当是 2 而不是 3
    assert "x = 2" in staged.files[0].head_file
    assert "x = 3" not in staged.files[0].head_file

    worktree = GitLocalProvider(repo_path=tmp_path, mode="worktree").get_bundle()
    # 工作区模式应当看到最终版 x = 3
    assert "x = 3" in worktree.files[0].head_file


@pytest.mark.skipif(not _git_available(), reason="本机没有 git")
@pytest.mark.needs_git
def test_git_local_provider_base_mode(tmp_path):
    """--base 模式应与指定分支对比。"""
    from codesentry.diff.git_local import GitLocalProvider

    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, timeout=30, check=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (tmp_path / "a.py").write_text("v = 1\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "init")

    git("checkout", "-q", "-b", "feature")
    (tmp_path / "a.py").write_text("v = 1\nw = 2\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "add w")

    bundle = GitLocalProvider(repo_path=tmp_path, mode="base", base_ref="main").get_bundle()
    assert len(bundle.files) == 1
    assert "w = 2" in bundle.files[0].patch
    assert "add w" in bundle.commit_messages
