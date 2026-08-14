"""End-to-end integration tests for the opt-in Web evidence pipeline."""

from __future__ import annotations

from inspect import signature
from pathlib import Path
from typing import Any

import pytest

from agent import DeepResearchAgent
from config import Configuration
from harness.models import HarnessRunResult
from harness.recorder import _stored_output
from main import _build_harness_response
from models import ResearchState, SummaryState, TodoItem
from research.artifacts import FileArtifactStore
from research.claim_verifier import FactualVerification, SupportSpan
from research.contracts import EventKind, ResearchCommand
from research.intelligence import (
    ClaimRecord,
    EvidenceRecord,
    ResearchIntelligenceBundle,
)
from research.legacy_sse import project_legacy_event
from research.profiles import ResearchMode
from research.providers import WebSourceProvider
from research.quality import EvidenceGateBlockedError
from research.report_document import (
    StructuredSummaryDocument,
    SummaryParagraph,
    SummaryQualityAssessment,
)
from research.report_renderer import render_structured_report
from research.session import RunSession
from research.sources import SourceProviderRegistry
from research.summary_quality import SummaryQualityGateV1
from research.web_capture import WebCaptureService


class _Planner:
    """Return one deterministic task for the legacy Web fallback."""

    def plan_todo_list(
        self,
        state: SummaryState,
        prior_context: dict[str, Any] | None = None,
    ) -> list[TodoItem]:
        del prior_context
        return [
            TodoItem(
                id=1,
                title="Overview",
                intent="Collect an evidence-backed overview",
                query=state.research_topic or "overview",
            )
        ]

    def create_fallback_task(self, state: SummaryState) -> TodoItem:
        return self.plan_todo_list(state)[0]


class _Summarizer:
    """Return one bounded atomic claim with legacy-compatible structure."""

    summary = (
        "### Overview\n"
        "The Alpha system uses deterministic evidence for production decisions."
    )

    def stream_summary(self, request: object):
        del request
        collected: list[str] = []

        def chunks():
            collected.append(self.summary)
            yield self.summary

        return chunks(), lambda: "".join(collected)


class _SemanticScorer:
    """Return deterministic semantic support for integration tests."""

    name = "fake-semantic"
    version = "1"

    def score(
        self,
        claim: ClaimRecord,
        evidence: tuple[EvidenceRecord, ...],
    ) -> float:
        del claim, evidence
        return 1.0


class _FactualVerifier:
    """Select every claim-bound Evidence item as exact positive support."""

    name = "fake-factual"
    version = "1"
    prompt_version = "fixture-v1"

    def verify(
        self,
        claim: ClaimRecord,
        evidence: tuple[EvidenceRecord, ...],
    ) -> FactualVerification:
        evidence_by_id = {item.evidence_id: item for item in evidence}
        supporting = tuple(
            item for item in claim.evidence_ids if item in evidence_by_id
        )
        return FactualVerification(
            verdict="supported",
            factual_score=1.0,
            supporting_evidence_ids=supporting,
            support_spans=tuple(
                SupportSpan(
                    evidence_id=evidence_id,
                    exact_text=evidence_by_id[evidence_id].excerpt,
                )
                for evidence_id in supporting
            ),
        )


