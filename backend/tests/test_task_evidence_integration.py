"""End-to-end tests for task evidence through the research coordinator."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

import pytest
from test_evidence import make_context

from agent import DeepResearchAgent
from config import Configuration
from models import ResearchState, SummaryState, TodoItem
from research.claim_verifier import FactualVerification
from research.claim_verifier import SupportSpan as FactualSupportSpan
from research.intelligence import ResearchIntelligenceBundle
from research.pipeline import ResearchKernel
from research.profiles import (
    CoveragePolicy,
    ResearchDimension,
    ResearchMode,
    ResearchProfile,
    ResearchProfileRegistry,
    ResearchTaskTemplate,
    built_in_profile_registry,
)
from research.providers import GitHubSourceProvider, WebSourceProvider
from research.quality import EvidenceGateBlockedError
from research.report_document import StructuredSummaryDocument, SummaryParagraph
from research.report_renderer import render_structured_report
from research.session import RunSession
from research.sources import SourceProviderRegistry
from research.summary_quality import SummaryQualityGateV1
from research.task_quality import (
    ClaimJudgment,
    ClaimVerdict,
    QualityMode,
    TaskQualityController,
)
from research.task_quality import (
    SupportSpan as TaskSupportSpan,
)
from research.web_capture import WebCaptureService

_SHA = "a" * 40
_PROFILE_ID = "e2e.github.v1"


class _Planner:
    def __init__(self, tasks: tuple[TodoItem, ...] = ()) -> None:
        self.tasks = tasks

    def plan_todo_list(
        self,
        state: SummaryState,
        prior_context: dict[str, Any] | None = None,
    ) -> list[TodoItem]:
        del prior_context
        if self.tasks:
            return list(self.tasks)
        return [
            TodoItem(
                id=1,
                title="Web overview",
                intent="Collect evidence for an overview",
                query=state.research_topic or "overview evidence",
            )
        ]

    def create_fallback_task(self, state: SummaryState) -> TodoItem:
        return self.plan_todo_list(state)[0]


class _RepositoryAdapter:
    def collect_repository_context(
        self,
        target: object,
        *,
        operation_scope: object,
        token: str | None = None,
        base_url: str | None = None,
    ) -> object:
        del operation_scope, token, base_url
        return make_context(target.full_name)  # type: ignore[attr-defined]


class _SearchDispatcher:
    def __init__(
        self,
        result_factory: Callable[[str, int], Mapping[str, object]],
    ) -> None:
        self.result_factory = result_factory
        self.calls: list[str] = []

    def __call__(
        self,
        query: str,
        config: Configuration,
        loop_count: int,
        **kwargs: object,
    ) -> tuple[dict[str, object], list[str], None, str]:
        del config, loop_count, kwargs
        self.calls.append(query)
        result = dict(self.result_factory(query, len(self.calls)))
        return result, [], None, "fixture-search"


class _EvidenceSummarizer:
    def __init__(
        self,
        select: Callable[[object], tuple[object, ...]] | None = None,
    ) -> None:
        self.select = select or (lambda request: tuple(request.evidence))  # type: ignore[attr-defined]
        self.requests: list[object] = []
        self.selected_evidence: list[tuple[object, ...]] = []

    def stream_summary(self, request: object):
        self.requests.append(request)
        selected = self.select(request)
        self.selected_evidence.append(selected)
        citations = " ".join(f"[{item.evidence_id}]" for item in selected)  # type: ignore[attr-defined]
        summary = (
            "- The task records an evidence-backed architecture observation. "
            f"{citations}"
        )
        return iter((summary,)), lambda: summary


class _RelevantRanker:
    def score(self, query: str, intent: str, evidence: object) -> float:
        del query, intent, evidence
        return 1.0


class _SupportedJudge:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def judge(self, claims: object, evidence: object, budget: object):
        del budget
        materialized = tuple(evidence)  # type: ignore[arg-type]
        self.calls.append(materialized)
        by_id = {item.evidence_id: item for item in materialized}  # type: ignore[attr-defined]
        return tuple(
            ClaimJudgment(
                claim_id=claim.claim_id,
                verdict=ClaimVerdict.SUPPORTED,
                supporting_evidence_ids=tuple(
                    evidence_id
                    for evidence_id in claim.evidence_ids
                    if evidence_id in by_id
                ),
                support_spans=tuple(
                    TaskSupportSpan(
                        evidence_id=evidence_id,
                        exact_text=by_id[evidence_id].excerpt,
                    )
                    for evidence_id in claim.evidence_ids
                    if evidence_id in by_id
                ),
                reason_code="fixture_supported",
            )
            for claim in claims  # type: ignore[union-attr]
        )


class _UncertainJudge:
    def judge(self, claims: object, evidence: object, budget: object):
        del evidence, budget
        return tuple(
            ClaimJudgment(
                claim_id=claim.claim_id,
                verdict=ClaimVerdict.UNCERTAIN,
                reason_code="fixture_uncertain",
            )
            for claim in claims  # type: ignore[union-attr]
        )


class _PlainReporter:
    def generate_report(
        self,
        state: SummaryState,
        notes_context: dict[str, Any] | None = None,
    ) -> str:
        del state, notes_context
        return "Fixture report."


class _StructuredReporter(_PlainReporter):
    def __init__(self) -> None:
        self.rendered_document: StructuredSummaryDocument | None = None
        self.structured_calls = 0

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
        claims = tuple(item for item in bundle.claims if item.reportable)
        paragraphs = tuple(
            SummaryParagraph(
                section_id=claim.dimension,
                paragraph_type="factual",
                text=claim.statement,
                claim_ids=(claim.claim_id,),
                citation_ids=claim.evidence_ids,
            )
            for claim in claims
        )
        return StructuredSummaryDocument(
            task_id="report",
            paragraphs=paragraphs,
            claim_ids=tuple(item.claim_id for item in claims),
        )

    def render_structured_document(
        self,
        state: SummaryState,
        document: StructuredSummaryDocument,
        notes_context: dict[str, Any] | None = None,
    ) -> str:
        del notes_context
        self.rendered_document = document
        bundle = ResearchIntelligenceBundle.from_dict(state.research_intelligence)
        return render_structured_report(
            document,
            title=bundle.report_spec.title,
            claims=bundle.claims,
            evidence=bundle.evidence,
            evidence_frozen=bundle.evidence_frozen,
        )


class _SemanticScorer:
    name = "fixture-semantic"
    version = "1"

    def score(self, claim: object, evidence: object) -> float:
        del claim, evidence
        return 1.0


class _FactualVerifier:
    name = "fixture-factual"
    version = "1"
    prompt_version = "fixture-v1"

    def verify(self, claim: object, evidence: object) -> FactualVerification:
        materialized = tuple(evidence)  # type: ignore[arg-type]
        by_id = {item.evidence_id: item for item in materialized}  # type: ignore[attr-defined]
        supporting = tuple(
            item for item in claim.evidence_ids if item in by_id  # type: ignore[attr-defined]
        )
        return FactualVerification(
            verdict="supported",
            factual_score=1.0,
            supporting_evidence_ids=supporting,
            support_spans=tuple(
                FactualSupportSpan(
                    evidence_id=evidence_id,
                    exact_text=by_id[evidence_id].excerpt,
                )
                for evidence_id in supporting
            ),
        )


def _config(**overrides: object) -> Configuration:
    values: dict[str, object] = {
        "enable_notes": False,
        "enable_quality_gate": True,
        "enable_github_research": True,
        "enable_evidence_web": True,
        "enable_summary_quality_shadow": True,
        "enable_search_cache": False,
        "fetch_full_page": False,
        "task_quality_mode": "evidence",
        "max_concurrent_tasks": 1,
    }
    values.update(overrides)
    return Configuration.from_env(overrides=values)


def _template(
    template_id: str,
    dimension: str,
    title: str,
    intent: str,
    query: str,
    mode: ResearchMode,
) -> ResearchTaskTemplate:
    return ResearchTaskTemplate(
        template_id=template_id,
        dimension=dimension,
        title=title,
        intent=intent,
        query_template=query,
        source_strategy="fixture_sources",
        supported_modes=frozenset({mode}),
    )


def _github_profile(*, max_excerpt_chars: int = 1200) -> ResearchProfile:
    base = built_in_profile_registry().get("github.repository.v1")
    architecture_task = next(
        item for item in base.task_templates if item.dimension == "architecture"
    )
    return replace(
        base,
        profile_id=_PROFILE_ID,
        dimensions=(ResearchDimension(id="architecture", title="Architecture"),),
        task_templates=(architecture_task,),
        retrieval_budget=replace(
            base.retrieval_budget,
            max_tasks=1,
            max_evidence=24,
            max_excerpt_chars=max_excerpt_chars,
        ),
        coverage_policy=CoveragePolicy(
            required_dimensions=("architecture",),
            min_coverage_score=1.0,
        ),
        report_sections=(),
    )


def _web_profile(
    templates: tuple[ResearchTaskTemplate, ...],
    *,
    dimensions: tuple[ResearchDimension, ...],
    max_evidence: int = 24,
    max_excerpt_chars: int = 1200,
) -> ResearchProfile:
    base = built_in_profile_registry().get("web.evidence.v1")
    required = tuple(item.id for item in dimensions if item.required)
    return replace(
        base,
        dimensions=dimensions,
        task_templates=templates,
        retrieval_budget=replace(
            base.retrieval_budget,
            max_tasks=max(1, len(templates)),
            max_evidence=max_evidence,
            max_excerpt_chars=max_excerpt_chars,
        ),
        coverage_policy=CoveragePolicy(
            required_dimensions=required,
            min_coverage_score=1.0,
        ),
        report_sections=(),
    )


def _kernel(
    profile: ResearchProfile,
    search: _SearchDispatcher,
    *,
    with_github: bool,
    capture_max_paragraph_chars: int = 8000,
) -> ResearchKernel:
    capture_service = WebCaptureService(
        resolver=lambda _hostname, _port=443: ("93.184.216.34",),
        max_paragraph_chars=capture_max_paragraph_chars,
    )
    providers: list[object] = [
        WebSourceProvider(dispatcher=search, capture_service=capture_service)
    ]
    if with_github:
        providers.insert(0, GitHubSourceProvider(adapter=_RepositoryAdapter()))
    return ResearchKernel(
        profile_registry=ResearchProfileRegistry((profile,)),
        provider_registry=SourceProviderRegistry(providers),  # type: ignore[arg-type]
    )


def _agent(
    config: Configuration,
    profile: ResearchProfile,
    search: _SearchDispatcher,
    summarizer: _EvidenceSummarizer,
    judge: object,
    *,
    reporter: object | None = None,
    planner: _Planner | None = None,
    with_github: bool = False,
    capture_max_paragraph_chars: int = 8000,
) -> DeepResearchAgent:
    return DeepResearchAgent(
        config=config,
        planner=planner or _Planner(),
        search_adapter=search,
        context_preparer=lambda *_args: ("", ""),
        summarizer=summarizer,
        reporting=reporter or _PlainReporter(),
        note_agent=None,
        github_adapter=None,
        research_kernel=_kernel(
            profile,
            search,
            with_github=with_github,
            capture_max_paragraph_chars=capture_max_paragraph_chars,
        ),
        summary_quality_gate=SummaryQualityGateV1(
            semantic_scorer=_SemanticScorer(),
            factual_verifier=_FactualVerifier(),
        ),
        task_quality_controller=TaskQualityController(
            ranker=_RelevantRanker(),
            judge=judge,  # type: ignore[arg-type]
        ),
    )


def _session(
    config: Configuration,
    *,
    topic: str,
    mode: ResearchMode,
    profile_id: str,
) -> RunSession:
    from research.contracts import ResearchCommand

    command = ResearchCommand(
        topic=topic,
        config=config,
        research_mode=mode,
        research_profile_id=profile_id,
    )
    session = RunSession(
        command=command,
        state=ResearchState(research_topic=command.topic),
    )
    session.start()
    return session


def _one_web_result(text: str, *, url: str = "https://docs.example.test/architecture") -> dict[str, object]:
    return {
        "results": [
            {
                "title": "Architecture documentation",
                "url": url,
                "content": text,
                "raw_content": f"<html><body><p>{text}</p></body></html>",
            }
        ]
    }


@pytest.mark.parametrize("structured_reporting", [True, False])
def test_web_planner_task_block_does_not_turn_unaccepted_capture_into_claims(
    structured_reporting: bool,
) -> None:
    """A terminal Judge block must leave collected-but-unaccepted evidence unfrozen."""
    search = _SearchDispatcher(
        lambda _query, _index: _one_web_result(
            "The overview evidence describes a stable public architecture."
        )
    )
    profile = built_in_profile_registry().get("web.evidence.v1")
    config = _config(task_quality_mode=QualityMode.STRICT.value)
    summarizer = _EvidenceSummarizer(lambda request: tuple(request.evidence))
    judge = _UncertainJudge()
    reporter = _StructuredReporter() if structured_reporting else _PlainReporter()
    agent = _agent(
        config,
        profile,
        search,
        summarizer,
        judge,
        reporter=reporter,
        planner=_Planner(),
    )
    session = _session(
        config,
        topic="Overview evidence",
        mode=ResearchMode.WEB,
        profile_id="web.evidence.v1",
    )

    if structured_reporting:
        with pytest.raises(EvidenceGateBlockedError):
            agent.execute(session, None)
    else:
        agent.execute(session, None)

    assert session.state.todo_items[0].status == "skipped"
    assert session.state.todo_items[0].retry_count == 2
    assert len(summarizer.requests) == 3
    bundle = ResearchIntelligenceBundle.from_dict(session.state.research_intelligence)
    assert bundle.evidence
    assert bundle.claims == ()
    assert bundle.evidence_frozen is False
    assert bundle.coverage.allow_report is False
    if structured_reporting:
        assert reporter.structured_calls == 0  # type: ignore[attr-defined]
    else:
        assert session.state.structured_report == "Fixture report."


def test_github_code_evidence_reaches_summary_judge_and_final_bundle() -> None:
    """Repository-only evidence stays pinned through a completed task."""
    search = _SearchDispatcher(lambda _query, _index: {"results": []})
    profile = _github_profile()
    config = _config()
    def code_only(request):
        return tuple(
            item for item in request.evidence
            if item.provider == "github" and item.locator.get("locator_type") == "line"
        )[:1]
    summarizer = _EvidenceSummarizer(code_only)
    judge = _SupportedJudge()
    agent = _agent(
        config,
        profile,
        search,
        summarizer,
        judge,
        with_github=True,
    )
    session = _session(
        config,
        topic="Review https://github.com/owner/repo",
        mode=ResearchMode.GITHUB,
        profile_id=_PROFILE_ID,
    )

    agent.execute(session, None)

    assert len(search.calls) == 1
    assert session.state.todo_items[0].status == "completed"
    assert len(summarizer.requests) == 1
    assert len(judge.calls) == 1
    summary_evidence = summarizer.selected_evidence[0]
    source_code = summary_evidence[0]
    assert source_code.provider == "github"
    assert source_code.locator == {
        "locator_type": "line",
        "url": f"https://github.com/owner/repo/blob/{_SHA}/src/main.py#L1-L2",
        "file_path": "src/main.py",
        "line_start": 1,
        "line_end": 2,
        "page_start": None,
        "page_end": None,
        "section": None,
        "paragraph": None,
        "fragment": None,
    }
    assert judge.calls[0][0].evidence_id == source_code.evidence_id  # type: ignore[attr-defined]
    bundle = ResearchIntelligenceBundle.from_dict(session.state.research_intelligence)
    record = next(item for item in bundle.evidence if item.evidence_id == source_code.evidence_id)  # type: ignore[attr-defined]
    assert record.evidence_type == "source_code"
    assert record.excerpt == source_code.excerpt  # type: ignore[attr-defined]
    assert record.source.resolved_version == _SHA
    assert record.locator.as_dict() == dict(source_code.locator)  # type: ignore[attr-defined]
    assert bundle.evidence_frozen is True


def test_mixed_repository_and_web_evidence_keep_identity_through_report() -> None:
    """One task preserves both providers' evidence IDs and exact report citations."""
    web_excerpt = "The architecture guide explains how public modules connect."
    search = _SearchDispatcher(
        lambda _query, _index: _one_web_result(web_excerpt)
    )
    profile = _github_profile()
    config = _config()

    def select_mixed(request: object) -> tuple[object, ...]:
        evidence = tuple(request.evidence)  # type: ignore[attr-defined]
        repository = next(
            item
            for item in evidence
            if item.provider == "github" and item.locator.get("locator_type") == "line"
        )
        web = next(item for item in evidence if item.provider == "web")
        return repository, web

    summarizer = _EvidenceSummarizer(select_mixed)
    judge = _SupportedJudge()
    reporter = _StructuredReporter()
    agent = _agent(
        config,
        profile,
        search,
        summarizer,
        judge,
        reporter=reporter,
        with_github=True,
    )
    session = _session(
        config,
        topic="Review https://github.com/owner/repo",
        mode=ResearchMode.GITHUB,
        profile_id=_PROFILE_ID,
    )

    agent.execute(session, None)

    assert session.state.todo_items[0].status == "completed"
    assert len(summarizer.requests) == len(judge.calls) == 1
    task_evidence = summarizer.selected_evidence[0]
    assert {item.provider for item in summarizer.requests[0].evidence} == {"github", "web"}  # type: ignore[attr-defined]
    assert {item.provider for item in task_evidence} == {"github", "web"}
    expected_ids = {item.evidence_id for item in task_evidence}  # type: ignore[attr-defined]
    judged_by_id = {
        item.evidence_id: item
        for item in judge.calls[0]  # type: ignore[union-attr]
    }
    assert set(judged_by_id) == expected_ids
    bundle = ResearchIntelligenceBundle.from_dict(session.state.research_intelligence)
    final_by_id = {item.evidence_id: item for item in bundle.evidence}
    assert expected_ids.issubset(final_by_id)
    for task_record in task_evidence:
        final_record = final_by_id[task_record.evidence_id]
        assert final_record.excerpt == task_record.excerpt
        assert final_record.locator.as_dict() == dict(task_record.locator)
        assert final_record.source.as_dict() == dict(task_record.source_provenance)
        assert judged_by_id[task_record.evidence_id].excerpt == task_record.excerpt
        assert judged_by_id[task_record.evidence_id].locator == task_record.locator
        assert judged_by_id[task_record.evidence_id].source_provenance == task_record.source_provenance
    assert reporter.rendered_document is not None
    report_citations = {
        evidence_id
        for paragraph in reporter.rendered_document.paragraphs
        for evidence_id in paragraph.citation_ids
    }
    assert report_citations == expected_ids
    persisted_citations = {
        evidence_id
        for paragraph in session.state.structured_summary["paragraphs"]
        for evidence_id in paragraph["citation_ids"]
    }
    assert persisted_citations == expected_ids


