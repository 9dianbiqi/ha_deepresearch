"""Deterministic completeness checks for generated research reports."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from models import TodoItem

_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$")
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_CITATION_RE = re.compile(
    r"(?i)(?:\[[^\]]+\]\(https?://|\b(?:sources?|references?)\s*:\s*|"
    + "(?:\u6765\u6e90|\u53c2\u8003)\s*[:\uff1a])"
)
_TRUNCATION_SUFFIXES = (
    "...",
    "\u2026",
    ":",
    "\uff1a",
    ",",
    "\uff0c",
    "(",
    "\uff08",
    "-",
    "\u2014",
)
_MIN_REPORT_CHARS = 160
_MIN_SECTIONS = 2


@dataclass(frozen=True, slots=True)
class ReportValidationResult:
    """Describe deterministic report completeness checks."""

    valid: bool
    output_chars: int
    has_title: bool
    section_count: int
    completed_sections: int
    covered_tasks: int
    total_tasks: int
    requires_citations: bool
    has_citations: bool
    finish_reason: str | None = None
    failure_reasons: tuple[str, ...] = ()

    @property
    def failure_reason(self) -> str | None:
        """Return the first stable failure code, if validation failed."""
        return self.failure_reasons[0] if self.failure_reasons else None

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-ready validation summary."""
        return {
            "valid": self.valid,
            "output_chars": self.output_chars,
            "has_title": self.has_title,
            "section_count": self.section_count,
            "completed_sections": self.completed_sections,
            "covered_tasks": self.covered_tasks,
            "total_tasks": self.total_tasks,
            "requires_citations": self.requires_citations,
            "has_citations": self.has_citations,
            "finish_reason": self.finish_reason,
            "failure_reason": self.failure_reason,
            "failure_reasons": list(self.failure_reasons),
        }


def _task_is_covered(text: str, task: TodoItem) -> bool:
    """Return whether a report contains a task title or explicit task marker."""
    title = task.title.strip()
    if title and title.casefold() in text.casefold():
        return True
    task_id = re.escape(str(task.id))
    marker = (
        rf"(?i)(?:\btask\s*[-#]?\s*{task_id}\b|"
        rf"{chr(0x4EFB)}{chr(0x52A1)}\s*{task_id}(?:\b|[^\d]))"
    )
    return bool(re.search(marker, text))


def validate_report(
    report: object,
    tasks: Sequence[TodoItem],
    *,
    finish_reason: str | None = None,
) -> ReportValidationResult:
    r"""Validate one report without asking another model to judge it.

    The checks intentionally focus on stable structural guarantees.  They
    reject the observed ``# title\n\n## 1.`` output while allowing normal
    Markdown prose and non-English section headings.
    """
    text = report.strip() if isinstance(report, str) else ""
    lines = text.splitlines()
    headings: list[tuple[int, str, int]] = []
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if match:
            headings.append((len(match.group(1)), match.group(2).strip(), index))

    first_nonempty_index = next(
        (index for index, line in enumerate(lines) if line.strip()),
        -1,
    )
    first_heading = headings[0] if headings else None
    has_title = bool(
        first_heading
        and first_heading[2] == first_nonempty_index
        and first_heading[0] == 1
        and first_heading[1].strip(
            " .:;!?" + "\uff1a\uff1b\uff01\uff1f"
        )
    )

    section_headings = [heading for heading in headings if heading[0] >= 2]
    completed_sections = 0
    unfinished_heading = False
    for position, (level, title, line_index) in enumerate(section_headings):
        next_index = len(lines)
        for next_level, _, next_line_index in section_headings[position + 1 :]:
            if next_level <= level:
                next_index = next_line_index
                break
        body = [line.strip() for line in lines[line_index + 1 : next_index] if line.strip()]
        if re.fullmatch(r"\d+[.)]?", title.strip()) or not body:
            unfinished_heading = True
        if body:
            completed_sections += 1

    completed_tasks = [
        task for task in tasks if task.status in {"completed", "skipped"}
    ]
    covered_tasks = sum(_task_is_covered(text, task) for task in completed_tasks)
    requires_citations = any(
        isinstance(task.sources_summary, str) and task.sources_summary.strip()
        for task in completed_tasks
    )
    has_citations = bool(_URL_RE.search(text) or _CITATION_RE.search(text))

    final_line = next((line.strip() for line in reversed(lines) if line.strip()), "")
    reasons: list[str] = []
    if not text:
        reasons.append("empty_report")
    if len(text) < _MIN_REPORT_CHARS:
        reasons.append("report_too_short")
    if not has_title:
        reasons.append("missing_title")
    if len(section_headings) < _MIN_SECTIONS:
        reasons.append("insufficient_sections")
    if unfinished_heading:
        reasons.append("unfinished_section")
    if text.count("```") % 2:
        reasons.append("unclosed_code_fence")
    if final_line.endswith(_TRUNCATION_SUFFIXES) or (
        re.search(r"(?i)(?:to be continued|incomplete)$", final_line)
        or final_line.endswith(("\u5f85\u7eed", "\u672a\u5b8c"))
    ):
        reasons.append("truncated_report")
    if completed_tasks and covered_tasks < len(completed_tasks):
        reasons.append("task_coverage_missing")
    if requires_citations and not has_citations:
        reasons.append("citations_missing")

    return ReportValidationResult(
        valid=not reasons,
        output_chars=len(text),
        has_title=has_title,
        section_count=len(section_headings),
        completed_sections=completed_sections,
        covered_tasks=covered_tasks,
        total_tasks=len(completed_tasks),
        requires_citations=requires_citations,
        has_citations=has_citations,
        finish_reason=finish_reason,
        failure_reasons=tuple(dict.fromkeys(reasons)),
    )


__all__ = ["ReportValidationResult", "validate_report"]
