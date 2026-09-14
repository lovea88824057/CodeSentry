"""上下文层：把 diff 加工成能塞进模型、且模型能准确引用的形态。"""

from codesentry.context.budget import TokenCounter, clip_tokens, get_model_max_tokens
from codesentry.context.chunker import Chunk, ChunkPlan, compute_files_tokens, plan_chunks
from codesentry.context.hunk import (
    HUNK_FORMAT_LEGEND,
    render_file_block,
    render_file_index,
    render_files_for_prompt,
)

__all__ = [
    "TokenCounter", "clip_tokens", "get_model_max_tokens",
    "Chunk", "ChunkPlan", "compute_files_tokens", "plan_chunks",
    "HUNK_FORMAT_LEGEND", "render_file_block", "render_file_index", "render_files_for_prompt",
]
