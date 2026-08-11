"""Deterministic report completeness checks."""

from __future__ import annotations

from models import TodoItem
from research.report_validation import validate_report


def _task(*, sources: str | None = "- Source https://example.test") -> TodoItem:
    return TodoItem(
        id=1,
        title="Collect evidence",
        intent="Collect evidence",
        query="collect evidence",
        status="completed",
        summary="A completed summary.",
        sources_summary=sources,
    )


def test_valid_report_has_required_structure_and_citation() -> None:
    report = (
        "# Research report\n\n"
        "## Task 1: Collect evidence\n"
        "The task summary is complete and explains the evidence and its limits.\n\n"
        "## Findings\n"
        "The findings are actionable and traceable to the collected source. "
        "Source: https://example.test/reference"
    )

    result = validate_report(report, [_task()])

    assert result.valid
    assert result.failure_reasons == ()
    assert result.covered_tasks == 1
    assert result.has_citations


def test_empty_and_short_reports_are_rejected() -> None:
    result = validate_report("# Report\n\n## 1.", [_task()])

    assert not result.valid
    assert "report_too_short" in result.failure_reasons
    assert "unfinished_section" in result.failure_reasons
    assert "insufficient_sections" in result.failure_reasons


def test_report_requires_citations_when_tasks_have_sources() -> None:
    report = (
        "# Research report\n\n"
        "## Task 1: Collect evidence\n"
        "The task summary is complete and explains the evidence and its limits.\n\n"
        "## Findings\n"
        "The findings are actionable and traceable to the collected source, "
        "but the citation was omitted from this draft."
    )

    result = validate_report(report, [_task()])

    assert not result.valid
    assert "citations_missing" in result.failure_reasons


def test_unclosed_fence_and_obvious_truncation_are_rejected() -> None:
    report = (
        "# Research report\n\n"
        "## Task 1: Collect evidence\n"
        "The task summary is complete and explains the evidence and its limits.\n\n"
        "## Findings\n"
        "The findings are actionable.\n\n````\npartial"
    )

    result = validate_report(report, [_task(sources=None)])

    assert not result.valid
    assert "unclosed_code_fence" in result.failure_reasons
    assert "truncated_report" not in result.failure_reasons


def test_task_coverage_is_required_for_completed_tasks() -> None:
    report = (
        "# Research report\n\n"
        "## Findings\n"
        "The findings are complete and sufficiently detailed for this run.\n\n"
        "## Limitations\n"
        "The report records the known limitations and follow-up considerations."
    )

    result = validate_report(report, [_task(sources=None)])

    assert not result.valid
    assert "task_coverage_missing" in result.failure_reasons
