"""Three-dimensional structured-summary quality gate tests."""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest

from research.claim_verifier import (
    FactualVerification,
    StructuredFactualSupportVerifier,
    SupportSpan,
    VerifierResponseError,
)
from research.intelligence import (
    ClaimRecord,
    EvidenceLocator,
    EvidenceRecord,
    GenericReportSpec,
    ResearchIntelligenceBundle,
    SourceReference,
)
from research.profiles import ResearchMode
from research.report_document import StructuredSummaryDocument, SummaryParagraph
from research.summary_quality import SummaryQualityGateV1


class FixedSemanticScorer:
    """Return one configured offline semantic score."""

    name = "fake-semantic"
    version = "1"

    def __init__(self, score: float = 1.0) -> None:
        self._score = score

    def score(
        self,
        claim: ClaimRecord,
        evidence: tuple[EvidenceRecord, ...],
    ) -> float:
        del claim, evidence
        return self._score


class FixedFactualVerifier:
    """Return strict offline support using all supporting Evidence."""

    name = "fake-factual"
    version = "1"
    prompt_version = "fixture-v1"

    def __init__(self, verdict: str = "supported", score: float = 1.0) -> None:
        self._verdict = verdict
        self._score = score

    def verify(
        self,
        claim: ClaimRecord,
        evidence: tuple[EvidenceRecord, ...],
    ) -> FactualVerification:
        evidence_by_id = {item.evidence_id: item for item in evidence}
        supporting = claim.evidence_ids if self._verdict in {"supported", "partial"} else ()
        conflicting = (
            claim.conflicting_evidence_ids if self._verdict == "contradicted" else ()
        )
        return FactualVerification(
            verdict=self._verdict,
            factual_score=self._score,
            supporting_evidence_ids=supporting,
            conflicting_evidence_ids=conflicting,
            support_spans=tuple(
                SupportSpan(
                    evidence_id=item,
                    exact_text=evidence_by_id[item].excerpt,
                )
                for item in (*supporting, *conflicting)
            ),
            reasons=(f"fixture:{self._verdict}",),
        )


def _source(index: int) -> SourceReference:
    return SourceReference(
        provider_id="fake",
        source_kind="web",
        source_id=f"source-{index}",
        canonical_url=f"https://source-{index}.example.test/article",
        resolved_version="capture-v1",
        content_hash=f"hash-{index}",
    )


def _evidence(
    index: int,
    *,
    level: str = "full_text",
    source: SourceReference | None = None,
) -> EvidenceRecord:
    source = source or _source(index)
    return EvidenceRecord(
        evidence_id=f"ev-{index}",
        source=source,
        evidence_type="web_paragraph",
        evidence_level=level,
        title=f"Evidence {index}",
        excerpt=f"Exact supporting sentence {index}.",
        locator=EvidenceLocator(
            locator_type="paragraph",
            url=source.canonical_url,
            section="Results",
            paragraph=f"p-{index}",
        ),
    )


def _case(
    *,
    evidence: tuple[EvidenceRecord, ...] | None = None,
    supporting_ids: tuple[str, ...] | None = None,
    conflicting_ids: tuple[str, ...] = (),
    citations: tuple[str, ...] | None = None,
    confidence: str = "high",
    evidence_frozen: bool = True,
    paragraph_type: str = "factual",
) -> tuple[StructuredSummaryDocument, ResearchIntelligenceBundle]:
    evidence = evidence or (_evidence(1), _evidence(2))
    supporting_ids = (
        tuple(item.evidence_id for item in evidence)
        if supporting_ids is None
        else supporting_ids
    )
    citations = citations if citations is not None else supporting_ids
    claim = ClaimRecord(
        claim_id="claim-1",
        dimension="overview",
        statement="The evidence supports the result.",
        confidence=confidence,
        evidence_ids=supporting_ids,
        conflicting_evidence_ids=conflicting_ids,
    )
    bundle = ResearchIntelligenceBundle(
        mode=ResearchMode.WEB,
        profile_id="web.evidence.v1",
        profile_version=1,
        sources=tuple(dict.fromkeys(item.source for item in evidence)),
        evidence=evidence,
        claims=(claim,),
        report_spec=GenericReportSpec(
            title="Summary quality",
            claim_ids=(claim.claim_id,),
            citation_ids=tuple(item.evidence_id for item in evidence),
        ),
        evidence_frozen=evidence_frozen,
    )
    paragraph = SummaryParagraph(
        section_id="overview",
        paragraph_type=paragraph_type,
        text="A factual paragraph requiring traceable support.",
        claim_ids=(claim.claim_id,),
        citation_ids=citations,
    )
    return (
        StructuredSummaryDocument(
            task_id="task-1",
            paragraphs=(paragraph,),
            claim_ids=(claim.claim_id,),
        ),
        bundle,
    )


