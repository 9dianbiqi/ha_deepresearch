"""Structured paragraph citation validation and rendering tests."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from research.intelligence import (
    ClaimRecord,
    CoverageDecision,
    EvidenceLocator,
    EvidenceRecord,
    GenericReportSpec,
    ResearchIntelligenceBundle,
    SourceReference,
)
from research.profiles import ResearchMode, built_in_profile_registry
from research.report_document import StructuredSummaryDocument, SummaryParagraph
from research.report_renderer import (
    StructuredReportValidationError,
    render_structured_report,
)
from research.report_validation import validate_structured_citations
from services.reporter import (
    GenericReportingContext,
    ReportingService,
    StructuredReportGenerationError,
)


def _records() -> tuple[
    tuple[ClaimRecord, ...],
    tuple[EvidenceRecord, ...],
]:
    """Build two claims and two addressable evidence records."""
    source = SourceReference(
        provider_id="github",
        source_kind="repository_source",
        source_id="owner/repo:src/main.py",
        canonical_url="https://github.com/owner/repo",
        resolved_version="0123456789abcdef0123456789abcdef01234567",
        content_hash="sha256:source",
    )
    architecture = EvidenceRecord(
        evidence_id="ev_architecture",
        source=source,
        evidence_type="source_excerpt",
        evidence_level="full_text",
        title="Application module",
        excerpt="The application is initialized in the main module.",
        locator=EvidenceLocator(
            locator_type="line",
            url=(
                "https://github.com/owner/repo/blob/"
                "0123456789abcdef0123456789abcdef01234567/"
                "src/main.py#L4-L8"
            ),
            file_path="src/main.py",
            line_start=4,
            line_end=8,
        ),
    )
    streaming = EvidenceRecord(
        evidence_id="ev_streaming",
        source=source,
        evidence_type="source_excerpt",
        evidence_level="full_text",
        title="Streaming route",
        excerpt="The route returns a server-sent event stream.",
        locator=EvidenceLocator(
            locator_type="line",
            url=(
                "https://github.com/owner/repo/blob/"
                "0123456789abcdef0123456789abcdef01234567/"
                "src/main.py#L20-L28"
            ),
            file_path="src/main.py",
            line_start=20,
            line_end=28,
        ),
    )
    claims = (
        ClaimRecord(
            claim_id="claim_architecture",
            dimension="overview",
            statement="The main module initializes the application.",
            confidence="high",
            evidence_ids=(architecture.evidence_id,),
        ),
        ClaimRecord(
            claim_id="claim_streaming",
            dimension="overview",
            statement="The route streams server-sent events.",
            confidence="high",
            evidence_ids=(streaming.evidence_id,),
        ),
    )
    return claims, (architecture, streaming)


def _records_with_conflict() -> tuple[
    tuple[ClaimRecord, ...],
    tuple[EvidenceRecord, ...],
]:
    """Extend the base ledger with evidence that contradicts one claim."""
    claims, evidence = _records()
    conflicting = EvidenceRecord(
        evidence_id="ev_conflicting",
        source=evidence[0].source,
        evidence_type="source_excerpt",
        evidence_level="full_text",
        title="Conflicting initialization path",
        excerpt="A separate factory initializes the application instead.",
        locator=EvidenceLocator(
            locator_type="line",
            url=(
                "https://github.com/owner/repo/blob/"
                "0123456789abcdef0123456789abcdef01234567/"
                "src/main.py#L30-L36"
            ),
            file_path="src/main.py",
            line_start=30,
            line_end=36,
        ),
    )
    architecture = replace(
        claims[0],
        conflicting_evidence_ids=(conflicting.evidence_id,),
    )
    return (architecture, claims[1]), (*evidence, conflicting)


def _document() -> StructuredSummaryDocument:
    """Build a valid document that reuses one citation across paragraphs."""
    return StructuredSummaryDocument(
        task_id="report",
        paragraphs=(
            SummaryParagraph(
                section_id="overview",
                paragraph_type="factual",
                text="The main module initializes the application.",
                claim_ids=("claim_architecture",),
                citation_ids=("ev_architecture",),
            ),
            SummaryParagraph(
                section_id="overview",
                paragraph_type="factual",
                text="Application setup is visible in the captured module.",
                claim_ids=("claim_architecture",),
                citation_ids=("ev_architecture",),
            ),
            SummaryParagraph(
                section_id="overview",
                paragraph_type="analysis",
                text="The streaming route supports progressive updates.",
                claim_ids=("claim_streaming",),
                citation_ids=("ev_streaming",),
            ),
            SummaryParagraph(
                section_id="limitations",
                paragraph_type="limitation",
                text="Runtime behavior was not exercised.",
            ),
        ),
        claim_ids=("claim_architecture", "claim_streaming"),
    )


def test_renderer_owns_stable_markers_and_complete_trace_mapping() -> None:
    """Markers derive from IDs and traces reach source, locator, and excerpt."""
    claims, evidence = _records()
    document = _document()

    rendered = render_structured_report(
        document,
        title="Repository report",
        claims=reversed(claims),
        evidence=reversed(evidence),
        section_titles={"overview": "Overview", "limitations": "Limitations"},
    )
    repeated = render_structured_report(
        document,
        title="Repository report",
        claims=claims,
        evidence=evidence,
        section_titles={"overview": "Overview", "limitations": "Limitations"},
    )

    assert rendered.markdown == repeated.markdown
    assert "The main module initializes the application. [E1]" in rendered.markdown
    assert "The route streams server-sent events. — Evidence: [E2]" in rendered.markdown
    assert rendered.markdown.count("Application module](https://github.com/") == 1
    assert rendered.markdown.endswith("\n")

    first, second = rendered.paragraph_traces[:2]
    assert first.citations[0].evidence_id == "ev_architecture"
    assert first.citations[0].claim_ids == ("claim_architecture",)
    assert first.citations[0].source_id == "owner/repo:src/main.py"
    assert first.citations[0].locator.line_start == 4
    assert first.citations[0].excerpt.startswith("The application")
    assert second.citations[0].evidence_id == first.citations[0].evidence_id
    assert rendered.as_dict()["citation_gate"]["valid"] is True  # type: ignore[index]


@pytest.mark.parametrize(
    ("paragraph", "reason"),
    [
        (
            SummaryParagraph(
                section_id="overview",
                paragraph_type="factual",
                text="A factual statement.",
                claim_ids=("claim_architecture",),
            ),
            "paragraph_citation_missing",
        ),
        (
            SummaryParagraph(
                section_id="overview",
                paragraph_type="analysis",
                text="An externally grounded analysis.",
                claim_ids=("claim_architecture",),
            ),
            "paragraph_citation_missing",
        ),
        (
            SummaryParagraph(
                section_id="overview",
                paragraph_type="factual",
                text="A factual statement.",
                claim_ids=("claim_architecture",),
                citation_ids=("ev_unknown",),
            ),
            "unknown_evidence_id",
        ),
        (
            SummaryParagraph(
                section_id="overview",
                paragraph_type="factual",
                text="A factual statement.",
                claim_ids=("claim_architecture",),
                citation_ids=("ev_streaming",),
            ),
            "citation_not_bound_to_claim",
        ),
        (
            SummaryParagraph(
                section_id="overview",
                paragraph_type="factual",
                text="A factual statement at https://evil.example.invalid.",
                claim_ids=("claim_architecture",),
                citation_ids=("ev_architecture",),
            ),
            "illegal_locator_url",
        ),
        (
            SummaryParagraph(
                section_id="overview",
                paragraph_type="factual",
                text="A model-authored marker [E1].",
                claim_ids=("claim_architecture",),
                citation_ids=("ev_architecture",),
            ),
            "system_citation_marker_in_text",
        ),
    ],
)
def test_structured_gate_fails_closed(
    paragraph: SummaryParagraph,
    reason: str,
) -> None:
    """Unknown, unbound, missing, and injected citations are rejected."""
    claims, evidence = _records()
    document = StructuredSummaryDocument(
        task_id="report",
        paragraphs=(paragraph,),
        claim_ids=("claim_architecture",),
    )

    gate = validate_structured_citations(document, claims, evidence)

    assert not gate.valid
    assert reason in gate.failure_reasons
    with pytest.raises(StructuredReportValidationError) as raised:
        render_structured_report(
            document,
            title="Report",
            claims=claims,
            evidence=evidence,
        )
    assert reason in raised.value.gate.failure_reasons


def test_limitation_without_claim_or_citation_is_valid() -> None:
    """A limitation paragraph can explicitly remain uncited."""
    claims, evidence = _records()
    document = StructuredSummaryDocument(
        task_id="report",
        paragraphs=(
            SummaryParagraph(
                section_id="limitations",
                paragraph_type="limitation",
                text="The deployed service was not exercised.",
            ),
        ),
    )

    result = render_structured_report(
        document,
        title="Report",
        claims=claims,
        evidence=evidence,
    )

    assert result.citation_gate.valid
    assert "The deployed service was not exercised." in result.markdown
    assert result.paragraph_traces[0].citations == ()


def test_conflicting_evidence_is_bound_rendered_and_traced() -> None:
    """Explicit conflict paragraphs retain their Claim relationship."""
    claims, evidence = _records_with_conflict()
    document = StructuredSummaryDocument(
        task_id="report",
        paragraphs=(
            SummaryParagraph(
                section_id="conflicts",
                paragraph_type="analysis",
                text="The captured sources disagree about initialization.",
                claim_ids=("claim_architecture",),
                citation_ids=("ev_conflicting",),
            ),
        ),
        claim_ids=("claim_architecture",),
    )

    result = render_structured_report(
        document,
        title="Conflict report",
        claims=claims,
        evidence=evidence,
    )

    citation = result.paragraph_traces[0].citations[0]
    assert citation.evidence_id == "ev_conflicting"
    assert citation.claim_ids == ("claim_architecture",)
    assert "Conflicting evidence: [E2]" in result.markdown
    assert "disagree about initialization. [E2]" in result.markdown


def test_paragraph_url_allowlist_uses_only_actual_citation_ids() -> None:
    """An uncited locator from the same Claim cannot enter paragraph prose."""
    claims, evidence = _records_with_conflict()
    cited_url = evidence[0].locator.url
    uncited_url = evidence[2].locator.url
    cited_document = StructuredSummaryDocument(
        task_id="report",
        paragraphs=(
            SummaryParagraph(
                section_id="overview",
                paragraph_type="factual",
                text=f"The cited locator is {cited_url}",
                claim_ids=("claim_architecture",),
                citation_ids=("ev_architecture",),
            ),
        ),
        claim_ids=("claim_architecture",),
    )
    uncited_document = StructuredSummaryDocument(
        task_id="report",
        paragraphs=(
            SummaryParagraph(
                section_id="overview",
                paragraph_type="factual",
                text=f"The uncited locator is {uncited_url}",
                claim_ids=("claim_architecture",),
                citation_ids=("ev_architecture",),
            ),
        ),
        claim_ids=("claim_architecture",),
    )

    cited_gate = validate_structured_citations(cited_document, claims, evidence)
    uncited_gate = validate_structured_citations(uncited_document, claims, evidence)

    assert cited_gate.valid
    assert not uncited_gate.valid
    assert uncited_url in uncited_gate.illegal_urls
    assert "illegal_locator_url" in uncited_gate.failure_reasons


def test_unknown_claim_and_unfrozen_evidence_are_rejected() -> None:
    """The document cannot widen the frozen claim or evidence ledgers."""
    claims, evidence = _records()
    paragraph = SummaryParagraph(
        section_id="overview",
        paragraph_type="factual",
        text="An unknown claim cannot be cited.",
        claim_ids=("claim_unknown",),
        citation_ids=("ev_architecture",),
    )
    document = StructuredSummaryDocument(
        task_id="report",
        paragraphs=(paragraph,),
        claim_ids=("claim_unknown",),
    )

    unknown_gate = validate_structured_citations(document, claims, evidence)
    frozen_gate = validate_structured_citations(
        _document(),
        claims,
        evidence,
        evidence_frozen=False,
    )

    assert "claim_unknown" in unknown_gate.unknown_claim_ids
    assert "unknown_claim_id" in unknown_gate.failure_reasons
    assert "evidence_not_frozen" in frozen_gate.failure_reasons


class _ReporterAgent:
    """Minimal deterministic reporter double."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.prompts: list[str] = []
        self.clear_count = 0

    def run(self, prompt: str, **_: object) -> str:
        """Capture the prompt and return the configured response."""
        self.prompts.append(prompt)
        return self.response

    def clear_history(self) -> None:
        """Record history cleanup."""
        self.clear_count += 1


