# PR-Agent 源码分析报告

> 分析对象：`D:\Git\pr-agent`（PR-Agent / Qodo Merge，v0.45.0，约 39,000 行 Python）
> 分析目的：拆解其架构与关键机制，作为 CodeSentry 的设计依据。
> 所有结论均基于源码定位，标注了文件名与行号，可直接跳转核对。

---

## 0. 一句话地图

```
入口层     cli.py / servers/*.py / action.yaml / Dockerfile
             ↓  把 CLI 参数、webhook 事件、Action 输入统一成 (pr_url, [command, ...args])
调度层     agent/pr_agent.py  command2class 字典分发 + 参数覆盖 + 异常兜底
             ↓
工具层     tools/*.py  每个命令一个类，统一 async run()
             ↓  组装 self.vars → 调模型 → 解析 → 发布
算法层     algo/  diff 解析 · token 预算与分块 · YAML 解析 · 多块合并 · 去重
             ↓
模型层     algo/ai_handlers/  BaseAiHandler 抽象 + LiteLLM 实现（30+ 供应商）
             ↓
平台层     git_providers/  GitProvider 抽象 + 工厂，9 个平台实现
配置层     settings/*.toml + config_loader.py  Dynaconf 字段级合并
```

调用一条命令的完整路径：

```
cli.py:run()
  → PRAgent().handle_request(pr_url, ["review", ...args])
    → apply_repo_settings(pr_url)              读仓库级 .pr_agent.toml
    → update_settings_from_args(args)          处理 --section.key=value
    → command2class["review"](pr_url, args=...)
      → PRReviewer.__init__()                  取 provider、组装 self.vars
      → PRReviewer.run()                       发进度评论 → 调模型 → 渲染 → 发布
```

---

## 1. 入口层：四种形态，一个出口

| 入口 | 文件 | 作用 |
|---|---|---|
| CLI | `pr_agent/cli.py` | `pr-agent --pr_url=... review`；也支持 `--stdin` / `--diff-file` 纯 diff 模式 |
| GitHub Action | `action.yaml` + `github_action/entrypoint.sh` | 容器化 Action，转发到 `servers/github_action_runner.py` |
| Webhook 服务 | `servers/github_app.py`(539行)、`gitlab_webhook.py`(528行)、`gitea_app.py`、`bitbucket_app.py`、`azuredevops_server_webhook.py`、`bitbucket_server_webhook.py`、`gerrit_server.py` | 各平台的 HTTP 事件接收与鉴权 |
| Serverless | `github_lambda_webhook.py`、`gitlab_lambda_webhook.py`、`github_polling.py` | AWS Lambda / 轮询模式 |

**共同出口**：全部收敛到 `PRAgent.handle_request(pr_url, request, notify)`。

CLI 的纯 diff 模式值得注意（`cli.py:152-175`）——它是唯一不依赖平台的路径：
`--stdin` / `--diff-file` 会强制 `config.git_provider = "plain-diff"`、
写入 `plain_diff.content`，并强制 `publish_output = True`。
**这就是 CodeSentry 的起点**：把这条"少数派路径"变成主路径。

---

## 2. 调度层：`command2class` 字典分发

`pr_agent/agent/pr_agent.py:30`：

```python
command2class = {
    "auto_review": PRReviewer,   "answer": PRReviewer,     "review": PRReviewer,
    "review_pr": PRReviewer,     "describe": PRDescription, "describe_pr": PRDescription,
    "improve": PRCodeSuggestions, "improve_code": PRCodeSuggestions,
    "ask": PRQuestions,          "ask_question": PRQuestions, "ask_line": PR_LineQuestions,
    "update_changelog": PRUpdateChangelog, "config": PRConfig, "settings": PRConfig,
    "help": PRHelpMessage,       "similar_issue": PRSimilarIssue,
    "add_docs": PRAddDocs,       "generate_labels": PRGenerateLabels,
}
commands = list(command2class.keys())
```

要点：

- **别名指向同一个类**：`review` / `review_pr` / `auto_review` 都是 `PRReviewer`，
  靠构造参数区分（`is_answer` / `is_auto`，见 `_run_command` 的 `if/elif` 分支）。
- **只有三个命令需要特判**（`answer` / `auto_review` / 其他），其余走通用分支。
- **未知命令返回 `False` 而非抛异常**（`pr_agent.py:265`）。

`_run_command()` 的完整流程（`pr_agent.py:208-294`）：

