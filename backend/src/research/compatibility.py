"""Compatibility adapters between the GitHub v1 payload and intelligence v2."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from .evidence import (
    Artifact,
    EvidenceItem,
    GitHubEvidenceBundle,
    RepositorySnapshot,
    ResearchClaim,
    github_evidence_bundle_from_dict,
)
from .intelligence import (
    ArtifactDescriptorV2,
    ArtifactManifestV2,
    ClaimRecord,
    CoverageDecision,
    EvidenceLocator,
    EvidenceRecord,
    GenericReportSpec,
    ResearchIntelligenceBundle,
    SourceReference,
    stable_evidence_id,
)
from .profiles import ResearchMode

_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_REPOSITORY_RE = re.compile(r"github\.com/([^/]+/[^/#?]+)")


def _sha(value: object) -> str | None:
    """Return a normalized commit SHA when the value is usable in a URL."""
    text = str(value or "").strip()
    return text.lower() if _SHA_RE.fullmatch(text) else None


def _json_hash(value: object) -> str:
    """Hash bounded source metadata without persisting a second full body."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _repository_from_url(url: str) -> str | None:
    """Extract an owner/repository pair from a GitHub URL."""
    match = _REPOSITORY_RE.search(url)
    if match is None:
        return None
    return match.group(1).rstrip("/.")


def _base_repository_url(source: SourceReference) -> str:
    """Return a canonical GitHub repository URL for URL construction."""
    return source.canonical_url.rstrip("/").split("/commit/", 1)[0].split("/blob/", 1)[0]


def _github_file_url(source: SourceReference, *, path: str, sha: str | None) -> str:
    """Construct a commit-pinned GitHub file URL when a SHA is present."""
    if sha:
        return f"{_base_repository_url(source)}/blob/{sha}/{quote(path.lstrip('/'), safe='/')}"
    return f"{_base_repository_url(source)}/{quote(path.lstrip('/'), safe='/')}"


def _line_locator(source: SourceReference, item: EvidenceItem) -> EvidenceLocator | None:
    """Build a strict line locator or return None when v1 lacks a safe address."""
    path = str(item.file_path or "").strip()
    start = item.line_start
    end = item.line_end
    sha = _sha(item.commit_sha) or _sha(source.resolved_version)
    if not path or start is None or end is None or start < 1 or end < start or not sha:
        return None
    url = f"{_github_file_url(source, path=path, sha=sha)}#L{start}-L{end}"
    return EvidenceLocator(
        locator_type="line",
        url=url,
        file_path=path,
        line_start=start,
        line_end=end,
    )


def _locator_for_item(source: SourceReference, item: EvidenceItem) -> EvidenceLocator:
    """Map v1 URL and file metadata to a provider-neutral locator."""
    line_locator = _line_locator(source, item)
    if line_locator is not None:
        return line_locator
    path = str(item.file_path or "").strip() or None
    if path:
        return EvidenceLocator(
            locator_type="file",
            url=_github_file_url(source, path=path, sha=_sha(item.commit_sha) or _sha(source.resolved_version)),
            file_path=path,
            fragment="line-range-unavailable" if item.line_start is not None else None,
        )
    url = str(item.source_url or "").strip() or source.canonical_url
    evidence_type = item.evidence_type.strip().lower()
    locator_type = "metadata" if "metadata" in evidence_type else evidence_type or "source"
    return EvidenceLocator(locator_type=locator_type, url=url)


def _source_for_snapshot(snapshot: Any) -> SourceReference:
    """Project one v1 repository snapshot into a canonical source reference."""
    repository = str(snapshot.repository).strip()
    canonical_url = str(snapshot.repository_url or f"https://github.com/{repository}").strip()
    metadata = {
        "metadata": dict(snapshot.metadata),
        "languages": dict(snapshot.languages),
        "file_manifest": [dict(item) for item in snapshot.file_manifest],
        "contributors": [dict(item) for item in snapshot.contributors],
        "releases": [dict(item) for item in snapshot.releases],
        "collection_status": snapshot.collection_status,
        "notice_codes": list(snapshot.notice_codes),
    }
    return SourceReference(
        provider_id="github",
        source_kind="repository",
        source_id=repository,
        canonical_url=canonical_url,
        requested_ref=snapshot.requested_ref,
        resolved_version=_sha(snapshot.commit_sha),
        captured_at=snapshot.collected_at,
        content_hash=_json_hash(metadata),
    )


def _fallback_source(item: EvidenceItem) -> SourceReference:
    """Create a bounded source for a legacy evidence item with no snapshot."""
    repository = _repository_from_url(item.source_url) or f"legacy/{item.snapshot_id}"
    return SourceReference(
        provider_id="github",
        source_kind="repository",
        source_id=repository,
        canonical_url=f"https://github.com/{repository}" if "/" in repository else item.source_url,
        requested_ref=None,
        resolved_version=_sha(item.commit_sha),
        captured_at=item.captured_at,
        content_hash=item.content_hash or _json_hash(item.excerpt),
    )


