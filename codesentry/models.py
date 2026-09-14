"""CodeSentry 数据模型。

设计原则：所有跨模块传递的数据都用 pydantic 模型描述，好处有三个：
1. 自带校验，边界处立刻暴露脏数据，而不是等到渲染/发布阶段才炸；
2. 字段有类型提示，IDE 能跳转，改字段时编译器/类型检查器能找出所有引用点；
3. 可以直接 `model_dump()` 成 JSON，给 `--json-output` 和未来的 CI 集成复用。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class EditType(str, Enum):
    """文件的变更类型。与 git 的语义对齐。

    ADDED / DELETED / MODIFIED / RENAMED 之外还有 UNKNOWN：
    diff 头信息不完整时（例如手工拼的片段）落到这里，不做特殊处理。
    """

    ADDED = "ADDED"
    DELETED = "DELETED"
    MODIFIED = "MODIFIED"
    RENAMED = "RENAMED"
    UNKNOWN = "UNKNOWN"


class FilePatchInfo(BaseModel):
    """单个文件的完整变更信息。

    为什么要有 base_file / head_file 这两个"全文"字段？
    —— 因为它是抵抗模型幻觉行号的唯一依据。模型返回 "第 42 行有问题" 时，
    我们拿 head_file 的第 42 行出来看看，对不上就说明模型在编，可以降级处理。
    只靠 diff 是做不到这一点的（diff 里只有 hunk，没有完整行号空间）。

    本地审查模式的一个优势：head_file 可以直接读磁盘上的当前文件，
    不像平台模式必须额外调一次 API 拉取。
    """

    filename: str                                    # 新文件路径（仓库相对）
    base_path: Optional[str] = None                  # 旧文件路径（rename 时与 filename 不同）
    base_file: str = ""                              # 旧版本全文，取不到则为空串
    head_file: str = ""                              # 新版本全文，取不到则为空串
    patch: str = ""                                  # 该文件的 unified diff 原文
    edit_type: EditType = EditType.UNKNOWN
    language: Optional[str] = None
    tokens: int = -1                                 # patch 的 token 数，-1 表示尚未计算
    skipped: bool = False                            # 是否被忽略规则/策略排除
    skip_reason: str = ""                            # 排除原因，会写进最终报告
    clipped: bool = False                            # 是否因超预算被裁剪过内容

    @property
    def head_line_count(self) -> int:
        """新文件总行数。用于校验模型给出的行号是否越界。"""
        return len(self.head_file.splitlines()) if self.head_file else 0

    @property
    def has_head_context(self) -> bool:
        """是否拿到了新文件全文。没拿到就不能做行号校验，只能降级。"""
        return bool(self.head_file)

    def get_head_lines(self, start: int, end: int) -> list[str]:
        """取新文件 [start, end] 闭区间的行（1-based，两侧都含）。

        越界时返回空列表，调用方据此判断"模型的行号不可信"。
        """
        lines = self.head_file.splitlines()
        if start < 1 or end < start or end > len(lines):
            return []
        return lines[start - 1:end]


class DiffBundle(BaseModel):
    """一次审查的全部输入。provider 层产出的就是这个对象。

    把"从哪来"（provider/source_ref）也记下来，是为了在报告里说清楚
    这次审的到底是 staged、还是与 main 的差异，避免用户看错对象。
    """

    title: str = ""
    description: str = ""
    commit_messages: str = ""
    files: list[FilePatchInfo] = Field(default_factory=list)
    provider: str = ""                               # "git-local" / "diff-file" / "stdin"
    source_ref: str = ""                             # 人类可读的来源描述，如 "main..HEAD"

    @property
    def total_patch_tokens(self) -> int:
        """所有文件 patch 的 token 合计。分块决策的输入。"""
        return sum(f.tokens for f in self.files if f.tokens > 0)

    @property
    def active_files(self) -> list[FilePatchInfo]:
        """未被跳过的文件。"""
        return [f for f in self.files if not f.skipped]


# --------------------------------------------------------------------------- #
# 模型输出结构（LLM 返回值 → 这里）
# --------------------------------------------------------------------------- #

class KeyIssue(BaseModel):
    """一个关键问题。

    start_line / end_line 允许为 None：模型经常给不出准确行号，
    或者给了一个越界的行号。这时我们宁可在报告里标注"行号未验证"，
    也不要假装它是对的 —— 错误的行号比没有行号更伤信任。
    """

    relevant_file: str
    issue_content: str
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    severity: Literal["critical", "high", "medium", "low"] = "medium"
    # 由本地校验填入：True=行号已对齐文件内容，False=越界或缺失
    line_verified: bool = False

    @field_validator("severity", mode="before")
    @classmethod
    def _normalize_severity(cls, v: Any) -> str:
        """模型可能返回 "Critical" / "严重" 之类，统一收敛到小写枚举值。"""
        if not isinstance(v, str):
            return "medium"
        v = v.strip().lower()
        mapping = {"critical": "critical", "严重": "critical", "blocker": "critical",
                   "high": "high", "高": "high",
                   "medium": "medium", "中": "medium", "moderate": "medium",
                   "low": "low", "低": "low", "minor": "low"}
        return mapping.get(v, "medium")

    @field_validator("start_line", "end_line", mode="before")
    @classmethod
    def _coerce_line(cls, v: Any) -> Optional[int]:
        """行号可能是 "42"、42、"-1"、"N/A"。非正整数一律归为 None。"""
        if v is None:
            return None
        try:
            n = int(str(v).strip())
        except (TypeError, ValueError):
            return None
        return n if n > 0 else None


class FileSummary(BaseModel):
    """单个文件的审查要点。"""

    relevant_file: str
    changes_summary: str = ""


class RunDetails(BaseModel):
    """运行详情。让用户对"这次花了多少"有感知，也便于排查异常。"""

    model: str = ""
    fallback_models: list[str] = Field(default_factory=list)
    provider: str = ""
    source_ref: str = ""
    num_files: int = 0
    num_skipped_files: int = 0
    num_chunks: int = 0
    num_llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed_seconds: float = 0.0


class ReviewResult(BaseModel):
    """结构化审查结论。这是本项目的核心产物。

    所有"可选段落"都是 Optional 或带默认值，因为 prompt 里的 require_*
    开关会让模型不输出对应字段；解析层不能因此报错。
    """

    summary: str = ""
    score: Optional[int] = None
    risk_level: Optional[Literal["low", "medium", "high"]] = None
    merge_recommendation: Optional[str] = None
    effort_to_review: Optional[str] = None
    suggested_tests: Optional[str] = None
    key_issues: list[KeyIssue] = Field(default_factory=list)
    file_summaries: list[FileSummary] = Field(default_factory=list)
    run_details: Optional[RunDetails] = None
    # 模型没按 YAML 返回时的原始文本，渲染层会原样贴出来
    raw_text: Optional[str] = None
    # 解析过程中产生的降级说明，例如 "第 2 块结果 YAML 解析失败"
    warnings: list[str] = Field(default_factory=list)

    @field_validator("score", mode="before")
    @classmethod
    def _coerce_score(cls, v: Any) -> Optional[int]:
        """分数容忍 "7/10"、"7分" 这类写法。"""
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return int(v)
        text = str(v).strip()
        import re
        m = re.search(r"\d+", text)
        return int(m.group()) if m else None

    @field_validator("risk_level", mode="before")
    @classmethod
    def _coerce_risk(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        text = str(v).strip().lower()
        mapping = {"low": "low", "低": "low", "minor": "low",
                   "medium": "medium", "中": "medium", "moderate": "medium", "medium risk": "medium",
                   "high": "high", "高": "high", "critical": "high", "severe": "high"}
        return mapping.get(text)