1. `apply_repo_settings(pr_url)` —— 同步拉取并应用被审查仓库的 `.pr_agent.toml`
2. 解析 `request`（支持字符串或列表两种形态，字符串走 `shlex` 切分）
3. `CliArgs.validate_user_args(args)` —— **安全校验**，禁止用参数覆盖 key/secret 类配置
4. `update_settings_from_args(args)` —— 把 `--section.key=value` 写成配置
5. `response_language` 注入：非 en-us 时给**所有**带 `extra_instructions` 的配置段
   追加"必须用该语言回答"（`pr_agent.py:236-257`）
6. 未知命令 -> warning + 返回 False
7. 实例化 tool 并 `await .run()`

外层 `handle_request()` 保证"**永不抛异常**"，永远返回 bool
（`pr_agent.py:296-309`），并在 `finally` 里 `flush_telemetry()` ——
因为 Serverless 环境会冻结实例，不跑 `atexit`。

`prepare_command()`（`pr_agent.py:141-176`）是自己写的分词器，理由很实际：
`str.split(" ")` 会把 `--section.key="words with spaces"` 拆坏，
而 `shlex.split` 会吃掉引号标记，导致带引号的 `#` 被 YAML 当成注释。

---

## 3. 工具层：13 个 Tool，统一生命周期

| 文件 | 类名(行号) | 职责 | 命令 |
|---|---|---|---|
| `pr_reviewer.py` | `PRReviewer`(146) | 整体审查报告 + 行级关键问题 | `/review` |
| `pr_description.py` | `PRDescription`(49) | PR 标题/类型/描述/改动摘要 | `/describe` |
| `pr_code_suggestions.py` | `PRCodeSuggestions`(124) | 可提交的行级代码建议（1936 行，最复杂） | `/improve` |
| `pr_add_docs.py` | `PRAddDocs`(20) | 生成 docstring 并提交 | `/add_docs` |
| `pr_update_changelog.py` | `PRUpdateChangelog`(38) | 更新 CHANGELOG.md | `/update_changelog` |
| `pr_generate_labels.py` | `PRGenerateLabels`(34) | 推断并打标签 | `/generate_labels` |
| `pr_questions.py` | `PRQuestions`(19) | 针对整个 PR 提问 | `/ask` |
| `pr_line_questions.py` | `PR_LineQuestions`(20) | 针对某行提问 | `/ask_line` |
| `pr_help_message.py` | `PRHelpMessage`(27) | 帮助文案 | `/help` |
| `pr_help_docs.py` | `PRHelpDocs`(326) | 仓库文档 RAG 问答 | `/help_docs` |
| `pr_similar_issue.py` | `PRSimilarIssue`(85) | 向量检索相似 issue | `/similar_issue` |
| `pr_config.py` | `PRConfig`(8) | 打印配置（不调模型） | `/config` |
| `ticket_pr_compliance_check.py` | — | PR 与 ticket 合规校验 | `/compliance` |

### PRReviewer 的生命周期（最值得照搬的模式）

**`__init__`（`pr_reviewer.py:157-247`）只做三件事**：

```python
self.git_provider = get_git_provider_with_context(pr_url)      # 1. 取 provider
self.main_language = get_main_pr_language(...)
self.pr_description, _ = self.git_provider.get_pr_description(...)
self.vars = {                                                  # 2. 组装 prompt 变量
    "title": self.git_provider.pr.title,
    "description": self.pr_description,
    "diff": "",                       # 先留空，用于计算模板本身的 token
    "num_max_findings": get_settings().pr_reviewer.num_max_findings,
    "require_score": get_settings().pr_reviewer.require_score_review,
    "require_tests": get_settings().pr_reviewer.require_tests_review,
    "extra_instructions": ...,
    "repo_context": build_repo_context(self.git_provider),
    "diff_hunk_format": render_diff_hunk_format(include_line_numbers=True),
    ...
}
self.token_handler = TokenHandler(pr, self.vars, prompt.system, prompt.user)   # 3. 建 token 计数器
```

**`run()`（`pr_reviewer.py:258-453`）是一张固定的流程图**：

```
1. 无文件 -> 直接返回
2. 增量审查门禁（-i）
3. publish_comment("Preparing review...", is_temporary=True)   ← 进度反馈
4. retry_with_fallback_models(self._prepare_prediction)        ← 带 fallback 的调用
5. _prepare_pr_review()                                        ← 渲染 + 校验
6. should_publish 判定（无重大问题且状态未变时跳过发布，避免噪声）
7. 发布：persistent_comment / check_run / 普通评论 三选一（含作者身份校验）
finally:
8. remove_comment(progress_response)                           ← 清理进度评论
9. 失败时 publish_comment(失败说明)
```

两个值得注意的细节：

