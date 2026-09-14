"""Diff 来源抽象。

把"diff 从哪来"独立成一层，是本项目相对 pr-agent 最大的结构差异：
pr-agent 的入口必须是一个 PR URL，被 GitProvider 抽象绑死在平台上；
我们把抽象放在更靠前的位置 —— 只要能得到一串 diff，就能审。

于是新增输入形态（GitHub PR、GitLab MR、HTTP 拉取的 patch）时，
只需再加一个 DiffProvider 子类，工具层、报告层、prompt 层一行都不用改。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from codesentry.models import DiffBundle


class DiffProviderError(RuntimeError):
    """diff 获取失败。用独立异常类型是为了让 CLI 能区分"用户环境问题"
    （不在 git 仓库里、ref 不存在）和"程序 bug"，前者应该给出可操作的提示。"""


class DiffProvider(ABC):
    """所有 diff 来源的统一接口。"""

    #: 供报告展示的来源标识
    name: str = "unknown"

    @abstractmethod
    def get_bundle(self) -> DiffBundle:
        """返回待审查的完整输入。"""
        raise NotImplementedError

    def describe_target(self) -> str:
        """给用户看的一句话描述，说明这次审的是什么东西。"""
        return self.name

    # --- 可选能力：有全文时才能做行号校验，没有就自动降级 ---

    def supports_line_validation(self) -> bool:
        """是否具备填充 head_file 全文的能力。"""
        return False

    def attach_file_contents(self, bundle: DiffBundle) -> DiffBundle:
        """为 bundle 里的文件填充 base_file / head_file。

        默认实现什么都不做（纯 diff 输入拿不到全文），
        子类覆写后即可开启行号校验。返回同一对象，便于链式调用。
        """
        return bundle
