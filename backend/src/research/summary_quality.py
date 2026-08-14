"""Three-dimensional quality gate for structured research summaries."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from .claim_verifier import (
    FactualSupportVerifier,
    FactualVerification,
    SemanticSupportScorer,
    UnavailableFactualSupportVerifier,
    UnavailableSemanticSupportScorer,
    VerifierResponseError,
    validate_factual_verification,
)
from .intelligence import (
    ClaimRecord,
    EvidenceLocator,
    EvidenceRecord,
    ResearchIntelligenceBundle,
)
from .report_document import (
    ClaimSupportAssessment,
    ParagraphQualityAssessment,
    StructuredSummaryDocument,
    SummaryParagraph,
    SummaryQualityAssessment,
)


@dataclass(frozen=True, kw_only=True)
class SummaryQualityWeights:
    """Normalized weights for semantic, factual, and citation quality."""

    semantic: float = 0.30
    factual: float = 0.45
    citation: float = 0.25

    def __post_init__(self) -> None:
        """Require finite non-negative weights summing to one."""
        values = (self.semantic, self.factual, self.citation)
        if any(
            not isinstance(item, (int, float))
            or isinstance(item, bool)
            or not math.isfinite(float(item))
            or float(item) < 0.0
            for item in values
        ):
            raise ValueError("Summary quality weights must be finite and non-negative.")
        if not math.isclose(sum(values), 1.0, abs_tol=1e-9):
            raise ValueError("Summary quality weights must sum to one.")


@dataclass(frozen=True, kw_only=True)
class SummaryQualityThresholds:
    """Thresholds applied independently before the aggregate threshold."""

    semantic: float = 0.72
    factual: float = 0.75
    citation: float = 0.85
    overall: float = 0.78

    def __post_init__(self) -> None:
        """Require every threshold to be a finite unit-interval value."""
        for field_name in ("semantic", "factual", "citation", "overall"):
            value = getattr(self, field_name)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                or not 0.0 <= float(value) <= 1.0
            ):
                raise ValueError(f"{field_name} threshold must be between zero and one.")

    def as_dict(self) -> dict[str, float]:
        """Return the contract-compatible threshold mapping."""
        return {
            "semantic_score": float(self.semantic),
            "factual_score": float(self.factual),
            "citation_score": float(self.citation),
            "overall_score": float(self.overall),
        }


class SummaryQualityGateV1:
    """Evaluate structured paragraphs against frozen, claim-bound Evidence."""

    def __init__(
        self,
        *,
        semantic_scorer: SemanticSupportScorer | None = None,
        factual_verifier: FactualSupportVerifier | None = None,
        weights: SummaryQualityWeights | None = None,
        thresholds: SummaryQualityThresholds | None = None,
    ) -> None:
        """Bind injectable scorers and deterministic quality policy."""
        self._semantic_scorer = semantic_scorer or UnavailableSemanticSupportScorer()
        self._factual_verifier = factual_verifier or UnavailableFactualSupportVerifier()
        self._weights = weights or SummaryQualityWeights()
        self._thresholds = thresholds or SummaryQualityThresholds()

    def evaluate(
        self,
        document: StructuredSummaryDocument,
        bundle: ResearchIntelligenceBundle,
    ) -> SummaryQualityAssessment:
        """Return a fail-closed, explainable assessment without mutating Evidence."""
        if not isinstance(document, StructuredSummaryDocument):
            raise TypeError("Summary quality gate requires a structured summary.")
        if not isinstance(bundle, ResearchIntelligenceBundle):
            raise TypeError("Summary quality gate requires an intelligence bundle.")

        evidence_by_id = {item.evidence_id: item for item in bundle.evidence}
        claim_by_id = {item.claim_id: item for item in bundle.claims}
        core_claim_ids = set(bundle.report_spec.claim_ids)
        if not core_claim_ids:
            core_claim_ids = {
                item.claim_id
                for item in bundle.claims
                if item.reportable and item.confidence.casefold() in {"high", "strong", "core"}
            }

        claim_assessments: dict[str, ClaimSupportAssessment] = {}
        verification_by_claim: dict[str, FactualVerification] = {}
        for claim_id in document.claim_ids:
            claim = claim_by_id.get(claim_id)
            if claim is None:
                continue
            assessment, verification = self._assess_claim(
                claim,
                evidence_by_id=evidence_by_id,
                is_core=claim_id in core_claim_ids,
            )
            claim_assessments[claim_id] = assessment
            verification_by_claim[claim_id] = verification

        paragraph_assessments = tuple(
            self._assess_paragraph(
                paragraph,
                bundle=bundle,
                evidence_by_id=evidence_by_id,
                claim_by_id=claim_by_id,
                claim_assessments=claim_assessments,
                verification_by_claim=verification_by_claim,
                core_claim_ids=core_claim_ids,
            )
            for paragraph in document.paragraphs
        )
        scorable = tuple(
            item
            for paragraph, item in zip(document.paragraphs, paragraph_assessments)
            if paragraph.paragraph_type != "limitation" and paragraph.claim_ids
        )
        overall_score = (
            sum(item.support_confidence for item in scorable) / len(scorable)
            if scorable
            else 0.0
        )
        passed = bool(scorable) and overall_score >= self._thresholds.overall
        passed = passed and all(not item.blockers for item in paragraph_assessments)
        return SummaryQualityAssessment(
            passed=passed,
            overall_score=overall_score,
            thresholds=self._thresholds.as_dict(),
            paragraph_assessments=paragraph_assessments,
            claim_assessments=tuple(
                claim_assessments[item]
                for item in document.claim_ids
                if item in claim_assessments
            ),
            verifier=self._factual_verifier.name,
            verifier_version=self._factual_verifier.version,
            prompt_version=self._factual_verifier.prompt_version,
        )

    def _assess_claim(
        self,
        claim: ClaimRecord,
        *,
        evidence_by_id: dict[str, EvidenceRecord],
        is_core: bool,
    ) -> tuple[ClaimSupportAssessment, FactualVerification]:
        """Evaluate semantic and factual support plus deterministic citations."""
        bound_ids = tuple(dict.fromkeys((*claim.evidence_ids, *claim.conflicting_evidence_ids)))
        bound_evidence = tuple(
            evidence_by_id[item] for item in bound_ids if item in evidence_by_id
        )
        reasons: list[str] = []
        semantic_score = self._safe_semantic_score(claim, bound_evidence, reasons)
        verification = self._safe_verify(claim, bound_evidence, reasons)
        reasons.extend(verification.reasons)
        citation_score, citation_reasons = self._citation_score(
            tuple(item.evidence_id for item in bound_evidence),
            claim=claim,
            evidence_by_id=evidence_by_id,
        )
        reasons.extend(citation_reasons)
        confidence = self._weighted_score(
            semantic=semantic_score,
            factual=verification.factual_score,
            citation=citation_score,
        )
        supporting_records = tuple(
            evidence_by_id[item]
            for item in claim.evidence_ids
            if item in evidence_by_id
        )
        if supporting_records and all(item.evidence_level == "metadata" for item in supporting_records):
            confidence = min(confidence, 0.60)
            reasons.append("confidence_cap:metadata_only")
        independent_sources = self._independent_source_count(supporting_records)
        if supporting_records and independent_sources <= 1:
            confidence = min(confidence, 0.75)
            reasons.append("confidence_cap:single_source")
        if claim.conflicting_evidence_ids or verification.conflicting_evidence_ids:
            confidence = min(confidence, 0.59)
            reasons.append("confidence_cap:unresolved_conflict")
        if is_core and verification.verdict == "unsupported":
            confidence = min(confidence, 0.49)
            reasons.append("confidence_cap:core_unsupported")
        if verification.verdict == "unverified":
            confidence = 0.0
            reasons.append("confidence_cap:unverified")
        return (
            ClaimSupportAssessment(
                claim_id=claim.claim_id,
                semantic_score=semantic_score,
                factual_score=verification.factual_score,
                citation_score=citation_score,
                support_confidence=confidence,
                verdict=verification.verdict,
                supporting_evidence_ids=verification.supporting_evidence_ids,
                conflicting_evidence_ids=verification.conflicting_evidence_ids,
                reasons=tuple(dict.fromkeys(reasons)),
            ),
            verification,
        )

    def _assess_paragraph(
        self,
        paragraph: SummaryParagraph,
        *,
        bundle: ResearchIntelligenceBundle,
        evidence_by_id: dict[str, EvidenceRecord],
        claim_by_id: dict[str, ClaimRecord],
        claim_assessments: dict[str, ClaimSupportAssessment],
        verification_by_claim: dict[str, FactualVerification],
        core_claim_ids: set[str],
    ) -> ParagraphQualityAssessment:
        """Project claim assessments onto one paragraph and enforce hard blockers."""
        blockers: list[str] = []
        warnings: list[str] = []
        if not bundle.evidence_frozen:
            blockers.append("evidence_not_frozen")
        if paragraph.paragraph_type == "limitation":
            if paragraph.citation_ids:
                warnings.append("limitation_has_citations")
            return ParagraphQualityAssessment(
                paragraph_id=paragraph.paragraph_id,
                semantic_score=0.0,
                factual_score=0.0,
                citation_score=0.0,
                support_confidence=0.0,
                level="unverified",
                blockers=tuple(blockers),
                warnings=tuple(warnings or ("not_scored:limitation",)),
            )
        if paragraph.paragraph_type == "factual" and not paragraph.claim_ids:
            blockers.append("factual_missing_claims")
        if paragraph.paragraph_type == "factual" and not paragraph.citation_ids:
            blockers.append("factual_missing_citations")

        claims: list[ClaimRecord] = []
        assessments: list[ClaimSupportAssessment] = []
        for claim_id in paragraph.claim_ids:
            claim = claim_by_id.get(claim_id)
            assessment = claim_assessments.get(claim_id)
            if claim is None or assessment is None:
                blockers.append(f"unknown_claim:{claim_id}")
                continue
            claims.append(claim)
            assessments.append(assessment)

        for evidence_id in paragraph.citation_ids:
            if evidence_id not in evidence_by_id:
                blockers.append(f"unknown_evidence:{evidence_id}")
                continue
            if not any(
                evidence_id in claim.evidence_ids + claim.conflicting_evidence_ids
                for claim in claims
            ):
                blockers.append(f"citation_not_bound:{evidence_id}")

        citation_scores: list[float] = []
        for claim in claims:
            citations = tuple(
                item
                for item in paragraph.citation_ids
                if item in claim.evidence_ids + claim.conflicting_evidence_ids
            )
            if not citations:
                blockers.append(f"claim_missing_citation:{claim.claim_id}")
            score, reasons = self._citation_score(
                citations,
                claim=claim,
                evidence_by_id=evidence_by_id,
            )
            citation_scores.append(score)
            warnings.extend(f"{claim.claim_id}:{item}" for item in reasons)
            verification = verification_by_claim.get(claim.claim_id)
            if (
                claim.claim_id in core_claim_ids
                and verification is not None
                and verification.verdict == "contradicted"
            ):
                blockers.append(f"core_claim_contradicted:{claim.claim_id}")
            supporting = tuple(
                evidence_by_id[item]
                for item in claim.evidence_ids
                if item in evidence_by_id
            )
            is_strong = claim.claim_id in core_claim_ids or claim.confidence.casefold() in {
                "high",
                "strong",
                "core",
            }
            if is_strong and supporting and all(
                item.evidence_level in {"metadata", "derived"} for item in supporting
            ):
                blockers.append(f"strong_claim_low_grade_only:{claim.claim_id}")

        semantic_score = self._average(item.semantic_score for item in assessments)
        factual_score = self._average(item.factual_score for item in assessments)
        citation_score = self._average(citation_scores)
        confidence = self._weighted_score(
            semantic=semantic_score,
            factual=factual_score,
            citation=citation_score,
        )
        if assessments:
            confidence = min(
                confidence,
                min(item.support_confidence for item in assessments),
            )
        if paragraph.claim_ids:
            if semantic_score < self._thresholds.semantic:
                blockers.append("semantic_score_below_threshold")
            if factual_score < self._thresholds.factual:
                blockers.append("factual_score_below_threshold")
            if citation_score < self._thresholds.citation:
                blockers.append("citation_score_below_threshold")
        level = self._confidence_level(confidence, assessments)
        return ParagraphQualityAssessment(
            paragraph_id=paragraph.paragraph_id,
            semantic_score=semantic_score,
            factual_score=factual_score,
            citation_score=citation_score,
            support_confidence=confidence,
            level=level,
            blockers=tuple(dict.fromkeys(blockers)),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    def _safe_semantic_score(
        self,
        claim: ClaimRecord,
        evidence: tuple[EvidenceRecord, ...],
        reasons: list[str],
    ) -> float:
        """Convert scorer failure or invalid output to a zero score."""
        try:
            score = self._semantic_scorer.score(claim, evidence)
            if (
                not isinstance(score, (int, float))
                or isinstance(score, bool)
                or not math.isfinite(float(score))
                or not 0.0 <= float(score) <= 1.0
            ):
                raise ValueError("Semantic score is outside the unit interval.")
            return float(score)
        except (TimeoutError, RuntimeError, TypeError, ValueError) as exc:
            reasons.append(f"semantic_scorer_unavailable:{type(exc).__name__}")
            return 0.0

    def _safe_verify(
        self,
        claim: ClaimRecord,
        evidence: tuple[EvidenceRecord, ...],
        reasons: list[str],
    ) -> FactualVerification:
        """Convert timeout, unavailability, or malformed output to unverified."""
        try:
            result = self._factual_verifier.verify(claim, evidence)
            if not isinstance(result, FactualVerification):
                raise VerifierResponseError("Verifier returned an invalid result type.")
            validate_factual_verification(result, claim=claim, evidence=evidence)
            return result
        except (TimeoutError, RuntimeError, TypeError, ValueError) as exc:
            reason = f"factual_verifier_unverified:{type(exc).__name__}"
            reasons.append(reason)
            return FactualVerification.unverified(reason)

    def _citation_score(
        self,
        citation_ids: tuple[str, ...],
        *,
        claim: ClaimRecord,
        evidence_by_id: dict[str, EvidenceRecord],
    ) -> tuple[float, tuple[str, ...]]:
        """Compute deterministic citation integrity, precision, and provenance."""
        reasons: list[str] = []
        if not citation_ids:
            return 0.0, ("missing_citation",)
        known_records: list[EvidenceRecord] = []
        bound_ids = set(claim.evidence_ids + claim.conflicting_evidence_ids)
        for evidence_id in citation_ids:
            record = evidence_by_id.get(evidence_id)
            if record is None:
                reasons.append(f"unknown_evidence:{evidence_id}")
                continue
            if evidence_id not in bound_ids:
                reasons.append(f"citation_not_bound:{evidence_id}")
                continue
            known_records.append(record)
        if len(known_records) != len(citation_ids):
            return 0.0, tuple(dict.fromkeys(reasons))
        precision = self._average(
            1.0 if self._is_precise_locator(item.locator) else 0.0
            for item in known_records
        )
        if precision < 1.0:
            reasons.append("imprecise_locator")
        level_scores = {
            "full_text": 1.0,
            "abstract": 0.80,
            "metadata": 0.55,
            "derived": 0.45,
        }
        evidence_quality = self._average(
            level_scores[item.evidence_level] for item in known_records
        )
        if any(item.evidence_level == "metadata" for item in known_records):
            reasons.append("metadata_evidence")
        if any(item.evidence_level == "derived" for item in known_records):
            reasons.append("derived_evidence")
        source_factor = 1.0
        if self._independent_source_count(tuple(known_records)) <= 1:
            source_factor = 0.85
            reasons.append("single_independent_source")
        conflict_factor = 1.0
        if claim.conflicting_evidence_ids:
            conflict_factor = 0.70
            reasons.append("unresolved_conflict")
        score = (0.60 + 0.40 * precision) * evidence_quality * source_factor * conflict_factor
        return max(0.0, min(1.0, score)), tuple(dict.fromkeys(reasons))

    def _weighted_score(self, *, semantic: float, factual: float, citation: float) -> float:
        """Combine the three dimensions using the configured policy weights."""
        return (
            semantic * self._weights.semantic
            + factual * self._weights.factual
            + citation * self._weights.citation
        )

    @staticmethod
    def _is_precise_locator(locator: EvidenceLocator) -> bool:
        """Recognize paragraph, fragment, line, or page-addressable locations."""
        return bool(
            locator.paragraph
            or locator.fragment
            or (locator.line_start is not None and locator.line_end is not None)
            or (locator.page_start is not None and locator.page_end is not None)
        )

    @staticmethod
    def _independent_source_count(evidence: tuple[EvidenceRecord, ...]) -> int:
        """Deduplicate sources by canonical URL before falling back to source ID."""
        identities = {
            item.source.canonical_url.rstrip("/").casefold() or item.source.source_id
            for item in evidence
        }
        return len(identities)

    @staticmethod
    def _average(values: Iterable[float]) -> float:
        """Return an average for any finite iterable, or zero for no values."""
        materialized = tuple(values)
        return sum(materialized) / len(materialized) if materialized else 0.0

    def _confidence_level(
        self,
        confidence: float,
        assessments: list[ClaimSupportAssessment],
    ) -> str:
        """Map support confidence to a user-facing level without optimism."""
        if not assessments or any(item.verdict == "unverified" for item in assessments):
            return "unverified"
        if confidence >= self._thresholds.overall:
            return "high"
        if confidence >= 0.60:
            return "medium"
        return "low"


__all__ = [
    "SummaryQualityGateV1",
    "SummaryQualityThresholds",
    "SummaryQualityWeights",
]
