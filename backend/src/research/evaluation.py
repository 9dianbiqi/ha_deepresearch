"""Read-only quality assessment for persisted research snapshots."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .contracts import RunSnapshot


@dataclass(frozen=True, kw_only=True)
class AssessmentFinding:
    """One immutable quality finding produced by offline assessment."""

    severity: str
    message: str
    code: str | None = None

    def __post_init__(self) -> None:
        """Reject malformed values before they reach persisted assessment data."""
        if not isinstance(self.severity, str) or not self.severity.strip():
            raise ValueError("Assessment finding severity must not be empty.")
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("Assessment finding message must not be empty.")
        if self.code is not None and not isinstance(self.code, str):
            raise ValueError("Assessment finding code must be text or null.")

    def as_dict(self) -> dict[str, str | None]:
        """Return a detached JSON-ready representation."""
        return {
            "severity": self.severity,
            "message": self.message,
            "code": self.code,
        }


@dataclass(frozen=True, kw_only=True)
class ResearchAssessment:
    """Immutable result of evaluating one persisted research snapshot."""

    run_id: str
    evaluated_at: datetime
    score: float
    findings: tuple[AssessmentFinding, ...] = field(default_factory=tuple)
    schema_version: int = 1

    def __post_init__(self) -> None:
        """Validate the stable assessment persistence boundary."""
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("Assessment run ID must not be empty.")
        if (
            not isinstance(self.evaluated_at, datetime)
            or self.evaluated_at.tzinfo is None
            or self.evaluated_at.utcoffset() is None
        ):
            raise ValueError("Assessment timestamp must be timezone-aware.")
        if (
            isinstance(self.score, bool)
            or not isinstance(self.score, (int, float))
            or not math.isfinite(self.score)
            or not 0.0 <= float(self.score) <= 1.0
        ):
            raise ValueError("Assessment score must be finite and between 0 and 1.")
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != 1
        ):
            raise ValueError("Assessment schema version must be 1.")

        findings = tuple(self.findings)
        if not all(isinstance(item, AssessmentFinding) for item in findings):
            raise TypeError("Assessment findings must be AssessmentFinding values.")
        object.__setattr__(self, "score", float(self.score))
        object.__setattr__(self, "findings", findings)

    def as_dict(self) -> dict[str, Any]:
        """Return a detached JSON-ready representation."""
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "evaluated_at": self.evaluated_at.isoformat(),
            "score": self.score,
            "findings": [finding.as_dict() for finding in self.findings],
        }


class OfflineEvaluationService:
    """Assess persisted output without mutating or executing a research run."""

    def evaluate(self, snapshot: RunSnapshot) -> ResearchAssessment:
        """Apply baseline quality rules to snapshot output and follow-up context."""
        output = snapshot.output
        followup_context = snapshot.followup_context
        findings: list[AssessmentFinding] = []
        score = 1.0

        if not output:
            findings.append(
                AssessmentFinding(
                    severity="error",
                    code="missing_output",
                    message="Run completed without a result payload.",
                )
            )
            return ResearchAssessment(
                run_id=snapshot.run_id,
                evaluated_at=datetime.now(timezone.utc),
                score=0.0,
                findings=tuple(findings),
            )

        raw_todo_items = output.get("todo_items", [])
        todo_items = (
            raw_todo_items
            if isinstance(raw_todo_items, Sequence)
            and not isinstance(raw_todo_items, (str, bytes, bytearray))
            else ()
        )
        raw_report = output.get("report_markdown")
        report_markdown = raw_report if isinstance(raw_report, str) else ""

        if not todo_items:
            findings.append(
                AssessmentFinding(
                    severity="warning",
                    code="missing_tasks",
                    message="Planner did not produce any explicit todo items.",
                )
            )
            score -= 0.2

        if not report_markdown.strip():
            findings.append(
                AssessmentFinding(
                    severity="error",
                    code="missing_report",
                    message="Final report markdown is empty.",
                )
            )
            score -= 0.5

        incomplete_tasks: list[str] = []
        tasks_without_summary: list[str] = []
        for item in todo_items:
            status = getattr(item, "status", None)
            title = getattr(item, "title", None)
            summary = getattr(item, "summary", None)
            if isinstance(item, Mapping):
                status = item.get("status")
                title = item.get("title")
                summary = item.get("summary")
            if status != "completed" and title:
                incomplete_tasks.append(str(title))
            if title and (not isinstance(summary, str) or not summary.strip()):
                tasks_without_summary.append(str(title))

        if incomplete_tasks:
            findings.append(
                AssessmentFinding(
                    severity="warning",
                    code="incomplete_tasks",
                    message=f"Some tasks did not complete: {', '.join(incomplete_tasks)}",
                )
            )
            score -= 0.2

        if tasks_without_summary:
            findings.append(
                AssessmentFinding(
                    severity="warning",
                    code="missing_summaries",
                    message=(
                        "Some tasks are missing summaries: "
                        f"{', '.join(tasks_without_summary)}"
                    ),
                )
            )
            score -= 0.1

        if not followup_context:
            findings.append(
                AssessmentFinding(
                    severity="warning",
                    code="missing_compressed_context",
                    message="Compressed context payload is empty.",
                )
            )
            score -= 0.1

        return ResearchAssessment(
            run_id=snapshot.run_id,
            evaluated_at=datetime.now(timezone.utc),
            score=max(score, 0.0),
            findings=tuple(findings),
        )
