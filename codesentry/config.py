"""配置加载与合并。

优先级（高 → 低）：CLI 参数 > 环境变量 > .codesentry.toml > 内置默认。

为什么自己写而不是上 Dynaconf（pr-agent 的选择）？
—— Dynaconf 的 merge 语义很好用，但它的"值从哪来"是不透明的：出问题时
很难回答"这个 model 到底是谁设的"。本项目按需自研一层薄封装，
顺手记录每个键的 provenance（来源），`codesentry config` 一跑就能看到全貌。
对调试配置问题来说，可观测性比功能丰富更重要。
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# 内置默认值
# --------------------------------------------------------------------------- #

DEFAULTS: dict[str, dict[str, Any]] = {
    "config": {
        "model": "deepseek/deepseek-chat",
        "fallback_models": [],
        "max_model_tokens": 32000,
        "max_output_tokens": 4000,
        "timeout": 120,
        "temperature": 0.2,
        "response_language": "zh-CN",
        "verbosity": 1,
        "api_base": "",
        "max_retries": 2,
        "parallel_chunks": True,
        "output_relevant_configurations": False,
    },
    "review": {
        "require_score": True,
        "require_risk_assessment": True,
        "require_tests_review": True,
        "require_merge_recommendation": False,
        "require_effort_estimate": True,
        "require_file_summaries": True,
        "num_max_findings": 5,
        "extra_instructions": "",
        "report_skipped_files": True,
        "max_chunks": 5,
        "prompt_file": "",
    },
    "ignore": {
        "paths": [
            "**/node_modules/**", "**/dist/**", "**/build/**", "**/.venv/**",
            "**/venv/**", "**/__pycache__/**", "**/*.lock", "**/*.min.js",
            "**/*.min.css", "**/package-lock.json", "**/pnpm-lock.yaml",
            "**/poetry.lock", "**/uv.lock", "**/Cargo.lock",
        ],
        "extensions": [
            ".md", ".txt", ".rst", ".svg", ".png", ".jpg", ".jpeg", ".gif",
            ".ico", ".pdf", ".woff", ".woff2", ".ttf", ".lock", ".sum",
        ],
        "max_file_tokens": 8000,
        "large_file_policy": "clip",     # "clip" | "skip"
    },
}

# 配置文件的候选名（按顺序查找）
CONFIG_FILENAMES = (".codesentry.toml", "codesentry.toml")


# --------------------------------------------------------------------------- #
# 强类型配置模型
# --------------------------------------------------------------------------- #

class ConfigSection(BaseModel):
    """[config] —— 模型与运行时行为。"""

    model: str
    fallback_models: list[str] = Field(default_factory=list)
    max_model_tokens: int = 32000
    max_output_tokens: int = 4000
    timeout: int = 120
    temperature: float = 0.2
    response_language: str = "zh-CN"
    verbosity: int = 1
    api_base: str = ""
    max_retries: int = 2
    parallel_chunks: bool = True
    output_relevant_configurations: bool = False


class ReviewSection(BaseModel):
    """[review] —— 审查内容开关。这些字段会被注入 prompt 变量。"""

    require_score: bool = True
    require_risk_assessment: bool = True
    require_tests_review: bool = True
    require_merge_recommendation: bool = False
    require_effort_estimate: bool = True
    require_file_summaries: bool = True
    num_max_findings: int = 5
    extra_instructions: str = ""
    report_skipped_files: bool = True
    max_chunks: int = 5
    prompt_file: str = ""


class IgnoreSection(BaseModel):
    """[ignore] —— 文件过滤规则。"""

    paths: list[str] = Field(default_factory=list)
    extensions: list[str] = Field(default_factory=list)
    max_file_tokens: int = 8000
    large_file_policy: str = "clip"


class Settings(BaseModel):
    """最终生效配置。

    provenance 记录"每个点分键的最终来源"，只用于展示（`codesentry config`），
    不参与任何业务逻辑，所以不放进各个 Section 里污染其定义。
    """

    config: ConfigSection
    review: ReviewSection
    ignore: IgnoreSection
    provenance: dict[str, str] = Field(default_factory=dict)
    config_file: Optional[str] = None


# --------------------------------------------------------------------------- #
# 值解析
# --------------------------------------------------------------------------- #

def parse_value(raw: str) -> Any:
    """把字符串解析成合适的 Python 类型。

    CLI/环境变量进来的都是字符串，但 max_model_tokens 显然应该是 int，
    require_score 应该是 bool。按"最具体优先"的顺序尝试。
    """
    text = raw.strip()
    low = text.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("none", "null", ""):
        return None
    # 先试 JSON（能处理 list/dict，也处理带引号的字符串）
    if text and text[0] in "[{\"'":
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return raw


def apply_override(data: dict[str, dict[str, Any]], dotted_key: str, value: Any) -> bool:
    """把 "config.max_model_tokens=8000" 这样的点分键写进嵌套字典。

    返回 True 表示键路径合法（落在已知 section 内），False 表示是拼错的前缀。
    这里刻意只允许写入已存在的 section，避免用户打错字后静默生成一个
    永远不生效的配置段。
    """
    if "." not in dotted_key:
        return False
    section, _, key = dotted_key.partition(".")
    if section not in data:
        return False
    data[section][key] = value
    return True


def _deep_copy_defaults() -> dict[str, dict[str, Any]]:
    """深拷贝默认值。

    必须深拷贝：默认值里有 list，如果直接共享引用，
    一次 append 就会污染后续所有加载。
    """
    return {sec: {k: (list(v) if isinstance(v, list) else v) for k, v in vals.items()}
            for sec, vals in DEFAULTS.items()}


# --------------------------------------------------------------------------- #
# 配置来源：文件 / 环境变量
# --------------------------------------------------------------------------- #

def find_config_file(start: Optional[Path] = None) -> Optional[Path]:
    """从 start 逐级向上找 .codesentry.toml。

    向上查找是为了让你在子目录运行 `codesentry review` 时，
    仍能命中仓库根目录的配置 —— 与 git 找 .git 的行为一致。
    """
    cur = (start or Path.cwd()).resolve()
    for _ in range(32):                    # 兜底深度，防止符号链接成环
        for name in CONFIG_FILENAMES:
            candidate = cur / name
            if candidate.is_file():
                return candidate
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def load_config_file(path: Path) -> dict[str, dict[str, Any]]:
    """读取 TOML 配置。

    解析失败直接抛异常而不是静默忽略：配置文件写错却"看起来正常运行"，
    比启动就报错危险得多。
    """
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    out: dict[str, dict[str, Any]] = {}
    for section, values in raw.items():
        if not isinstance(values, dict):
            continue
        out[section] = dict(values)
    return out


def load_env_overrides(environ: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """从环境变量提取覆盖项。

    支持两种写法：
      CODESENTRY_CONFIG__MAX_MODEL_TOKENS=8000   # 通用形式：<SECTION>__<KEY>
      CODESENTRY_MODEL=deepseek/deepseek-chat    # 简写形式：直接映射到 config.<key>
    """
    env = environ if environ is not None else os.environ
    overrides: dict[str, Any] = {}
    for name, value in env.items():
        if not name.startswith("CODESENTRY_"):
            continue
        rest = name[len("CODESENTRY_"):]
        if "__" in rest:
            section, _, key = rest.partition("__")
            overrides[f"{section.lower()}.{key.lower()}"] = parse_value(value)
        else:
            # 简写一律落到 [config] 段
            overrides[f"config.{rest.lower()}"] = parse_value(value)
    return overrides


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #

def load_settings(
    cli_overrides: Optional[list[str]] = None,
    config_path: Optional[str | Path] = None,
    cwd: Optional[Path] = None,
    environ: Optional[dict[str, str]] = None,
) -> Settings:
    """按优先级合并所有来源，返回最终配置。

    参数：
        cli_overrides: 形如 ["config.model=xxx", "review.num_max_findings=3"] 的原始字符串列表
        config_path:   显式指定配置文件；为 None 时自动向上查找
        cwd:           查找配置文件与 git 仓库的起点（测试用）
        environ:       注入环境变量字典（测试用）
    """
    data = _deep_copy_defaults()
    provenance: dict[str, str] = {
        f"{sec}.{key}": "默认值" for sec in data for key in data[sec]
    }

    # --- 1) 配置文件 ---
    resolved_path: Optional[Path]
    if config_path is not None:
        resolved_path = Path(config_path)
        if not resolved_path.is_file():
            raise FileNotFoundError(f"配置文件不存在：{resolved_path}")
    else:
        resolved_path = find_config_file(cwd)

    if resolved_path is not None:
        file_data = load_config_file(resolved_path)
        for section, values in file_data.items():
            if section not in data:
                # 未知 section 只警告不报错：便于不同版本间前向兼容
                continue
            for key, value in values.items():
                data[section][key] = value
                provenance[f"{section}.{key}"] = f"配置文件 {resolved_path.name}"

    # --- 2) 环境变量 ---
    for dotted_key, value in load_env_overrides(environ).items():
        if apply_override(data, dotted_key, value):
            provenance[dotted_key] = "环境变量"

    # --- 3) CLI 覆盖（最高优先级） ---
    for item in cli_overrides or []:
        if "=" not in item:
            continue
        dotted_key, _, raw_value = item.partition("=")
        dotted_key = dotted_key.lstrip("-").strip()
        if apply_override(data, dotted_key, parse_value(raw_value)):
            provenance[dotted_key] = "CLI 参数"

    settings = Settings(
        config=ConfigSection(**data["config"]),
        review=ReviewSection(**data["review"]),
        ignore=IgnoreSection(**data["ignore"]),
        provenance=provenance,
        config_file=str(resolved_path) if resolved_path else None,
    )
    return settings


def describe_settings(settings: Settings) -> str:
    """把配置渲染成"键 = 值  (来源)"的清单，供 `codesentry config` 展示。"""
    lines: list[str] = []
    if settings.config_file:
        lines.append(f"配置文件: {settings.config_file}")
    else:
        lines.append("配置文件: (未找到，使用内置默认值)")
    lines.append("")
    for section in ("config", "review", "ignore"):
        lines.append(f"[{section}]")
        section_data = getattr(settings, section)
        # 从类上取 model_fields：实例访问在 pydantic 2.11+ 已弃用
        for key in type(section_data).model_fields:
            value = getattr(section_data, key)
            source = settings.provenance.get(f"{section}.{key}", "-")
            rendered = json.dumps(value, ensure_ascii=False) if isinstance(value, list) else value
            lines.append(f"  {key} = {rendered}    # 来源: {source}")
        lines.append("")
    return "\n".join(lines)
