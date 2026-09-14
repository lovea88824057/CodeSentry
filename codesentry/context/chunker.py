"""分块策略：决定把哪些文件放进哪一次模型调用。

这是从 pr-agent 的 `_pack_pr_multi_diffs` 学来并简化的核心机制，
采用经典的 **map-reduce** 思路：

    map    大 diff 按 token 预算切成多个块，每块单独调一次模型（可并行）
    reduce 把各块的结构化结果合并成一份报告（见 report/merge.py）

三个层次的降级，按"信息损失从小到大"排列：

    1. 装得下        -> 整发一次。最快、最准，90% 的情况走这条路径
    2. 装不下        -> 按文件切块，每块独立审查。信息不丢，只是跨块关联会弱一些
    3. 单文件也超限  -> 按 large_file_policy 裁剪(clip)或丢弃(skip)。
                        裁剪优先：丢掉全部信息 vs 丢掉局部信息，显然是后者更好

关键取舍：**按文件切块，而不是按 token 硬切**。
按 token 硬切会把一个函数劈成两半，模型看到半截代码会编出根本不存在的 bug。
按文件切块保证了每一块内部始终是语法完整的代码单元。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from codesentry.context.budget import TokenCounter, clip_tokens
from codesentry.models import FilePatchInfo
from codesentry.utils.logger import get_logger

#: 输出预留之外的安全余量。tiktoken 估算本身有偏差，留一点缓冲避免踩线失败。
SAFETY_MARGIN_TOKENS = 500

#: 裁剪后至少要保留这么多 token，否则不如直接丢弃（留一句话的代码没有审查价值）。
MIN_KEEP_TOKENS = 400


@dataclass
class Chunk:
    """一个待发送给模型的批次。"""

    index: int
    files: list[FilePatchInfo] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return sum(max(f.tokens, 0) for f in self.files)

    @property
    def filenames(self) -> list[str]:
        return [f.filename for f in self.files]


@dataclass
class ChunkPlan:
    """分块结果。

    为什么要把 `dropped` 单独带出来，而不是只返回 chunks？
    —— 因为"被放弃的文件"必须能被报告出来。早期版本这里返回纯 list，
    结果超出预算被丢弃的文件在最终报告里没有任何痕迹，用户会以为
    自己看到的是全部改动。审查工具最不能做的事就是"静默漏掉东西"。
    """

    chunks: list[Chunk] = field(default_factory=list)
    dropped: list[FilePatchInfo] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return sum(c.tokens for c in self.chunks)


def compute_files_tokens(files: list[FilePatchInfo], counter: TokenCounter) -> None:
    """就地计算每个文件的 patch token 数。

    单独抽成一函数是因为它必须在分块**之前**全部算完：
    分块是"按 token 降序装箱"的贪心算法，缺一个值排序就不对了。
    """
    for info in files:
        if info.tokens < 0:
            info.tokens = counter.count(info.patch) if info.patch else 0


def plan_chunks(
    files: list[FilePatchInfo],
    prompt_tokens: int,
    max_model_tokens: int,
    max_output_tokens: int,
    max_chunks: int = 5,
    large_file_policy: str = "clip",
    counter: TokenCounter | None = None,
) -> ChunkPlan:
    """把文件装进若干个块。

    参数：
        prompt_tokens:  模板本身（不含 diff）占用的 token，每块都要重复占用
        max_model_tokens: 上下文窗口上限
        max_output_tokens: 给模型输出预留的额度
        max_chunks:     最多切几块。超出后不再新增块，改为在最后一块里裁剪或跳过
        large_file_policy: "clip" | "skip"，处理单文件超限

    返回：ChunkPlan（含 chunks 与 dropped）。files 为空时返回空 plan。
    """
    active = [f for f in files if not f.skipped]
    if not active:
        return ChunkPlan()
    if counter is None:
        counter = TokenCounter()

    # 每块可用来放 diff 的净容量
    capacity = max_model_tokens - max_output_tokens - SAFETY_MARGIN_TOKENS
    if capacity <= prompt_tokens:
        # 模板本身就快把窗口占满了（通常意味着 max_model_tokens 配得太小）
        get_logger().warning(
            f"提示词模板已占用 {prompt_tokens} tokens，超过可用预算 {capacity}。"
            "请调大 config.max_model_tokens 或精简 extra_instructions。"
        )
        capacity = prompt_tokens + 1000          # 至少让流程能跑起来，别直接崩

    # 大文件优先装箱：先把最难安置的放进去，小文件负责填缝。
    # 反过来（小文件优先）会导致最后剩下一个大文件放不进任何块。
    ordered = sorted(active, key=lambda f: max(f.tokens, 0), reverse=True)

    max_chunks = max(1, max_chunks)
    # 始终维护"最后一个块就是当前打开的块"这个不变式，避免额外的 current 变量
    # 与 chunks 列表出现状态不同步（早期版本正是在这里丢过文件）。
    chunks: list[Chunk] = [Chunk(index=0)]
    dropped: list[FilePatchInfo] = []
    used = prompt_tokens

    for info in ordered:
        need = max(info.tokens, 0)

        if used + need > capacity:
            # 放不下：如果当前块已有内容、且块数还有余量，就开新块再试
            if chunks[-1].files and len(chunks) < max_chunks:
                chunks.append(Chunk(index=len(chunks)))
                used = prompt_tokens

            if used + need > capacity:
                # 新块也放不下（单文件超限），或块数已达上限 -> 裁剪或跳过
                info = _handle_oversized(info, capacity - used, large_file_policy, counter)
                if info.skipped:
                    dropped.append(info)
                    continue

        chunks[-1].files.append(info)
        used += max(info.tokens, 0)

    # 丢掉空块（所有文件都被跳过时会出现）
    return ChunkPlan(chunks=[c for c in chunks if c.files], dropped=dropped)


def _handle_oversized(
    info: FilePatchInfo,
    remaining_tokens: int,
    policy: str,
    counter: TokenCounter,
) -> FilePatchInfo:
    """处理"单个文件就超过预算"的情况。

    返回一个新的 FilePatchInfo（不修改入参），可能带 skipped=True。
    """
    name = info.filename
    if policy == "skip":
        get_logger().warning(f"文件 {name} 超出预算，按 skip 策略跳过")
        dropped = info.model_copy(deep=True)
        dropped.skipped = True
        dropped.skip_reason = f"单文件超出 token 预算（需 {info.tokens}）"
        return dropped

    if remaining_tokens < MIN_KEEP_TOKENS:
        get_logger().warning(
            f"文件 {name} 超出预算且剩余空间不足（{remaining_tokens} < {MIN_KEEP_TOKENS} tokens），已跳过"
        )
        dropped = info.model_copy(deep=True)
        dropped.skipped = True
        dropped.skip_reason = f"超出 token 预算且无法有效裁剪（需 {info.tokens}）"
        return dropped

    clipped_patch = clip_tokens(info.patch, remaining_tokens, counter)
    get_logger().warning(
        f"文件 {name} 超出预算（{info.tokens} tokens），已裁剪至约 {remaining_tokens} tokens"
    )
    clipped = info.model_copy(deep=True)
    clipped.patch = clipped_patch
    clipped.tokens = counter.count(clipped_patch)
    clipped.clipped = True
    return clipped
