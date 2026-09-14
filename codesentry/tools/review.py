"""审查工具 —— 本项目的核心流程编排。

流程（每一步的职责边界都很清楚，便于单独测试与替换）：

    1. 取 diff          provider.get_bundle()
    2. 过滤             utils.filter.apply_ignore_rules()
    3. 计 token         TokenCounter + 渲染后的真实文本
    4. 分块             context.chunker.plan_chunks()
    5. 渲染提示词       prompts.loader.render_prompt()
    6. 调用模型         handler.complete_with_fallback()（可并行）
    7. 解析校验         report.schema.*（YAML 兜底 + 行号校验）
    8. 合并             report.merge.merge_results()
    9. 渲染与输出       report.render_* + output.*

两步之间没有隐式依赖，任何一步都可被替换或跳过。
**dry-run 模式**尤其重要：它跑到第 5 步就停，把将要发送的提示词和
token 统计打出来 —— 调 prompt 时不用真的花钱调模型，这是开发期最常用的开关。
"""

from __future__ import annotations

import asyncio
import json

from codesentry.config import Settings
from codesentry.context.budget import TokenCounter, get_model_max_tokens
from codesentry.context.chunker import Chunk, plan_chunks
from codesentry.context.hunk import (
    HUNK_FORMAT_LEGEND,
    render_file_block,
    render_file_index,
    render_files_for_prompt,
)
from codesentry.diff.base import DiffProvider
from codesentry.llm import BaseLLMHandler, model_chain
from codesentry.llm.base import LLMError
from codesentry.models import DiffBundle, ReviewResult
from codesentry.prompts.loader import get_review_prompt, render_prompt, resolve_language_name
from codesentry.report.merge import merge_results
from codesentry.report.render_json import render_json
from codesentry.report.render_md import render_markdown
from codesentry.report.schema import cap_issues, parse_review, sort_issues, validate_line_numbers
from codesentry.tools.base import BaseTool, ToolResult
from codesentry.utils.filter import apply_ignore_rules, render_skipped_summary
from codesentry.utils.logger import get_logger
from codesentry.utils.run_details import RunDetailsBuilder


