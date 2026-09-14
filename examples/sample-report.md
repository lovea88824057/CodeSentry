> **示例输出说明**：本文件由离线 FakeLLMHandler 生成，用于展示报告的排版与结构，
> 其中的结论是测试用的假数据，**不是真实模型的审查意见**。
> 真实运行时换成真实模型即可（见 README 的「快速开始」）。

# 🛡️ CodeSentry 审查报告

> **[sample.diff] 外部 diff 文件** · 文件 tests\fixtures\sample.diff

## 摘要

本次改动调整了用户查询与 CSV 导出的实现，引入了重试工具。

## 结论指标

| 指标 | 值 |
|---|---|
| 质量评分 | `██████░░░░` **6/10** |
| 风险等级 | 🟡 **中** |
| 合并建议 | ⚠️ 建议谨慎合并 |
| 建议审查工作量 | 约 15 分钟 |
| 本次范围 | 5 / 8 个文件 |

## 关键问题（2）

### 1. 🔴 [严重] `app/report.py` L6-L8 ✅

> 文件句柄从未关闭，长时间运行会耗尽文件描述符。

### 2. 🟠 [高] `app/service.py` L17-L18 ✅

> 未处理 db.query 返回 None 的情况，会抛 AttributeError。

## 文件逐项

| 文件 | 要点 |
|---|---|
| `app/retry.py` | 新增重试工具，逻辑直接。 |

## 建议补充的测试

建议补充 rows 为空、以及查询不到用户时的用例。

<details>
<summary>本次变更全貌（点击展开）</summary>

```
新增文件（1）：app/retry.py
修改文件（3）：app/service.py、app/report.py、app/nested/deep.py
删除文件（1）：legacy/old_util.py
```

</details>

<details>
<summary>未纳入审查的文件（点击展开）</summary>

- `docs/handbook.md` —— 无内容改动（仅重命名或权限变更）
- `package-lock.json` —— 命中忽略规则 `**/package-lock.json`
- `assets/logo.png` —— 二进制文件

</details>

<details>
<summary>运行详情</summary>

| 项 | 值 |
|---|---|
| 模型 | `deepseek/deepseek-chat` |
| 备用模型 | `openai/gpt-4o-mini` |
| 数据来源 | diff-file · 文件 tests\fixtures\sample.diff |
| 文件数 | 8（跳过 3） |
| 模型调用 | 1 次（1 批） |
| Token 用量 | 输入 652 · 输出 140 |
| 耗时 | 0.0 秒 |

</details>

---

*由 [CodeSentry](https://github.com/) 生成 · 本地优先的 AI 代码审查 Agent*