class _Reporter:
    """Generate structured documents and track the one allowed rewrite."""

    def __init__(self, *, fail_first: bool = False) -> None:
        self.fail_first = fail_first
        self.structured_calls = 0

    def generate_report(
        self,
        state: SummaryState,
        notes_context: dict[str, Any] | None = None,
    ) -> str:
        del state, notes_context
        return "Legacy report remains unchanged."

    def generate_structured_document(
        self,
        state: SummaryState,
        notes_context: dict[str, Any] | None = None,
        *,
        quality_feedback: tuple[str, ...] = (),
    ) -> StructuredSummaryDocument:
        del notes_context, quality_feedback
        self.structured_calls += 1
        bundle = ResearchIntelligenceBundle.from_dict(state.research_intelligence)
        claim = bundle.claims[0]
        citations = (
            ()
            if self.fail_first and self.structured_calls == 1
            else claim.evidence_ids
        )
        return StructuredSummaryDocument(
            task_id="report",
            paragraphs=(
                SummaryParagraph(
                    section_id=claim.dimension,
                    paragraph_type="factual",
                    text=claim.statement,
                    claim_ids=(claim.claim_id,),
                    citation_ids=citations,
                ),
            ),
            claim_ids=(claim.claim_id,),
        )

    def render_structured_document(
        self,
        state: SummaryState,
        document: StructuredSummaryDocument,
        notes_context: dict[str, Any] | None = None,
    ):
        del notes_context
        bundle = ResearchIntelligenceBundle.from_dict(state.research_intelligence)
        return render_structured_report(
            document,
            title=bundle.report_spec.title,
            claims=bundle.claims,
            evidence=bundle.evidence,
            evidence_frozen=bundle.evidence_frozen,
        )


def _search(
    query: str,
    config: Configuration,
    loop_count: int,
    **kwargs: object,
) -> tuple[dict[str, Any], list[str], None, str]:
    """Return two independent full-text pages without network access."""
    del query, config, loop_count, kwargs
    sentence = (
        "The Alpha system uses deterministic evidence for production decisions "
        "and paragraph-level citations."
    )
    return (
        {
            "results": [
                {
                    "title": "Alpha evidence one",
                    "url": "https://one.example.test/alpha",
                    "content": sentence,
                    "raw_content": f"<html><body><p>{sentence}</p></body></html>",
                },
                {
                    "title": "Alpha evidence two",
                    "url": "https://two.example.test/alpha",
                    "content": sentence,
                    "raw_content": f"<html><body><p>{sentence}</p></body></html>",
                },
            ]
        },
        [],
        None,
        "duckduckgo",
    )


def _context(
    search_result: dict[str, Any] | None,
    answer_text: str | None,
    config: Configuration,
) -> tuple[str, str]:
    del search_result, answer_text, config
    return "Two public Web sources.", "Worker-only source body."


def _config(**overrides: object) -> Configuration:
    values: dict[str, object] = {
        "enable_notes": False,
        "enable_quality_gate": False,
        "enable_github_research": True,
        "enable_evidence_web": True,
        "enable_summary_quality_shadow": True,
        "max_concurrent_tasks": 1,
    }
    values.update(overrides)
    return Configuration.from_env(overrides=values)


def _agent(
    config: Configuration,
    *,
    reporter: _Reporter | None = None,
    artifact_store: FileArtifactStore | None = None,
    passing_gate: bool = True,
) -> tuple[DeepResearchAgent, _Reporter]:
    effective_reporter = reporter or _Reporter()
    gate = (
        SummaryQualityGateV1(
            semantic_scorer=_SemanticScorer(),
            factual_verifier=_FactualVerifier(),
        )
        if passing_gate
        else SummaryQualityGateV1()
    )
    capture_kwargs: dict[str, object] = {}
    if "resolver" in signature(WebCaptureService).parameters:
        capture_kwargs["resolver"] = (
            lambda _hostname, _port=443: ("93.184.216.34",)
        )
    capture_service = WebCaptureService(**capture_kwargs)
    provider_registry = SourceProviderRegistry(
        (
            WebSourceProvider(
                dispatcher=_search,
                capture_service=capture_service,
            ),
        )
    )
    return (
        DeepResearchAgent(
            config=config,
            planner=_Planner(),
            search_adapter=_search,
            context_preparer=_context,
            summarizer=_Summarizer(),
            reporting=effective_reporter,
            note_agent=None,
            github_adapter=None,
            source_provider_registry=provider_registry,
            artifact_store=artifact_store,
            summary_quality_gate=gate,
        ),
        effective_reporter,
    )


def _session(
    config: Configuration,
    *,
    mode: ResearchMode | None = None,
    profile_id: str | None = None,
    permission_mode: str = "default",
) -> RunSession:
    command = ResearchCommand(
        topic="Alpha evidence research",
        config=config,
        permission_mode=permission_mode,
        research_mode=mode,
        research_profile_id=profile_id,
    )
    session = RunSession(
        command=command,
        state=ResearchState(research_topic=command.topic),
    )
    session.start()
    return session


