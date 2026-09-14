"""CodeSentry —— 本地优先的 AI 代码审查 Agent。

模块地图：
    diff/      输入层：把 git / diff 文件 / stdin 统一成 DiffBundle
    context/   上下文层：行号渲染、token 预算、分块
    llm/       模型层：litellm 多 Provider 抽象 + 重试 + fallback
    prompts/   提示词层：TOML 模板 + Jinja2 渲染
    tools/     工具层：命令注册表 + 审查流程编排
    report/    报告层：YAML 解析校验、多块合并、Markdown/JSON 渲染
    output/    输出层：终端 / 文件
    utils/     通用工具：日志、过滤、统计
"""

__version__ = "0.1.0"
