"""从 diff 文件 / stdin 构造输入。

这是 CodeSentry 最"轻"的入口：不需要在 git 仓库里，不需要任何平台凭证，
把一段 diff 管道进来就能审。适合三种场景：
1. 从 GitHub PR 页面复制 `.diff` 保存到本地再看
2. 在 CI 里 `git diff > changes.diff` 后交给 agent 审查
3. 离线复现某个 bug 的补丁

可选地传入 `--repo <path>`，就能借用工作区里的真实文件补齐 head_file，
把行号校验能力也打开 —— 纯 diff 本身是没有"新文件全文"的。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from codesentry.diff.base import DiffProvider, DiffProviderError
from codesentry.diff.parse import parse_unified_diff
from codesentry.models import DiffBundle, EditType
from codesentry.utils.logger import get_logger


class FromFileProvider(DiffProvider):
    """从文件或 stdin 读取 unified diff。"""

    name = "diff-file"

    def __init__(
        self,
        diff_file: Optional[str | Path] = None,
        use_stdin: bool = False,
        repo_path: Optional[str | Path] = None,
        title: Optional[str] = None,
    ) -> None:
        if diff_file is None and not use_stdin:
            raise DiffProviderError("必须指定 diff 文件或使用 stdin")
        self.diff_file = Path(diff_file) if diff_file is not None else None
        self.use_stdin = use_stdin
        # repo_path 只用于"补齐文件全文"，不用于取 diff
        self.repo_path = Path(repo_path).resolve() if repo_path else None
        self.title = title
        if self.use_stdin and self.diff_file is not None:
            self.name = "stdin"

    def describe_target(self) -> str:
        if self.use_stdin:
            return "标准输入中的 diff"
        return f"文件 {self.diff_file}"

    def _read_diff(self) -> str:
        if self.use_stdin:
            # 显式用 utf-8：Windows 控制台默认 GBK，diff 里出现非 ASCII
            # 字符（比如中文注释）时会直接解码失败。
            data = sys.stdin.buffer.read()
            try:
                return data.decode("utf-8")
            except UnicodeDecodeError:
                get_logger().warning("stdin 内容不是合法 UTF-8，已按容错方式解码")
                return data.decode("utf-8", errors="replace")
        assert self.diff_file is not None
        if not self.diff_file.is_file():
            raise DiffProviderError(f"diff 文件不存在：{self.diff_file}")
        try:
            return self.diff_file.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise DiffProviderError(
                f"{self.diff_file} 不是 UTF-8 文本，无法解析为 unified diff"
            ) from exc

    def supports_line_validation(self) -> bool:
        return self.repo_path is not None

    def attach_file_contents(self, bundle: DiffBundle) -> DiffBundle:
        """用工作区文件补齐新版本全文。

        刻意只补 head_file：diff 文件里没有基准版本的快照，
        而 base_file 在本项目的用法里仅作参考，缺了不影响行号校验。
        """
        if self.repo_path is None:
            return bundle
        for info in bundle.files:
            if info.edit_type is EditType.DELETED:
                info.head_file = ""
                continue
            target = self.repo_path / info.filename
            if target.is_file():
                try:
                    info.head_file = target.read_text(encoding="utf-8", errors="replace")
                except OSError as exc:
                    get_logger().debug(f"读取 {info.filename} 失败：{exc}")
        return bundle

    def get_bundle(self) -> DiffBundle:
        text = self._read_diff()
        if not text.strip():
            raise DiffProviderError("diff 内容为空，没有可审查的改动")
        files = parse_unified_diff(text)
        bundle = DiffBundle(
            title=self.title or self._default_title(),
            description="",
            commit_messages="",
            files=files,
            provider=self.name,
            source_ref=self.describe_target(),
        )
        return self.attach_file_contents(bundle)

    def _default_title(self) -> str:
        if self.use_stdin:
            return "[stdin] 外部传入的 diff"
        assert self.diff_file is not None
        return f"[{self.diff_file.name}] 外部 diff 文件"