@pytest.mark.parametrize(
    ("mode", "profile_id"),
    [
        (ResearchMode.WEB, None),
        (None, "web.evidence.v1"),
        (ResearchMode.WEB, "web.evidence.v1"),
    ],
)
def test_web_evidence_selection_is_explicit_and_flag_gated(
    mode: ResearchMode | None,
    profile_id: str | None,
) -> None:
    agent, _ = _agent(_config())
    session = _session(_config(), mode=mode, profile_id=profile_id)

    assert agent._should_use_research_kernel(session) is True

    disabled = _session(
        _config(enable_evidence_web=False),
        mode=mode,
        profile_id=profile_id,
    )
    assert agent._should_use_research_kernel(disabled) is False


def test_disabled_explicit_profile_falls_back_without_mislabeling() -> None:
    config = _config(enable_evidence_web=False)
    agent, reporter = _agent(config)
    session = _session(config, profile_id="web.evidence.v1")

    agent.execute(session, None)

    assert session.state.research_profile_id == "web.default.v1"
    assert session.state.research_intelligence == {}
    assert session.state.structured_report == "Legacy report remains unchanged."
    assert reporter.structured_calls == 0
    assert session.metrics["evidence_web"]["outcome"] == "compatibility_fallback"
    assert session.metrics["summary_quality_shadow"] == {
        "enabled": True,
        "blocking": False,
        "outcome": "not_applicable",
        "reason": "no_evidence_bundle",
    }


def test_explicit_web_evidence_persists_bodies_and_emits_safe_sse(
    tmp_path: Path,
) -> None:
    config = _config()
    store = FileArtifactStore(tmp_path)
    reporter = _Reporter(fail_first=True)
    agent, _ = _agent(config, reporter=reporter, artifact_store=store)
    session = _session(
        config,
        mode=ResearchMode.WEB,
        profile_id="web.evidence.v1",
    )

    agent.execute(session, None)

    assert reporter.structured_calls == 2
    assert session.state.research_profile_id == "web.evidence.v1"
    assert session.state.structured_summary
    assert session.state.quality_assessment["passed"] is True
    bundle = ResearchIntelligenceBundle.from_dict(session.state.research_intelligence)
    artifact_types = {
        item.artifact_type for item in bundle.artifact_manifest.artifacts
    }
    assert {
        "web_page_snapshot",
        "structured_summary",
        "quality_assessment",
    }.issubset(artifact_types)
    snapshot = next(
        item
        for item in bundle.artifact_manifest.artifacts
        if item.artifact_type == "web_page_snapshot"
    )
    assert b"deterministic evidence" in store.get(session.run_id, snapshot.artifact_id)
    assert store.get(session.run_id, "artifact_structured_summary_json")
    assert store.get(session.run_id, "artifact_quality_assessment_json")
    restored_output = _stored_output(dict(session.to_snapshot().output))
    assert restored_output.structured_summary == session.state.structured_summary
    assert restored_output.quality_assessment == session.state.quality_assessment
    response = _build_harness_response(
        HarnessRunResult(
            run_id=session.run_id,
            status="completed",
            output=restored_output,
        ),
        mode="internal",
    )
    assert response.structured_summary == session.state.structured_summary
    assert response.quality_assessment == session.state.quality_assessment
    document = StructuredSummaryDocument.from_dict(session.state.structured_summary)
    assessment = SummaryQualityAssessment.from_dict(
        session.state.quality_assessment
    )
    assert agent._quality_binding_is_valid(
        session,
        bundle=bundle,
        document=document,
        assessment=assessment,
    )
    tampered_payload = assessment.as_dict()
    tampered_payload["overall_score"] = 0.5
    assert not agent._quality_binding_is_valid(
        session,
        bundle=bundle,
        document=document,
        assessment=SummaryQualityAssessment.from_dict(tampered_payload),
    )

    projected = [
        item
        for event in session.events
        if (item := project_legacy_event(event)) is not None
    ]
    quality_event = next(
        item for item in projected if item["type"] == "summary_quality_update"
    )
    assert set(quality_event) == {
        "run_id",
        "schema_version",
        "sequence",
        "type",
        "overall_score",
        "passed",
        "paragraph_count",
        "claim_count",
        "blocked_paragraph_count",
        "blocker_codes",
    }
    assert "deterministic evidence" not in str(quality_event)
    artifact_events = [
        item for item in projected if item["type"] == "artifact_ready"
    ]
    assert artifact_events
    assert "raw_content" not in str(projected)
    assert "Worker-only source body" not in str(projected)


