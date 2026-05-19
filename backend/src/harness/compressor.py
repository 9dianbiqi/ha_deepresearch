"""Compression helpers for long research context."""

from __future__ import annotations

from models import SummaryStateOutput


class ContextCompressor:
    """Produce compact summaries from research output."""

    def compress_output(self, output: SummaryStateOutput | None) -> dict[str, object]:
        """Return a lightweight context package for replay and follow-up runs."""
        if output is None:
            return {
                "report_excerpt": "",
                "task_briefs": [],
                "open_questions": [],
            }

        task_briefs = []
        open_questions: list[str] = []

        for item in output.todo_items:
            summary = (item.summary or "").strip()
            sources = (item.sources_summary or "").strip()
            task_briefs.append(
                {
                    "task_id": item.id,
                    "title": item.title,
                    "status": item.status,
                    "summary_excerpt": summary[:280],
                    "sources_excerpt": sources[:220],
                }
            )
            if item.status != "completed":
                open_questions.append(item.title)

        report = (output.report_markdown or output.running_summary or "").strip()
        return {
            "report_excerpt": report[:1000],
            "task_briefs": task_briefs,
            "open_questions": open_questions,
        }
