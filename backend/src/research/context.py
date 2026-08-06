"""Bounded follow-up context projected from canonical research task state."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from models import SummaryStateOutput, TodoItem

from .session import RunSession


def _clean_text(value: object, *, limit: int | None = None) -> str | None:
    """Return stripped non-empty text, optionally truncated to ``limit``."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if limit is not None:
        return cleaned[:limit]
    return cleaned


def _first_source_line(value: object) -> str | None:
    """Return the first non-empty source-summary line within the item budget."""
    if not isinstance(value, str):
        return None
    for line in value.splitlines():
        cleaned = _clean_text(line, limit=180)
        if cleaned is not None:
            return cleaned
    return None


@dataclass(frozen=True, slots=True, kw_only=True)
class FollowupContext:
    """Small versioned memory safe to carry into a follow-up run."""

    source_run_id: str
    key_findings: tuple[str, ...]
    key_sources: tuple[str, ...]
    open_questions: tuple[str, ...]
    schema_version: int = 1

    @classmethod
    def from_legacy(
        cls,
        raw: Mapping[str, object] | None,
    ) -> FollowupContext | None:
        """Convert a historical context dictionary into bounded typed memory."""
        if not raw:
            return None

        nested = raw.get("reasoning_memory")
        memory = nested if isinstance(nested, Mapping) else raw

        def bounded(
            field: str,
            *,
            count: int,
            item_limit: int | None = None,
        ) -> tuple[str, ...]:
            values = memory.get(field)
            if not isinstance(values, (list, tuple)):
                return ()
            result: list[str] = []
            for value in values:
                cleaned = _clean_text(value, limit=item_limit)
                if cleaned is None:
                    continue
                result.append(cleaned)
                if len(result) == count:
                    break
            return tuple(result)

        source_run_id = _clean_text(raw.get("source_run_id")) or "legacy"
        return cls(
            source_run_id=source_run_id,
            key_findings=bounded(
                "key_findings",
                count=FollowupContextProjector.MAX_FINDINGS,
                item_limit=FollowupContextProjector.MAX_ITEM_CHARS,
            ),
            key_sources=bounded(
                "key_sources",
                count=FollowupContextProjector.MAX_SOURCES,
                item_limit=FollowupContextProjector.MAX_ITEM_CHARS,
            ),
            open_questions=bounded(
                "open_questions",
                count=FollowupContextProjector.MAX_OPEN_QUESTIONS,
            ),
        )

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-ready representation."""
        return {
            "schema_version": self.schema_version,
            "source_run_id": self.source_run_id,
            "key_findings": list(self.key_findings),
            "key_sources": list(self.key_sources),
            "open_questions": list(self.open_questions),
        }


class FollowupContextProjector:
    """Project deterministic bounded memory from canonical task summaries."""

    MAX_FINDINGS = 5
    MAX_SOURCES = 3
    MAX_OPEN_QUESTIONS = 10
    MAX_ITEM_CHARS = 180

    def project(self, session: RunSession) -> FollowupContext:
        """Project follow-up context without reading events or raw web state."""
        return self.project_output(
            session.to_legacy_output(),
            source_run_id=session.run_id,
        )

    def project_output(
        self,
        output: SummaryStateOutput | None,
        *,
        source_run_id: str,
    ) -> FollowupContext:
        """Project the same bounded memory from a legacy output view."""
        tasks = output.todo_items if output is not None else []
        findings: list[str] = []
        sources: list[str] = []
        questions: list[str] = []

        for task in tasks:
            if task.status == "completed":
                finding = _clean_text(task.summary, limit=self.MAX_ITEM_CHARS)
                if finding is not None and len(findings) < self.MAX_FINDINGS:
                    findings.append(finding)
            else:
                question = _clean_text(task.title)
                if question is not None and len(questions) < self.MAX_OPEN_QUESTIONS:
                    questions.append(question)

            source = _first_source_line(task.sources_summary)
            if source is not None and len(sources) < self.MAX_SOURCES:
                sources.append(source)

        return FollowupContext(
            source_run_id=source_run_id,
            key_findings=tuple(findings),
            key_sources=tuple(sources),
            open_questions=tuple(questions),
        )

    def to_legacy_reasoning_memory(
        self,
        context: FollowupContext,
    ) -> dict[str, object]:
        """Return the historical reasoning-memory dictionary."""
        assembled = ResearchContextAssembler().assemble(context)
        if assembled is None:
            return {
                "key_findings": [],
                "key_sources": [],
                "open_questions": [],
            }
        return assembled


class ResearchContextAssembler:
    """Prepare bounded typed follow-up memory for coordinator input."""

    def assemble(self, context: FollowupContext | None) -> dict[str, object] | None:
        """Return only safe, budgeted follow-up fields."""
        if context is None:
            return None
        findings = self._bounded_text(
            context.key_findings,
            count=FollowupContextProjector.MAX_FINDINGS,
            item_limit=FollowupContextProjector.MAX_ITEM_CHARS,
        )
        sources = self._bounded_text(
            context.key_sources,
            count=FollowupContextProjector.MAX_SOURCES,
            item_limit=FollowupContextProjector.MAX_ITEM_CHARS,
        )
        questions = self._bounded_text(
            context.open_questions,
            count=FollowupContextProjector.MAX_OPEN_QUESTIONS,
        )
        return {
            "key_findings": findings,
            "key_sources": sources,
            "open_questions": questions,
        }

    @staticmethod
    def _bounded_text(
        values: tuple[str, ...],
        *,
        count: int,
        item_limit: int | None = None,
    ) -> list[str]:
        bounded: list[str] = []
        for value in values:
            cleaned = _clean_text(value, limit=item_limit)
            if cleaned is None:
                continue
            bounded.append(cleaned)
            if len(bounded) == count:
                break
        return bounded


def project_legacy_compressed_context(
    output: SummaryStateOutput | None,
    *,
    followup_context: FollowupContext | None = None,
) -> dict[str, object]:
    """Return the bounded historical compressor envelope."""
    if output is None:
        tasks: list[TodoItem] = []
        report = ""
    else:
        tasks = output.todo_items
        report = _clean_text(output.report_markdown or output.running_summary) or ""

    completed_tasks: list[dict[str, object]] = []
    incomplete_tasks: list[dict[str, object]] = []
    for task in tasks:
        task_payload = {
            "task_id": task.id,
            "title": task.title,
            "summary_excerpt": (_clean_text(task.summary) or "")[:280],
            "sources_excerpt": (_clean_text(task.sources_summary) or "")[:220],
        }
        if task.status == "completed":
            if len(completed_tasks) < FollowupContextProjector.MAX_FINDINGS:
                completed_tasks.append(task_payload)
        elif len(incomplete_tasks) < FollowupContextProjector.MAX_OPEN_QUESTIONS:
            incomplete_tasks.append(task_payload)

    projected = followup_context or FollowupContextProjector().project_output(
        output,
        source_run_id="legacy",
    )
    reasoning_memory = ResearchContextAssembler().assemble(projected)
    return {
        "run_summary": {
            "completed_tasks": completed_tasks,
            "incomplete_tasks": incomplete_tasks,
            "report_excerpt": report[:1000],
        },
        "reasoning_memory": reasoning_memory
        or {
            "key_findings": [],
            "key_sources": [],
            "open_questions": [],
        },
    }
