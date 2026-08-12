"""Task 4 tests for generic state, checkpoint, and SSE compatibility."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import Configuration
from models import ResearchState
from research.compatibility import GitHubEvidenceV1Adapter
from research.contracts import EventKind, ResearchCommand, ResearchEvent, RunStatus
from research.evidence import github_evidence_bundle_from_dict
from research.intelligence import ResearchIntelligenceBundle
from research.legacy_sse import project_legacy_event
from research.repository import FileRunRepository
from research.session import RunSession

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "github_evidence_v1.json"


def _v1_fixture() -> dict[str, Any]:
    """Load the frozen legacy GitHub bundle."""
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return payload["single_bundle"]


def _session() -> RunSession:
    """Build a running session for generic state transition tests."""
    command = ResearchCommand(
        topic="generic research",
        config=Configuration.from_env(),
        research_mode="paper",
        research_profile_id="paper.abstract.v1",
    )
    session = RunSession(command=command, state=ResearchState())
    session.start()
    return session


def _v2_bundle(*, mode: str = "github") -> dict[str, Any]:
    """Build a minimal valid v2 payload from the legacy fixture adapter."""
    legacy = github_evidence_bundle_from_dict(_v1_fixture())
    assert legacy is not None
    payload = ResearchIntelligenceBundle.from_dict(
        GitHubEvidenceV1Adapter.to_v2(legacy).as_dict()
    ).as_dict()
    payload["mode"] = mode
    return payload


def test_generic_source_and_intelligence_events_do_not_leak_bodies() -> None:
    """Generic events expose bounded metadata while state retains the bundle."""
    session = _session()
    source_event = session.record_source_context(
        {"sources": [{"provider_id": "openalex", "abstract": "private"}]},
        provider_ids=("openalex",),
        source_count=1,
    )
    evidence_event, coverage_event = session.record_research_intelligence(
        _v2_bundle(mode="paper"), provider_ids=("openalex",)
    )

    assert session.state.source_context["sources"][0]["abstract"] == "private"
    assert session.state.research_intelligence["schema_version"] == 2
    assert "private" not in json.dumps(source_event.as_dict())
    assert project_legacy_event(source_event)["type"] == "research_source"
    assert project_legacy_event(evidence_event)["type"] == "research_evidence"
    assert project_legacy_event(coverage_event)["type"] == "coverage_update"
    assert "evidence_owner_repo_metadata" not in json.dumps(evidence_event.as_dict())


def test_checkpoint_round_trip_preserves_generic_fields() -> None:
    """New checkpoints persist and restore generic state without changing phases."""
    session = _session()
    session.record_source_context(
        {"sources": [{"provider_id": "openalex"}]},
        provider_ids=("openalex",),
        source_count=1,
    )
    session.record_research_intelligence(_v2_bundle(), provider_ids=("github",))
    snapshot = session.persist_checkpoint("evidence_completed")

    restored = RunSession.restore_from_snapshot(snapshot)

    assert restored.state.research_mode == "github"
    assert restored.state.research_profile_id == "github.repository.v1"
    assert restored.state.source_context == session.state.source_context
    assert restored.state.research_intelligence["schema_version"] == 2


def test_old_checkpoint_adapts_github_v1_in_memory() -> None:
    """A v1-only checkpoint gains a v2 recovery view without being rewritten."""
    session = _session()
    session.state.github_intelligence = _v1_fixture()
    snapshot = session.persist_checkpoint("evidence_completed")
    old_state = dict(snapshot.checkpoint_state or {})
    continuation = dict(old_state["state"])
    continuation.pop("research_intelligence", None)
    continuation.pop("source_context", None)
    continuation.pop("research_mode", None)
    continuation.pop("research_profile_id", None)
    continuation["github_intelligence"] = _v1_fixture()
    old_state["state"] = continuation
    old_snapshot = replace(snapshot, checkpoint_state=old_state)

    restored = RunSession.restore_from_snapshot(old_snapshot)

    assert restored.state.github_intelligence["schema_version"] == 1
    assert restored.state.research_intelligence["schema_version"] == 2


def test_repository_preserves_safe_v2_intelligence_descriptors_and_locators(tmp_path) -> None:
    """The schema-v1 envelope can carry generic output compatibility fields."""
    session = _session()
    session.record_research_intelligence(_v2_bundle(mode="paper"), provider_ids=("openalex",))
    session.set_report("# Report")
    prepared = session.prepare_terminal(RunStatus.COMPLETED, EventKind.RUN_COMPLETED)
    session.confirm_terminal(prepared)
    repository = FileRunRepository(tmp_path)
    repository.save(session.to_snapshot())

    loaded = repository.load(session.run_id)

    intelligence = loaded.output["research_intelligence"]
    assert intelligence["schema_version"] == 2
    assert intelligence["sources"][0]["canonical_url"].startswith("https://github.com/")
    assert intelligence["evidence"][0]["locator"]["url"]
    assert "\"content\"" not in str(intelligence)


def test_legacy_github_evidence_projection_remains_unchanged() -> None:
    """Events without a non-GitHub mode keep the established SSE type."""
    event = ResearchEvent(
        kind=EventKind.EVIDENCE_COLLECTED,
        run_id="019fefc4-d5f5-7d50-8e6e-3abb0a532c97",
        sequence=1,
        occurred_at=datetime.now(timezone.utc),
        payload={"snapshot_count": 1, "evidence_count": 2, "claim_count": 1},
    )

    projected = project_legacy_event(event)

    assert projected is not None
    assert projected["type"] == "github_evidence"
