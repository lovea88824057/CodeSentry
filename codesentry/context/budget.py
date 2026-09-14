"""Token 预算计算。

这一层的唯一职责是回答"这段文本能不能塞进模型"。看似简单，
但它决定了整个分块策略的正确性 —— 算错就会在真实调用时收到
context length exceeded，而这属于最令人恼火的失败：花了钱、等了半天、什么也没拿到。

用 tiktoken 做本地估算而不是调 API 精确计数：本地估算有 5%~15% 偏差，
但只要预算留够余量（max_output_tokens + 安全系数）就完全够用，
而"零网络、零延迟、可离线单测"的价值远大于那点精度。
"""

from __future__ import annotations

import threading
from typing import Optional

import tiktoken

from codesentry.utils.logger import get_logger

# 已知模型的上下文窗口。没收录的走配置里的 max_model_tokens。
# 只收录常被用到的，避免维护一张很快就会过期的长表。
MODEL_MAX_TOKENS: dict[str, int] = {
    "gpt-4o": 128000, "gpt-4o-mini": 128000,
    "gpt-4.1": 1000000, "gpt-4.1-mini": 1000000,
    "gpt-4-turbo": 128000, "gpt-4": 8192, "gpt-3.5-turbo": 16385,
    "o1": 200000, "o3": 200000, "o4-mini": 200000,
    "claude-3-5-sonnet": 200000, "claude-3-7-sonnet": 200000,
    "claude-sonnet-4": 200000, "claude-opus-4": 200000,
    "deepseek-chat": 65536, "deepseek-reasoner": 65536,
    "qwen-max": 32768, "qwen-plus": 131072, "qwen2.5-coder": 131072,
    "glm-4": 131072, "moonshot-v1-128k": 131072,
    "gemini-2.0-flash": 1000000, "gemini-2.5-pro": 1000000,
    "llama3.1": 131072, "qwen2.5-coder:7b": 32768,
}

# 兜底窗口。取保守值：宁可多切一块，也不要在最后一步失败。
DEFAULT_MAX_TOKENS = 32000

# 编码缓存。tiktoken 的 encoding 构造有开销（要加载 BPE 词表），
# 一次运行里会调用上千次 count()，必须缓存。
_ENCODING_CACHE: dict[str, "tiktoken.Encoding"] = {}
_CACHE_LOCK = threading.Lock()


def _get_encoding(model: str) -> "tiktoken.Encoding":
    """按模型选编码，未知模型退到 o200k_base。

    为什么用 o200k_base 兜底？它是目前兼容面最广的新式编码，
    对中文和代码的切分都还算合理。对 DeepSeek/Qwen 这类国产模型，
    官方 tokenizer 略有差异，但用于"预算估算"足够。
    """
    key = model or "__default__"
    with _CACHE_LOCK:
        cached = _ENCODING_CACHE.get(key)
        if cached is not None:
            return cached
        try:
            enc = tiktoken.encoding_for_model(model)
        except Exception:
            try:
                enc = tiktoken.get_encoding("o200k_base")
            except Exception:                     # pragma: no cover - 极端兜底
                enc = tiktoken.get_encoding("cl100k_base")
        _ENCODING_CACHE[key] = enc
        return enc


def _encode_len(enc: "tiktoken.Encoding", text: str) -> int:
    """编码并返回长度，遇到特殊 token 字面量时降级处理。

    这里踩过一个真实的坑：tiktoken 默认把 `<|endoftext|>` 之类的字符串
    视作"特殊 token 字面量"，直接 encode 会抛 ValueError，而不是当成
    普通文本处理。代码审查场景下这绝非边缘情况 —— 仓库里出现
    `<|endoftext|>` 往往正是因为它是个 LLM 相关项目（prompt 模板、
    tokenizer 测试、fenced code block 里的示例），也就是说**越是对
    AI 友好的仓库越容易踩到**。tiktoken 报错会中断整个审查流程，
    而我们的定位只是"估算预算"，没有任何理由为它崩掉。

    处理策略（由严到宽，只在报错时才降级）：
      1. 正常 encode —— 绝大多数文本走这条，零额外开销；
      2. `disallowed_special=()` —— 把所有特殊 token 当普通文本编码，
         计数仍然准确（就是按 BPE 切分那些字符），这是我们要的语义；
      3. 极端兜底 —— 连降级都失败，按字符数粗略折算（1 token ≈ 4 字符），
         宁可估算不准也不要中断流程。
    """
    try:
        return len(enc.encode(text))
    except (ValueError, UnicodeEncodeError):
        pass
    try:
        return len(enc.encode(text, disallowed_special=()))
    except Exception:                             # pragma: no cover - 极端兜底
        get_logger().warning("tiktoken 编码失败，回退到按字符数粗略估算 token")
        return max(1, len(text) // 4)


def get_model_max_tokens(model: str, configured: int = 0) -> int:
    """决定本次请求可用的上下文上限。

    优先级：显式配置 > 模型表 > 默认值。
    配置里给一个值（比如 32000）是常见做法：既比模型真实上限保守，
    又能让不同模型的预算表现一致，便于横向对比。
    """
    if configured and configured > 0:
        return configured
    name = (model or "").lower()
    # 去掉 provider 前缀再查表：litellm 的写法是 "deepseek/deepseek-chat"
    bare = name.split("/", 1)[-1]
    for candidate in (bare, name):
        for known, limit in MODEL_MAX_TOKENS.items():
            if candidate.startswith(known):
                return limit
    return DEFAULT_MAX_TOKENS


class TokenCounter:
    """token 计数器。"""

    def __init__(self, model: str = "") -> None:
        self.model = model
        self._encoding = _get_encoding(model)

    def count(self, text: str) -> int:
        if not text:
            return 0
        return _encode_len(self._encoding, text)

    def count_many(self, texts: list[str]) -> int:
        return sum(self.count(t) for t in texts)


def clip_tokens(text: str, max_tokens: int, counter: TokenCounter) -> str:
    """按行裁剪文本，使其 token 数不超过 max_tokens。

    为什么"按行"裁而不是按字符截断？—— 代码截断在半行处会产生语法残缺，
    模型看到残缺代码容易产生"这段代码有 bug"的错误结论。
    按行裁至少保证每一行是完整的。

    实现用二分查找行数：直接逐行累加编码在万行级文件上会明显变慢
    （每行一次 encode 调用），二分只需 log2(n) 次全量编码。
    """
    if max_tokens <= 0:
        return ""
    if counter.count(text) <= max_tokens:
        return text

    lines = text.splitlines(keepends=True)
    if not lines:
        return ""

    # 裁剪提示语本身也要占 token，必须从预算里先扣掉，
    # 否则"裁到 50 token"的结果实际是 50 + 提示语，会超出调用方给的额度。
    note = f"\n... [已裁剪 {{n}} 行：内容超出上下文预算]\n"
    note_tokens = counter.count(note.format(n=len(lines)))
    budget = max_tokens - note_tokens
    if budget <= 0:
        return note.format(n=len(lines))

    low, high = 0, len(lines)
    while low < high:
        mid = (low + high + 1) // 2
        if counter.count("".join(lines[:mid])) <= budget:
            low = mid
        else:
            high = mid - 1

    if low <= 0:
        # 连第一行都放不下：极端情况（单行超长），只能硬截
        get_logger().warning("单行内容超过 token 预算，已硬截断")
        return text[:max(0, max_tokens * 2)]
    clipped = "".join(lines[:low])
    return clipped + note.format(n=len(lines) - low)