def test_exhausted_evidence_budget_skips_task_without_calling_summary() -> None:
    """Nonempty search results cannot reach a summary after evidence admission is exhausted."""
    search = _SearchDispatcher(
        lambda query, index: _one_web_result(
            f"The quota evidence for {query} supports a bounded research finding.",
            url=f"https://quota.example.test/page-{index}",
        )
    )
    templates = (
        _template(
            "first_task",
            "overview",
            "First task",
            "Find first quota evidence",
            "first quota evidence",
            ResearchMode.WEB,
        ),
        _template(
            "second_task",
            "architecture",
            "Second task",
            "Find second quota evidence",
            "second quota evidence",
            ResearchMode.WEB,
        ),
    )
    profile = _web_profile(
        templates,
        dimensions=(
            ResearchDimension(id="overview", title="Overview"),
            ResearchDimension(
                id="architecture",
                title="Architecture",
                required=False,
            ),
        ),
        max_evidence=1,
    )
    config = _config()
    summarizer = _EvidenceSummarizer(lambda request: tuple(request.evidence[:1]))  # type: ignore[attr-defined]
    judge = _SupportedJudge()
    agent = _agent(config, profile, search, summarizer, judge)
    session = _session(
        config,
        topic="Evidence budget exhaustion",
        mode=ResearchMode.WEB,
        profile_id="web.evidence.v1",
    )

    agent.execute(session, None)

    assert len(search.calls) == 4
    assert all(call.strip() for call in search.calls)
    assert [item.status for item in session.state.todo_items] == ["completed", "skipped"]
    assert session.state.todo_items[1].retry_count == 2
    assert len(summarizer.requests) == 1
    assert len(judge.calls) == 1
    assert len(summarizer.requests[0].evidence) == 1  # type: ignore[attr-defined]


