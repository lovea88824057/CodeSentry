"""文件输出。

默认写到 `.codesentry/review-<时间戳>.md`，而不是固定的 review.md。
理由：审查是高频动作，固定文件名会让上一次的结果被无声覆盖 ——
用户回头想对比"昨天的报告和今天有什么不同"时就找不到了。
带时间戳虽然文件名长，但换来的是完整的可追溯性。

`--output` 显式指定路径时则严格按用户要求写（不做时间戳处理），
因为那时用户多半是在 CI 里用固定路径做产物。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from codesentry.output.base import Publisher, PublishContext
from codesentry.utils.logger import get_logger

DEFAULT_OUTPUT_DIR = ".codesentry"


class FilePublisher(Publisher):
    """把报告写入文件。"""

    def __init__(
        self,
        output_path: str | Path | None = None,
        json_output_path: str | Path | None = None,
        output_dir: str | Path = DEFAULT_OUTPUT_DIR,
        timestamped: bool = True,
    ) -> None:
        self.output_path = Path(output_path) if output_path else None
        self.json_output_path = Path(json_output_path) if json_output_path else None
        self.output_dir = Path(output_dir)
        # 用户显式给了路径就不再自动加时间戳，尊重其意图
        self.timestamped = timestamped and output_path is None

    def publish(self, context: PublishContext) -> None:
        markdown_path = self._resolve_markdown_path()
        if markdown_path is not None and context.markdown:
            if self._write(markdown_path, context.markdown):
                get_logger().info(f"报告已写入：{markdown_path}")

        if self.json_output_path is not None and context.json_text:
            if self._write(self.json_output_path, context.json_text):
                get_logger().info(f"JSON 已写入：{self.json_output_path}")

    def _resolve_markdown_path(self) -> Path | None:
        """决定 Markdown 到底写到哪。

        返回 None 表示"不写文件" —— 纯终端模式（既没给 --output，
        也不希望产生副产品）时应当如此。
        """
        if self.output_path is not None:
            return self.output_path
        if self.timestamped:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            return self.output_dir / f"review-{stamp}.md"
        return None

    @staticmethod
    def _write(path: Path, content: str) -> bool:
        """写文件。自动建父目录，失败只告警不中断。"""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # 显式 utf-8：Windows 默认编码是 GBK，写中文报告会乱码
            path.write_text(content, encoding="utf-8")
            return True
        except OSError as exc:
            get_logger().error(f"写入 {path} 失败：{exc}")
            return False