- **进度评论 + finally 清理**：用户在漫长的模型调用期间能看到"正在处理"，
  结束后进度评论被删掉，不污染讨论。这是很低的成本换来的体验提升。
- **"无重大问题就不发评论"**（`_should_publish_review_no_suggestions`）：
  配合 `publish_output_no_suggestions` 配置，避免每次 PR 都刷一条
  "未发现重大问题"的噪声评论。

---

## 4. GitProvider 抽象与能力声明

### 接口设计（`git_providers/git_provider.py`，941 行）

基类 `GitProvider(ABC)`（L151）定义了四大类方法：

| 类别 | 代表方法 |
|---|---|
| 元数据 | `get_files` L323、`get_diff_files` L327、`get_pr_branch` L354、`get_user_id` L358、`get_commit_messages` L822、`get_pr_id` L537 |
| 内容读取 | `get_pr_description` L377、`get_repo_file_content` L520、`get_repo_context_ref` L523、`clone` L304 |
| 输出发布 | `publish_comment` L559、`publish_inline_comment` L724（抽象）、`publish_inline_comments` L732（抽象）、`publish_code_suggestions` L346、`publish_description` L334、`publish_labels` L756 |
| 评论管理 | `edit_comment` L365、`remove_comment` L740、`get_issue_comments` L744、`resolve_comment_thread` L582、`unresolve_comment_thread` L579、`add_reaction` L766 |
| 持久化 | `publish_persistent_comment` L602、`publish_persistent_comment_full` L656 |

### 能力声明：`is_supported(capability)`

`git_provider.py:153` 起有一长串 `supports_*` 方法，配合 `is_supported()`：

```python
def is_supported(self, capability: str) -> bool: ...
def supports_thread_resolution(self) -> bool: ...
def supports_comment_editing(self) -> bool:
    # 巧妙的实现：判断子类是否真的覆写了基类的 no-op
    return type(self).edit_comment is not GitProvider.edit_comment
```

**设计意图**：调用方永远不应该 `isinstance(provider, GithubProvider)`，
而是问 `provider.is_supported("...")`。因为各平台的 API 能力差异很大，
用能力查询比用类型判断更能表达"我需要的是这个能力"而不是"我需要这个平台"。

`supports_comment_editing` 的实现特别值得学：基类的 `edit_comment` 是返回 None 的
no-op，无法与"编辑成功"区分。它通过**比较方法对象是否是基类那一份**来判断
子类有没有真的实现 —— 零额外维护成本的能力探测。

### 工厂（`git_providers/__init__.py`）

```python
_GIT_PROVIDERS = {'github': GithubProvider, 'gitlab': GitLabProvider,
                  'bitbucket': ..., 'bitbucket_server': ..., 'azure': ...,
                  'codecommit': ..., 'local': LocalGitProvider,
                  'gerrit': ..., 'gitea': ..., 'plain-diff': PlainDiffGitProvider}

def register_git_provider(provider_id, provider_class): ...   # 外部包可注册（幂等 + 防遮蔽）
def get_git_provider_with_context(pr_url) -> GitProvider: ...  # 优先复用 context 里的实例
```

`get_git_provider_with_context` 用 `starlette_context` 做**请求级实例缓存**，
同一个请求内多次取 provider 不会重复初始化（对需要鉴权的平台很重要）。

### 本地与纯 diff 两个特殊 provider

- **`LocalGitProvider`**（`local_git_provider.py`）：用 `Repo` 读本地仓库，
  造一个 `PullRequestMimic` 假装是 PR 对象。输出写到 `review.md` / `description.md` /
  `improve.md`。明确把 `inline_code_comments` 关掉（本地无处可发）。
- **`PlainDiffGitProvider`**（`plain_diff_provider.py`）：从配置里读 diff 内容，
  输出到 stdout 或 `--output`。**它有 `publish_structured_review()`** —— 说明
  "结构化结果"这条路官方也走过。

---

## 5. 算法层：四个核心机制

### 5.1 diff 解析与"带行号渲染"（最重要的机制）

核心数据结构 `FilePatchInfo`（`algo/types.py:15`）：

```python
@dataclass
class FilePatchInfo:
    base_file: str      # 旧文件完整内容
    head_file: str      # 新文件完整内容（用于 existing_code 校验！）
    patch: str
    filename: str
    tokens: int = -1
    edit_type: EDIT_TYPE
    num_plus_lines / num_minus_lines: int
    language: Optional[str]
    ai_file_summary: str = None
    head_file_is_complete: bool = True
```

