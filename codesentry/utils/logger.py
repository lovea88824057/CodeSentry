"""日志与终端输出工具。

用标准库 logging 而不是 loguru：本项目只有 2000 多行，
不值得为一个日志后端增加依赖和一层抽象。输出目标只有一个（stderr），
格式也只有一个（人类可读），标准库完全够用。
"""

from __future__ import annotations

import logging
import sys

_LOGGER_NAME = "codesentry"
_configured = False


class _ColorFormatter(logging.Formatter):
    """给不同级别上色。纯 ANSI，不依赖 rich，保证在任何终端都能跑。"""

    COLORS = {
        logging.DEBUG: "\033[36m",     # 青
        logging.INFO: "\033[32m",      # 绿
        logging.WARNING: "\033[33m",   # 黄
        logging.ERROR: "\033[31m",     # 红
        logging.CRITICAL: "\033[35m",  # 紫
    }
    RESET = "\033[0m"
    DIM = "\033[2m"

    def __init__(self, use_color: bool = True) -> None:
        super().__init__()
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        if not self.use_color:
            return f"[{record.levelname}] {message}"
        color = self.COLORS.get(record.levelno, "")
        return f"{color}[{record.levelname}]{self.RESET} {message}"


def setup_logger(verbosity: int = 1, use_color: bool = True) -> logging.Logger:
    """按 verbosity 配置日志级别。

    verbosity 语义（与 pr-agent 的 config.verbosity_level 对齐，便于迁移习惯）：
        0 = 只报错
        1 = 常规进度（默认）
        2 = 调试，会打印 prompt、token 统计等
    """
    level = {0: logging.ERROR, 1: logging.INFO}.get(verbosity, logging.DEBUG)
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level)
    # 幂等：重复调用（例如测试里）不应该叠加 handler 导致日志重复
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_ColorFormatter(use_color=use_color))
    logger.addHandler(handler)
    logger.propagate = False
    global _configured
    _configured = True
    return logger


def get_logger() -> logging.Logger:
    """取 logger。未初始化时给一个默认配置，避免调用方拿到无 handler 的哑 logger。"""
    if not _configured:
        return setup_logger()
    return logging.getLogger(_LOGGER_NAME)
