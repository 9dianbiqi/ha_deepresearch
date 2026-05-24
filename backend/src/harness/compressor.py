"""Compression helpers for long research context."""

from __future__ import annotations

from models import SummaryStateOutput


class ContextCompressor:
    """Produce compact summaries from research output."""

    def compress_output(self, output: SummaryStateOutput | None) -> dict[str, object]:
        """Return a lightweight context package for replay and follow-up runs."""
        if output is None:
            return {
                "run_summary": {
                    "completed_tasks": [],
                    "incomplete_tasks": [],
                    "report_excerpt": "",
                },
                "reasoning_memory": {
                    "key_findings": [],
                    "key_sources": [],
                    "open_questions": [],
                },
            }

        completed_tasks: list[dict[str, object]] = []
        incomplete_tasks: list[dict[str, object]] = []
        key_findings: list[str] = []
        key_sources: list[str] = []
        open_questions: list[str] = []

        for item in output.todo_items:
            summary = (item.summary or "").strip()
            sources = (item.sources_summary or "").strip()
            task_payload = {
                "task_id": item.id,
                "title": item.title,
                "summary_excerpt": summary[:280],
                "sources_excerpt": sources[:220],
            }
            if item.status == "completed":
                completed_tasks.append(task_payload)
            else:
                incomplete_tasks.append(task_payload)
                open_questions.append(item.title)

            if summary:
                key_findings.append(summary[:180])
            if sources:
                first_source = sources.splitlines()[0].strip()
                if first_source:
                    key_sources.append(first_source[:180])

        report = (output.report_markdown or output.running_summary or "").strip()
        return {
            "run_summary": {
                "completed_tasks": completed_tasks,
                "incomplete_tasks": incomplete_tasks,
                "report_excerpt": report[:1000],
            },
            "reasoning_memory": {
                "key_findings": key_findings,
                "key_sources": key_sources,
                "open_questions": open_questions,
            },
        }