def _gate(
    *,
    verifier: object | None = None,
    semantic_score: float = 1.0,
) -> SummaryQualityGateV1:
    return SummaryQualityGateV1(
        semantic_scorer=FixedSemanticScorer(semantic_score),
        factual_verifier=verifier or FixedFactualVerifier(),  # type: ignore[arg-type]
    )


def test_complete_two_source_summary_passes() -> None:
    document, bundle = _case()

    result = _gate().evaluate(document, bundle)

    assert result.passed is True
    assert result.overall_score == pytest.approx(1.0)
    assert dict(result.thresholds) == {
        "semantic_score": 0.72,
        "factual_score": 0.75,
        "citation_score": 0.85,
        "overall_score": 0.78,
    }
    assert result.paragraph_assessments[0].level == "high"
    assert result.claim_assessments[0].verdict == "supported"


@pytest.mark.parametrize(
    ("citations", "expected_blocker"),
    [
        ((), "factual_missing_citations"),
        (("unknown",), "unknown_evidence:unknown"),
    ],
)
def test_factual_paragraph_rejects_missing_or_unknown_citations(
    citations: tuple[str, ...],
    expected_blocker: str,
) -> None:
    document, bundle = _case(citations=citations)

    result = _gate().evaluate(document, bundle)

    assert result.passed is False
    assert expected_blocker in result.paragraph_assessments[0].blockers


def test_paragraph_rejects_citation_not_bound_to_claim() -> None:
    evidence = (_evidence(1), _evidence(2), _evidence(3))
    document, bundle = _case(
        evidence=evidence,
        supporting_ids=("ev-1", "ev-2"),
        citations=("ev-1", "ev-3"),
    )

    result = _gate().evaluate(document, bundle)

    assert result.passed is False
    assert "citation_not_bound:ev-3" in result.paragraph_assessments[0].blockers


def test_core_contradiction_is_a_hard_blocker() -> None:
    evidence = (_evidence(1), _evidence(2))
    document, bundle = _case(
        evidence=evidence,
        supporting_ids=(),
        conflicting_ids=("ev-1", "ev-2"),
        citations=("ev-1", "ev-2"),
    )

    result = _gate(verifier=FixedFactualVerifier("contradicted", 0.0)).evaluate(
        document,
        bundle,
    )

    assessment = result.claim_assessments[0]
    assert result.passed is False
    assert assessment.verdict == "contradicted"
    assert assessment.support_confidence <= 0.59
    assert "core_claim_contradicted:claim-1" in result.paragraph_assessments[0].blockers


def test_partial_support_with_unresolved_conflict_is_capped() -> None:
    evidence = (_evidence(1), _evidence(2), _evidence(3))
    document, bundle = _case(
        evidence=evidence,
        supporting_ids=("ev-1", "ev-2"),
        conflicting_ids=("ev-3",),
        citations=("ev-1", "ev-2", "ev-3"),
    )

    result = _gate(verifier=FixedFactualVerifier("partial", 0.80)).evaluate(
        document,
        bundle,
    )

    assessment = result.claim_assessments[0]
    assert assessment.verdict == "partial"
    assert assessment.support_confidence <= 0.59
    assert "confidence_cap:unresolved_conflict" in assessment.reasons


def test_strong_claim_with_only_metadata_or_derived_evidence_is_blocked() -> None:
    document, bundle = _case(
        evidence=(_evidence(1, level="metadata"), _evidence(2, level="derived")),
    )

    result = _gate().evaluate(document, bundle)

    assert result.passed is False
    assert (
        "strong_claim_low_grade_only:claim-1"
        in result.paragraph_assessments[0].blockers
    )


def test_metadata_only_and_single_source_confidence_caps() -> None:
    shared_source = _source(1)
    metadata = (
        _evidence(1, level="metadata", source=shared_source),
        _evidence(2, level="metadata", source=shared_source),
    )
    document, bundle = _case(evidence=metadata, confidence="medium")

    result = _gate().evaluate(document, bundle)

    assessment = result.claim_assessments[0]
    assert assessment.support_confidence <= 0.60
    assert "confidence_cap:metadata_only" in assessment.reasons
    assert "confidence_cap:single_source" in assessment.reasons


def test_single_full_text_source_is_capped_at_point_seven_five() -> None:
    evidence = (_evidence(1),)
    document, bundle = _case(evidence=evidence)

    result = _gate().evaluate(document, bundle)

    assert result.claim_assessments[0].support_confidence == pytest.approx(0.75)
    assert result.passed is False


def test_different_source_ids_with_same_canonical_url_are_not_independent() -> None:
    source_one = _source(1)
    source_two = SourceReference(
        provider_id="second-provider",
        source_kind="web",
        source_id="mirrored-source",
        canonical_url=source_one.canonical_url,
        resolved_version="capture-v1",
        content_hash="mirror-hash",
    )
    evidence = (
        _evidence(1, source=source_one),
        _evidence(2, source=source_two),
    )
    document, bundle = _case(evidence=evidence)

    result = _gate().evaluate(document, bundle)

    assessment = result.claim_assessments[0]
    assert assessment.support_confidence == pytest.approx(0.75)
    assert "confidence_cap:single_source" in assessment.reasons


