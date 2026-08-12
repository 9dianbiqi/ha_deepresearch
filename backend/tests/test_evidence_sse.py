"""Contract tests for GitHub evidence SSE projections."""

from __future__ import annotations

from datetime import datetime, timezone

from research.contracts import EventKind, ResearchEvent
from research.legacy_sse import project_legacy_event


def event(kind: EventKind, payload: dict[str, object]) -> ResearchEvent:
    """Create a typed event with the same wire shape as a running session."""
    return ResearchEvent(
        kind=kind,
        run_id="019fefc4-d5f5-7d50-8e6e-3abb0a532c97",
        sequence=1,
        occurred_at=datetime.now(timezone.utc),
        payload=payload,
    )


def test_evidence_and_coverage_events_are_backward_compatible_sse() -> None:
    """New typed events project to additive flat event types."""
    evidence = project_legacy_event(
        event(
            EventKind.EVIDENCE_COLLECTED,
            {
                "snapshot_count": 2,
                "evidence_count": 12,
                "claim_count": 5,
                "artifact_count": 4,
            },
        )
    )
    coverage = project_legacy_event(
        event(
            EventKind.COVERAGE_UPDATED,
            {
                "coverage_score": 0.8,
                "covered_dimensions": ["overview"],
                "missing_dimensions": ["maintenance"],
                "gap_queries": ["maintenance evidence"],
                "allow_report": True,
            },
        )
    )
    artifact = project_legacy_event(
        event(
            EventKind.ARTIFACT_READY,
            {
                "artifact_id": "artifact_1",
                "artifact_type": "report_html",
                "mime_type": "text/html",
                "path": "artifacts/report.html",
                "title": "HTML",
            },
        )
    )
    assert evidence is not None and evidence["type"] == "github_evidence"
    assert coverage is not None and coverage["type"] == "coverage_update"
    assert coverage["missing_dimensions"] == ["maintenance"]
    assert artifact is not None and artifact["type"] == "artifact_ready"
