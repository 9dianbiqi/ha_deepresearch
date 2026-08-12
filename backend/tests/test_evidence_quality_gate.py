"""Deterministic report-before-generation Evidence quality gate tests."""

from __future__ import annotations

from research.intelligence import (
    ClaimRecord,
    CoverageDecision,
    EvidenceLocator,
    EvidenceRecord,
    GenericReportSpec,
    ResearchIntelligenceBundle,
    SourceReference,
)
from research.profiles import (
    CoveragePolicy,
    ResearchDimension,
    ResearchMode,
    ResearchProfile,
)
from research.quality import EvidenceQualityGate


def _profile(*, required: tuple[str, ...] = ("overview",)) -> ResearchProfile:
    """Build a small profile with explicit coverage rules."""
    return ResearchProfile(
        profile_id="test.quality.v1",
        version=1,
        mode=ResearchMode.PAPER,
        dimensions=tuple(
            ResearchDimension(id=item, title=item) for item in required
        ),
        task_templates=(),
        source_priority=("fake",),
        coverage_policy=CoveragePolicy(
            required_dimensions=required,
            min_coverage_score=1.0,
            min_evidence_per_dimension=1,
            min_independent_sources=1,
        ),
    )


def _bundle(*, dimension: str = "overview") -> ResearchIntelligenceBundle:
    """Build one valid bundle for gate assertions."""
    source = SourceReference(
        provider_id="fake",
        source_kind="paper",
        source_id="paper-1",
        canonical_url="https://example.test/paper-1",
        resolved_version="v1",
        content_hash="hash",
    )
    evidence = EvidenceRecord(
        evidence_id="ev-1",
        source=source,
        evidence_type="abstract",
        evidence_level="abstract",
        title="Abstract",
        excerpt="A bounded abstract excerpt.",
        locator=EvidenceLocator(locator_type="abstract", url=source.canonical_url),
    )
    claim = ClaimRecord(
        claim_id="claim-1",
        dimension=dimension,
        statement="The source supports the dimension.",
        confidence="high",
        evidence_ids=(evidence.evidence_id,),
    )
    return ResearchIntelligenceBundle(
        mode=ResearchMode.PAPER,
        profile_id="test.quality.v1",
        profile_version=1,
        sources=(source,),
        evidence=(evidence,),
        claims=(claim,),
        coverage=CoverageDecision(
            required_dimensions=(dimension,),
            covered_dimensions=(dimension,),
            coverage_score=1.0,
            allow_report=True,
        ),
        report_spec=GenericReportSpec(
            title="Quality test",
            claim_ids=(claim.claim_id,),
            citation_ids=(evidence.evidence_id,),
        ),
    )


def test_quality_gate_is_deterministic_and_allows_complete_bundle() -> None:
    """A complete bundle passes without an LLM or provider call."""
    decision = EvidenceQualityGate().evaluate(_bundle(), _profile())

    assert decision.allow_report is True
    assert decision.coverage_score == 1.0
    assert decision.missing_dimensions == ()
    assert decision.dimension_results[0]["independent_source_count"] == 1


def test_quality_gate_blocks_missing_required_dimension() -> None:
    """Missing profile dimensions block report generation and expose a gap."""
    decision = EvidenceQualityGate().evaluate(
        _bundle(), _profile(required=("overview", "method"))
    )

    assert decision.allow_report is False
    assert decision.missing_dimensions == ("method",)
    assert "missing_dimension:method" in decision.blockers
    assert decision.gap_queries
