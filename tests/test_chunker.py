"""分块策略测试。

分块的核心约束有三条，缺一不可：
    1. 每一块都不能超过 token 预算（否则真实调用会失败）
    2. 块数不能超过 max_chunks（否则成本失控）
    3. 除了"单文件本身超限 / 预算彻底耗尽"，文件不能凭空消失；
       确实被放弃的必须出现在 plan.dropped 里，好让报告如实告知用户
第三条尤其重要 —— "文件被静默丢弃"是审查类工具最不可接受的 bug。
"""

from codesentry.context.budget import TokenCounter, clip_tokens, get_model_max_tokens
from codesentry.context.chunker import (
    MIN_KEEP_TOKENS,
    SAFETY_MARGIN_TOKENS,
    compute_files_tokens,
    plan_chunks,
)
from codesentry.models import FilePatchInfo


def make_file(name: str, tokens: int) -> FilePatchInfo:
    """构造一个"token 数可控"的假文件。

    patch 内容只是占位符：分块算法只看 tokens 字段，
    这样可以精确控制测试场景，不受 tiktoken 分词波动影响。
    """
    info = FilePatchInfo(filename=name, patch=f"# {name}\n" * max(tokens, 1))
    info.tokens = tokens
    return info


def packed_names(plan) -> list[str]:
    return [f.filename for c in plan.chunks for f in c.files]


def test_single_chunk_when_everything_fits():
    files = [make_file("a.py", 100), make_file("b.py", 200)]
    plan = plan_chunks(files, prompt_tokens=500, max_model_tokens=10000,
                       max_output_tokens=1000, max_chunks=5)
    assert len(plan.chunks) == 1
    assert set(packed_names(plan)) == {"a.py", "b.py"}
    assert plan.dropped == []


def test_no_file_is_lost_across_chunks():
    """10 个文件、预算紧张时，所有文件都必须出现在某一块里（或被明确记为 dropped）。"""
    files = [make_file(f"f{i}.py", 600) for i in range(10)]
    plan = plan_chunks(files, prompt_tokens=500, max_model_tokens=3000,
                       max_output_tokens=500, max_chunks=5)
    packed = packed_names(plan)
    dropped = [f.filename for f in plan.dropped]
    assert sorted(packed + dropped) == sorted(f.filename for f in files)
    assert len(packed) == len(set(packed)), "同一个文件不应出现在多个块里"


def test_each_chunk_respects_budget():
    """每一块的实际占用不能超过（窗口 - 输出预留 - 安全余量）。"""
    files = [make_file(f"f{i}.py", 900) for i in range(8)]
    prompt_tokens, max_model, max_out = 800, 4000, 500
    plan = plan_chunks(files, prompt_tokens=prompt_tokens, max_model_tokens=max_model,
                       max_output_tokens=max_out, max_chunks=5)

    capacity = max_model - max_out - SAFETY_MARGIN_TOKENS
    for chunk in plan.chunks:
        assert prompt_tokens + chunk.tokens <= capacity


def test_oversized_single_file_is_clipped_not_dropped():
    """单文件超限且策略为 clip 时，应该裁剪后保留，而不是丢弃。"""
    files = [make_file("huge.py", 50000), make_file("small.py", 100)]
    plan = plan_chunks(files, prompt_tokens=500, max_model_tokens=8000,
                       max_output_tokens=1000, max_chunks=5,
                       large_file_policy="clip", counter=TokenCounter(""))
    names = packed_names(plan)
    assert "small.py" in names
    assert "huge.py" in names, "clip 策略下超大文件不应被完全丢弃"
    huge = next(f for c in plan.chunks for f in c.files if f.filename == "huge.py")
    assert huge.clipped is True
    assert huge.tokens <= 8000


def test_oversized_single_file_skipped_when_policy_is_skip():
    """skip 策略下超大文件应被标记跳过，并记入 dropped（不能静默消失）。"""
    files = [make_file("huge.py", 50000), make_file("small.py", 100)]
    plan = plan_chunks(files, prompt_tokens=500, max_model_tokens=8000,
                       max_output_tokens=1000, max_chunks=5, large_file_policy="skip")
    names = packed_names(plan)
    assert "huge.py" not in names
    assert "small.py" in names
    assert [f.filename for f in plan.dropped] == ["huge.py"]
    assert "预算" in plan.dropped[0].skip_reason


def test_max_chunks_is_respected_and_drops_are_reported():
    """块数不得超过 max_chunks；被放弃的文件必须出现在 dropped 里。"""
    files = [make_file(f"f{i}.py", 1000) for i in range(20)]
    plan = plan_chunks(files, prompt_tokens=500, max_model_tokens=2500,
                       max_output_tokens=300, max_chunks=3)
    assert len(plan.chunks) <= 3
    assert plan.dropped, "预算耗尽时应当有文件被明确记为 dropped，而不是无限开新块"
    # 覆盖性检查：每个文件要么被装进块、要么被记为 dropped
    accounted = set(packed_names(plan)) | {f.filename for f in plan.dropped}
    assert accounted == {f.filename for f in files}


def test_empty_and_all_skipped_inputs():
    empty = plan_chunks([], prompt_tokens=100, max_model_tokens=8000, max_output_tokens=1000)
    assert empty.chunks == [] and empty.dropped == []

    skipped = make_file("a.py", 100)
    skipped.skipped = True
    only_skipped = plan_chunks([skipped], prompt_tokens=100, max_model_tokens=8000,
                               max_output_tokens=1000)
    assert only_skipped.chunks == [] and only_skipped.dropped == []


