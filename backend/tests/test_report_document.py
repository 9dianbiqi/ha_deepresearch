"""Structured summary and evidence-quality contract tests."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from research.compatibility import GitHubEvidenceV1Adapter
from research.intelligence import ResearchIntelligenceBundle
from research.report_document import (
    ClaimSupportAssessment,
    ParagraphQualityAssessment,
    StructuredSummaryDocument,
    SummaryParagraph,
    SummaryQualityAssessment,
    stable_paragraph_id,
)

SHARED_FIXTURE_PATH = (
    Path(__file__).parents[2] / "shared" / "fixtures" / "research_quality_v1.json"
)
LEGACY_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "github_evidence_v1.json"


def shared_fixture() -> dict[str, Any]:
    """Load the front-end/back-end shared summary fixture."""
    return json.loads(SHARED_FIXTURE_PATH.read_text(encoding="utf-8"))


def claim_assessment(**overrides: object) -> ClaimSupportAssessment:
    """Build one valid claim assessment with selected overrides."""
    values: dict[str, object] = {
        "claim_id": "claim_streaming",
        "semantic_score": 0.93,
        "factual_score": 0.91,
        "citation_score": 1.0,
        "support_confidence": 0.93,
        "verdict": "supported",
        "supporting_evidence_ids": ("ev_streaming_route",),
    }
    values.update(overrides)
    return ClaimSupportAssessment(**values)  # type: ignore[arg-type]


def test_shared_fixture_round_trips_as_detached_json() -> None:
    """Both versioned contracts round-trip through plain JSON."""
    fixture = shared_fixture()
    document = StructuredSummaryDocument.from_dict(fixture["structured_summary"])
    assessment = SummaryQualityAssessment.from_dict(
        fixture["quality_assessment"]
    )

    detached = json.loads(
        json.dumps(
            {
                "structured_summary": document.as_dict(),
                "quality_assessment": assessment.as_dict(),
            },
            ensure_ascii=False,
        )
    )

    assert detached == fixture
    assert StructuredSummaryDocument.from_dict(detached["structured_summary"]) == document
    assert SummaryQualityAssessment.from_dict(detached["quality_assessment"]) == assessment


def test_paragraph_id_is_stable_and_content_derived() -> None:
    """Formatting noise and claim order do not alter paragraph identity."""
    first = stable_paragraph_id(
        section_id="Architecture",
        text="The service streams research progress.",
        claim_ids=("claim_b", "claim_a"),
    )
    second = stable_paragraph_id(
        section_id=" architecture ",
        text="  The service   streams research progress.  ",
        claim_ids=("claim_a", "claim_b"),
    )

    assert first == second
    paragraph = SummaryParagraph(
        section_id="Architecture",
        paragraph_type="factual",
        text="The service streams research progress.",
        claim_ids=("claim_b", "claim_a"),
    )
    assert paragraph.paragraph_id == first
    with pytest.raises(ValueError, match="paragraph_id"):
        SummaryParagraph(
            paragraph_id="para_untrusted",
            section_id="Architecture",
            paragraph_type="factual",
            text="The service streams research progress.",
            claim_ids=("claim_b", "claim_a"),
        )


@pytest.mark.parametrize("paragraph_type", ["fact", "", "FAcTuAl"])
def test_paragraph_rejects_unknown_types(paragraph_type: str) -> None:
    """Paragraph kinds use the frozen cross-client vocabulary."""
    with pytest.raises(ValueError, match="paragraph_type"):
        SummaryParagraph(
            section_id="overview",
            paragraph_type=paragraph_type,
            text="A paragraph.",
        )


@pytest.mark.parametrize(
    ("field_name", "value", "error_type"),
    [
        ("semantic_score", -0.01, ValueError),
        ("factual_score", 1.01, ValueError),
        ("citation_score", float("nan"), ValueError),
        ("support_confidence", float("inf"), ValueError),
        ("semantic_score", True, TypeError),
    ],
)
def test_claim_assessment_rejects_invalid_scores(
    field_name: str,
    value: object,
    error_type: type[Exception],
) -> None:
    """Every score is numeric, finite, and within the unit interval."""
    with pytest.raises(error_type):
        claim_assessment(**{field_name: value})


def test_assessment_vocabularies_and_thresholds_are_validated() -> None:
    """Unknown verdicts, levels, and out-of-range thresholds fail closed."""
    with pytest.raises(ValueError, match="verdict"):
        claim_assessment(verdict="likely")
    with pytest.raises(ValueError, match="level"):
        ParagraphQualityAssessment(
            paragraph_id="para_example",
            semantic_score=0.8,
            factual_score=0.8,
            citation_score=0.9,
            support_confidence=0.8,
            level="certain",
        )
    with pytest.raises(ValueError, match="threshold"):
        SummaryQualityAssessment(
            passed=False,
            overall_score=0.4,
            thresholds={"overall_score": 1.1},
            verifier="fake",
            verifier_version="1",
            prompt_version="v1",
        )


def test_contracts_are_immutable_including_threshold_mapping() -> None:
    """Frozen objects do not expose mutable aggregate configuration."""
    assessment = SummaryQualityAssessment(
        passed=True,
        overall_score=0.9,
        thresholds={"overall_score": 0.78},
        verifier="fake",
        verifier_version="1",
        prompt_version="v1",
    )

    with pytest.raises(FrozenInstanceError):
        assessment.overall_score = 0.1  # type: ignore[misc]
    with pytest.raises(TypeError):
        assessment.thresholds["overall_score"] = 0.1  # type: ignore[index]


def test_document_rejects_unknown_claim_and_duplicate_paragraph() -> None:
    """Document references and deterministic paragraph IDs remain coherent."""
    paragraph = SummaryParagraph(
        section_id="overview",
        paragraph_type="factual",
        text="A supported fact.",
        claim_ids=("claim_1",),
        citation_ids=("ev_1",),
    )
    with pytest.raises(ValueError, match="unknown"):
        StructuredSummaryDocument(
            task_id="task_1",
            paragraphs=(paragraph,),
            claim_ids=(),
        )
    with pytest.raises(ValueError, match="unique"):
        StructuredSummaryDocument(
            task_id="task_1",
            paragraphs=(paragraph, paragraph),
            claim_ids=("claim_1",),
        )


def test_unknown_contract_schema_versions_are_rejected() -> None:
    """Version negotiation remains explicit for documents and assessments."""
    fixture = shared_fixture()
    document = dict(fixture["structured_summary"])
    document["schema_version"] = 99
    with pytest.raises(ValueError, match="schema"):
        StructuredSummaryDocument.from_dict(document)

    assessment = dict(fixture["quality_assessment"])
    assessment["schema_version"] = 99
    with pytest.raises(ValueError, match="schema"):
        SummaryQualityAssessment.from_dict(assessment)

    document.pop("schema_version")
    with pytest.raises(ValueError, match="schema_version"):
        StructuredSummaryDocument.from_dict(document)


def test_schema_v2_bundle_without_new_artifact_fields_still_reads() -> None:
    """Existing Runs require no structured-summary migration."""
    legacy_fixture = json.loads(LEGACY_FIXTURE_PATH.read_text(encoding="utf-8"))
    current_payload = GitHubEvidenceV1Adapter.to_v2(
        legacy_fixture["single_bundle"]
    ).as_dict()

    assert "structured_summary" not in current_payload
    assert "quality_assessment" not in current_payload
    restored = ResearchIntelligenceBundle.from_dict(current_payload)
    assert restored.as_dict() == current_payload
