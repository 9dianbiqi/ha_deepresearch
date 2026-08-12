"""Deterministic report-before-generation Evidence quality gate."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from typing import Any

from .intelligence import CoverageDecision, ResearchIntelligenceBundle
from .profiles import ResearchProfile


class EvidenceGateBlockedError(RuntimeError):
    """Raised by callers that require report generation to be explicitly allowed."""

    def __init__(self, decision: CoverageDecision) -> None:
        """Retain the deterministic decision for application-layer mapping."""
        self.decision = decision
        super().__init__("Evidence quality gate blocked report generation.")


class EvidenceQualityGate:
    """Evaluate schema-v2 Evidence without invoking an LLM or a provider."""

    def __init__(self, profile: ResearchProfile | None = None) -> None:
        """Optionally bind one profile for repeated evaluations."""
        self._profile = profile

    def evaluate(
        self,
        bundle: ResearchIntelligenceBundle,
        profile: ResearchProfile | None = None,
    ) -> CoverageDecision:
        """Return a deterministic CoverageDecision for one profile and bundle."""
        if not isinstance(bundle, ResearchIntelligenceBundle):
            raise TypeError("Evidence quality gate requires a v2 intelligence bundle.")
        profile = profile or self._profile
        if not isinstance(profile, ResearchProfile):
            raise TypeError("Evidence quality gate requires a ResearchProfile.")
        blockers: list[str] = []
        warnings: list[str] = []
        if bundle.mode is not profile.mode:
            blockers.append("mode_mismatch")
        if bundle.profile_id != profile.profile_id:
            blockers.append("profile_mismatch")
        if bundle.profile_version != profile.version:
            blockers.append("profile_version_mismatch")
        if len(bundle.evidence) > profile.retrieval_budget.max_evidence:
            blockers.append("evidence_budget")

        source_ids = {source.source_id for source in bundle.sources}
        evidence_ids = {evidence.evidence_id for evidence in bundle.evidence}
        evidence_by_dimension: dict[str, list[Any]] = defaultdict(list)
        source_by_dimension: dict[str, set[str]] = defaultdict(set)
        for evidence in bundle.evidence:
            if evidence.source.source_id not in source_ids:
                blockers.append(f"unknown_source:{evidence.evidence_id}")
            if not evidence.source.content_hash:
                blockers.append(f"missing_content_hash:{evidence.evidence_id}")
            if not evidence.source.captured_at:
                blockers.append(f"missing_capture_time:{evidence.evidence_id}")
            if profile.mode.value == "github" and not evidence.source.resolved_version:
                blockers.append(f"missing_version:{evidence.evidence_id}")
            if profile.citation_policy.require_locator and not evidence.locator.url:
                blockers.append(f"missing_locator:{evidence.evidence_id}")
            if len(evidence.excerpt) > profile.retrieval_budget.max_excerpt_chars:
                blockers.append(f"excerpt_budget:{evidence.evidence_id}")
            for claim in bundle.claims:
                if evidence.evidence_id in claim.evidence_ids + claim.conflicting_evidence_ids:
                    evidence_by_dimension[claim.dimension].append(evidence)
                    source_by_dimension[claim.dimension].add(evidence.source.source_id)

        for claim in bundle.claims:
            referenced = claim.evidence_ids + claim.conflicting_evidence_ids
            if any(item not in evidence_ids for item in referenced):
                blockers.append(f"unknown_evidence:{claim.claim_id}")
            if claim.reportable and not claim.evidence_ids:
                blockers.append(f"weak_claim:{claim.claim_id}")
            if claim.conflicting_evidence_ids:
                warnings.append(f"conflicting_claim:{claim.claim_id}")

        required = tuple(profile.coverage_policy.required_dimensions)
        dimension_results: list[dict[str, Any]] = []
        covered: list[str] = []
        missing: list[str] = []
        for dimension in required:
            dimension_evidence = evidence_by_dimension.get(dimension, [])
            evidence_count = len({item.evidence_id for item in dimension_evidence})
            independent_count = len(source_by_dimension.get(dimension, set()))
            dimension_blockers: list[str] = []
            if evidence_count < profile.coverage_policy.min_evidence_per_dimension:
                dimension_blockers.append("insufficient_evidence")
            if independent_count < profile.coverage_policy.min_independent_sources:
                dimension_blockers.append("insufficient_independent_sources")
            is_covered = not dimension_blockers
            if is_covered:
                covered.append(dimension)
            else:
                missing.append(dimension)
                blockers.append(f"missing_dimension:{dimension}")
            dimension_results.append(
                {
                    "dimension": dimension,
                    "covered": is_covered,
                    "evidence_count": evidence_count,
                    "independent_source_count": independent_count,
                    "blockers": dimension_blockers,
                }
            )

        score = len(covered) / len(required) if required else 1.0
        existing_missing = tuple(
            item for item in bundle.coverage.missing_dimensions if item not in missing
        )
        missing = list(dict.fromkeys([*missing, *existing_missing]))
        if score < profile.coverage_policy.min_coverage_score:
            blockers.append("coverage_score_below_threshold")
        blockers = list(dict.fromkeys(blockers))
        warnings = list(dict.fromkeys(warnings))
        gap_queries = tuple(
            bundle.coverage.gap_queries
            or tuple(f"{dimension} evidence" for dimension in missing)
        )[: profile.retrieval_budget.max_tasks]
        allow_report = not blockers and not missing and score >= profile.coverage_policy.min_coverage_score
        return replace(
            bundle.coverage,
            required_dimensions=required,
            covered_dimensions=tuple(covered),
            missing_dimensions=tuple(missing),
            weak_claims=tuple(
                dict.fromkeys(
                    [
                        *bundle.coverage.weak_claims,
                        *(
                            claim.claim_id
                            for claim in bundle.claims
                            if claim.reportable and not claim.evidence_ids
                        ),
                    ]
                )
            ),
            coverage_score=score,
            allow_report=allow_report,
            gap_queries=gap_queries,
            blockers=tuple(blockers),
            warnings=tuple(warnings),
            dimension_results=tuple(dimension_results),
        )


__all__ = ["EvidenceGateBlockedError", "EvidenceQualityGate"]