def test_balanced_web_drops_failed_paragraphs_and_keeps_limitation(
    tmp_path: Path,
) -> None:
    config = _config()
    agent, reporter = _agent(
        config,
        artifact_store=FileArtifactStore(tmp_path),
        passing_gate=False,
    )
    session = _session(
        config,
        mode=ResearchMode.WEB,
        profile_id="web.evidence.v1",
    )

    agent.execute(session, None)

    assert reporter.structured_calls == 2
    assert session.state.quality_assessment["passed"] is False
    paragraphs = session.state.structured_summary["paragraphs"]
    assert [item["paragraph_type"] for item in paragraphs] == ["limitation"]
    assert "were omitted" in session.state.structured_report


def test_strict_failure_checkpoint_cannot_be_bypassed_on_resume(
    tmp_path: Path,
) -> None:
    config = _config()
    store = FileArtifactStore(tmp_path)
    agent, reporter = _agent(
        config,
        artifact_store=store,
        passing_gate=False,
    )
    session = _session(
        config,
        mode=ResearchMode.WEB,
        profile_id="web.evidence.v1",
        permission_mode="strict",
    )

    with pytest.raises(EvidenceGateBlockedError):
        agent.execute(session, None)

    assert session.checkpoint_phase == "summary_quality_completed"
    assert reporter.structured_calls == 2
    quality_events_before = sum(
        event.kind is EventKind.SUMMARY_QUALITY_UPDATE for event in session.events
    )
    artifact_events_before = sum(
        event.kind is EventKind.ARTIFACT_READY for event in session.events
    )

    with pytest.raises(EvidenceGateBlockedError):
        agent.resume(session, None)

    assert reporter.structured_calls == 2
    assert sum(
        event.kind is EventKind.SUMMARY_QUALITY_UPDATE for event in session.events
    ) == quality_events_before
    assert sum(
        event.kind is EventKind.ARTIFACT_READY for event in session.events
    ) == artifact_events_before
    assert store.get(session.run_id, "artifact_structured_summary_json")
    assert store.get(session.run_id, "artifact_quality_assessment_json")


def test_quality_sse_rejects_boolean_score() -> None:
    session = _session(_config(enable_evidence_web=False))
    session.record_summary_quality(
        {"paragraphs": []},
        {
            "overall_score": True,
            "passed": False,
            "paragraph_assessments": [],
            "claim_assessments": [],
        },
    )

    event = project_legacy_event(session.events[-1])

    assert event is not None
    assert event["overall_score"] == 0.0


def test_evidence_configuration_is_safe_and_defaults_are_fail_closed() -> None:
    config = Configuration.from_env(
        overrides={"llm_api_key": "secret-that-must-not-be-snapshotted"}
    )

    snapshot = config.safe_snapshot()

    assert config.enable_evidence_web is False
    assert config.enable_summary_quality_shadow is True
    assert snapshot["enable_evidence_web"] is False
    assert snapshot["enable_summary_quality_shadow"] is True
    assert snapshot["summary_semantic_threshold"] == 0.72
    assert snapshot["summary_factual_threshold"] == 0.75
    assert snapshot["summary_citation_threshold"] == 0.85
    assert snapshot["summary_overall_threshold"] == 0.78
    assert "llm_api_key" not in snapshot
    assert "secret-that-must-not-be-snapshotted" not in str(snapshot)
