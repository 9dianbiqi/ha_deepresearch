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

        tasks_block = []
        for task in state.todo_items:
            summary_block = task.summary or "暂无可用信息"
            sources_block = task.sources_summary or "暂无来源"
            tasks_block.append(
                f"### 任务 {task.id}: {task.title}\n"
                f"- 任务目标：{task.intent}\n"
                f"- 检索查询：{task.query}\n"
                f"- 执行状态：{task.status}\n"
                f"- 任务总结：\n{summary_block}\n"
                f"- 来源概览：\n{sources_block}\n"
            )

        note_references = []
        for task in state.todo_items:
            if task.note_id:
                note_references.append(
                    f"- 任务 {task.id}《{task.title}》：note_id={task.note_id}"
                )

        notes_section_text = "\n".join(note_references) if note_references else "- 暂无可用任务笔记"

        note_content_section = ""
        if notes_context:
            parts = []
            for note_id, note_data in notes_context.items():
                content = note_data.get("content", "")
                if content:
                    parts.append(f"### 笔记 {note_id}\n{content}\n")
            if parts:
                note_content_section = (
                    "\n任务笔记完整内容（已由系统自动同步，无需手动读取）：\n" + "\n".join(parts)
                )

        prompt = (
            f"研究主题：{state.research_topic}\n"
            f"任务概览：\n{''.join(tasks_block)}\n"
            f"可用任务笔记清单：\n{notes_section_text}\n"
            f"{note_content_section}\n"
            "请基于以上所有信息撰写最终研究报告。笔记内容已提供，请直接引用。"
        )

        response = self._agent.run(prompt)
        self._agent.clear_history()

        report_text = response.strip()
        if self._config.strip_thinking_tokens:
            report_text = strip_thinking_tokens(report_text)

        return report_text or "报告生成失败，请检查输入。"
