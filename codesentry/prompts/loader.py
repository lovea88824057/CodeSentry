"""提示词加载与渲染。

提示词放在 TOML 里而不是 Python 字符串里，有两个实际好处：
1. 调 prompt 不需要改代码，也不怕误改逻辑；
2. 用户可以整份替换（配置 `review.prompt_file = "my_prompt.toml"`），
   在不动源码的前提下接入自己的团队规范。

模板渲染用 Jinja2 + StrictUndefined：变量名拼错会立刻抛异常。
这是从 pr-agent 的 AGENTS.md 里学到的教训 —— 他们明确要求
"模板引用的变量必须在 vars 字典里显式给出"，因为 Jinja2 默认的
Undefined 会安静地把缺失变量渲染成空串，于是 prompt 里少了一整段
而没人发现，直到审查质量莫名下降。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, StrictUndefined

from codesentry.utils.logger import get_logger

#: 内置提示词目录
BUILTIN_PROMPT_DIR = Path(__file__).resolve().parent

#: locale -> 自然语言名。用于在 prompt 里明确要求输出语言。
_LANGUAGE_NAMES = {
    "zh": "Chinese (简体中文)", "zh-cn": "Chinese (简体中文)", "zh-tw": "Chinese (繁體中文)",
    "en": "English", "en-us": "English", "en-gb": "English",
    "ja": "Japanese (日本語)", "ko": "Korean (한국어)",
    "de": "German (Deutsch)", "fr": "French (Français)",
    "es": "Spanish (Español)", "ru": "Russian (Русский)",
    "pt": "Portuguese (Português)", "it": "Italian (Italiano)",
}


@dataclass
class PromptTemplate:
    """一组 system / user 模板。"""

    name: str
    system: str
    user: str


class PromptError(RuntimeError):
    """提示词文件缺失或格式错误。"""


def resolve_language_name(locale: str) -> str:
    """把 "zh-CN" 这类 locale 映射成人类可读语言名。未收录则原样返回。"""
    if not locale:
        return "English"
    key = locale.strip().lower()
    if key in _LANGUAGE_NAMES:
        return _LANGUAGE_NAMES[key]
    return _LANGUAGE_NAMES.get(key.split("-")[0], locale)


def load_prompt_file(path: str | Path | None = None) -> dict[str, PromptTemplate]:
    """读取提示词 TOML。path 为 None 时用内置的 review.toml。"""
    target = Path(path) if path else BUILTIN_PROMPT_DIR / "review.toml"
    if not target.is_file():
        raise PromptError(f"提示词文件不存在：{target}")
    try:
        with open(target, "rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise PromptError(f"提示词文件 {target} 不是合法 TOML：{exc}") from exc

    templates: dict[str, PromptTemplate] = {}
    for section, value in raw.items():
        if not isinstance(value, dict):
            continue
        system = value.get("system", "")
        user = value.get("user", "")
        if not system and not user:
            get_logger().warning(f"提示词段 [{section}] 既没有 system 也没有 user，已忽略")
            continue
        templates[section] = PromptTemplate(name=section, system=system, user=user)

    if not templates:
        raise PromptError(f"提示词文件 {target} 里没有可用的段")
    return templates


def get_review_prompt(prompt_file: str | None = None) -> PromptTemplate:
    """取 review 提示词。"""
    templates = load_prompt_file(prompt_file or None)
    template = templates.get("review_prompt")
    if template is None:
        available = ", ".join(templates)
        raise PromptError(f"提示词文件里缺少 [review_prompt] 段（现有：{available}）")
    return template


def get_reflect_prompt(prompt_file: str | None = None) -> PromptTemplate:
    """取自我反思提示词。缺失时抛错，由调用方决定是否降级。"""
    templates = load_prompt_file(prompt_file or None)
    template = templates.get("reflect_prompt")
    if template is None:
        raise PromptError("提示词文件里缺少 [reflect_prompt] 段")
    return template


def _build_env() -> Environment:
    """构造 Jinja2 环境。

    - StrictUndefined：变量缺失立刻报错，不静默渲染成空串（见模块 docstring）
    - trim_blocks / lstrip_blocks：让 `{% if %}` 这类块级标签不留下空行，
      否则模型会看到一堆无意义的空白行，既浪费 token 也干扰阅读
    """
    return Environment(
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=False,
    )


def render_prompt(template: PromptTemplate, variables: dict[str, Any]) -> tuple[str, str]:
    """渲染出 (system, user)。

    变量缺失会抛 PromptError 并明确指出是哪个变量 —— 这种错误必须
    在开发期就暴露，而不是等到线上发现"某段要求凭空消失了"。
    """
    env = _build_env()
    try:
        system = env.from_string(template.system).render(**variables) if template.system else ""
        user = env.from_string(template.user).render(**variables) if template.user else ""
    except Exception as exc:                     # jinja2.UndefinedError 等
        raise PromptError(
            f"渲染提示词 [{template.name}] 失败：{exc}\n"
            f"可用变量：{', '.join(sorted(variables))}"
        ) from exc
    return system.strip(), user.strip()
