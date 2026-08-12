"""Provider-neutral Evidence, Claim, Coverage, and artifact schema-v2 contracts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .profiles import ResearchMode

INTELLIGENCE_SCHEMA_VERSION = 2
_MAX_EXCERPT_CHARS = 12_000
_SHA_RE = re.compile(r"[0-9a-fA-F]{7,64}")
_GITHUB_LINE_URL_RE = re.compile(
    r"^https://github\.com/[^/\s]+/[^/\s]+/blob/"
    r"(?P<sha>[0-9a-fA-F]{7,64})/.+#L(?P<start>[1-9][0-9]*)-L(?P<end>[1-9][0-9]*)$"
)
_EVIDENCE_LEVELS = frozenset({"metadata", "abstract", "full_text", "derived"})


def _now_iso() -> str:
    """Return a timezone-aware capture timestamp."""
    return datetime.now(timezone.utc).isoformat()


def _text(value: object, *, field_name: str, limit: int = 4096) -> str:
    """Normalize one required bounded text field."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be empty.")
    normalized = value.strip()
    if len(normalized) > limit:
        raise ValueError(f"{field_name} exceeds its bounded length.")
    return normalized


def _optional_text(value: object, *, limit: int = 4096) -> str | None:
    """Normalize an optional bounded text field."""
    if value is None:
        return None
    return _text(value, field_name="Text", limit=limit)


def _string_tuple(value: object) -> tuple[str, ...]:
    """Detach string lists from untrusted JSON."""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def _mapping_tuple(value: object) -> tuple[Mapping[str, Any], ...]:
    """Detach mapping lists from untrusted JSON."""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(dict(item) for item in value if isinstance(item, Mapping))


def _json_copy(value: object) -> object:
    """Return a detached JSON-compatible copy or reject non-JSON values."""
    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("Intelligence attributes must be JSON serializable.") from exc