关键函数 `decouple_and_convert_to_hunks_with_lines_numbers`
（`algo/git_patch_processing.py:314-437`）：把 unified diff 转成
`__new hunk__` / `__old hunk__` 两段式，每行前置新文件绝对行号
（计算方式是 `start2 + i`，见 L390 / L431）。

配套工具：
- `extend_patch` L30 / `process_patch_lines` L75 —— 给 hunk 前后补上下文行
- `check_if_hunk_lines_matches_to_file` L204 —— 校验 hunk 是否真能对上原文件
- `handle_patch_deletions` L281 / `omit_deletion_hunks` L241 —— 剔除纯删除 hunk
- `extract_hunk_lines_from_patch` L440 —— 反向抽取行区间

### 5.2 Token 预算（`algo/token_handler.py`，198 行）

```python
class TokenEncoder:                       # L22
    def get_token_encoder(): ...          # L28  按模型选 encoding，o200k_base 兜底，带线程锁缓存

class TokenHandler:                       # L52
    def __init__(pr, vars, system, user): # L69  先用 Jinja2 渲染再计数
    def count_tokens(text, force_accurate=False): ...   # L181
```

`force_accurate=True` 时（L157 `_get_token_count_by_model_type`）：
OpenAI 系列直接估算；**Anthropic 调官方 `client.messages.count_tokens` API**
（L112-136，限制 9MB）；其他模型乘 `model_token_count_estimate_factor`。

`get_max_tokens(model)`（`algo/utils.py:1384`）、`clip_tokens(text, max)`（L1476）。

### 5.3 分块：三分支策略

`get_pr_multi_diffs()`（`algo/pr_processing.py:549`）→ `_pack_pr_multi_diffs()`（L215-282）：

```python
# 快路径：整体放得下就一次发（L611-613）
if total_tokens + OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD < get_max_tokens(model):
    return ["\n".join(patches_extended)]

# 否则按文件降序装箱，超预算即开新 chunk（L253-257）
if patch and (total_tokens + new_patch_tokens > get_max_tokens(model) - SOFT_THRESHOLD):
    final_diff_list.append("\n".join(patches))
    patches = []; total_tokens = token_handler.prompt_tokens; call_number += 1
```

- 单文件仍超限 → `large_patch_policy` = `clip` / `skip`（L236-251）
- 块数上限 `max_calls`（默认 5）
- 并行执行：`_predict_chunks()` L1511 用 `asyncio.gather`
- 失败块恢复：`_recover_failed_chunks()` L1554-1613 —— **只重试失败的块**，
  并按 fallback 模型的 token 上限重算预算
- 压缩：`pr_generate_compressed_diff()` L359，删除纯删除 hunk，
  富余时补"新增/修改/删除文件清单"（L135-172）

### 5.4 多块结果合并（reduce）

`algo/review_merge.py:merge_review_chunks()` L37，逐字段定义合并语义：

| 字段 | 策略 | 实现 |
|---|---|---|
| 文本类 | 取首个非空 | `_first_non_empty` L74 |
| 分数 | 取最差 | `_merge_score` L108 |
| 风险等级 | 取最坏（用优先级元组） | `_worst_of` L117 |
| 列表类 | 并集 + 按身份去重 | `_union_of_lists` L176 |
| key_issues | 按 (文件, 内容) 身份去重 | `_key_issue_identity` L190 |
| 耗时 | 不累加（并行执行） | `_merge_contribution_time` L288 |

同类机制还有 `review_finding_state.py`（跨运行的发现状态持久化）。

### 5.5 跨运行去重（`algo/inline_comment_dedup.py`）

反模式是"每次运行都把同样的行级建议重发一遍"（issue #2037 记录了这个现象）。
解法是把指纹**嵌在评论正文里**：

```python
BODY_MARKER_RE = re.compile(r"<!-- pr-agent-dedup: ([a-f0-9]{12}) -->")
CODE_MARKER_RE = re.compile(r"<!-- pr-agent-dedup-code: ([a-f0-9]{12}) -->")
KEY_ISSUE_LOCATION_MARKER_RE = re.compile(r"<!-- pr-agent-key-issue-location: ([a-f0-9]{12}) -->")
```