def test_imprecise_url_only_locator_degrades_citation_quality() -> None:
    precise = _evidence(1)
    source = _source(2)
    imprecise = EvidenceRecord(
        evidence_id="ev-2",
        source=source,
        evidence_type="web_page",
        evidence_level="full_text",
        title="Unlocated page",
        excerpt="Exact supporting sentence 2.",
        locator=EvidenceLocator(locator_type="url", url=source.canonical_url),
    )
    document, bundle = _case(evidence=(precise, imprecise))

    result = _gate().evaluate(document, bundle)

    paragraph = result.paragraph_assessments[0]
    assert paragraph.citation_score < 0.85
    assert "citation_score_below_threshold" in paragraph.blockers


def test_core_unsupported_claim_is_capped_at_point_four_nine() -> None:
    document, bundle = _case()

    result = _gate(verifier=FixedFactualVerifier("unsupported", 0.0)).evaluate(
        document,
        bundle,
    )

    assessment = result.claim_assessments[0]
    assert assessment.verdict == "unsupported"
    assert assessment.support_confidence <= 0.49
    assert "confidence_cap:core_unsupported" in assessment.reasons


@pytest.mark.parametrize(
    "invoker",
    [
        lambda claim, evidence: "not-json",
        lambda claim, evidence: (_ for _ in ()).throw(TimeoutError()),
    ],
)
def test_invalid_json_and_timeout_fail_closed_as_unverified(invoker: object) -> None:
    verifier = StructuredFactualSupportVerifier(
        invoker,  # type: ignore[arg-type]
        name="strict-fake",
        version="1",
        prompt_version="prompt-v1",
    )
    document, bundle = _case()

    result = _gate(verifier=verifier).evaluate(document, bundle)

    assessment = result.claim_assessments[0]
    assert result.passed is False
    assert assessment.verdict == "unverified"
    assert assessment.factual_score == 0.0
    assert assessment.support_confidence == 0.0
    assert result.paragraph_assessments[0].level == "unverified"


def test_default_dependencies_do_not_invent_high_confidence() -> None:
    document, bundle = _case()

    result = SummaryQualityGateV1().evaluate(document, bundle)

    assert result.passed is False
    assert result.claim_assessments[0].verdict == "unverified"
    assert result.claim_assessments[0].support_confidence == 0.0


def test_unfrozen_evidence_blocks_even_an_otherwise_valid_summary() -> None:
    document, bundle = _case(evidence_frozen=False)

    result = _gate().evaluate(document, bundle)

    assert result.passed is False
    assert "evidence_not_frozen" in result.paragraph_assessments[0].blockers


def _valid_payload() -> dict[str, object]:
    return {
        "verdict": "supported",
        "factual_score": 1.0,
        "supporting_evidence_ids": ["ev-1", "ev-2"],
        "conflicting_evidence_ids": [],
        "support_spans": [
            {"evidence_id": "ev-1", "exact_text": "Exact supporting sentence 1."},
            {"evidence_id": "ev-2", "exact_text": "Exact supporting sentence 2."},
        ],
        "reasons": ["direct support"],
    }


def test_strict_verifier_accepts_exact_claim_bound_spans() -> None:
    document, bundle = _case()
    del document
    verifier = StructuredFactualSupportVerifier(
        lambda claim, evidence: json.dumps(_valid_payload()),
        name="strict-fake",
        version="1",
        prompt_version="prompt-v1",
    )

    result = verifier.verify(bundle.claims[0], bundle.evidence)

    assert result.verdict == "supported"
    assert tuple(item.evidence_id for item in result.support_spans) == ("ev-1", "ev-2")


@pytest.mark.parametrize(
    "mutation",
    [
        {"unknown": "field"},
        {"supporting_evidence_ids": ["unknown"]},
        {
            "support_spans": [
                {"evidence_id": "ev-1", "exact_text": "invented quotation"},
                {"evidence_id": "ev-2", "exact_text": "Exact supporting sentence 2."},
            ]
        },
    ],
)
def test_strict_verifier_rejects_unknown_fields_ids_and_invented_spans(
    mutation: Mapping[str, object],
) -> None:
    document, bundle = _case()
    del document
    payload = _valid_payload()
    payload.update(mutation)
    verifier = StructuredFactualSupportVerifier(
        lambda claim, evidence: payload,
        name="strict-fake",
        version="1",
        prompt_version="prompt-v1",
    )

    with pytest.raises(VerifierResponseError):
        verifier.verify(bundle.claims[0], bundle.evidence)
