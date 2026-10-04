"""Frozen compatibility fixtures for the general research-core migration."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

from fastapi.testclient import TestClient

from main import create_app
from models import SummaryStateOutput, TodoItem
from research.contracts import EventKind, ResearchEvent
from research.evidence import github_evidence_bundle_from_dict
from research.legacy_sse import project_legacy_event

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "github_evidence_v1.json"


def load_fixture() -> dict[str, Any]:
    """Load the immutable schema-v1 compatibility fixture."""
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_frozen_github_v1_fixture_contains_single_and_multi_repository_bundles() -> None:
    """Keep representative GitHub v1 payloads available before v2 migration."""
    fixture = load_fixture()

    single = github_evidence_bundle_from_dict(fixture["single_bundle"])
    multi = github_evidence_bundle_from_dict(fixture["multi_bundle"])

    assert single is not None
    assert len(single.snapshots) == 1
    assert single.snapshots[0].repository == "owner/repo"
    assert multi is not None
    assert [item.repository for item in multi.snapshots] == [
        "owner/repo",
        "other/repo",
    ]
    assert all(
        claim.evidence_ids
        for claim in single.claims + multi.claims
    )


def test_frozen_checkpoint_represents_the_pre_v2_shape() -> None:
    """The old checkpoint has GitHub fields but no generic intelligence field."""
    checkpoint = load_fixture()["legacy_checkpoint"]
    continuation = checkpoint["state"]

    assert set(continuation) == {
        "research_topic",
        "github_context",
        "github_intelligence",
        "report_note_id",
        "report_note_path",
        "permission_mode",
        "caller_mode",
        "use_history_memory",
        "memory_scope",
    }
    assert "research_intelligence" not in continuation
    assert "research_mode" not in continuation
    assert "research_profile_id" not in continuation


def _event(kind: EventKind, payload: dict[str, object]) -> ResearchEvent:
    """Build one current-schema event for SSE compatibility assertions."""
    from datetime import datetime, timezone

    return ResearchEvent(
        kind=kind,
        run_id="019fefc4-d5f5-7d50-8e6e-3abb0a532c97",
        sequence=1,
        occurred_at=datetime.now(timezone.utc),
        payload=payload,
    )


def test_current_github_sse_event_names_remain_compatible() -> None:
    """The legacy projector keeps the three existing GitHub event names."""
    events = [
        project_legacy_event(
            _event(
                EventKind.EVIDENCE_COLLECTED,
                {
                    "snapshot_count": 1,
                    "evidence_count": 2,
                    "claim_count": 1,
                    "artifact_count": 0,
                },
            )
        ),
        project_legacy_event(
            _event(
                EventKind.COVERAGE_UPDATED,
                {
                    "coverage_score": 1.0,
                    "covered_dimensions": ["overview"],
                    "missing_dimensions": [],
                    "gap_queries": [],
                    "allow_report": True,
                },
            )
        ),
        project_legacy_event(
            _event(
                EventKind.ARTIFACT_READY,
                {
                    "artifact_id": "artifact_1",
                    "artifact_type": "evidence_json",
                    "mime_type": "application/json",
                    "path": "artifacts/evidence.json",
                    "title": "Evidence",
                    "checksum": "checksum",
                },
            )
        ),
    ]

    assert [event["type"] for event in events if event is not None] == [
        "github_evidence",
        "coverage_update",
        "artifact_ready",
    ]


class _FixtureRunner:
    """Small runner proving the public API still exposes GitHub intelligence."""

    def run(self, request: Any) -> Any:
        """Return a completed result backed by the frozen v1 bundle."""
        todo = TodoItem(
            id=1,
            title="GitHub fixture",
            intent="compatibility",
            query=request.topic,
            status="completed",
            summary="fixture summary",
            sources_summary="Source: https://github.com/owner/repo",
        )
        return SimpleNamespace(
            run_id=request.run_id,
            status="completed",
            output=SummaryStateOutput(
                report_markdown="# Fixture report",
                todo_items=[todo],
                github_intelligence=load_fixture()["single_bundle"],
            ),
            metrics={},
            findings=[],
            compressed_context={},
            policy_decisions=[],
        )

    def stream(self, request: Any) -> Iterator[dict[str, Any]]:
        """Return the minimum stream shape required by app construction."""
        del request
        yield {"type": "status", "run_id": "fixture"}
        yield {"type": "done", "run_id": "fixture"}


def test_current_api_response_still_returns_github_intelligence() -> None:
    """The public research response keeps the established GitHub field."""
    client = TestClient(
        create_app(harness_runner=_FixtureRunner()),
        headers={"Authorization": "Bearer test-app-key"},
    )

    response = client.post("/research", json={"topic": "owner/repo"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["github_intelligence"]["schema_version"] == 1
    assert payload["github_intelligence"]["snapshots"][0]["repository"] == (
        "owner/repo"
    )
