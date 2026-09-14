"""LiteLLM 后端。

为什么选 litellm 做统一入口（而不是自己写各家 SDK 适配）？
—— 它把 "openai/deepseek/qwen/anthropic/ollama/..." 上百个供应商收敛成一个
`completion(model=..., messages=...)` 调用。对我们的价值不是"支持得多"，
而是**换模型只改一个字符串**：写 prompt 时可以在 DeepSeek 上快速迭代，
最终跑大模型时只改配置，代码零改动。对调 prompt 这种高频试错场景，
这个体验差异是决定性的。

调用方注意：litellm 是**懒加载**的。它一 import 就会装一堆回调、
读取环境变量、初始化 tracing，在不需要它的单测里非常拖累。
"""

from __future__ import annotations

import time
from typing import Any

from codesentry.llm.base import BaseLLMHandler, LLMResponse
from codesentry.utils.logger import get_logger

_litellm: Any = None


def _load_litellm() -> Any:
    """懒加载 litellm 并做一次性全局配置。"""
    global _litellm
    if _litellm is not None:
        return _litellm
    import litellm  # noqa: PLC0415 - 故意延迟到首次真实调用

    # 关掉启动 banner 和 debug 输出：默认它会往 stdout 打一堆装饰性文字，
    # 直接污染我们精心排版的终端报告。
    litellm.suppress_debug_info = True
    try:
        litellm.set_verbose = False
    except Exception:      # 不同版本属性名有差异，失败不影响主流程
        pass
    # drop_params：某些供应商不支持 temperature / max_tokens 等参数时会直接报错。
    # 打开后 litellm 会自动剔除不被支持的参数，代价是静默 —— 可通过 verbosity=2 的
    # 日志观察实际发出的请求来补偿。
    litellm.drop_params = True
    _litellm = litellm
    return litellm


class LiteLLMHandler(BaseLLMHandler):
    """基于 litellm 的多 Provider 实现。"""

    def __init__(
        self,
        timeout: int = 120,
        max_retries: int = 2,
        api_base: str = "",
        max_output_tokens: int = 0,
    ) -> None:
        super().__init__(timeout=timeout, max_retries=max_retries)
        self.api_base = api_base or ""
        self.max_output_tokens = max_output_tokens

    @property
    def deployment_id(self) -> str:
        return "litellm"

    async def _call_once(
        self, model: str, system: str, user: str, temperature: float
    ) -> LLMResponse:
        litellm = _load_litellm()

        messages: list[dict[str, str]] = []
        # 有些模型（如部分 o 系列）不接受 system 消息，litellm 会自动转换，
        # 但保底起见这里按标准两段式构造。
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "timeout": self.timeout,
        }
        if self.api_base:
            # 自定义端点（vLLM / one-api / 中转站）走这里
            kwargs["api_base"] = self.api_base
        if self.max_output_tokens > 0:
            kwargs["max_tokens"] = self.max_output_tokens

        started = time.monotonic()
        response = await litellm.acompletion(**kwargs)
        elapsed = time.monotonic() - started

        text = self._extract_text(response)
        finish_reason = ""
        try:
            finish_reason = response.choices[0].finish_reason or ""
        except (AttributeError, IndexError, TypeError):
            pass

        prompt_tokens = completion_tokens = 0
        try:
            usage = getattr(response, "usage", None)
            if usage is not None:
                prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
                completion_tokens = getattr(usage, "completion_tokens", 0) or 0
        except Exception:      # 少数供应商不返回 usage，属于正常情况
            pass

        resolved_model = model
        try:
            resolved_model = getattr(response, "model", None) or model
        except Exception:
            pass

        if not text.strip():
            # 空响应在"输出被安全策略拦截"或"模型只返回了思考内容"时会出现。
            # 抛异常让它进入重试 / fallback，比返回空串更有用。
            raise RuntimeError(
                f"模型 {model} 返回空内容（finish_reason={finish_reason!r}）"
            )

        if finish_reason == "length":
            get_logger().warning(
                f"模型 {model} 输出被 max_tokens 截断，报告可能不完整。"
                "可调大 config.max_output_tokens。"
            )

        return LLMResponse(
            text=text,
            model=resolved_model,
            finish_reason=finish_reason,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            elapsed_seconds=elapsed,
        )

    @staticmethod
    def _extract_text(response: Any) -> str:
        """从响应里取正文。

        需要兼容三种形态：
        1. 普通字符串（绝大多数模型）
        2. 分段列表（部分多模态/推理模型把内容拆成 [{"type":"text","text":...}]）
        3. 带 reasoning_content 的推理模型（DeepSeek-R1 等），此时 content 可能是空的
        """
        try:
            message = response.choices[0].message
        except (AttributeError, IndexError, TypeError) as exc:
            raise RuntimeError(f"无法解析模型响应结构：{response!r}") from exc

        content = getattr(message, "content", None)
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    parts.append(str(item.get("text", "")))
                else:
                    parts.append(str(getattr(item, "text", "")))
            joined = "".join(parts)
            if joined.strip():
                return joined

        # 兜底：推理模型可能把全部内容放在 reasoning_content
        reasoning = getattr(message, "reasoning_content", None)
        if isinstance(reasoning, str) and reasoning.strip():
            get_logger().warning("模型只返回了 reasoning_content，已作为正文使用")
            return reasoning
        return ""