def _digest(parts: Sequence[object]) -> str:
    """Build a deterministic short digest from normalized public inputs."""
    encoded = json.dumps(list(parts), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


@dataclass(frozen=True, kw_only=True)
class SourceReference:
    """One canonical provider source observation."""

    provider_id: str
    source_kind: str
    source_id: str
    canonical_url: str
    requested_ref: str | None = None
    resolved_version: str | None = None
    captured_at: str = field(default_factory=_now_iso)
    content_hash: str

    def __post_init__(self) -> None:
        """Validate source identity, version, timestamp, and content hash."""
        for field_name in ("provider_id", "source_kind", "source_id", "canonical_url", "content_hash"):
            object.__setattr__(
                self,
                field_name,
                _text(getattr(self, field_name), field_name=field_name),
            )
        for field_name in ("requested_ref", "resolved_version"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _text(value, field_name=field_name))
        _text(self.captured_at, field_name="captured_at", limit=128)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible source reference."""
        return {
            "provider_id": self.provider_id,
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "canonical_url": self.canonical_url,
            "requested_ref": self.requested_ref,
            "resolved_version": self.resolved_version,
            "captured_at": self.captured_at,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SourceReference:
        """Restore one source reference from JSON."""
        return cls(**dict(value))


@dataclass(frozen=True, kw_only=True)
class EvidenceLocator:
    """Source-specific locator such as a file line range or paper page."""

    locator_type: str
    url: str
    file_path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    page_start: int | None = None
    page_end: int | None = None
    section: str | None = None
    paragraph: str | None = None
    fragment: str | None = None

    def __post_init__(self) -> None:
        """Validate locator pairing and GitHub line-addressability."""
        object.__setattr__(self, "locator_type", _text(self.locator_type, field_name="locator_type", limit=64))
        object.__setattr__(self, "url", _text(self.url, field_name="locator url", limit=8192))
        self._validate_range(self.line_start, self.line_end, "line")
        self._validate_range(self.page_start, self.page_end, "page")
        if self.locator_type == "line":
            if not self.file_path:
                raise ValueError("line locator requires file_path.")
            if self.line_start is None or self.line_end is None:
                raise ValueError("line locator requires line_start and line_end.")
            match = _GITHUB_LINE_URL_RE.fullmatch(self.url)
            if match is None:
                raise ValueError("line locator URL must be commit-pinned with #Lx-Ly.")
            if int(match.group("start")) != self.line_start or int(match.group("end")) != self.line_end:
                raise ValueError("line locator URL range does not match line fields.")
            if _SHA_RE.fullmatch(match.group("sha")) is None:
                raise ValueError("line locator URL must contain a valid commit SHA.")
        for field_name in ("file_path", "section", "paragraph", "fragment"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _text(value, field_name=field_name, limit=1024))

    @staticmethod
    def _validate_range(start: int | None, end: int | None, label: str) -> None:
        """Validate one paired positive ordered range."""
        if (start is None) != (end is None):
            raise ValueError(f"{label} range must provide both start and end.")
        if start is not None and (
            not isinstance(start, int)
            or isinstance(start, bool)
            or start < 1
            or end is None
            or not isinstance(end, int)
            or isinstance(end, bool)
            or end < start
        ):
            raise ValueError(f"{label} range must be positive and ordered.")

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible locator."""
        return {
            "locator_type": self.locator_type,
            "url": self.url,
            "file_path": self.file_path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "section": self.section,
            "paragraph": self.paragraph,
            "fragment": self.fragment,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EvidenceLocator:
        """Restore one locator from JSON."""
        return cls(**dict(value))


@dataclass(frozen=True, kw_only=True)
class EvidenceRecord:
    """One bounded, source-addressable observation."""

    evidence_id: str
    source: SourceReference
    evidence_type: str
    evidence_level: str
    title: str
    excerpt: str
    locator: EvidenceLocator
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate identity, evidence level, excerpt, and JSON attributes."""
        if not isinstance(self.source, SourceReference):
            raise TypeError("Evidence source must be a SourceReference.")
        if not isinstance(self.locator, EvidenceLocator):
            raise TypeError("Evidence locator must be an EvidenceLocator.")
        for field_name in ("evidence_id", "evidence_type", "title"):
            object.__setattr__(self, field_name, _text(getattr(self, field_name), field_name=field_name, limit=512))
        object.__setattr__(self, "evidence_level", _text(self.evidence_level, field_name="evidence_level", limit=32))
        if self.evidence_level not in _EVIDENCE_LEVELS:
            raise ValueError("Evidence level is unsupported.")
        if not isinstance(self.excerpt, str):
            raise TypeError("Evidence excerpt must be text.")
        excerpt = self.excerpt.strip()
        if len(excerpt) > _MAX_EXCERPT_CHARS:
            raise ValueError("Evidence excerpt exceeds its bound.")
        object.__setattr__(self, "excerpt", excerpt)
        if not isinstance(self.attributes, Mapping):
            raise TypeError("Evidence attributes must be a mapping.")
        object.__setattr__(self, "attributes", _json_copy(dict(self.attributes)))

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible evidence record."""
        return {
            "evidence_id": self.evidence_id,
            "source": self.source.as_dict(),
            "evidence_type": self.evidence_type,
            "evidence_level": self.evidence_level,
            "title": self.title,
            "excerpt": self.excerpt,
            "locator": self.locator.as_dict(),
            "attributes": dict(self.attributes),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EvidenceRecord:
        """Restore one evidence record from JSON."""
        raw = dict(value)
        raw["source"] = SourceReference.from_dict(raw.get("source", {}))
        raw["locator"] = EvidenceLocator.from_dict(raw.get("locator", {}))
        return cls(**raw)


@dataclass(frozen=True, kw_only=True)
class ClaimRecord:
    """One reportable statement linked to evidence IDs."""

    claim_id: str
    dimension: str
    statement: str
    confidence: str
    evidence_ids: tuple[str, ...] = ()
    conflicting_evidence_ids: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    reportable: bool = True

    def __post_init__(self) -> None:
        """Validate claim text and detach ID/limitation sequences."""
        for field_name in ("claim_id", "dimension", "statement", "confidence"):
            object.__setattr__(self, field_name, _text(getattr(self, field_name), field_name=field_name, limit=4096))
        if not isinstance(self.reportable, bool):
            raise TypeError("Claim reportable flag must be boolean.")
        for field_name in ("evidence_ids", "conflicting_evidence_ids", "limitations"):
            object.__setattr__(self, field_name, _string_tuple(getattr(self, field_name)))

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible claim."""
        return {
            "claim_id": self.claim_id,
            "dimension": self.dimension,
            "statement": self.statement,
            "confidence": self.confidence,
            "evidence_ids": list(self.evidence_ids),
            "conflicting_evidence_ids": list(self.conflicting_evidence_ids),
            "limitations": list(self.limitations),
            "reportable": self.reportable,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ClaimRecord:
        """Restore one claim from JSON."""
        return cls(**dict(value))


@dataclass(frozen=True, kw_only=True)
class CoverageDecision:
    """Deterministic Profile coverage result."""

    required_dimensions: tuple[str, ...] = ()
    covered_dimensions: tuple[str, ...] = ()
    missing_dimensions: tuple[str, ...] = ()
    weak_claims: tuple[str, ...] = ()
    conflicting_claims: tuple[str, ...] = ()
    coverage_score: float = 0.0
    allow_report: bool = False
    gap_queries: tuple[str, ...] = ()
    retry_count: int = 0
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    dimension_results: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        """Clamp scores and validate retry counters."""
        for field_name in (
            "required_dimensions",
            "covered_dimensions",
            "missing_dimensions",
            "weak_claims",
            "conflicting_claims",
            "gap_queries",
            "blockers",
            "warnings",
        ):
            object.__setattr__(self, field_name, _string_tuple(getattr(self, field_name)))
        if not isinstance(self.coverage_score, (int, float)) or isinstance(self.coverage_score, bool):
            raise TypeError("Coverage score must be numeric.")
        object.__setattr__(self, "coverage_score", max(0.0, min(1.0, float(self.coverage_score))))
        if not isinstance(self.allow_report, bool):
            raise TypeError("Coverage allow_report flag must be boolean.")
        if not isinstance(self.retry_count, int) or isinstance(self.retry_count, bool) or self.retry_count < 0:
            raise ValueError("Coverage retry_count must be non-negative.")
        object.__setattr__(self, "dimension_results", _mapping_tuple(self.dimension_results))

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible coverage decision."""
        return {
            "required_dimensions": list(self.required_dimensions),
            "covered_dimensions": list(self.covered_dimensions),
            "missing_dimensions": list(self.missing_dimensions),
            "weak_claims": list(self.weak_claims),
            "conflicting_claims": list(self.conflicting_claims),
            "coverage_score": self.coverage_score,
            "allow_report": self.allow_report,
            "gap_queries": list(self.gap_queries),
            "retry_count": self.retry_count,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "dimension_results": [dict(item) for item in self.dimension_results],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CoverageDecision:
        """Restore one coverage decision from JSON."""
        return cls(**dict(value))


@dataclass(frozen=True, kw_only=True)
class GenericReportSpec:
    """Format-neutral report requirements shared by all research modes."""

    title: str
    executive_summary: str = ""
    sections: tuple[Mapping[str, Any], ...] = ()
    claim_ids: tuple[str, ...] = ()
    citation_ids: tuple[str, ...] = ()
    tables: tuple[Mapping[str, Any], ...] = ()
    charts: tuple[Mapping[str, Any], ...] = ()
    diagrams: tuple[Mapping[str, Any], ...] = ()
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate report title and detach list fields."""
        object.__setattr__(self, "title", _text(self.title, field_name="report title", limit=512))
        if not isinstance(self.executive_summary, str):
            raise TypeError("Report executive summary must be text.")
        object.__setattr__(self, "sections", _mapping_tuple(self.sections))
        object.__setattr__(self, "claim_ids", _string_tuple(self.claim_ids))
        object.__setattr__(self, "citation_ids", _string_tuple(self.citation_ids))
        object.__setattr__(self, "tables", _mapping_tuple(self.tables))
        object.__setattr__(self, "charts", _mapping_tuple(self.charts))
        object.__setattr__(self, "diagrams", _mapping_tuple(self.diagrams))
        object.__setattr__(self, "limitations", _string_tuple(self.limitations))

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible report specification."""
        return {
            "title": self.title,
            "executive_summary": self.executive_summary,
            "sections": [dict(item) for item in self.sections],
            "claim_ids": list(self.claim_ids),
            "citation_ids": list(self.citation_ids),
            "tables": [dict(item) for item in self.tables],
            "charts": [dict(item) for item in self.charts],
            "diagrams": [dict(item) for item in self.diagrams],
            "limitations": list(self.limitations),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> GenericReportSpec:
        """Restore one report specification from JSON."""
        return cls(**dict(value))


@dataclass(frozen=True, kw_only=True)
class ArtifactDescriptorV2:
    """Reference to content held by an ArtifactStore, without inline body."""

    artifact_id: str
    artifact_type: str
    mime_type: str
    path: str
    title: str
    description: str = ""
    source_ids: tuple[str, ...] = ()
    size_bytes: int = 0
    checksum: str = ""
    created_at: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        """Validate descriptor identity and path-safe metadata."""
        for field_name in ("artifact_id", "artifact_type", "mime_type", "path", "title", "checksum"):
            object.__setattr__(self, field_name, _text(getattr(self, field_name), field_name=field_name, limit=2048))
        if not isinstance(self.description, str):
            raise TypeError("Artifact description must be text.")
        if not isinstance(self.size_bytes, int) or isinstance(self.size_bytes, bool) or self.size_bytes < 0:
            raise ValueError("Artifact size_bytes must be non-negative.")
        object.__setattr__(self, "source_ids", _string_tuple(self.source_ids))
        _text(self.created_at, field_name="created_at", limit=128)

    def as_dict(self) -> dict[str, Any]:
        """Return a descriptor without an artifact body."""
        return {
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type,
            "mime_type": self.mime_type,
            "path": self.path,
            "title": self.title,
            "description": self.description,
            "source_ids": list(self.source_ids),
            "size_bytes": self.size_bytes,
            "checksum": self.checksum,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ArtifactDescriptorV2:
        """Restore one descriptor from JSON."""
        return cls(**dict(value))


@dataclass(frozen=True, kw_only=True)
class ArtifactManifestV2:
    """Versioned collection of external artifact descriptors."""

    artifacts: tuple[ArtifactDescriptorV2, ...] = ()
    schema_version: int = INTELLIGENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        """Require v2 and unique artifact IDs."""
        if self.schema_version != INTELLIGENCE_SCHEMA_VERSION:
            raise ValueError("Artifact manifest schema version is unsupported.")
        if not isinstance(self.artifacts, (list, tuple)):
            raise TypeError("Artifact manifest artifacts must be a sequence.")
        normalized = tuple(self.artifacts)
        if any(not isinstance(item, ArtifactDescriptorV2) for item in normalized):
            raise TypeError("Artifact manifest entries must be ArtifactDescriptorV2 objects.")
        object.__setattr__(self, "artifacts", normalized)
        ids = tuple(item.artifact_id for item in normalized)
        if len(ids) != len(set(ids)):
            raise ValueError("Artifact IDs must be unique.")

    def as_dict(self) -> dict[str, Any]:
        """Return the descriptor-only manifest."""
        return {
            "schema_version": self.schema_version,
            "artifacts": [item.as_dict() for item in self.artifacts],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ArtifactManifestV2:
        """Restore and validate one descriptor-only manifest."""
        if not isinstance(value, Mapping):
            raise ValueError("Artifact manifest must be an object.")
        if value.get("schema_version") != INTELLIGENCE_SCHEMA_VERSION:
            raise ValueError("Artifact manifest schema version is unsupported.")
        raw_artifacts = value.get("artifacts")
        if not isinstance(raw_artifacts, (list, tuple)):
            raise ValueError("Artifact manifest artifacts must be a sequence.")
        return cls(
            artifacts=tuple(
                ArtifactDescriptorV2.from_dict(item)
                for item in raw_artifacts
                if isinstance(item, Mapping)
            )
        )


@dataclass(frozen=True, kw_only=True)
class ResearchIntelligenceBundle:
    """Complete provider-neutral intelligence payload for one research Run."""

    mode: ResearchMode
    profile_id: str
    profile_version: int
    sources: tuple[SourceReference, ...] = ()
    evidence: tuple[EvidenceRecord, ...] = ()
    claims: tuple[ClaimRecord, ...] = ()
    coverage: CoverageDecision = field(default_factory=CoverageDecision)
    report_spec: GenericReportSpec = field(default_factory=lambda: GenericReportSpec(title="Research report"))
    artifact_manifest: ArtifactManifestV2 = field(default_factory=ArtifactManifestV2)
    evidence_frozen: bool = False
    schema_version: int = INTELLIGENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        """Validate version, identity uniqueness, references, and mode."""
        if self.schema_version != INTELLIGENCE_SCHEMA_VERSION:
            raise ValueError("Intelligence schema version is unsupported.")
        object.__setattr__(self, "mode", ResearchMode(self.mode))
        object.__setattr__(self, "profile_id", _text(self.profile_id, field_name="profile_id", limit=256))
        if not isinstance(self.profile_version, int) or isinstance(self.profile_version, bool) or self.profile_version < 1:
            raise ValueError("Profile version must be positive.")
        if not isinstance(self.evidence_frozen, bool):
            raise TypeError("evidence_frozen must be boolean.")
        for field_name, item_type in (
            ("sources", SourceReference),
            ("evidence", EvidenceRecord),
            ("claims", ClaimRecord),
        ):
            value = getattr(self, field_name)
            if not isinstance(value, (list, tuple)):
                raise TypeError(f"Bundle {field_name} must be a sequence.")
            normalized = tuple(value)
            if any(not isinstance(item, item_type) for item in normalized):
                raise TypeError(f"Bundle {field_name} entries have an invalid type.")
            object.__setattr__(self, field_name, normalized)
        if not isinstance(self.coverage, CoverageDecision):
            raise TypeError("Bundle coverage must be a CoverageDecision.")
        if not isinstance(self.report_spec, GenericReportSpec):
            raise TypeError("Bundle report_spec must be a GenericReportSpec.")
        if not isinstance(self.artifact_manifest, ArtifactManifestV2):
            raise TypeError("Bundle artifact_manifest must be an ArtifactManifestV2.")
        source_ids = tuple(item.source_id for item in self.sources)
        evidence_ids = tuple(item.evidence_id for item in self.evidence)
        claim_ids = tuple(item.claim_id for item in self.claims)
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("Source IDs must be unique.")
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("Evidence IDs must be unique.")
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("Claim IDs must be unique.")
        source_set = set(source_ids)
        evidence_set = set(evidence_ids)
        if any(item.source.source_id not in source_set for item in self.evidence):
            raise ValueError("Evidence references an unknown source.")
        for claim in self.claims:
            if any(item not in evidence_set for item in claim.evidence_ids + claim.conflicting_evidence_ids):
                raise ValueError("Claim references an unknown evidence ID.")
        if any(item not in claim_ids for item in self.report_spec.claim_ids):
            raise ValueError("Report specification references an unknown claim ID.")
        if any(item not in evidence_set for item in self.report_spec.citation_ids):
            raise ValueError("Report specification references an unknown evidence ID.")

    def as_dict(self) -> dict[str, Any]:
        """Return a detached schema-v2 JSON payload."""
        return {
            "schema_version": self.schema_version,
            "mode": self.mode.value,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "sources": [item.as_dict() for item in self.sources],
            "evidence": [item.as_dict() for item in self.evidence],
            "claims": [item.as_dict() for item in self.claims],
            "coverage": self.coverage.as_dict(),
            "report_spec": self.report_spec.as_dict(),
            "artifact_manifest": self.artifact_manifest.as_dict(),
            "evidence_frozen": self.evidence_frozen,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ResearchIntelligenceBundle:
        """Restore and validate one schema-v2 bundle."""
        if not isinstance(value, Mapping):
            raise ValueError("Intelligence bundle must be an object.")
        if value.get("schema_version") != INTELLIGENCE_SCHEMA_VERSION:
            raise ValueError("Intelligence schema version is unsupported.")
        raw_manifest = value.get("artifact_manifest")
        if not isinstance(raw_manifest, Mapping):
            raise ValueError("Artifact manifest is required.")
        manifest = ArtifactManifestV2.from_dict(raw_manifest)
        profile_version = value.get("profile_version")
        if not isinstance(profile_version, int) or isinstance(profile_version, bool):
            raise ValueError("Profile version must be an integer.")
        evidence_frozen = value.get("evidence_frozen")
        if not isinstance(evidence_frozen, bool):
            raise ValueError("evidence_frozen must be boolean.")
        return cls(
            schema_version=INTELLIGENCE_SCHEMA_VERSION,
            mode=ResearchMode(value.get("mode")),
            profile_id=str(value.get("profile_id") or ""),
            profile_version=profile_version,
            sources=tuple(
                SourceReference.from_dict(item)
                for item in value.get("sources", [])
                if isinstance(item, Mapping)
            ),
            evidence=tuple(
                EvidenceRecord.from_dict(item)
                for item in value.get("evidence", [])
                if isinstance(item, Mapping)
            ),
            claims=tuple(
                ClaimRecord.from_dict(item)
                for item in value.get("claims", [])
                if isinstance(item, Mapping)
            ),
            coverage=CoverageDecision.from_dict(value.get("coverage", {})),
            report_spec=GenericReportSpec.from_dict(value.get("report_spec", {})),
            artifact_manifest=manifest,
            evidence_frozen=evidence_frozen,
        )


def stable_source_id(source: SourceReference) -> str:
    """Return a deterministic ID for a provider source identity."""
    return f"src_{_digest((source.provider_id, source.source_kind, source.source_id, source.resolved_version))}"


def stable_evidence_id(
    source: SourceReference,
    *,
    locator: EvidenceLocator,
    excerpt: str,
) -> str:
    """Return a deterministic ID for one source locator and excerpt."""
    return f"ev_{_digest((stable_source_id(source), locator.as_dict(), hashlib.sha256(excerpt.encode('utf-8')).hexdigest()))}"


def stable_claim_id(*, profile_id: str, dimension: str, statement: str) -> str:
    """Return a deterministic ID for one normalized reportable claim."""
    normalized = " ".join(statement.casefold().split())
    return f"claim_{_digest((profile_id.strip(), dimension.strip(), normalized))}"


__all__ = [
    "INTELLIGENCE_SCHEMA_VERSION",
    "ArtifactDescriptorV2",
    "ArtifactManifestV2",
    "ClaimRecord",
    "CoverageDecision",
    "EvidenceLocator",
    "EvidenceRecord",
    "GenericReportSpec",
    "ResearchIntelligenceBundle",
    "SourceReference",
    "stable_claim_id",
    "stable_evidence_id",
    "stable_source_id",
]
