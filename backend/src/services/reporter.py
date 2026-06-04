"""Service that consolidates task results into the final report."""

from __future__ import annotations

from typing import Any

from hello_agents import ToolAwareSimpleAgent

from models import SummaryState
from config import Configuration
from utils import strip_thinking_tokens


class ReportingService:
    """Generates the final structured report."""

    def __init__(self, report_agent: ToolAwareSimpleAgent, config: Configuration) -> None:
        self._agent = report_agent
        self._config = config

    def generate_report(
        self,
        state: SummaryState,
        notes_context: dict[str, Any] | None = None,
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
                f"- 总结：\n{summary}\n"
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

        prompt = (
            f"研究主题：{state.research_topic}\n\n"
            f"{''.join(tasks_block)}\n"
            f"{note_section}\n\n"
            "请基于以上所有信息撰写最终研究报告。"
        )

        response = self._agent.run(prompt)
        self._agent.clear_history()

        report_text = response.strip()
        if self._config.strip_thinking_tokens:
            report_text = strip_thinking_tokens(report_text)

        return report_text or "报告生成失败，请检查输入。"
