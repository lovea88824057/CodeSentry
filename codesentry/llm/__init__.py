"""模型层：统一的 LLM 调用接口。

对外只暴露三样东西：
    BaseLLMHandler   抽象基类（重试 + fallback 已在基类实现）
    LiteLLMHandler   生产用实现
    FakeLLMHandler   离线测试用实现

工厂函数 `build_handler` 让"用哪个后端"变成一个配置项，
上层代码不关心自己拿到的是真模型还是假模型。
"""

from __future__ import annotations

from codesentry.config import Settings
from codesentry.llm.base import LLMError, FakeLLMHandler, BaseLLMHandler, LLMResponse

__all__ = [
    "BaseLLMHandler", "LiteLLMHandler", "FakeLLMHandler",
    "LLMResponse", "LLMError", "build_handler",
]


def build_handler(settings: Settings) -> BaseLLMHandler:
    """按配置构造模型 handler。

    litellm 的导入放在函数内部：只用 FakeLLMHandler 的测试路径
    完全不会触碰 litellm，单测启动时间能从几秒降到毫秒级。
    """
    from codesentry.llm.litellm_handler import LiteLLMHandler

    cfg = settings.config
    return LiteLLMHandler(
        timeout=cfg.timeout,
        max_retries=cfg.max_retries,
        api_base=cfg.api_base,
        max_output_tokens=cfg.max_output_tokens,
    )


def model_chain(settings: Settings) -> list[str]:
    """主模型 + fallback 链。自动去重并去掉空值。"""
    chain = [settings.config.model, *settings.config.fallback_models]
    seen: set[str] = set()
    result: list[str] = []
    for model in chain:
        if model and model not in seen:
            seen.add(model)
            result.append(model)
    return result