class ReviewTool(BaseTool):
    """结构化代码审查。"""

    name = "review"

    def __init__(
        self,
        settings: Settings,
        handler: BaseLLMHandler,
        provider: DiffProvider,
        dry_run: bool = False,
    ) -> None:
        super().__init__(settings, handler)
        self.provider = provider
        self.dry_run = dry_run

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #

    async def run(self) -> ToolResult:
        bundle = self.provider.get_bundle()
        total_files = len(bundle.files)
        if total_files == 0:
            get_logger().info("没有检测到任何改动")
            return ToolResult(
                markdown="## 没有检测到改动\n\n当前范围内没有需要审查的文件。\n",
                has_content=False,
            )

        apply_ignore_rules(bundle.files, self.settings.ignore)
        active_files = bundle.active_files
        if not active_files:
            get_logger().warning("所有文件都被过滤规则跳过了")
            return ToolResult(
                markdown=(
                    "## 没有可审查的内容\n\n"
                    f"范围内的 {total_files} 个文件全部被忽略规则过滤。"
                    "可检查 `.codesentry.toml` 的 `[ignore]` 配置。\n"
                ),
                has_content=False,
            )

        model = self.settings.config.model
        counter = TokenCounter(model)
        # 用渲染后的真实文本计 token，而不是 patch 原文。
        # 渲染会加上行号列和标记，体积比原 diff 大 10~20%；
        # 用 patch 原文计数会导致预算低估，进而在真实调用时超限。
        for info in active_files:
            info.tokens = counter.count(render_file_block(info))

        prompt_template = get_review_prompt(self.settings.review.prompt_file)
        file_index = render_file_index(active_files)
        base_vars = self._build_vars(bundle, file_index=file_index, diff="")

        system, user = render_prompt(prompt_template, base_vars)
        prompt_tokens = counter.count(system) + counter.count(user)

        max_model_tokens = get_model_max_tokens(model, self.settings.config.max_model_tokens)
        plan = plan_chunks(
            files=active_files,
            prompt_tokens=prompt_tokens,
            max_model_tokens=max_model_tokens,
            max_output_tokens=self.settings.config.max_output_tokens,
            max_chunks=self.settings.review.max_chunks,
            large_file_policy=self.settings.ignore.large_file_policy,
            counter=counter,
        )
        chunks = plan.chunks
        # 被分块策略放弃的文件（预算耗尽或单文件超限）也要进报告，
        # 否则用户会以为自己看到的是全部改动。
        dropped_files = plan.dropped

        get_logger().info(
            f"变更范围：{len(active_files)} 个文件 · "
            f"模板 {prompt_tokens} tokens · 分 {len(chunks)} 批 · 模型 {model}"
        )

        if self.dry_run:
            return self._dry_run_result(bundle, prompt_template, base_vars, chunks, prompt_tokens,
                                        max_model_tokens, system, dropped_files)

        details = RunDetailsBuilder(
            model=model,
            fallback_models=model_chain(self.settings)[1:],
            provider=bundle.provider,
            source_ref=bundle.source_ref,
        )
        details.set_scope(num_files=total_files, num_skipped_files=total_files - len(active_files))

        results = await self._run_chunks(chunks, prompt_template, base_vars, details)

        merged = merge_results(
            results,
            chunk_labels=[f"第 {c.index + 1} 批" for c in chunks],
        )
        # 行号校验放在合并之后：此时所有分块的文件都在同一张索引表里，
        # 跨块引用同一文件的情况也能正确匹配。
        validate_line_numbers(merged, active_files)
        sort_issues(merged)
        cap_issues(merged, self.settings.review.num_max_findings)
        merged.run_details = details.build(num_chunks=len(chunks))

        markdown = render_markdown(
            merged,
            title=bundle.title,
            source_ref=bundle.source_ref,
            file_index=file_index,
            skipped_summary=self._skipped_summary(bundle.files, dropped_files),
            total_files=total_files,
            active_files=len(active_files),
        )
        json_text = render_json(
            merged,
            title=bundle.title,
            source_ref=bundle.source_ref,
            total_files=total_files,
            active_files=len(active_files),
        )
        return ToolResult(
            markdown=markdown,
            json_text=json_text,
            result=merged,
            notices=self._build_notices(dropped_files),
            has_content=True,
        )

    # ------------------------------------------------------------------ #
    # 分块执行
    # ------------------------------------------------------------------ #

    async def _run_chunks(
        self,
        chunks: list[Chunk],
        prompt_template,
        base_vars: dict,
        details: RunDetailsBuilder,
    ) -> list[ReviewResult]:
        """逐块调用模型。

        默认并行（parallel_chunks）。并行不改变正确性，因为各块之间
        没有共享状态；而串行在 5 块 × 60 秒的场景下会让用户等 5 分钟，
        并行通常只需 1 分钟出头。

        任何一块彻底失败都不中断整体：记录一条告警，继续跑其余块。
        "拿到 4/5 的结果 + 一条说明"远好于"整体失败，什么都没有"。
        """
        if self.settings.config.parallel_chunks and len(chunks) > 1:
            tasks = [
                self._run_single_chunk(chunk, len(chunks), prompt_template, base_vars, details)
                for chunk in chunks
            ]
            return list(await asyncio.gather(*tasks))

        results: list[ReviewResult] = []
        for chunk in chunks:
            results.append(
                await self._run_single_chunk(chunk, len(chunks), prompt_template, base_vars, details)
            )
        return results

    async def _run_single_chunk(
        self,
        chunk: Chunk,
        total_chunks: int,
        prompt_template,
        base_vars: dict,
        details: RunDetailsBuilder,
    ) -> ReviewResult:
        """跑一个块：渲染 → 调用 → 解析。"""
        vars_for_chunk = dict(base_vars)
        # 注意用 dict() 拷贝再改：base_vars 是所有块共享的模板，
        # 直接改会串味（第 2 块会拿到第 1 块标记的 is_partial 状态）。
        vars_for_chunk["diff"] = render_files_for_prompt(chunk.files)
        vars_for_chunk["is_partial"] = total_chunks > 1
        vars_for_chunk["chunk_index"] = chunk.index + 1
        vars_for_chunk["chunk_total"] = total_chunks

        try:
            system, user = render_prompt(prompt_template, vars_for_chunk)
        except Exception as exc:      # noqa: BLE001
            get_logger().error(f"第 {chunk.index + 1} 批提示词渲染失败：{exc}")
            return ReviewResult(warnings=[f"第 {chunk.index + 1} 批提示词渲染失败：{exc}"])

        get_logger().info(
            f"[{chunk.index + 1}/{total_chunks}] 分析 {len(chunk.files)} 个文件："
            + "、".join(chunk.filenames[:5])
            + ("…" if len(chunk.filenames) > 5 else "")
        )
        if self.settings.config.verbosity >= 2:
            get_logger().debug(f"--- system ---\n{system}\n--- user ---\n{user}")

        try:
            response = await self.handler.complete_with_fallback(
                model_chain(self.settings),
                system,
                user,
                temperature=self.settings.config.temperature,
            )
        except LLMError as exc:
            get_logger().error(f"第 {chunk.index + 1} 批模型调用失败：{exc}")
            return ReviewResult(warnings=[f"第 {chunk.index + 1} 批模型调用失败：{exc}"])

        details.record_call(response)
        return parse_review(response.text)

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #

    def _skipped_summary(self, all_files, dropped_files) -> str:
        """合并"被规则过滤"与"被预算放弃"两类未审查文件。

        刻意把两类分开列出：前者是用户配置的结果（可以调整 .codesentry.toml），
        后者是资源约束的结果（要调整 max_chunks / max_model_tokens）。
        混在一起用户不知道该改哪个。
        """
        if not self.settings.review.report_skipped_files:
            return ""
        parts: list[str] = []
        filtered = render_skipped_summary(all_files)
        if filtered:
            parts.append(filtered)
        if dropped_files:
            parts.append("**因 token 预算不足未纳入分析（可调大 `review.max_chunks` 或 `config.max_model_tokens`）：**")
            parts.extend(f"- `{f.filename}` —— {f.skip_reason}" for f in dropped_files)
        return "\n".join(parts)

    def _build_notices(self, dropped_files) -> list[str]:
        notices: list[str] = []
        if dropped_files:
            names = "、".join(f.filename for f in dropped_files[:5])
            more = f" 等 {len(dropped_files)} 个" if len(dropped_files) > 5 else ""
            notices.append(
                f"有 {len(dropped_files)} 个文件因 token 预算不足未被分析：{names}{more}。"
                "可调大 review.max_chunks 或 config.max_model_tokens。"
            )
        return notices

    def _build_vars(self, bundle: DiffBundle, file_index: str, diff: str) -> dict:
        """组装提示词变量。

        **所有**模板引用的变量都必须在这里出现（哪怕值是空串 / False）。
        原因：模板用 StrictUndefined 渲染，缺一个变量就会抛异常。
        这是刻意的 —— 让"漏传变量"在第一次运行时就炸掉，
        而不是安静地渲染出一份缺了关键要求的提示词。
        """
        review = self.settings.review
        # 主语言：取出现次数最多的语言，比"第一个文件的语言"更能代表整体
        languages = [f.language for f in bundle.active_files if f.language]
        main_language = max(set(languages), key=languages.count) if languages else ""
        return {
            # --- 基本信息 ---
            "title": bundle.title or "(无标题)",
            "source_ref": bundle.source_ref or "(未知来源)",
            "provider": bundle.provider or "(未知)",
            "num_files": len(bundle.active_files),
            "language": main_language,
            "commit_messages": bundle.commit_messages or "",
            # --- 内容 ---
            "diff": diff,
            "file_index": file_index,
            "diff_hunk_format": HUNK_FORMAT_LEGEND,
            # --- 输出开关（必须全部显式给出）---
            "require_score": review.require_score,
            "require_risk_assessment": review.require_risk_assessment,
            "require_tests_review": review.require_tests_review,
            "require_merge_recommendation": review.require_merge_recommendation,
            "require_effort_estimate": review.require_effort_estimate,
            "require_file_summaries": review.require_file_summaries,
            "num_max_findings": review.num_max_findings,
            "extra_instructions": review.extra_instructions or "",
            # 默认只报问题：全开会让报告充满无风险的主观偏好，稀释真正重要的发现
            "include_low_severity": False,
            # --- 分块标记（单块时由 is_partial=False 隐藏整段）---
            "is_partial": False,
            "chunk_index": 1,
            "chunk_total": 1,
            # --- 语言 ---
            "response_language": self.settings.config.response_language,
            "response_language_text": resolve_language_name(self.settings.config.response_language),
        }

    # ------------------------------------------------------------------ #
    # dry-run
    # ------------------------------------------------------------------ #

    def _dry_run_result(
        self,
        bundle: DiffBundle,
        prompt_template,
        base_vars: dict,
        chunks: list[Chunk],
        prompt_tokens: int,
        max_model_tokens: int,
        system: str,
        dropped_files=None,
    ) -> ToolResult:
        """不调模型，只报告"将要发生什么"。

        这是调 prompt 时最常用的模式：确认变量替换正确、分块合理、
        预算没超标，然后再花真钱。把这件事做成一等公民（而不是
        让用户自己加 print），能省掉大量无谓的 API 调用。
        """
        preview = dict(base_vars)
        first_chunk = chunks[0] if chunks else None
        preview["diff"] = render_files_for_prompt(first_chunk.files) if first_chunk else ""
        preview["is_partial"] = len(chunks) > 1
        preview["chunk_index"] = 1
        preview["chunk_total"] = max(len(chunks), 1)
        rendered_system, rendered_user = render_prompt(prompt_template, preview)

        summary = {
            "dry_run": True,
            "source": f"{bundle.provider} · {bundle.source_ref}",
            "model": self.settings.config.model,
            "fallback_models": model_chain(self.settings)[1:],
            "total_files": len(bundle.files),
            "analyzed_files": len(bundle.active_files),
            "skipped_files": [f"{f.filename}: {f.skip_reason}" for f in bundle.files if f.skipped],
            "dropped_by_budget": [f"{f.filename}: {f.skip_reason}" for f in (dropped_files or [])],
            "template_tokens": prompt_tokens,
            "max_model_tokens": max_model_tokens,
            "chunks": [
                {
                    "index": c.index + 1,
                    "files": c.filenames,
                    "diff_tokens": c.tokens,
                    "estimated_total_tokens": prompt_tokens + c.tokens,
                }
                for c in chunks
            ],
        }

        lines = [
            "# CodeSentry dry-run",
            "",
            "> 未调用任何模型。以下是本次审查将要发送的内容摘要。",
            "",
            "```json",
            json.dumps(summary, ensure_ascii=False, indent=2),
            "```",
            "",
            "## 将要发送的 system 提示词",
            "",
            "```text",
            rendered_system,
            "```",
            "",
            "## 将要发送的 user 提示词",
            "",
            "```text",
            rendered_user,
            "```",
            "",
        ]
        return ToolResult(
            markdown="\n".join(lines),
            json_text=json.dumps(summary, ensure_ascii=False, indent=2),
            has_content=True,
        )
