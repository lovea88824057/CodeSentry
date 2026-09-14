"""本地 git 仓库作为 diff 来源。

四种模式覆盖了日常绝大部分场景：
    worktree      默认。`git diff HEAD`，即"所有未提交改动"（staged + unstaged）
    staged        `git diff --cached`，只审已经 git add 的内容（配合 pre-commit 用）
    base          `git diff <ref>`，与某分支对比 —— 等价于 PR 的 diff
    commit_range  `git diff A..B`，审指定区间

为什么直接用 subprocess 调 git 而不是 GitPython？
—— 我们要的只是"拿 diff 文本"和"从某个 revision 取文件内容"这两件事，
git 命令行本身就是最稳定的接口。GitPython 会因为工作区状态（比如
detached HEAD、未合并的 index）在边缘情况下抛出难以预测的异常，
而 `git diff` 的行为是确定且可用 `--no-color --no-ext-diff` 完全固定的。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

from codesentry.diff.base import DiffProvider, DiffProviderError
from codesentry.diff.parse import parse_unified_diff
from codesentry.models import DiffBundle, EditType, FilePatchInfo
from codesentry.utils.logger import get_logger

# diff 上下文行数。比默认的 3 行多给一些：审查需要看清函数签名和
# 控制流边界，3 行往往不够，而多出来的 token 开销远小于"看漏上下文"的代价。
DEFAULT_CONTEXT_LINES = 6

# git 输出的上限保护。超大仓库的一次 `git diff` 可能返回几十 MB，
# 这里设一个宽松但存在的上限，避免把内存打满。
MAX_DIFF_BYTES = 40 * 1024 * 1024


class GitLocalProvider(DiffProvider):
    """从本地 git 仓库取 diff。"""

    name = "git-local"

    def __init__(
        self,
        repo_path: Optional[str | Path] = None,
        mode: str = "worktree",
        base_ref: Optional[str] = None,
        commit_range: Optional[str] = None,
        context_lines: int = DEFAULT_CONTEXT_LINES,
    ) -> None:
        self.repo_path = self._resolve_repo_root(repo_path)
        self.mode = mode
        self.base_ref = base_ref
        self.commit_range = commit_range
        self.context_lines = context_lines
        self._validate_mode()

    # ------------------------------------------------------------------ #
    # git 调用基础设施
    # ------------------------------------------------------------------ #

    def _run_git(self, *args: str, check: bool = True) -> str:
        """执行 git 命令并返回 stdout。

        统一加 `-c core.quotepath=false`：不加的话中文文件名会被输出成
        `"\\344\\270\\255.py"` 这种八进制转义，后续所有路径匹配都会失败。
        """
        cmd = ["git", "-c", "core.quotepath=false", *args]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",     # 非 UTF-8 内容不中断流程
                timeout=120,
            )
        except FileNotFoundError as exc:
            raise DiffProviderError(
                "找不到 git 命令。请确认 git 已安装并在 PATH 中。"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise DiffProviderError(f"git 命令超时：{' '.join(cmd)}") from exc

        if check and proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            raise DiffProviderError(f"git 命令失败（退出码 {proc.returncode}）：{stderr or ' '.join(cmd)}")
        return proc.stdout

    @staticmethod
    def _resolve_repo_root(repo_path: Optional[str | Path]) -> Path:
        """确认目标目录在一个 git 工作区内，返回仓库根目录。"""
        start = Path(repo_path).resolve() if repo_path else Path.cwd().resolve()
        if not start.exists():
            raise DiffProviderError(f"路径不存在：{start}")
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=str(start),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        except FileNotFoundError as exc:
            raise DiffProviderError("找不到 git 命令。请确认 git 已安装并在 PATH 中。") from exc
        if proc.returncode != 0:
            raise DiffProviderError(
                f"{start} 不是 git 仓库（或其子目录）。\n"
                f"提示：可以用 `codesentry review --diff-file <file.diff>` 审查现成的 diff 文件。"
            )
        return Path(proc.stdout.strip())

    def _validate_mode(self) -> None:
        if self.mode == "base" and not self.base_ref:
            raise DiffProviderError("base 模式必须提供 --base <分支名>")
        if self.mode == "commit_range":
            if not self.commit_range or ".." not in self.commit_range:
                raise DiffProviderError("commit_range 模式必须提供形如 A..B 的区间")
        if self.mode not in ("worktree", "staged", "base", "commit_range"):
            raise DiffProviderError(f"未知的 diff 模式：{self.mode}")

    # ------------------------------------------------------------------ #
    # diff 参数构造
    # ------------------------------------------------------------------ #

    def _diff_args(self) -> list[str]:
        """按模式拼出 `git diff` 的参数。"""
        common = [f"--unified={self.context_lines}", "--no-color", "--no-ext-diff", "--ignore-submodules=dirty"]
        if self.mode == "staged":
            return ["diff", "--cached", *common]
        if self.mode == "base":
            return ["diff", str(self.base_ref), *common]
        if self.mode == "commit_range":
            return ["diff", str(self.commit_range), *common]
        # worktree：优先与 HEAD 比。空仓库（还没有 commit）时 HEAD 不存在，
        # 退化成不带 rev 的 `git diff`（只会显示已 add 的内容），避免直接报错。
        if self._has_head():
            return ["diff", "HEAD", *common]
        return ["diff", *common]

    def _has_head(self) -> bool:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=str(self.repo_path), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
        return proc.returncode == 0

    def _base_rev(self) -> Optional[str]:
        """基准侧的 revision。None 表示"没有基准"（文件视为新增）。"""
        if self.mode in ("worktree", "staged"):
            return "HEAD" if self._has_head() else None
        if self.mode == "base":
            return str(self.base_ref)
        if self.mode == "commit_range":
            return str(self.commit_range).split("..", 1)[0] or None
        return None

    def _head_rev(self) -> Optional[str]:
        """新版本的 revision。None 表示"从磁盘读当前文件"。

        为什么 worktree/base 模式读磁盘而不是 HEAD？
        —— 这两种模式审的就是工作区当前状态，磁盘上的文件才是最准确的
        "新版本"。用 HEAD 会漏掉用户还没提交的后续修改，导致行号对不上。
        """
        if self.mode == "staged":
            # 已 add 的内容在 index 里，磁盘可能还叠了后续未 add 的改动
            return ":0"
        if self.mode == "commit_range":
            return str(self.commit_range).split("..", 1)[-1] or None
        return None

    def describe_target(self) -> str:
        if self.mode == "staged":
            return "已暂存的改动（git diff --cached）"
        if self.mode == "base":
            return f"与 {self.base_ref} 的差异"
        if self.mode == "commit_range":
            return f"提交区间 {self.commit_range}"
        return "未提交的改动（git diff HEAD）"

    # ------------------------------------------------------------------ #
    # 文件内容读取
    # ------------------------------------------------------------------ #

    def _show_file(self, rev: Optional[str], path: str) -> str:
        """取某个 revision 下文件的完整内容。取不到就返回空串。

        特意吞掉异常返回空串：拿不到全文只意味着"放弃行号校验"，
        不应该让整次审查失败。调用方通过 FilePatchInfo.has_head_context 判断。
        """
        if not path:
            return ""
        if rev is None:
            return self._read_disk(path)
        spec = f"{rev}:{path}" if rev != ":0" else f":{path}"
        try:
            proc = subprocess.run(
                ["git", "-c", "core.quotepath=false", "show", spec],
                cwd=str(self.repo_path), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=60,
            )
        except (subprocess.SubprocessError, OSError):
            return ""
        if proc.returncode != 0:
            return ""
        return proc.stdout

    def _read_disk(self, path: str) -> str:
        """读工作区里的文件。文件不存在（例如已被删除）返回空串。"""
        target = self.repo_path / path
        if not target.is_file():
            return ""
        try:
            return target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            get_logger().debug(f"读取文件失败 {path}: {exc}")
            return ""

    def supports_line_validation(self) -> bool:
        return True

    def attach_file_contents(self, bundle: DiffBundle) -> DiffBundle:
        """给每个文件补上旧/新版本全文。

        这是本地模式相对平台模式的核心优势：head_file 直接读磁盘，
        零额外 API 调用，于是行号校验几乎无成本地变成默认能力。
        """
        base_rev = self._base_rev()
        head_rev = self._head_rev()
        for info in bundle.files:
            if info.edit_type is EditType.ADDED:
                info.base_file = ""
            else:
                info.base_file = self._show_file(base_rev, info.base_path or info.filename)

            if info.edit_type is EditType.DELETED:
                info.head_file = ""
            else:
                info.head_file = self._show_file(head_rev, info.filename)
        return bundle

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #

    def get_bundle(self) -> DiffBundle:
        diff_text = self._run_git(*self._diff_args())
        if len(diff_text.encode("utf-8", errors="replace")) > MAX_DIFF_BYTES:
            raise DiffProviderError(
                f"diff 体积超过 {MAX_DIFF_BYTES // (1024 * 1024)}MB，请缩小审查范围"
                "（例如用 --commit-range 指定单个提交，或 --base 与更近的分支对比）。"
            )

        files = parse_unified_diff(diff_text)
        bundle = DiffBundle(
            title=self._build_title(),
            description="",
            commit_messages=self._get_commit_messages(),
            files=files,
            provider=self.name,
            source_ref=self.describe_target(),
        )
        return self.attach_file_contents(bundle)

    def _build_title(self) -> str:
        """用"分支名 + 最新提交标题"拼一个人话标题。"""
        branch = "unknown"
        try:
            branch = self._run_git("rev-parse", "--abbrev-ref", "HEAD", check=False).strip() or "unknown"
            if branch == "HEAD":  # detached HEAD 状态
                branch = self._run_git("rev-parse", "--short", "HEAD", check=False).strip() or "detached"
        except DiffProviderError:
            pass
        subject = ""
        if self._has_head():
            subject = self._run_git("log", "-1", "--pretty=%s", check=False).strip()
        if subject:
            return f"[{branch}] {subject}"
        return f"[{branch}] 本地改动"

    def _get_commit_messages(self) -> str:
        """取最近若干条提交信息，给模型提供意图线索。

        为什么提交信息对审查有价值？—— "为什么改"往往不在 diff 里。
        知道这是 fix 还是 refactor，能让模型对同一段代码给出不同结论。
        """
        if not self._has_head():
            return ""
        rev_range = ""
        if self.mode == "commit_range":
            rev_range = str(self.commit_range)
        elif self.mode == "base":
            rev_range = f"{self.base_ref}..HEAD"
        args = ["log", "--no-merges", "--pretty=format:%h %s"]
        if rev_range:
            args.append(rev_range)
        else:
            args.append("-10")
        out = self._run_git(*args, check=False)
        return out.strip()


def make_file_patch_info_stub(filename: str) -> FilePatchInfo:
    """构造一个占位 FilePatchInfo。测试与边界场景用。"""
    from codesentry.diff.parse import guess_language

    return FilePatchInfo(filename=filename, language=guess_language(filename))