def test_oversized_prompt_template_does_not_crash():
    """模板本身就把窗口占满时，不能崩 —— 要给出可用容量继续跑。"""
    files = [make_file("a.py", 100)]
    plan = plan_chunks(files, prompt_tokens=99999, max_model_tokens=8000,
                       max_output_tokens=1000, max_chunks=5)
    assert len(plan.chunks) == 1


def test_large_files_are_packed_first():
    """大文件应优先装箱 —— 否则最后剩下的大文件会放不进任何块。"""
    files = [make_file("tiny.py", 50), make_file("huge.py", 1500), make_file("mid.py", 700)]
    plan = plan_chunks(files, prompt_tokens=500, max_model_tokens=3000,
                       max_output_tokens=500, max_chunks=5)
    assert plan.dropped == []
    assert set(packed_names(plan)) == {"tiny.py", "huge.py", "mid.py"}


# --------------------------------------------------------------------------- #
# token 预算工具
# --------------------------------------------------------------------------- #

def test_get_model_max_tokens_precedence():
    # 显式配置优先
    assert get_model_max_tokens("deepseek/deepseek-chat", configured=12345) == 12345
    # 未配置时查表（去掉 provider 前缀也能命中）
    assert get_model_max_tokens("deepseek/deepseek-chat") == 65536
    assert get_model_max_tokens("openai/gpt-4o") == 128000
    # 未知模型回落到保守默认值
    assert get_model_max_tokens("unknown/model-xyz") == 32000


def test_clip_tokens_stays_within_budget():
    """裁剪后的总长度（含提示语）必须落在预算内。"""
    counter = TokenCounter("")
    text = "\n".join(f"line {i} " + "x" * 20 for i in range(200))
    clipped = clip_tokens(text, 50, counter)

    assert counter.count(clipped) <= 50
    assert "已裁剪" in clipped                  # 必须明确告知被裁剪了
    # 每一行都应是完整的，不能出现半行
    body_lines = [l for l in clipped.splitlines() if l.startswith("line ")]
    assert body_lines
    for line in body_lines:
        assert line.endswith("x" * 20)


def test_clip_tokens_noop_when_under_budget():
    counter = TokenCounter("")
    text = "short text"
    assert clip_tokens(text, 1000, counter) == text


def test_compute_files_tokens_fills_missing_values():
    counter = TokenCounter("")
    files = [make_file("a.py", -1), make_file("b.py", 123)]
    # 手工把 a 的 tokens 恢复成"未计算"并给它真实内容
    files[0].patch = "def f():\n    return 1\n"
    files[0].tokens = -1
    compute_files_tokens(files, counter)
    assert files[0].tokens > 0
    assert files[1].tokens == 123              # 已算过的不重算


def test_min_keep_tokens_is_reasonable():
    """MIN_KEEP_TOKENS 太小会导致"裁完只剩两行代码"，没有审查价值。"""
    assert MIN_KEEP_TOKENS >= 200


# --------------------------------------------------------------------------- #
# 特殊 token 字面量的编码健壮性（真实 bug 回归测试）
# --------------------------------------------------------------------------- #

def test_count_survives_special_token_literals():
    """diff 里出现 `<|endoftext|>` 这类字面量时，计数不能抛异常。

    这是从真实仓库上冒烟时踩到的 bug：tiktoken 默认把这些字符串当作
    "特殊 token"，直接 encode 会抛 ValueError，把整条审查链路打断。
    而含这类字符串的仓库恰恰多半是 LLM 相关项目（prompt 模板、tokenizer
    测试、文档里的示例）—— 也就是说越是对 AI 友好的代码库越容易触发。
    我们的定位只是"估算预算"，没有任何理由为此崩掉。
    """
    counter = TokenCounter("")
    # 逐个覆盖 tiktoken 会拦截的常见特殊 token 字面量
    for literal in ("<|endoftext|>", "<|fim_prefix|>", "<|im_start|>", "<|endofprompt|>"):
        text = f'prompt = "{literal}"\nresponse = model(prompt)\n'
        assert counter.count(text) > 0, f"{literal} 应当能正常计数"


def test_special_token_literals_count_as_plain_text():
    """降级后应当是"按普通文本切分"，而不是被整体计成 1 个 token。

    如果实现里直接把特殊 token 整体跳过，计数会严重低估，
    进而让分块失准 —— 必须验证降级路径仍在做真实的 BPE 切分。
    """
    counter = TokenCounter("")
    one = counter.count("<|endoftext|>")
    assert one >= 3, "特殊 token 字面量应按普通文本切分（这里应切出若干子词）"
    # 与等价长度的普通 ASCII 文本相比，量级应当一致（不应被计成 1）
    assert one >= counter.count("endoftext") - 2


def test_clip_tokens_handles_special_token_literals():
    """端到端：含特殊 token 字面量的超长文本也能被正常裁剪。"""
    counter = TokenCounter("")
    text = "\n".join(f'line {i} = "<|endoftext|>"' for i in range(300))
    clipped = clip_tokens(text, 80, counter)
    assert counter.count(clipped) <= 80
    assert "已裁剪" in clipped


def test_count_many_with_special_tokens():
    counter = TokenCounter("")
    total = counter.count_many(["plain text", "<|endoftext|>", ""])
    assert total > counter.count("plain text")
