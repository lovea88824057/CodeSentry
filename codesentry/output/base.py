"""输出层抽象。

把"报告往哪送"独立出来，是为了让同一份审查结论能同时流向终端、
文件和（未来）PR 评论，而不需要 agent 层知道任何一方的细节。

这个分层在二期接入 GitHub 时会体现出价值：那时只需新增一个
`GitHubPublisher`，把 Markdown 发成 PR 评论，`tools/review.py` 一行不用改。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from codesentry.models import ReviewResult


@dataclass
class PublishContext:
    """发布时需要的元信息。

    单独打包成对象而不是散着一堆参数传，是因为输出目标会越来越多，
    每加一个目标就改一次函数签名是不可持续的。
    """

    title: str = ""
    source_ref: str = ""
    markdown: str = ""
    json_text: str = ""
    total_files: int = 0
    active_files: int = 0
    result: ReviewResult | None = None
    extra: dict = field(default_factory=dict)


class Publisher(ABC):
    """输出目标。"""

    @abstractmethod
    def publish(self, context: PublishContext) -> None:
        """把报告送出去。"""
        raise NotImplementedError
