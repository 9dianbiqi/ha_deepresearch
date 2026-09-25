"""End-to-end integration tests for the opt-in Web evidence pipeline."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from inspect import signature
from pathlib import Path
from threading import Lock
from time import sleep
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
from research.context import FollowupContext, ResearchContextAssembler
from research.contracts import EventKind, ResearchCommand
from research.intelligence import (
    ClaimRecord,
    EvidenceLocator,
    EvidenceRecord,
    ResearchIntelligenceBundle,
    SourceReference,
)
from research.legacy_sse import project_legacy_event
from research.operations import GovernedOperations, OperationScope
from research.profiles import ResearchMode
from research.providers import WebSourceProvider
from research.quality import EvidenceGateBlockedError
from research.report_document import (
    StructuredSummaryDocument,
    SummaryParagraph,
    SummaryQualityAssessment,
)
from research.report_renderer import render_structured_report
from research.session import (
    CancellationRequestedError,
    DeadlineExceededError,
    RunSession,
)
from research.sources import SourceProviderRegistry
from research.summary_quality import SummaryQualityGateV1
from research.task_quality import (
    ClaimJudgment,
    ClaimVerdict,
    TaskQualityController,
)
from research.task_quality import (
    SupportSpan as TaskSupportSpan,
)
from research.web_capture import WebCaptureService


@pytest.mark.parametrize(
    ("mode", "profile_id"),
    [(ResearchMode.WEB, None), (None, "web.evidence.v1")],
)
def test_task_summary_and_judge_read_shared_captures_before_final_report(
    monkeypatch, mode, profile_id,
):
    config = _config(enable_quality_gate=True, max_concurrent_tasks=2)
    agent, _ = _agent(config)
    provider = agent._research_kernel.provider_registry.get("web")
    original_capture = provider.capture_search_result
    captured = []
    requests = []
    judgments = []

    def capture(result, context):
        page = original_capture(result, context)
        captured.append(page)
        return page

    monkeypatch.setattr(provider, "capture_search_result", capture)

    def candidate_search(query, search_config, loop_count, **kwargs):
        assert search_config.fetch_full_page is False
        return _search(query, search_config, loop_count, **kwargs)

    monkeypatch.setattr(provider, "_dispatcher", candidate_search)

    planner_calls = []
    planned_tasks = [
        TodoItem(
            id=index, title=f"Verify {index}",
            intent="Collect an evidence-backed overview",
            query=f"Alpha evidence research {index}",
        ) for index in (1, 2)
    ]

    def plan(state, **kwargs):
        planner_calls.append(kwargs)
        return planned_tasks

    monkeypatch.setattr(agent.planner, "plan_todo_list", plan)

    class GroundedSummary:
        def stream_summary(self, request):
            assert len(captured) == 2
            assert len(request.evidence) == 2
            assert "Worker-only source body" not in request.context
            assert all(item.evidence_level == "full_text" for item in request.evidence)
            assert all(item.locator["paragraph"] for item in request.evidence)
            requests.append(request)
            item = request.evidence[0]
            summary = f"- {item.excerpt} [{item.evidence_id}]"
            return iter((summary,)), lambda: summary

    class Relevant:
        def score(self, query, intent, evidence):
            return 1.0

    class Judge:
        def judge(self, claims, evidence, budget):
            judgments.append(evidence)
            assert len(captured) == 2
            assert all(item.evidence_level == "full_text" for item in evidence)
            by_id = {item.evidence_id: item for item in evidence}
            return tuple(
                ClaimJudgment(
                    claim_id=claim.claim_id, verdict=ClaimVerdict.SUPPORTED,
                    supporting_evidence_ids=claim.evidence_ids,
                    support_spans=tuple(
                        TaskSupportSpan(evidence_id=eid, exact_text=by_id[eid].excerpt)
                        for eid in claim.evidence_ids
                    ),
                    reason_code="fixture_supported",
                )
                for claim in claims
            )

    agent.summarizer = GroundedSummary()
    agent._task_quality_controller = TaskQualityController(ranker=Relevant(), judge=Judge())
    session = _session(config, mode=mode, profile_id=profile_id)
    checkpoints = []
    session.checkpoint_writer = lambda snapshot: checkpoints.append(snapshot.checkpoint_state)
    session.related_history_context = {"summary": "Previous research"}
    session.user_memory_context = {"preference": "Prefer official sources"}
    prior = FollowupContext(
        source_run_id="previous", key_findings=("Alpha finding",),
        key_sources=(), open_questions=("How does it work?",),
    )
    agent.execute(session, prior)
    assert len(planner_calls) == 1
    assert planner_calls[0]["prior_context"] == ResearchContextAssembler().assemble(prior)
    assert planner_calls[0]["related_history"] == session.related_history_context
    assert planner_calls[0]["user_memories"] == session.user_memory_context
    assert planner_calls[0]["operation_scope"].operations.session is session
    planning = next(item for item in checkpoints if item["phase"] == "planning_completed")
    assert [(item["id"], item["title"], item["intent"], item["query"])
            for item in planning["task_state"]] == [
        (item.id, item.title, item.intent, item.query) for item in planned_tasks
    ]
    assert all(item.status == "completed" for item in session.state.todo_items)
    assert len(requests) == 2
    assert len(judgments) == 2
    assert len(captured) == 2  # Neither the second task nor finalization reads again.
    bundle = ResearchIntelligenceBundle.from_dict(session.state.research_intelligence)
    canonical = {item.evidence_id: item for item in bundle.evidence}
    for request in requests:
        for item in request.evidence:
            assert canonical[item.evidence_id].excerpt == item.excerpt
            assert canonical[item.evidence_id].locator.as_dict() == dict(item.locator)


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


class _OverlapDetectingQualityAgent:
    """Return valid semantic JSON while detecting overlapping shared-agent calls."""

    def __init__(self) -> None:
        self._state_lock = Lock()
        self.active = 0
        self.max_active = 0

    def run(self, prompt: str, **kwargs: object) -> str:
        del prompt, kwargs
        with self._state_lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        sleep(0.03)
        with self._state_lock:
            self.active -= 1
        return '{"semantic_score": 1.0}'

    def clear_history(self) -> None:
        """Mirror the shared SimpleAgent cleanup boundary."""
        return None


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


def test_disabled_explicit_profile_falls_back_without_mislabeling(monkeypatch) -> None:
    config = _config(enable_evidence_web=False)
    agent, reporter = _agent(config)
    session = _session(config, profile_id="web.evidence.v1")
    original_plan = agent.planner.plan_todo_list
    planner_calls = []

    def plan(state, prior_context=None):
        planner_calls.append(prior_context)
        return original_plan(state, prior_context=prior_context)

    monkeypatch.setattr(agent.planner, "plan_todo_list", plan)
    agent.execute(session, None)

    assert len(planner_calls) == 1
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


def test_shared_quality_agent_calls_are_serialized_across_runs() -> None:
    config = _config()
    agent, _ = _agent(config)
    agent._summary_quality_gate = None
    detector = _OverlapDetectingQualityAgent()
    agent._quality_semantic_agent = detector  # type: ignore[assignment]
    agent._quality_factual_agent = detector  # type: ignore[assignment]
    session = _session(config, mode=ResearchMode.WEB, profile_id="web.evidence.v1")
    scope = OperationScope(
        operations=GovernedOperations(session, agent._operation_authorizer)
    )
    gate = agent._summary_quality_gate_for_scope(scope)
    source = SourceReference(
        provider_id="fake",
        source_kind="web",
        source_id="source-1",
        canonical_url="https://public.example.test/source",
        resolved_version="capture-v1",
        content_hash="hash-1",
    )
    evidence = EvidenceRecord(
        evidence_id="ev-1",
        source=source,
        evidence_type="web_paragraph",
        evidence_level="full_text",
        title="Evidence",
        excerpt="Exact support.",
        locator=EvidenceLocator(
            locator_type="paragraph",
            url=source.canonical_url,
            paragraph="p-1",
        ),
    )
    claim = ClaimRecord(
        claim_id="claim-1",
        dimension="overview",
        statement="A supported claim.",
        confidence="unverified",
        evidence_ids=(evidence.evidence_id,),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(gate._semantic_scorer.score, claim, (evidence,))
            for _ in range(2)
        ]
        assert [future.result() for future in futures] == [1.0, 1.0]

    assert detector.max_active == 1


def test_evidence_web_empty_plan_uses_fallback_once(monkeypatch):
    config = _config()
    agent, _ = _agent(config)
    calls = []
    fallback_calls = []

    def plan(*args, **kwargs):
        calls.append(kwargs)
        return []

    def fallback(state):
        fallback_calls.append(state)
        return TodoItem(id=1, title="Fallback", intent="Overview", query=state.research_topic)

    monkeypatch.setattr(agent.planner, "plan_todo_list", plan)
    monkeypatch.setattr(agent.planner, "create_fallback_task", fallback)
    session = _session(config, mode=ResearchMode.WEB)
    agent.execute(session, None)
    assert len(calls) == len(fallback_calls) == 1
    assert [task.title for task in session.state.todo_items] == ["Fallback"]


def test_evidence_web_plan_budget_fails_before_task_side_effects(monkeypatch):
    config = _config()
    agent, _ = _agent(config)
    monkeypatch.setattr(agent.planner, "plan_todo_list", lambda *args, **kwargs: [
        TodoItem(id=index, title="Task", intent="Overview", query="Alpha")
        for index in range(1, 10)
    ])

    def forbidden(*args, **kwargs):
        pytest.fail("Task side effects must not run before plan budget validation")

    monkeypatch.setattr(agent, "_create_task_notes_for_tasks", forbidden)
    monkeypatch.setattr(agent, "_execute_work_items", forbidden)
    session = _session(config, mode=ResearchMode.WEB)
    with pytest.raises(ValueError, match="Planned tasks exceed"):
        agent.execute(session, None)
    assert session.state.todo_items == []
    assert session.checkpoint_phase != "planning_completed"


@pytest.mark.parametrize("error_type", [CancellationRequestedError, DeadlineExceededError])
def test_evidence_web_planner_control_errors_are_not_fallback(monkeypatch, error_type):
    config = _config()
    agent, _ = _agent(config)

    def plan(*args, **kwargs):
        raise error_type("Controlled planning stop")

    def forbidden(*args, **kwargs):
        pytest.fail("Controlled planning errors must not generate a fallback")

    monkeypatch.setattr(agent.planner, "plan_todo_list", plan)
    monkeypatch.setattr(agent.planner, "create_fallback_task", forbidden)
    monkeypatch.setattr(agent, "_create_task_notes_for_tasks", forbidden)
    with pytest.raises(error_type):
        agent.execute(_session(config, mode=ResearchMode.WEB), None)


@pytest.mark.parametrize("phase", ["planning_completed", "research_tasks_progress"])
def test_evidence_web_task_resume_does_not_replan(monkeypatch, tmp_path, phase):
    config = _config()
    agent, _ = _agent(config, artifact_store=FileArtifactStore(tmp_path))
    session = _session(config, mode=ResearchMode.WEB)
    original = TodoItem(id=7, title="Saved task", intent="Saved intent", query="Saved query")
    session.install_plan([original])
    scope = OperationScope(operations=GovernedOperations(session, agent._operation_authorizer))
    prepared = agent._prepare_kernel_research(session, operation_scope=scope)
    agent._install_kernel_baseline(session, prepared)
    agent._persist_research_checkpoint(session, phase, prepared)

    def forbidden(*args, **kwargs):
        pytest.fail("Task resume must use the saved plan without invoking Planner")

    class ReachedExecution(Exception):
        pass

    def stop_at_execution(current_session, work_items):
        assert [(item.task_id, item.title, item.intent, item.original_query) for item in work_items] == [
            (7, "Saved task", "Saved intent", "Saved query")
        ]
        raise ReachedExecution

    monkeypatch.setattr(agent.planner, "plan_todo_list", forbidden)
    monkeypatch.setattr(agent.planner, "create_fallback_task", forbidden)
    monkeypatch.setattr(agent, "_execute_work_items", stop_at_execution)
    with pytest.raises(ReachedExecution):
        agent.resume(session, None)
