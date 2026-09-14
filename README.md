# CodeSentry 🛡️

**本地优先的 AI 代码审查 Agent。** 把一段 diff 交给它，拿回一份结构化的审查报告。

不需要 PR 平台、不需要 webhook、不需要在 CI 里挂服务。
一条命令，或者双击一个 `.bat`。

```
codesentry review                  # 审查当前仓库所有未提交的改动
codesentry review --base main      # 与 main 对比（等价于看一个 PR）
codesentry review --staged         # 只审 git add 过的内容
git diff main | codesentry review --stdin   # 从管道读 diff，不需要 git 仓库
```

---

## 这个项目是怎么来的

它是对 [PR-Agent](https://github.com/qodo-ai/pr-agent)（Qodo Merge，v0.45.0）做**源码级拆解后的一次重新实现**。

> 完整的源码分析报告（含架构分层、13 个 Tool 的清单与行号、7 个可复刻机制、
> Provider 抽象、配置系统、发布与去重策略、复刻取舍对照表）见
> [`docs/pr-agent-architecture-analysis.md`](docs/pr-agent-architecture-analysis.md)。

PR-Agent 是个成熟的工业级项目（约 3.9 万行），但它有两个对本场景不适用的前提：
必须挂在一个 git 平台上（GitHub / GitLab / Bitbucket…），以及极其庞大的依赖面
（9 个 provider 实现、webhook 服务、OTEL telemetry、RAG 检索…）。

CodeSentry 保留了它最有价值的机制，砍掉了与本场景无关的部分，并把输入层重新设计成
**以 diff 为中心**而不是以平台为中心：

| | PR-Agent | CodeSentry |
|---|---|---|
| 输入 | 必须是 PR URL | 本地 git diff / staged / 分支对比 / `.diff` 文件 / stdin |
| 输出 | 回写平台评论 | 终端 + Markdown，可选 JSON |
| 依赖 | 20+ 重量级依赖、Dynaconf、starlette | litellm + tiktoken + jinja2 + pydantic + rich |
| 规模 | ~39,000 行 | ~4,500 行（含大量中文注释）+ 1,100 行测试 |

### 从 PR-Agent 学到的六个关键机制

这些是拆解源码后确认"值得照搬"的部分，每一处都在本项目的注释里标明了出处：

1. **命令分发用显式字典**（`pr_agent.py:30` 的 `command2class`）
   → 本项目 `codesentry/tools/registry.py`。别名指向同一个类，加命令只加一行。

2. **Tool 生命周期固定**（`pr_reviewer.py:157` 的 `__init__` 组装 vars、`run()` 走流程）
   → `codesentry/tools/review.py`。构造函数只做依赖注入与变量组装，`run()` 只做编排。

3. **prompt 变量集中在一个 flat dict + `require_*` 布尔开关控制段落**
   （`pr_reviewer.py:206-240`）
   → `ReviewTool._build_vars()`。改"报告里要不要这一段"是配置问题，不用改代码。

4. **强约束 YAML 输出 + Pydantic schema**（`settings/pr_reviewer_prompts.toml`）
   → `codesentry/prompts/review.toml`。比 JSON mode 兼容性好得多，小模型也稳。

5. **diff 渲染成带行号的结构化文本**（`git_patch_processing.py:314` 的
   `decouple_and_convert_to_hunks_with_lines_numbers`）
   → `codesentry/context/hunk.py`。**这是全项目对审查质量影响最大的一处设计**，
   详见下节。

6. **token 预算三分支 + 分块 map-reduce**（`pr_processing.py:215, 549`）
   → `codesentry/context/chunker.py` + `codesentry/report/merge.py`。

---

## 三个核心设计决策

### 一、让程序替模型算好行号

unified diff 的行号只出现在 hunk 头（`@@ -10,6 +12,8 @@`），模型想引用
"第 42 行"就得自己做加法。实测这条链路上模型出错率很高 —— 加了忘记减、
把旧行号当新行号用。既然这是纯机械劳动，就交给程序做：

```
### 文件 `app/report.py` (MODIFIED · Python · +7/-3)
@@ -1,6 +1,14 @@
    1| import csv
    2| 
    4| def export(rows, path, batch_size=100):
     |-    with open(path, "w") as fh:
    5|+    """把 rows 分批写入 csv。"""
    6|+    fh = open(path, "w")
```

行号列是新文件中的**绝对行号**，模型只需照抄。同一套规则在 system prompt 里
同步说明，并有测试（`test_line_number_rendering_matches_new_file_positions`）
盯着渲染层与解析层不会各写一套逻辑而错位。

### 二、拿文件真实内容校验行号

本地模式有个平台模式没有的优势：**新版本文件就在磁盘上**，读一下就知道
模型给的行号对不对。于是：

| 情况 | 处理 |
|---|---|
| 有文件全文 + 行号在范围内 | 标 ✅，读者可信 |
| 有文件全文 + 行号越界 | 直接清空行号 + 记录告警（这个行号一定是错的） |
| 拿不到全文（纯 diff 模式） | 保留行号，标 ⚠️ 行号未校验 |

关键取舍：**错误行号比没有行号更伤信任**。一个指向错误位置的行号会让读者
翻到无关代码，然后开始怀疑整份报告。所以越界就删，无依据就不妄断。

### 三、分块策略保证"不静默丢失"

大 diff 必然超上下文。处理分三层降级，按信息损失从小到大：

1. **装得下** → 整发一次（90% 的情况）
2. **装不下** → 按文件切块（不是按 token 硬切！硬切会把函数劈成两半，
   模型看到半截代码会编出根本不存在的 bug），每块独立审查，可并行
3. **单文件也超限** → 裁剪（`clip`）或丢弃（`skip`）

无论走到哪一层，**被放弃的文件都会出现在报告的"未纳入审查"清单里**，
并说明原因。审查工具最不能做的事就是让用户以为看到的是全部改动。

---

## 快速开始

### 1. 装依赖

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e .        # Windows
# .venv/bin/python -m pip install -e .              # Linux / macOS
```

### 2. 配 API Key

复制 `.env.example` 为 `.env`，填入 key。litellm 会按模型名前缀自动挑环境变量：

```ini
DEEPSEEK_API_KEY=sk-xxxxxxxx
```

| 模型写法 | 需要的环境变量 |
|---|---|
| `deepseek/deepseek-chat` | `DEEPSEEK_API_KEY` |
| `openai/gpt-4o-mini` | `OPENAI_API_KEY` |
| `dashscope/qwen-plus` | `DASHSCOPE_API_KEY` |
| `anthropic/claude-sonnet-4` | `ANTHROPIC_API_KEY` |
| `ollama/qwen2.5-coder` | 无需 key（本地） |

### 3. 跑起来

```bash
codesentry config                                     # 先看看配置，不花钱
codesentry review --dry-run                           # 看看要发什么给模型，不花钱
codesentry review --output review.md                  # 真跑一次
```

或者直接双击 `scripts\run_review.bat`。

---

## 命令参考

```
codesentry review [路径] [选项]
```

**diff 来源**（默认：未提交的改动）

| 选项 | 说明 |
|---|---|
| `--staged` | 只审 `git add` 过的内容（适合 pre-commit） |
| `--base REF` | 与分支对比，例如 `--base main` |
| `--commit-range A..B` | 审指定提交区间，例如 `HEAD~1..HEAD` |
| `--diff-file PATH` | 审一个现成的 unified diff 文件 |
| `--stdin` | 从标准输入读 diff |

**输出**

| 选项 | 说明 |
|---|---|
| `--output PATH` | 把 Markdown 报告写入文件 |
| `--json-output PATH` | 输出结构化 JSON（给 CI 消费） |
| `--no-color` | 禁用终端彩色 |

**行为**

| 选项 | 说明 |
|---|---|
| `--model MODEL` | 临时指定模型 |
| `--extra-instructions TEXT` | 追加要求，例如 `"重点看并发安全"` |
| `--dry-run` | 只打印将要发送的提示词与 token 预算，**不调模型、不花钱** |
| `-v` / `-vv` | 提高日志详细度（`-vv` 会打印完整提示词） |
| `-q` | 静默模式 |

**退出码**：`0` 成功 · `1` 运行错误 · `2` 没有改动 · `3` 用法错误

---

## 配置

优先级：**CLI 参数 > 环境变量 > `.codesentry.toml` > 内置默认**。
任何配置项都能从命令行临时覆盖（`--section.key=value`），
`codesentry config` 会打印每一项的最终值和**来源**。

```toml
[config]
model = "deepseek/deepseek-chat"
fallback_models = ["openai/gpt-4o-mini"]   # 主模型失败时依次尝试
max_model_tokens = 32000
max_output_tokens = 4000
temperature = 0.2
response_language = "zh-CN"                # 叙述用中文，代码保持原文

[review]
require_score = true
require_risk_assessment = true
require_tests_review = true
require_merge_recommendation = false
num_max_findings = 5
extra_instructions = ""

[ignore]
paths = ["**/node_modules/**", "**/*.lock", "**/package-lock.json"]
extensions = [".md", ".txt", ".svg", ".png"]
max_file_tokens = 8000
large_file_policy = "clip"                 # "clip" | "skip"
```

> `[ignore]` 不是小事：一次审查的成本几乎正比于送进去的 token 数。
> 前端仓库里 `package-lock.json` 常常占掉 80% 的体积，而它对代码审查的价值是零。

---

## 架构

```
codesentry/
├── diff/      输入层    git / diff 文件 / stdin  →  DiffBundle
├── context/   上下文层  行号渲染 · token 预算 · 分块
├── llm/       模型层    litellm 多 Provider 抽象 + 重试 + fallback
├── prompts/   提示词层  TOML 模板 + Jinja2 渲染
├── tools/     工具层    命令注册表 + 审查流程编排
├── report/    报告层    YAML 解析校验 · 多块合并 · Markdown/JSON 渲染
├── output/    输出层    终端 / 文件
└── utils/     通用      日志 · 过滤 · 运行统计
```

主流程（`tools/review.py`）：

```
1. provider.get_bundle()        取 diff
2. apply_ignore_rules()         过滤（并记录跳过了什么）
3. TokenCounter                 用"渲染后的真实文本"计 token
4. plan_chunks()                分块（含被放弃文件的记录）
5. render_prompt()              渲染 system / user
6. complete_with_fallback()     ×N 块，可并行，主模型失败自动换备用
7. parse_review()               YAML 三层兜底 + 行号校验
8. merge_results()              reduce（分数取最差、问题取并集去重）
9. render_markdown() + output   渲染与输出
```

每个模块都可以单独测试。`llm/base.py` 里的 `FakeLLMHandler` 只需实现一个
`_call_once` 就能替掉整个模型层 —— 于是整条链路可以在**无网络、无 API key**
的情况下做端到端测试。这也是它在测试里被大量使用的原因。

---

## 开发

```bash
.venv\Scripts\python.exe -m pytest tests -q          # 83 个测试
```

测试重点分布：

| 文件 | 盯住什么 |
|---|---|
| `test_parse_diff.py` | diff 解析正确性、**行号渲染与解析层一致**、glob 语义 |
| `test_chunker.py` | 不超预算、不超块数、**文件不静默丢失**、特殊 token 字面量的编码健壮性 |
| `test_report_schema.py` | 各种畸形模型输出（围栏/前后缀/别名/错行号）的兜底 |
| `test_cli_smoke.py` | 端到端链路、prompt 内容、CLI 退出码、真实 git 仓库 |

---

## 已知边界与后续方向

**当前不做**（按需再加）：

- 行级 ```` ```suggestion ```` 可一键应用的修改建议
  （需要 `existing_code` 反查链路，机制在 PR-Agent 的 `pr_code_suggestions.py`）
- GitHub / GitLab PR 集成与评论回写（`output/` 已经是抽象的，加一个 Publisher 即可）
- 本地规则引擎（正则/AST 静态检查与 LLM 结论合并）
- 自我反思迭代（`prompts/review.toml` 里的 `[reflect_prompt]` 已预留）

**已知短板**：

- **只看 diff，看不到未改动的代码。** 这是本项目的结构性边界 —— 上下文里
  只有变更行，因此"这个改动和别的模块的隐含假设冲突"这类跨模块判断容易漏。
  缓解方向是加一层"相关文件检索"（先按改动路径找调用方，再一并喂给模型），
  但会显著抬高 token 成本，暂未做。需要这类判断时请配合人工或对话式工具使用。
- **审查质量未做过对照实验。** "把 diff 转成带行号的结构化文本能提升审查质量"
  是基于机制推理的结论（模型不必自己做 hunk 头加法、行号可校验），
  而非同批次 PR 的盲评结果。请按机制优势理解，不要当成实测数字。

**使用注意**：

- 审查结论是**辅助**，不是替代。高风险改动仍需人工确认。
- 报告里的行号若标 ⚠️，说明当时拿不到文件全文，未做校验。
- 单次审查会把 diff 发送给所配置的模型供应商，敏感代码请先评估合规性。
- `.codesentry.toml` 的 `[ignore].extensions` 默认忽略 `.md` 等文档类型。
  如果你的仓库文档本身需要审查（比如 prompt 模板就是核心资产），
  记得把对应扩展名从黑名单里去掉 —— 否则会得到"所有文件都被过滤"的空结果。
