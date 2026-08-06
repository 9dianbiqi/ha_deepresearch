"""Service that consolidates task results into the final report."""

from __future__ import annotations

import logging
from threading import Lock
from typing import Any

from hello_agents import SimpleAgent

from config import Configuration
from models import SummaryState
from research.operations import OperationRejectedError, OperationScope
from research.session import CancellationRequestedError, DeadlineExceededError
from utils import strip_thinking_tokens

logger = logging.getLogger(__name__)


class ReportingService:
    """Generates the final structured report."""

    def __init__(self, report_agent: SimpleAgent, config: Configuration) -> None:
        """Initialize the service with its reporting agent and configuration."""
        self._agent = report_agent
        self._config = config
        self._agent_lock = Lock()

    def generate_report(
        self,
        state: SummaryState,
        notes_context: dict[str, Any] | None = None,
        *,
        operation_scope: OperationScope | None = None,
    ) -> str:
        """Generate a structured report based on completed tasks and notes."""
        max_summary_chars = 800  # truncate verbose summaries for the reporter

        tasks_block = []
        for task in state.todo_items:
            summary = (task.summary or "暂无可用信息").strip()
            if len(summary) > max_summary_chars:
                summary = summary[:max_summary_chars] + "\n\n... [摘要已截断]"

            # Compact source list — title + URL only, not full content
            sources_compact = (task.sources_summary or "").strip()
            if sources_compact:
                source_lines = sources_compact.splitlines()
                compact = []
                for line in source_lines:
                    stripped = line.strip()
                    if stripped and ("http" in stripped or not stripped.startswith("*")):
                        compact.append(stripped)
                    elif stripped:
                        compact.append(stripped[:120])
                sources_compact = "\n".join(compact[:8])  # keep at most 8 lines

            tasks_block.append(
                f"### 任务 {task.id}: {task.title}\n"
                f"- 目标：{task.intent}\n"
                f"- 状态：{task.status}\n"
                + (f"- 仓库：{task.repository}\n" if task.repository else "")
                + (
                    f"- 来源策略：{task.source_strategy}\n"
                    if task.source_strategy else ""
                )
                + f"- 总结：\n{summary}\n"
                + (f"- 来源：\n{sources_compact}\n" if sources_compact else "")
            )

        # Compact note reference list instead of full note content
        note_ids = [t.note_id for t in state.todo_items if t.note_id]
        note_section = ""
        if note_ids:
            note_items = [
                f"- 任务 {t.id}《{t.title}》: {t.note_id}"
                for t in state.todo_items if t.note_id
            ]
            note_section = "可用笔记：\n" + "\n".join(note_items)
        else:
            note_section = "- 暂无可用任务笔记"

        github_section = ""
        if state.github_context:
            github_markdown = str(state.github_context.get("markdown") or "").strip()
            if len(github_markdown) > 4000:
                github_markdown = github_markdown[:4000] + "\n\n... [GitHub context truncated]"
            github_section = (
                "## GitHub 仓库结构化上下文\n"
                f"{github_markdown}\n\n"
                "请将本次报告视为 GitHub 项目专项研究，优先使用 GitHub API 数据；"
                "最终报告需包含：仓库信息、核心洞察、时间线、架构概览、"
                "活动指标、风险与限制、参考来源、置信度评估。\n\n"
            )

        prompt = (
            f"研究主题：{state.research_topic}\n\n"
            f"{github_section}"
            f"{''.join(tasks_block)}\n"
            f"{note_section}\n\n"
            "请基于以上所有信息撰写最终研究报告。"
        )

        with self._agent_lock:
            try:
                if operation_scope is None:
                    response = self._agent.run(prompt)
                else:
                    response = self._agent.run(
                        prompt,
                        _research_operation_scope=operation_scope,
                    )
            except (
                OperationRejectedError,
                CancellationRequestedError,
                DeadlineExceededError,
            ):
                raise
            except Exception:
                logger.error("Reporter LLM call failed")
                return (
                    "报告生成失败，请稍后重试。\n\n"
                    "各任务总结已保存在左侧任务清单中，可下载笔记查看。"
                )
            finally:
                self._agent.clear_history()

        report_text = response.strip()
        if self._config.strip_thinking_tokens:
            report_text = strip_thinking_tokens(report_text)

        return report_text or "报告生成失败，请检查输入。"