def _context() -> GenericReportingContext:
    """Build a frozen structured-reporting context."""
    claims, evidence = _records()
    profile = built_in_profile_registry().get("github.repository.v1")
    bundle = ResearchIntelligenceBundle(
        mode=ResearchMode.GITHUB,
        profile_id=profile.profile_id,
        profile_version=profile.version,
        sources=(evidence[0].source,),
        evidence=evidence,
        claims=claims,
        coverage=CoverageDecision(
            required_dimensions=("overview",),
            covered_dimensions=("overview",),
            coverage_score=1.0,
            allow_report=True,
        ),
        report_spec=GenericReportSpec(
            title="Repository report",
            claim_ids=tuple(item.claim_id for item in claims),
            citation_ids=tuple(item.evidence_id for item in evidence),
        ),
        evidence_frozen=True,
    )
    return GenericReportingContext(
        topic="owner/repo",
        profile=profile,
        bundle=bundle,
    )


def test_opt_in_structured_reporter_parses_json_then_uses_renderer() -> None:
    """The new boundary returns traces while the old string API remains intact."""
    paragraph = _document().paragraphs[0]
    payload = StructuredSummaryDocument(
        task_id="report",
        paragraphs=(paragraph,),
        claim_ids=("claim_architecture",),
    ).as_dict()
    agent = _ReporterAgent(json.dumps(payload))
    service = ReportingService(
        agent,  # type: ignore[arg-type]
        type("Config", (), {"strip_thinking_tokens": False})(),  # type: ignore[arg-type]
    )

    result = service.generate_structured_report(_context())

    assert result.markdown.startswith("# Repository report")
    assert result.paragraph_traces[0].citations[0].evidence_id == "ev_architecture"
    assert "Return exactly one JSON object" in agent.prompts[0]
    assert "Write concise Markdown" not in agent.prompts[0]
    assert agent.clear_count == 1


def test_opt_in_structured_reporter_rejects_non_json_without_fallback() -> None:
    """Malformed structured output fails closed instead of becoming Markdown."""
    agent = _ReporterAgent("```json\n{}\n```")
    service = ReportingService(
        agent,  # type: ignore[arg-type]
        type("Config", (), {"strip_thinking_tokens": False})(),  # type: ignore[arg-type]
    )

    with pytest.raises(StructuredReportGenerationError, match="invalid JSON"):
        service.generate_structured_report(_context())
    assert agent.clear_count == 1