def test_long_web_excerpt_is_capped_before_selection_and_judge_sees_full_cap() -> None:
    """A larger profile budget still binds a relevant 2,000-character canonical excerpt."""
    tail = "Phoenix quantization preserves the trailing invariant exactly."
    long_text = ("Unrelated implementation notes fill this source paragraph. " * 180) + tail
    search = _SearchDispatcher(lambda _query, _index: _one_web_result(long_text))
    template = _template(
        "tail_evidence",
        "overview",
        "Trailing invariant",
        "Find the Phoenix quantization trailing invariant",
        "Phoenix quantization invariant trailing clause",
        ResearchMode.WEB,
    )
    profile = _web_profile(
        (template,),
        dimensions=(ResearchDimension(id="overview", title="Overview"),),
        max_excerpt_chars=5000,
    )
    config = _config()
    summarizer = _EvidenceSummarizer(
        lambda request: tuple(
            item for item in request.evidence if item.provider == "web"  # type: ignore[attr-defined]
        )[:1]
    )
    judge = _SupportedJudge()
    agent = _agent(
        config,
        profile,
        search,
        summarizer,
        judge,
        capture_max_paragraph_chars=20_000,
    )
    session = _session(
        config,
        topic="Phoenix quantization invariant",
        mode=ResearchMode.WEB,
        profile_id="web.evidence.v1",
    )

    agent.execute(session, None)

    assert len(long_text) > 5000
    assert profile.retrieval_budget.max_excerpt_chars == 5000
    evidence = summarizer.selected_evidence[0][0]
    assert 1900 < len(evidence.excerpt) <= 2000
    assert evidence.excerpt.endswith(tail)
    assert judge.calls[0][0].excerpt == evidence.excerpt
    assert len(judge.calls[0][0].excerpt) <= 2000
    bundle = ResearchIntelligenceBundle.from_dict(session.state.research_intelligence)
    final_record = next(
        item for item in bundle.evidence if item.evidence_id == evidence.evidence_id
    )
    assert final_record.excerpt == evidence.excerpt
    assert final_record.locator.as_dict() == dict(evidence.locator)
    assert final_record.source.as_dict() == dict(evidence.source_provenance)
