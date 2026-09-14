"""运行统计。

为什么单独抽一个模块而不是零散地累加？
—— "这次审查花了多少"是用户最关心的问题之一，而它涉及多个来源
（每次模型调用的 usage、文件数、耗时、分块数）。把累加逻辑集中在一处，
可以保证无论走单块还是多块路径、无论有没有 fallback，
统计口径都完全一致。散落各处必然出现"这次怎么没统计上"的 bug。
"""

from __future__ import annotations

from codesentry.llm.base import LLMResponse
from codesentry.models import RunDetails


class RunDetailsBuilder:
    """累积一次审查的运行信息。"""

    def __init__(
        self,
        model: str = "",
        fallback_models: list[str] | None = None,
        provider: str = "",
        source_ref: str = "",
    ) -> None:
        self.model = model
        self.fallback_models = list(fallback_models or [])
        self.provider = provider
        self.source_ref = source_ref
        self.num_files = 0
        self.num_skipped_files = 0
        self.num_chunks = 0
        self.num_llm_calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.elapsed_seconds = 0.0
        # 实际生效的模型：发生 fallback 时会被覆盖，让报告如实反映
        self.effective_model = model

    def record_call(self, response: LLMResponse) -> None:
        """记录一次成功的模型调用。"""
        self.num_llm_calls += response.attempts
        self.prompt_tokens += response.prompt_tokens
        self.completion_tokens += response.completion_tokens
        # 耗时不累加：分块是并行跑的，累加会得出一个远大于真实墙钟时间的数字。
        # 取最大值是并行场景下的正确近似。
        self.elapsed_seconds = max(self.elapsed_seconds, response.elapsed_seconds)
        if response.model:
            self.effective_model = response.model

    def set_scope(self, num_files: int, num_skipped_files: int) -> None:
        self.num_files = num_files
        self.num_skipped_files = num_skipped_files

    def build(self, num_chunks: int) -> RunDetails:
        self.num_chunks = num_chunks
        return RunDetails(
            model=self.effective_model or self.model,
            fallback_models=self.fallback_models,
            provider=self.provider,
            source_ref=self.source_ref,
            num_files=self.num_files,
            num_skipped_files=self.num_skipped_files,
            num_chunks=num_chunks,
            num_llm_calls=self.num_llm_calls,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            elapsed_seconds=self.elapsed_seconds,
        )
