"""Diff 来源层：把各种输入统一成 DiffBundle。"""

from codesentry.diff.base import DiffProvider, DiffProviderError
from codesentry.diff.from_file import FromFileProvider
from codesentry.diff.git_local import GitLocalProvider

__all__ = ["DiffProvider", "DiffProviderError", "GitLocalProvider", "FromFileProvider"]