- body 指纹 = SHA256(文件, 锚定行, 归一化正文前 80 字符)
- code 指纹 = SHA256(文件, 锚定行, 第一个 ```` ```suggestion ```` 块内容)
- **OR 语义匹配**：能同时抓住"同文字不同代码"和"同代码不同文字"两种重述方式
- 归一化时先剥掉 `**Suggestion:**` 前缀和 `[label, importance: N]` 标签（L36-37 正则）

评论 ID 无法持久化（跨运行不可知），但**正文里的 HTML 注释可以** ——
这是"借用平台自身存储当数据库"的经典手法，零基础设施成本。

---

## 6. 模型层：极窄接口 + 供应商全覆盖

### 抽象接口（`algo/ai_handlers/base_ai_handler.py:4`）

```python
class BaseAiHandler(ABC):
    @abstractmethod
    def __init__(self): ...
    @property
    @abstractmethod
    def deployment_id(self): ...
    @abstractmethod
    async def chat_completion(self, model, system, user, temperature=0.2, img_path=None): ...
```

**接口极窄**是刻意设计：handler 唯一的职责是"把文本发出去、把文本拿回来"，
prompt 组装、结果解析、重试策略都不该渗进来。

### 三个实现

| 实现 | 文件 | 说明 |
|---|---|---|
| `OpenAIHandler` | `openai_ai_handler.py:15` | 直连 OpenAI SDK |
| `LangChainOpenAIHandler` | `langchain_ai_handler.py:23` | 经 LangChain（可选 extra 依赖） |
| `LiteLLMAIHandler` | `litellm_ai_handler.py:1939`（4554 行的巨型文件） | **主力后端** |

`LiteLLMAIHandler.chat_completion`（L3828 起）处理了非常多现实世界的坑：

- 指数退避重试（`_chat_completion_with_retry` L3844）
- GPT-5/6 的 `reasoning_effort`（L3928-4067）
- Claude extended thinking / adaptive thinking（L4043-4067）
- `max_tokens` vs `max_completion_tokens` 的选择
- Anthropic prompt caching（`cache_control_injection_points`）
- 部分模型不支持 system 消息 → 拼进 user（`user_message_only_models` L3969-3978）
- 图片输入（L3917-3919）
- 返回 `(resp, finish_reason)`

**支持的供应商**（L360-391 的映射表）：openai / azure / anthropic / bedrock /
vertex_ai / gemini / cohere / ollama / groq / xai / together_ai / replicate /
databricks / huggingface / openrouter / cloudflare / baseten / custom_openai /
openai_like / ovhcloud / predibase / sap / wandb / maritalk / nebius /
nlp_cloud / amazon_nova / gigachat / inception / lemonade / gdc 等，
基本等于 litellm 的全集。

### 模型路由与 fallback

- `algo/model_routing.py:35` `route_primary_model()`：按 PR 规模（hunks / files 数）
  从 `model_routing.rules` 里选更便宜的 primary 模型（默认关闭）
- `algo/pr_processing.py:480` `retry_with_fallback_models()`：把 `fallback_models`
  串成链，逐个尝试

配置侧对应的键（`settings/configuration.toml`）：

```toml
model="gpt-5.6"
fallback_models=["gpt-5.6-terra"]
#model_reasoning="gpt-5.6-terra"   # 自我反思用的推理模型
#model_weak="gpt-5.6-luna"         # 简单任务用的弱模型
```

---

## 7. Prompt 与配置系统：TOML 同源

### prompt 的组织（`settings/*.toml`）

每个 prompt 文件是 `[section]` 下两个多行字符串 `system` / `user`，
变量用 Jinja2：

```toml
[pr_code_suggestions_prompt]
system="""You are PR-Reviewer, an AI specializing in Pull Request (PR) code analysis...
The PR code diff will be in the following structured format:
{{ diff_hunk_format }}
{%- if not focus_only_on_problems %}
Provide up to {{ num_code_suggestions }} distinct ...
{% endif %}
class CodeSuggestion(BaseModel):     # ← 直接在 prompt 里给 Pydantic 定义，强制 YAML 输出
    relevant_file: str
    existing_code: str
    improved_code: str
    one_sentence_summary: str
    label: str
=====
"""
user="""--PR Info--
Title: '{{title}}'
The PR Diff:
======
{{ diff_no_line_numbers|trim }}
======"""
```

**为什么给 `class X(BaseModel)` 而不是 JSON Schema？**
模型对 Python 类定义的遵循度明显更高；而且 YAML 比 JSON 宽容
（代码 diff 里满是引号冒号，JSON 转义容易出错）。

**双份 diff 的巧思**：`pr_reviewer` 的 prompt 同时准备了
`diff`（带行号）和 `diff_no_line_numbers`（不带行号）。
前者用于需要精确定位的场景，后者用于纯阅读理解的场景。

### 配置加载（`config_loader.py`）

```python
dynconf_kwargs = {
    'core_loaders': [],                                    # 禁用默认 loader，否则 toml 会被加载两次
    'loaders': ['pr_agent.custom_merge_loader',            # 自定义合并 + 环境变量覆盖
                'dynaconf.loaders.env_loader'],
    'root_path': join(current_dir, "settings"),
    'merge_enabled': True,   # 关键：多 toml 同一 [section] 是字段级叠加，不是整段覆盖
}
global_settings = Dynaconf(
    envvar_prefix=False,
    load_dotenv=False,                       # 安全：不自动加载 .env
    settings_files=[... 24 个 toml ...],      # L20-45
    **dynconf_kwargs,
)
```

- `get_settings()`（L50）：优先返回 `starlette_context` 里的**请求级**设置，
  否则返回全局 —— 支持多请求并发时互不干扰
- `_find_repository_root()` / `_find_pyproject()`（L82-109）：
  从 cwd 向上找 `.git`，再读 `pyproject.toml` 的 `[tool.pr-agent]` 段作为本地配置
- `apply_secrets_manager_config()`（L112）：支持 AWS Secrets Manager 注入
  （嵌套键如 `openai.key`，且**只在环境变量未设时**才写入，保证 env 优先）

仓库级覆盖：`git_providers/utils.py::apply_repo_settings(pr_url)` ——
在命令执行前拉取被审查仓库的 `.pr_agent.toml`（可指定分支）。

### 配置键的组织（`settings/configuration.toml`）

49 个段落，值得注意的：

```toml
[config]
model / fallback_models / git_provider / publish_output / verbosity_level
max_model_tokens=32000          # 统一上限，不依赖模型真实窗口（便于横向对比）
max_output_tokens=0             # 输出预留
large_patch_policy="clip"       # "clip" | "skip"
persistent_inline_comments=false # 开启 HTML marker 去重
response_language="en-US"
ignore_pr_title / ignore_pr_target_branches / ignore_pr_labels / ignore_pr_authors
reaction_on_start="eyes"        # 命令触发时打个表情，用户知道被收到了

[pr_reviewer]
require_score_review=true / require_tests_review=true / require_security_review=true
require_estimate_effort_to_review=true / require_risk_assessment=false
persistent_comment=true         # 更新同一条评论而非新建
num_max_findings=3
enable_large_pr_chunking=false  # 分块审查（opt-in）
max_number_of_calls=3

[pr_code_suggestions]
suggestions_score_threshold=0
max_suggestions_per_file=0      # 0 = 不限制
demand_code_suggestions_self_review=false   # 作者自检复选框
parallel_calls=true
```

**设计模式**：所有"要不要输出某一段"都是 `require_xxx` 布尔值，
在 `self.vars` 里注入，模板用 `{% if %}` 控制。
新增一段报告内容 = 加一个配置键 + 模板里加一个 `if`，不改代码逻辑。

---

## 8. 输出发布与持久化

### 持久化评论（"更新而非刷屏"）

`GitProvider.publish_persistent_comment_full()`（`git_provider.py:656-721`）：

```python
identifiers = [identity_marker, legacy_initial_header] if identity_marker else [initial_header]
comment_to_update = None
for comment, _body in GitProvider._iter_persistent_comments(self, identifiers, ...):
    comment_to_update = comment; break          # 找回上次自己发的那条
if comment_to_update is not None:
    pr_comment_updated = pr_comment.replace(update_anchor, updated_anchor, 1)
    self.edit_comment(comment, pr_comment_updated)   # 原地编辑
```

配套机制：

- `_iter_persistent_comments()` L634：按身份标记 + 最新优先的顺序遍历，
  并可用 `require_agent_authorship` 校验"这条评论确实是本 agent 发的"
  （避免覆盖人类的评论）—— 对应 `is_comment_authored_by_pr_agent()`
- `update_header` 会插入 `#### (Review updated until commit <url>)`，
  让读者知道这条评论对应哪个版本
- `final_update_message` 追加一条 `**[Persistent review] updated to latest commit**`
  的状态评论，让关注者收到通知
- 失败时 `fallback_on_error` 决定是新建评论还是静默（默认回退到新建）

### 行级内联评论的落点（GitHub）

`github_provider.py:publish_code_suggestions()` L1012-1064：

```python
# 多行（L1041-1049）
post_parameters = {"body": body, "path": relevant_file,
                   "line": relevant_lines_end,           # 1-based，新文件
                   "start_line": relevant_lines_start, "start_side": "RIGHT"}
# 单行（L1050-1057）—— GitHub API 对单行/多行要求不同字段
post_parameters = {"body": body, "path": relevant_file,
                   "line": relevant_lines_start, "side": "RIGHT"}
```

发布前有两道校验：

1. `validate_comments_inside_hunks()`（L1687）—— 评论锚点必须落在 diff hunk 内
2. `_verify_code_comments()`（L974）/ `_try_fix_invalid_inline_comments()`（L987）
   —— 批量发布被拒后**逐条重试**，找出哪几条不合法并降级为普通评论
   （`_publish_inline_comments_fallback_with_verification` L909）

还有一条 legacy 路径：`create_inline_comment()` → `find_line_number_of_relevant_line_in_file()`
（`algo/utils.py:1593`），把绝对行号映射回 diff 的 `position`
（基于 `@@` 头 + 累加非 `-` 行的 delta，L1610-1639）——
用于不支持新版 review API 的平台。

### 建议的可应用性校验（`/improve` 的核心）

`pr_code_suggestions.py:_validate_suggestion()` L1238-1263：

```python
file_lines = diff_file.head_file.splitlines()
if relevant_lines_end > len(file_lines):
    return False, "the anchored range is outside the file", False
anchored_lines = file_lines[relevant_lines_start - 1:relevant_lines_end]
if existing_lines != anchored_lines:
    return False, "the existing code does not match the anchored range", True
```

**模型输出"文件绝对行号 + existing_code 片段"，再用 existing_code 与完整新文件做
字符串比对来判定能不能一键应用。**这比让模型自己算 diff position 稳得多。

### 结果渲染与过滤

- `_prepare_pr_code_suggestions()` L942-996：用 `one_sentence_summary_list`
  跳过同摘要建议（输出去重）
- `_limit_suggestions_per_file()` L1010-1047：按 score 降序，每文件最多 N 条
- score 阈值过滤在 `prepare_prediction_main` L1699-1708
- `push_inline_code_suggestions()` L1091-1136：`improved_code` 不可用时
  **降级为普通代码块评论**，而不是丢弃

### 自我反思（self-reflection）

先用常规模型出建议，再用**推理模型**回评：

- `self_reflect_on_suggestions()` L1889-1936：逐条给 `suggestion_score` + `why`，
  并**重新定位 `relevant_lines_start/end`**
- `analyze_self_reflection_response()` L874-926：把分数写回；
  行号缺失或为负则 `score = 0`（L883-889）；
  `existing_code == improved_code` 的建议清空一边（L917-924）
- `_self_reflect_with_fallback()` L839-872：在推理模型的 fallback 链里逐个尝试

反思 prompt 里写死了具体规则（`pr_code_suggestions_reflect_prompts.toml` L34-40）：
"existing_code 与 improved_code 相同 ≤ 7 分"、"加 docstring / 删 unused import ≤ 0 分"。
**把领域知识写进评分标准**是提高过滤精度的关键。

---

## 9. 工程化：测试、CI、容器

### 测试组织

| 目录 | 规模 | 测什么 |
|---|---|---|
| `tests/unittest/` | 235 个文件 | 纯单测，pytest + `PYTHONPATH=.`，`import-mode=importlib` |
| `tests/e2e_tests/` | 6 个文件 | 各 webhook 的真实端到端（`test_github_app.py`、`test_gitlab_webhook.py`…） |
| `tests/health_test/main.py` | 1 个 | 冒烟：起服务、打命令、看是否存活 |

单测覆盖很有针对性，例如 `test_cli_args_security.py`（参数越权）、
`test_apply_repo_settings_security.py`（仓库配置注入）、
`test_clip_tokens_invalid_budget.py`（边界值）、
`test_fix_json_escape_char.py`（模型输出的畸形 JSON）。

### CI（`.github/workflows/`）

`build-and-test.yaml`（单测 + 覆盖率）、`e2e_tests.yaml`、`docs-ci.yaml`、
`pre-commit.yml`、`codeql.yml`（安全扫描）、`pr-agent-review.yaml`（**自己审自己**）、
`publish.yml`、`release-drafter.yml`。

`pr-agent-review.yaml` 的写法值得注意：用固定 commit SHA 引用 Action 而不是 `@main`
（供应链安全），并通过 `env` 传 `GITHUB_ACTION_CONFIG.AUTO_DESCRIBE/AUTO_REVIEW/AUTO_IMPROVE`
控制自动执行的命令（注意全大写的环境变量命名风格）。

### Lint 与格式

只用 Ruff（`pyproject.toml` 的 `[tool.ruff]`，line-length=120，规则 `E/F/B/I`）。
有趣的是 `lint.ignore` 里列了 9 条**存量违规**并附注释
"treat it as a debt ledger: fix the code and drop entries rather than adding new ones"。
这是渐进式引入 linter 的实用做法：先让 CI 绿，再逐步还债。
**刻意不引入 `ruff format`** —— 避免一次性重排所有文件、污染 git blame。

### 容器

- `Dockerfile.github_action_dockerhub` —— GitHub Action 用（`action.yaml` 的 `image` 字段）
- `docker/Dockerfile` —— 多 target（`github_app` / `gitlab_webhook` / `test`）
- `docker/Dockerfile.lambda` —— AWS Lambda（配 `mangum`）
- 依赖统一由 `uv` + `uv.lock` 锁定，`required-version = "==0.12.10"` 三处同步

---

## 10. 复刻建议：CodeSentry 的取舍

| 机制 | 是否复刻 | CodeSentry 的做法 |
|---|---|---|
| `command2class` 字典分发 | ✅ | `tools/registry.py`，别名指向同一类 |
| Tool 生命周期（init 组装 vars / run 编排） | ✅ | `tools/review.py`，构造函数只做依赖注入 |
| prompt 变量 flat dict + `require_*` 开关 | ✅ | `ReviewTool._build_vars()`，模板用 `if` 控制段落 |
| 强制 YAML + Pydantic schema | ✅ | `prompts/review.toml` + `report/schema.py`（三层兜底） |
| diff 带行号渲染 | ✅ | `context/hunk.py`，同一套规则测试盯住渲染/解析一致 |
| token 三分支 + 分块 map-reduce | ✅ | `context/chunker.py` + `report/merge.py`（并额外记录 dropped） |
| 行号/可应用性校验 | ✅（部分） | `report/schema.py:validate_line_numbers`，越界即清空 |
| fallback 模型链 | ✅ | `llm/base.py:complete_with_fallback` |
| 进度反馈 | ✅ | 日志 + 渲染后一次性输出（本地场景无"进度评论"概念） |
| 持久化评论 / HTML marker 去重 | ❌ | 本地输出无"跨运行同一条评论"的问题；二期接平台时再引入 |
| 多 Provider（litellm） | ✅ | 一层封装，保留接口隔离以便换成 httpx 直连 |
| webhook 服务 / 9 个 provider / OTEL / RAG | ❌ | 与"本地 CLI 优先"的定位无关，砍掉 |
| Dynaconf | ❌ | 换成 stdlib `tomllib` + pydantic，并**记录每个键的来源**（`codesentry config`） |
| 自我反思 | ⏸️ | prompt 已预留 `[reflect_prompt]`，等真实误报数据再决定是否默认开 |

### 三个"本地优先"带来的结构性差异

1. **输入抽象前移**：pr-agent 把抽象放在 GitProvider（平台），
   CodeSentry 放在 DiffProvider（diff 来源）。于是新增输入形态不需要动工具层。
2. **行号校验几乎零成本**：平台模式要么额外调 API 取文件，要么放弃校验；
   本地模式直接读磁盘。所以 CodeSentry 把行号校验做成了**默认能力**。
3. **输出抽象同理**：`output/` 只有终端和文件两个实现，但接口在，
   二期加 GitHub 评论只需新增一个 Publisher。

---

## 附：关键文件速查

| 主题 | 位置 |
|---|---|
| 命令分发 | `pr_agent/agent/pr_agent.py:30` |
| 请求主流程 | `pr_agent/agent/pr_agent.py:208-294` |
| Reviewer 生命周期 | `pr_agent/tools/pr_reviewer.py:157` / `258` |
| Provider 基类 | `pr_agent/git_providers/git_provider.py:151` |
| 能力探测示例 | `pr_agent/git_providers/git_provider.py:592` |
| 持久化评论 | `pr_agent/git_providers/git_provider.py:656` |
| 行级建议落点 | `pr_agent/git_providers/github_provider.py:1012` / `659` |
| hunk 行号渲染 | `pr_agent/algo/git_patch_processing.py:314` |
| token 计数 | `pr_agent/algo/token_handler.py:181` |
| 分块 | `pr_agent/algo/pr_processing.py:215` / `549` |
| 多块合并 | `pr_agent/algo/review_merge.py:37` |
| 跨运行去重 | `pr_agent/algo/inline_comment_dedup.py:39-41` |
| 模型抽象 | `pr_agent/algo/ai_handlers/base_ai_handler.py:4` |
| LiteLLM 实现 | `pr_agent/algo/ai_handlers/litellm_ai_handler.py:1939` / `3828` |
| 可应用性校验 | `pr_agent/tools/pr_code_suggestions.py:1238` |
| 自我反思 | `pr_agent/tools/pr_code_suggestions.py:1889` / `874` |
| 配置加载 | `pr_agent/config_loader.py:17` / `50` |
| 配置默认值 | `pr_agent/settings/configuration.toml` |