def _evidence_level(item: EvidenceItem) -> str:
    """Map GitHub v1 evidence types to the v2 evidence trust level."""
    evidence_type = item.evidence_type.lower()
    if evidence_type in {"source_code", "source_file", "readme"}:
        return "full_text"
    if evidence_type in {"repository_metadata", "commit", "issue", "pull_request", "release"}:
        return "metadata"
    return "derived"


def _snapshot_attributes(snapshot: Any) -> dict[str, Any]:
    """Retain provider-specific collection metadata in bounded evidence attrs."""
    return {
        "snapshot_id": snapshot.snapshot_id,
        "collection_status": snapshot.collection_status,
        "notice_codes": list(snapshot.notice_codes),
        "notices": list(snapshot.notices),
        "metadata": dict(snapshot.metadata),
        "languages": dict(snapshot.languages),
    }


def _map_evidence(
    item: EvidenceItem,
    *,
    source: SourceReference,
    snapshot: Any | None,
) -> EvidenceRecord:
    """Map one legacy evidence item while retaining unmapped details."""
    locator = _locator_for_item(source, item)
    attributes: dict[str, Any] = {
        "legacy_evidence_id": item.evidence_id,
        "legacy_snapshot_id": item.snapshot_id,
        "commit_sha": item.commit_sha,
        "content_hash": item.content_hash,
        "legacy_source_url": item.source_url,
    }
    if snapshot is not None:
        attributes.update(_snapshot_attributes(snapshot))
    if item.file_path:
        attributes["file_path"] = item.file_path
    if item.line_start is not None:
        attributes["line_start"] = item.line_start
    if item.line_end is not None:
        attributes["line_end"] = item.line_end
    evidence_id = stable_evidence_id(source, locator=locator, excerpt=item.excerpt)
    return EvidenceRecord(
        evidence_id=evidence_id,
        source=source,
        evidence_type=item.evidence_type,
        evidence_level=_evidence_level(item),
        title=item.title or item.evidence_type,
        excerpt=item.excerpt,
        locator=locator,
        attributes=attributes,
    )


def _map_claim(
    claim: ResearchClaim,
    *,
    evidence_ids: Mapping[str, str],
) -> ClaimRecord:
    """Map one legacy claim and translate all evidence references."""
    bound = tuple(dict.fromkeys(evidence_ids[item] for item in claim.evidence_ids if item in evidence_ids))
    conflicting = tuple(
        dict.fromkeys(evidence_ids[item] for item in claim.conflicting_evidence_ids if item in evidence_ids)
    )
    return ClaimRecord(
        claim_id=claim.claim_id,
        dimension=claim.category,
        statement=claim.statement,
        confidence=claim.confidence,
        evidence_ids=bound,
        conflicting_evidence_ids=conflicting,
        limitations=claim.limitations,
        reportable=bool(bound),
    )


def _map_coverage(bundle: GitHubEvidenceBundle, claims: tuple[ClaimRecord, ...]) -> CoverageDecision:
    """Map v1 coverage and add deterministic dimension diagnostics."""
    claim_counts: dict[str, int] = {}
    for claim in claims:
        if claim.evidence_ids:
            claim_counts[claim.dimension] = claim_counts.get(claim.dimension, 0) + 1
    dimensions = tuple(
        {
            "dimension": dimension,
            "covered": dimension in bundle.coverage.covered_dimensions,
            "claim_count": claim_counts.get(dimension, 0),
        }
        for dimension in bundle.coverage.required_dimensions
    )
    warnings = tuple(
        notice
        for snapshot in bundle.snapshots
        for notice in snapshot.notice_codes
    )
    blockers = tuple(
        f"missing_dimension:{dimension}"
        for dimension in bundle.coverage.missing_dimensions
    )
    return CoverageDecision(
        required_dimensions=bundle.coverage.required_dimensions,
        covered_dimensions=bundle.coverage.covered_dimensions,
        missing_dimensions=bundle.coverage.missing_dimensions,
        weak_claims=tuple(
            claim.claim_id for claim in claims if not claim.evidence_ids
        ) or bundle.coverage.weak_claims,
        conflicting_claims=bundle.coverage.conflicting_claims,
        coverage_score=bundle.coverage.coverage_score,
        allow_report=bundle.coverage.allow_report,
        gap_queries=bundle.coverage.gap_queries,
        retry_count=bundle.coverage.retry_count,
        blockers=blockers,
        warnings=warnings,
        dimension_results=dimensions,
    )


