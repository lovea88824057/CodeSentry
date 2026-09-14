"""模型调用抽象。

接口刻意做得极窄：`_call_once(model, system, user, temperature)`。
理由是从 pr-agent 的 `BaseAiHandler` 学到的一条经验 ——
handler 层唯一的职责是"把一段文本发给模型并拿回文本"，
任何 prompt 组装、结果解析、重试策略都不该渗进来。
接口越窄，接新供应商（或换成 HTTP 直连）的成本就越低。

重试与 fallback 放在基类，因为它们与具体供应商无关：
所有 handler 都应该享受同样的"主模型失败就换备用模型"的保障。
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from codesentry.utils.logger import get_logger


class LLMError(RuntimeError):
    """模型调用失败（重试与 fallback 都耗尽后抛出）。"""


@dataclass
class LLMResponse:
    """一次模型调用的结果。

    除了文本，还把用量和耗时一并带回来：这些数据会进 RunDetails，
    让用户对"这次审查花了多少"有量化感知，而不是只看到一个结果。
    """

    text: str
    model: str = ""
    finish_reason: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed_seconds: float = 0.0
    attempts: int = 1
    # 若最终是 fallback 模型回答的，这里记录原始请求的模型名
    fell_back_from: str = ""
    usage_estimated: bool = False

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class BaseLLMHandler(ABC):
    """所有模型后端的基类。"""

    def __init__(self, timeout: int = 120, max_retries: int = 2) -> None:
        self.timeout = timeout
        # 重试次数是"每个模型内"的次数，不是总次数。
        # 与 fallback 链相乘，所以别设太大：3 个模型 × 3 次重试 = 9 次调用，
        # 全部超时时用户要等很久。默认 2 已经够覆盖大多数网络抖动。
        self.max_retries = max(0, max_retries)

    # ------------------------------------------------------------------ #
    # 子类实现
    # ------------------------------------------------------------------ #

    @abstractmethod
    async def _call_once(
        self, model: str, system: str, user: str, temperature: float
    ) -> LLMResponse:
        """发一次请求。不做重试，失败就抛异常。"""
        raise NotImplementedError

    @property
    def deployment_id(self) -> str:
        """当前生效的供应商标识（用于日志）。"""
        return "unknown"

    # ------------------------------------------------------------------ #
    # 公共能力：重试 + fallback
    # ------------------------------------------------------------------ #

    async def complete(
        self, model: str, system: str, user: str, temperature: float = 0.2
    ) -> LLMResponse:
        """单模型调用，带指数退避重试。"""
        last_error: Exception | None = None
        started = time.monotonic()
        for attempt in range(1, self.max_retries + 2):   # +1：至少试一次
            try:
                response = await self._call_once(model, system, user, temperature)
                response.attempts = attempt
                response.elapsed_seconds = time.monotonic() - started
                return response
            except Exception as exc:                     # noqa: BLE001 - 故意宽泛：网络层异常五花八门
                last_error = exc
                if attempt > self.max_retries:
                    break
                wait = min(2 ** (attempt - 1), 8)        # 1s, 2s, 4s... 封顶 8s
                get_logger().warning(
                    f"模型 {model} 第 {attempt} 次调用失败（{type(exc).__name__}: {exc}），{wait}s 后重试"
                )
                await asyncio.sleep(wait)
        raise LLMError(f"模型 {model} 调用失败（已重试 {self.max_retries} 次）：{last_error}") from last_error

    async def complete_with_fallback(
        self,
        models: list[str],
        system: str,
        user: str,
        temperature: float = 0.2,
    ) -> LLMResponse:
        """按顺序尝试模型列表，第一个成功的即为结果。

        这是从 pr-agent 的 `retry_with_fallback_models` 学来的机制。
        实用价值很高：主模型（通常是能力最强也最贵/最易限流的那个）
        偶发超时是常态，直接失败会让用户白等一场；换个模型就通了。
        """
        models = [m for m in models if m]
        if not models:
            raise LLMError("没有配置任何模型（config.model 为空）")

        errors: list[str] = []
        for idx, model in enumerate(models):
            try:
                response = await self.complete(model, system, user, temperature)
                if idx > 0:
                    response.fell_back_from = models[0]
                    get_logger().warning(f"已切换到备用模型 {model} 并成功返回")
                return response
            except LLMError as exc:
                errors.append(str(exc))
                if idx + 1 < len(models):
                    get_logger().warning(f"模型 {model} 全部尝试失败，切换到下一个备用模型")
        raise LLMError("所有模型均调用失败：\n" + "\n".join(f"  - {e}" for e in errors))


class FakeLLMHandler(BaseLLMHandler):
    """离线测试用的假 handler。

    它的存在本身就是"接口足够窄"的证明：只需实现一个 `_call_once`
    就能替掉整个模型层，于是分块、解析、渲染、报告这些逻辑
    都可以在没有网络、没有 API key 的情况下做端到端测试。
    """

    def __init__(self, responses: list[str] | None = None, fail_models: list[str] | None = None) -> None:
        super().__init__(timeout=5, max_retries=0)
        self.responses = list(responses or [])
        self.fail_models = set(fail_models or [])
        self.calls: list[dict] = []
        self._cursor = 0

    async def _call_once(self, model: str, system: str, user: str, temperature: float) -> LLMResponse:
        self.calls.append({"model": model, "system": system, "user": user, "temperature": temperature})
        if model in self.fail_models:
            raise RuntimeError(f"FakeLLMHandler: 模型 {model} 被配置为失败")
        if self.responses:
            text = self.responses[self._cursor % len(self.responses)]
            self._cursor += 1
        else:
            text = "summary: 假响应\n"
        return LLMResponse(
            text=text,
            model=model,
            finish_reason="stop",
            prompt_tokens=len(user) // 4,
            completion_tokens=len(text) // 4,
            usage_estimated=True,
        )

    @property
    def deployment_id(self) -> str:
        return "fake"