def _map_artifact(
    artifact: Artifact,
    *,
    evidence_ids: Mapping[str, str],
) -> ArtifactDescriptorV2:
    """Convert inline v1 content into a descriptor with size and checksum only."""
    content_bytes = artifact.content.encode("utf-8")
    checksum = artifact.checksum or hashlib.sha256(content_bytes).hexdigest()
    path = artifact.path or f"artifacts/{artifact.artifact_id}"
    return ArtifactDescriptorV2(
        artifact_id=artifact.artifact_id,
        artifact_type=artifact.artifact_type,
        mime_type=artifact.mime_type,
        path=path,
        title=artifact.title or artifact.artifact_type,
        description=artifact.description,
        source_ids=tuple(
            dict.fromkeys(evidence_ids[item] for item in artifact.source_ids if item in evidence_ids)
        ),
        size_bytes=len(content_bytes),
        checksum=checksum,
    )


class GitHubEvidenceV1Adapter:
    """Translate a persisted GitHub schema-v1 bundle into schema-v2."""

    @staticmethod
    def to_v2(value: Mapping[str, Any] | GitHubEvidenceBundle) -> ResearchIntelligenceBundle:
        """Convert v1 input without mutating the original Run payload."""
        if isinstance(value, GitHubEvidenceBundle):
            bundle = value
        elif isinstance(value, Mapping):
            if value.get("schema_version", 1) != 1:
                raise ValueError("GitHub v1 adapter requires schema_version=1.")
            parsed_bundle = github_evidence_bundle_from_dict(value)
            if parsed_bundle is None:
                raise ValueError("Invalid GitHub schema-v1 bundle.")
            bundle = parsed_bundle
        else:
            raise TypeError("GitHub v1 adapter input must be a mapping or bundle.")

        sources: list[SourceReference] = []
        source_by_snapshot: dict[str, SourceReference] = {}
        snapshot_by_id = {snapshot.snapshot_id: snapshot for snapshot in bundle.snapshots}
        for snapshot in bundle.snapshots:
            source = _source_for_snapshot(snapshot)
            if source.source_id in {item.source_id for item in sources}:
                suffix = source.resolved_version or snapshot.snapshot_id
                source = SourceReference(
                    provider_id=source.provider_id,
                    source_kind=source.source_kind,
                    source_id=f"{source.source_id}@{suffix}",
                    canonical_url=source.canonical_url,
                    requested_ref=source.requested_ref,
                    resolved_version=source.resolved_version,
                    captured_at=source.captured_at,
                    content_hash=source.content_hash,
                )
            sources.append(source)
            source_by_snapshot[snapshot.snapshot_id] = source

        records: list[EvidenceRecord] = []
        old_to_new: dict[str, str] = {}
        seen_ids: set[str] = set()
        for item in bundle.evidence:
            source_candidate: SourceReference | None = source_by_snapshot.get(item.snapshot_id)
            snapshot_candidate: RepositorySnapshot | None = snapshot_by_id.get(item.snapshot_id)
            if source_candidate is None:
                source_candidate = _fallback_source(item)
                if source_candidate.source_id not in {entry.source_id for entry in sources}:
                    sources.append(source_candidate)
                source_by_snapshot[item.snapshot_id] = source_candidate
            assert source_candidate is not None
            record = _map_evidence(
                item,
                source=source_candidate,
                snapshot=snapshot_candidate,
            )
            old_to_new[item.evidence_id] = record.evidence_id
            if record.evidence_id not in seen_ids:
                records.append(record)
                seen_ids.add(record.evidence_id)

        claims = tuple(_map_claim(item, evidence_ids=old_to_new) for item in bundle.claims)
        coverage = _map_coverage(bundle, claims)
        report_claim_ids = tuple(item.claim_id for item in claims if item.claim_id in {claim.claim_id for claim in claims})
        report_citation_ids = tuple(
            dict.fromkeys(old_to_new[item] for item in bundle.report_spec.citation_ids if item in old_to_new)
        )
        report_spec = GenericReportSpec(
            title=bundle.report_spec.title,
            executive_summary=bundle.report_spec.executive_summary,
            sections=bundle.report_spec.sections,
            claim_ids=report_claim_ids,
            citation_ids=report_citation_ids,
            tables=bundle.report_spec.tables,
            charts=bundle.report_spec.charts,
            diagrams=bundle.report_spec.diagrams,
            limitations=bundle.report_spec.limitations,
        )
        manifest = ArtifactManifestV2(
            artifacts=tuple(
                _map_artifact(artifact, evidence_ids=old_to_new)
                for artifact in bundle.artifacts
            )
        )
        return ResearchIntelligenceBundle(
            schema_version=2,
            mode=ResearchMode.GITHUB,
            profile_id="github.repository.v1",
            profile_version=1,
            sources=tuple(sources),
            evidence=tuple(records),
            claims=claims,
            coverage=coverage,
            report_spec=report_spec,
            artifact_manifest=manifest,
            evidence_frozen=bundle.evidence_frozen,
        )


__all__ = ["GitHubEvidenceV1Adapter"]
