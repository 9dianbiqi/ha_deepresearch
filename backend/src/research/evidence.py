"""Versioned GitHub evidence, claim, coverage, and artifact contracts."""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from urllib.parse import quote

GITHUB_INTELLIGENCE_SCHEMA_VERSION = 1
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_URL_RE = re.compile(r"https?://[^\s)]+")
_MAX_EVIDENCE = 180
_MAX_EXCERPT_CHARS = 1200
_MAX_SOURCE_CHUNKS_PER_FILE = 6
_MAX_SOURCE_CHUNK_LINES = 40
_MAX_SOURCE_CHUNK_CHARS = 1100


def _now_iso() -> str:
    """Return the current UTC timestamp in a stable wire format."""
    return datetime.now(timezone.utc).isoformat()


def _clean(value: object, limit: int = _MAX_EXCERPT_CHARS) -> str:
    """Normalize one bounded text field."""
    text = str(value or "").strip()
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def _stable_id(prefix: str, *parts: object) -> str:
    """Build a deterministic identifier from public research inputs."""
    raw = "\x1f".join(str(part or "") for part in parts)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}_{digest}"


def _valid_sha(value: object) -> str | None:
    """Return a normalized commit SHA or ``None`` for an unavailable ref."""
    text = str(value or "").strip()
    return text.lower() if _SHA_RE.fullmatch(text) else None


def _optional_int(value: object) -> int | None:
    """Return an integer from untrusted persisted JSON."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _string_tuple(value: object) -> tuple[str, ...]:
    """Return a tuple containing only string entries from persisted JSON."""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _mapping_tuple(value: object) -> tuple[Mapping[str, Any], ...]:
    """Return detached mappings from persisted JSON."""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(dict(item) for item in value if isinstance(item, Mapping))


def _optional_float(value: object) -> float:
    """Return a finite coverage score, falling back to zero."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0.0, min(1.0, float(value)))
    return 0.0


def _github_url(repository: str, *, sha: str | None = None, path: str | None = None) -> str:
    """Return a GitHub URL pinned to a commit when one is available."""
    base = f"https://github.com/{repository}"
    if sha and path:
        return f"{base}/blob/{sha}/{quote(path, safe='/')}"
    if sha:
        return f"{base}/commit/{sha}"
    return base


@dataclass(frozen=True, kw_only=True)
class RepositorySnapshot:
    """Immutable identity and collection metadata for one repository."""

    snapshot_id: str
    repository: str
    repository_url: str
    requested_ref: str | None
    default_branch: str
    commit_sha: str | None
    collected_at: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    languages: Mapping[str, int] = field(default_factory=dict)
    file_manifest: tuple[Mapping[str, Any], ...] = ()
    contributors: tuple[Mapping[str, Any], ...] = ()
    releases: tuple[Mapping[str, Any], ...] = ()
    collection_status: str = "partial"
    notices: tuple[str, ...] = ()
    notice_codes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible snapshot."""
        return {
            "schema_version": GITHUB_INTELLIGENCE_SCHEMA_VERSION,
            "snapshot_id": self.snapshot_id,
            "repository": self.repository,
            "repository_url": self.repository_url,
            "requested_ref": self.requested_ref,
            "default_branch": self.default_branch,
            "commit_sha": self.commit_sha,
            "collected_at": self.collected_at,
            "metadata": dict(self.metadata),
            "languages": dict(self.languages),
            "file_manifest": [dict(item) for item in self.file_manifest],
            "contributors": [dict(item) for item in self.contributors],
            "releases": [dict(item) for item in self.releases],
            "collection_status": self.collection_status,
            "notices": list(self.notices),
            "notice_codes": list(self.notice_codes),
        }


@dataclass(frozen=True, kw_only=True)
class EvidenceItem:
    """One bounded, source-addressable research observation."""

    evidence_id: str
    snapshot_id: str
    evidence_type: str
    title: str
    excerpt: str
    source_url: str
    commit_sha: str | None = None
    file_path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    captured_at: str = field(default_factory=_now_iso)
    content_hash: str = ""

    def __post_init__(self) -> None:
        """Fill the content hash from the immutable excerpt when omitted."""
        if not self.content_hash:
            digest = hashlib.sha256(self.excerpt.encode("utf-8")).hexdigest()
            object.__setattr__(self, "content_hash", digest)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible evidence record."""
        return {
            "evidence_id": self.evidence_id,
            "snapshot_id": self.snapshot_id,
            "evidence_type": self.evidence_type,
            "title": self.title,
            "excerpt": self.excerpt,
            "source_url": self.source_url,
            "commit_sha": self.commit_sha,
            "file_path": self.file_path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "captured_at": self.captured_at,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, kw_only=True)
class ResearchClaim:
    """One reportable conclusion linked to one or more evidence records."""

    claim_id: str
    category: str
    statement: str
    confidence: str
    evidence_ids: tuple[str, ...] = ()
    conflicting_evidence_ids: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible claim."""
        return {
            "claim_id": self.claim_id,
            "category": self.category,
            "statement": self.statement,
            "confidence": self.confidence,
            "evidence_ids": list(self.evidence_ids),
            "conflicting_evidence_ids": list(self.conflicting_evidence_ids),
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True, kw_only=True)
class CoverageResult:
    """Deterministic research coverage and gap-evaluation result."""

    required_dimensions: tuple[str, ...]
    covered_dimensions: tuple[str, ...]
    missing_dimensions: tuple[str, ...]
    weak_claims: tuple[str, ...] = ()
    conflicting_claims: tuple[str, ...] = ()
    coverage_score: float = 0.0
    allow_report: bool = False
    gap_queries: tuple[str, ...] = ()
    retry_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible coverage result."""
        return {
            "required_dimensions": list(self.required_dimensions),
            "covered_dimensions": list(self.covered_dimensions),
            "missing_dimensions": list(self.missing_dimensions),
            "weak_claims": list(self.weak_claims),
            "conflicting_claims": list(self.conflicting_claims),
            "coverage_score": round(self.coverage_score, 4),
            "allow_report": self.allow_report,
            "gap_queries": list(self.gap_queries),
            "retry_count": self.retry_count,
        }


@dataclass(frozen=True, kw_only=True)
class ReportSpec:
    """Format-neutral report intermediate representation."""

    title: str
    executive_summary: str = ""
    sections: tuple[Mapping[str, Any], ...] = ()
    claim_ids: tuple[str, ...] = ()
    citation_ids: tuple[str, ...] = ()
    tables: tuple[Mapping[str, Any], ...] = ()
    charts: tuple[Mapping[str, Any], ...] = ()
    diagrams: tuple[Mapping[str, Any], ...] = ()
    limitations: tuple[str, ...] = ()

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


@dataclass(frozen=True, kw_only=True)
class Artifact:
    """One persisted report artifact or deterministic visualization."""

    artifact_id: str
    artifact_type: str
    mime_type: str
    path: str
    title: str
    description: str = ""
    source_ids: tuple[str, ...] = ()
    content: str = ""
    checksum: str = ""

    def __post_init__(self) -> None:
        """Fill the artifact checksum from its content when omitted."""
        if not self.checksum:
            digest = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
            object.__setattr__(self, "checksum", digest)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible artifact manifest entry."""
        return {
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type,
            "mime_type": self.mime_type,
            "path": self.path,
            "title": self.title,
            "description": self.description,
            "source_ids": list(self.source_ids),
            "content": self.content,
            "checksum": self.checksum,
        }


@dataclass(frozen=True, kw_only=True)
class ArtifactManifest:
    """Versioned manifest for all downloadable report artifacts."""

    artifacts: tuple[Artifact, ...] = ()
    schema_version: int = GITHUB_INTELLIGENCE_SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        """Return the manifest without duplicating artifact content elsewhere."""
        return {
            "schema_version": self.schema_version,
            "artifacts": [item.as_dict() for item in self.artifacts],
        }


@dataclass(frozen=True, kw_only=True)
class GitHubEvidenceBundle:
    """Complete bounded GitHub intelligence payload stored in one Run."""

    snapshots: tuple[RepositorySnapshot, ...]
    evidence: tuple[EvidenceItem, ...]
    claims: tuple[ResearchClaim, ...]
    coverage: CoverageResult
    report_spec: ReportSpec
    artifacts: tuple[Artifact, ...] = ()
    evidence_frozen: bool = False

    @property
    def artifact_manifest(self) -> ArtifactManifest:
        """Return the normalized artifact manifest view."""
        return ArtifactManifest(artifacts=self.artifacts)

    def as_dict(self) -> dict[str, Any]:
        """Return the versioned bundle for Run persistence and SSE."""
        return {
            "schema_version": GITHUB_INTELLIGENCE_SCHEMA_VERSION,
            "snapshots": [item.as_dict() for item in self.snapshots],
            "evidence": [item.as_dict() for item in self.evidence],
            "claims": [item.as_dict() for item in self.claims],
            "coverage": self.coverage.as_dict(),
            "report_spec": self.report_spec.as_dict(),
            "artifacts": [item.as_dict() for item in self.artifacts],
            "artifact_manifest": self.artifact_manifest.as_dict(),
            "evidence_frozen": self.evidence_frozen,
        }


def _snapshot_from_context(context: Any) -> RepositorySnapshot:
    """Project the existing GitHub context into an immutable snapshot."""
    target = context.target
    repository = str(target.full_name)
    metadata = dict(context.repository or {})
    default_branch = str(metadata.get("default_branch") or "main")
    raw_commits = context.commits if isinstance(context.commits, list) else []
    commit_sha = _valid_sha(getattr(context, "commit_sha", None))
    if commit_sha is None and raw_commits:
        commit_sha = _valid_sha(raw_commits[0].get("sha"))
    raw_manifest = getattr(context, "file_manifest", [])
    manifest = tuple(
        dict(item) for item in raw_manifest if isinstance(item, Mapping)
    )
    status = "complete" if not context.notices and commit_sha else "partial"
    snapshot_id = _stable_id("snapshot", repository, commit_sha or default_branch)
    return RepositorySnapshot(
        snapshot_id=snapshot_id,
        repository=repository,
        repository_url=target.html_url,
        requested_ref=default_branch,
        default_branch=default_branch,
        commit_sha=commit_sha,
        collected_at=_now_iso(),
        metadata=metadata,
        languages=dict(context.languages or {}),
        file_manifest=manifest,
        contributors=tuple(dict(item) for item in context.contributors),
        releases=tuple(dict(item) for item in context.releases),
        collection_status=status,
        notices=tuple(str(item) for item in context.notices),
        notice_codes=tuple(
            str(item) for item in getattr(context, "notice_codes", [])
        ),
    )


def _append_evidence(
    target: list[EvidenceItem],
    *,
    snapshot: RepositorySnapshot,
    evidence_type: str,
    title: str,
    excerpt: str,
    source_url: str,
    file_path: str | None = None,
    commit_sha: str | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
) -> None:
    """Append one bounded evidence item when it has usable content."""
    bounded = _clean(excerpt)
    if not bounded:
        return
    target.append(
        EvidenceItem(
            evidence_id=_stable_id(
                "evidence",
                snapshot.snapshot_id,
                evidence_type,
                title,
                source_url,
                file_path or "",
                line_start or "",
                line_end or "",
            ),
            snapshot_id=snapshot.snapshot_id,
            evidence_type=evidence_type,
            title=_clean(title, 240),
            excerpt=bounded,
            source_url=source_url,
            commit_sha=commit_sha,
            file_path=file_path,
            line_start=line_start,
            line_end=line_end,
        )
    )


def _source_code_chunks(content: str) -> list[tuple[int, int, str]]:
    """Split one source file into bounded, line-numbered evidence excerpts."""
    lines = content.splitlines()
    chunks: list[tuple[int, int, str]] = []
    current: list[str] = []
    current_chars = 0
    start_line = 1
    for index, line in enumerate(lines, start=1):
        rendered = f"{index} | {line}"
        separator_chars = 1 if current else 0
        if current and (
            len(current) >= _MAX_SOURCE_CHUNK_LINES
            or current_chars + separator_chars + len(rendered) > _MAX_SOURCE_CHUNK_CHARS
        ):
            chunks.append((start_line, index - 1, "\n".join(current)))
            if len(chunks) >= _MAX_SOURCE_CHUNKS_PER_FILE:
                return chunks
            current = []
            current_chars = 0
            start_line = index
        current.append(rendered)
        current_chars += separator_chars + len(rendered)
    if current and len(chunks) < _MAX_SOURCE_CHUNKS_PER_FILE:
        chunks.append((start_line, len(lines), "\n".join(current)))
    return chunks


def _claims_for_snapshot(
    snapshot: RepositorySnapshot,
    evidence: Sequence[EvidenceItem],
) -> list[ResearchClaim]:
    """Create conservative deterministic claims from one snapshot."""
    # Import lazily so the analysis module can use the immutable evidence
    # contracts without creating an import cycle during module initialization.
    from .github_intelligence import analyze_repository

    return analyze_repository(snapshot, evidence)


def _coverage(
    snapshots: Sequence[RepositorySnapshot],
    claims: Sequence[ResearchClaim],
    *,
    retry_count: int = 0,
) -> CoverageResult:
    """Evaluate required GitHub research dimensions without an LLM judge."""
    required = ("overview", "architecture", "maintenance", "community", "license")
    covered = tuple(sorted({claim.category for claim in claims if claim.evidence_ids}))
    missing = tuple(item for item in required if item not in covered)
    weak_claims = tuple(item.claim_id for item in claims if not item.evidence_ids)
    repository_text = ", ".join(item.repository for item in snapshots)
    gap_queries = tuple(
        f"{repository_text} {dimension} evidence GitHub"
        for dimension in missing
    )
    score = len([item for item in required if item in covered]) / len(required)
    return CoverageResult(
        required_dimensions=required,
        covered_dimensions=covered,
        missing_dimensions=missing,
        weak_claims=weak_claims,
        coverage_score=score,
        allow_report="overview" in covered and "architecture" in covered,
        gap_queries=gap_queries[:4],
        retry_count=min(max(retry_count, 0), 1),
    )


def build_github_evidence_bundle(
    contexts: Sequence[Any],
    *,
    task_sources: Sequence[str] = (),
    retry_count: int = 0,
) -> GitHubEvidenceBundle:
    """Build bounded snapshots, evidence, claims, coverage, and report metadata."""
    snapshots = tuple(_snapshot_from_context(context) for context in contexts)
    evidence: list[EvidenceItem] = []
    for context, snapshot in zip(contexts, snapshots):
        repository = snapshot.repository
        sha = snapshot.commit_sha
        metadata = json.dumps(snapshot.metadata, ensure_ascii=False, sort_keys=True)
        _append_evidence(
            evidence,
            snapshot=snapshot,
            evidence_type="repository_metadata",
            title=f"{repository} repository metadata",
            excerpt=metadata,
            source_url=_github_url(repository, sha=sha),
            commit_sha=sha,
        )
        readme = _clean(getattr(context, "readme_excerpt", ""), 2400)
        _append_evidence(
            evidence,
            snapshot=snapshot,
            evidence_type="readme",
            title=f"{repository} README",
            excerpt=readme,
            source_url=_github_url(repository, sha=sha, path="README.md"),
            file_path="README.md",
            commit_sha=sha,
        )
        tree = _clean(getattr(context, "tree_excerpt", ""), 2400)
        _append_evidence(
            evidence,
            snapshot=snapshot,
            evidence_type="repository_tree",
            title=f"{repository} repository tree",
            excerpt=tree,
            source_url=_github_url(repository, sha=sha),
            commit_sha=sha,
        )
        raw_file_contents = getattr(context, "file_contents", [])
        if sha and isinstance(raw_file_contents, (list, tuple)):
            for item in raw_file_contents[:12]:
                if not isinstance(item, Mapping):
                    continue
                path = str(item.get("path") or "").strip()
                content = str(item.get("content") or "")
                if not path or not content:
                    continue
                for line_start, line_end, excerpt in _source_code_chunks(content):
                    _append_evidence(
                        evidence,
                        snapshot=snapshot,
                        evidence_type="source_code",
                        title=f"{path}:{line_start}-{line_end}",
                        excerpt=excerpt,
                        source_url=_github_url(repository, sha=sha, path=path),
                        file_path=path,
                        commit_sha=sha,
                        line_start=line_start,
                        line_end=line_end,
                    )
        if sha:
            for item in snapshot.file_manifest[:80]:
                path = str(item.get("path") or "").strip()
                if not path or str(item.get("type") or "blob") != "blob":
                    continue
                _append_evidence(
                    evidence,
                    snapshot=snapshot,
                    evidence_type="source_file",
                    title=path,
                    excerpt=f"文件存在于固定仓库快照：{path}",
                    source_url=_github_url(repository, sha=sha, path=path),
                    file_path=path,
                    commit_sha=sha,
                )
        for kind, values in (
            ("commit", getattr(context, "commits", [])),
            ("issue", getattr(context, "issues", [])),
            ("pull_request", getattr(context, "pull_requests", [])),
            ("release", getattr(context, "releases", [])),
        ):
            for value in values[:20]:
                if not isinstance(value, Mapping):
                    continue
                url = str(value.get("url") or _github_url(repository, sha=sha))
                title = str(value.get("title") or value.get("message") or value.get("tag") or kind)
                excerpt = json.dumps(dict(value), ensure_ascii=False, sort_keys=True)
                _append_evidence(
                    evidence,
                    snapshot=snapshot,
                    evidence_type=kind,
                    title=title,
                    excerpt=excerpt,
                    source_url=url,
                    commit_sha=sha if kind == "commit" else None,
                )
    for source in task_sources:
        for url in _URL_RE.findall(source or "")[:20]:
            if "github.com" not in url:
                continue
            external_snapshot: RepositorySnapshot | None = snapshots[0] if snapshots else None
            if external_snapshot is None:
                continue
            _append_evidence(
                evidence,
                snapshot=external_snapshot,
                evidence_type="external_web",
                title="外部研究来源",
                excerpt=f"研究任务引用了外部来源：{url}",
                source_url=url.rstrip(".,;"),
            )
    evidence = evidence[:_MAX_EVIDENCE]
    claims = _claims_for_snapshot(snapshots[0], evidence) if snapshots else []
    if len(snapshots) > 1:
        compare_ids = tuple(
            item.evidence_id
            for snapshot in snapshots
            for item in evidence
            if item.snapshot_id == snapshot.snapshot_id
        )[:12]
        claims.append(
            ResearchClaim(
                claim_id=_stable_id("claim", "comparison", *(item.repository for item in snapshots)),
                category="comparison",
                statement="多个 GitHub 仓库已使用统一的快照和证据模型，可进行维度对齐比较。",
                confidence="medium" if compare_ids else "low",
                evidence_ids=compare_ids,
            )
        )
    coverage = _coverage(snapshots, claims, retry_count=retry_count)
    report_spec = ReportSpec(
        title=(
            f"{snapshots[0].repository} GitHub 技术研究"
            if len(snapshots) == 1
            else "GitHub 项目技术对比研究"
        ),
        sections=tuple(
            {"id": dimension, "title": dimension, "required": True}
            for dimension in coverage.required_dimensions
        ),
        claim_ids=tuple(item.claim_id for item in claims),
        citation_ids=tuple(item.evidence_id for item in evidence),
        charts=(
            {"id": "repository_activity", "type": "timeline", "source": "commit"},
            {"id": "language_mix", "type": "bar", "source": "repository_metadata"},
        ),
        diagrams=({"id": "repository_tree", "type": "mermaid", "source": "repository_tree"},),
        limitations=tuple(coverage.missing_dimensions),
    )
    return GitHubEvidenceBundle(
        snapshots=snapshots,
        evidence=tuple(evidence),
        claims=tuple(claims),
        coverage=coverage,
        report_spec=report_spec,
    )


def github_evidence_bundle_from_dict(value: Mapping[str, Any]) -> GitHubEvidenceBundle | None:
    """Rehydrate a persisted bundle without trusting arbitrary nested values."""
    if value.get("schema_version", GITHUB_INTELLIGENCE_SCHEMA_VERSION) != GITHUB_INTELLIGENCE_SCHEMA_VERSION:
        return None
    raw_snapshots = value.get("snapshots")
    raw_evidence = value.get("evidence")
    raw_claims = value.get("claims")
    raw_coverage = value.get("coverage")
    raw_spec = value.get("report_spec")
    if (
        not isinstance(raw_snapshots, (list, tuple))
        or not isinstance(raw_evidence, (list, tuple))
        or not isinstance(raw_claims, (list, tuple))
        or not isinstance(raw_coverage, Mapping)
        or not isinstance(raw_spec, Mapping)
    ):
        return None
    snapshot_items: Sequence[Any] = raw_snapshots
    evidence_items: Sequence[Any] = raw_evidence
    claim_items: Sequence[Any] = raw_claims

    snapshots: list[RepositorySnapshot] = []
    for item in snapshot_items:
        if not isinstance(item, Mapping):
            continue
        repository = str(item.get("repository") or "").strip()
        snapshot_id = str(item.get("snapshot_id") or "").strip()
        if not repository or not snapshot_id:
            continue
        raw_metadata = item.get("metadata")
        raw_languages = item.get("languages")
        raw_manifest = item.get("file_manifest")
        raw_contributors = item.get("contributors")
        raw_releases = item.get("releases")
        raw_notices = item.get("notices")
        snapshots.append(
            RepositorySnapshot(
                snapshot_id=snapshot_id,
                repository=repository,
                repository_url=str(item.get("repository_url") or _github_url(repository)),
                requested_ref=(
                    str(item.get("requested_ref"))
                    if item.get("requested_ref") is not None
                    else None
                ),
                default_branch=str(item.get("default_branch") or "main"),
                commit_sha=_valid_sha(item.get("commit_sha")),
                collected_at=str(item.get("collected_at") or _now_iso()),
                metadata=dict(raw_metadata) if isinstance(raw_metadata, Mapping) else {},
                languages={
                    str(key): int(number)
                    for key, number in (dict(raw_languages) if isinstance(raw_languages, Mapping) else {}).items()
                    if isinstance(key, str) and isinstance(number, (int, float))
                },
                file_manifest=tuple(
                    dict(entry)
                    for entry in (raw_manifest if isinstance(raw_manifest, (list, tuple)) else [])
                    if isinstance(entry, Mapping)
                ),
                contributors=tuple(
                    dict(entry)
                    for entry in (raw_contributors if isinstance(raw_contributors, (list, tuple)) else [])
                    if isinstance(entry, Mapping)
                ),
                releases=tuple(
                    dict(entry)
                    for entry in (raw_releases if isinstance(raw_releases, (list, tuple)) else [])
                    if isinstance(entry, Mapping)
                ),
                collection_status=str(item.get("collection_status") or "partial"),
                notices=tuple(
                    entry
                    for entry in (raw_notices if isinstance(raw_notices, (list, tuple)) else [])
                    if isinstance(entry, str)
                ),
                notice_codes=_string_tuple(item.get("notice_codes")),
            )
        )

    evidence: list[EvidenceItem] = []
    for item in evidence_items:
        if not isinstance(item, Mapping):
            continue
        evidence_id = str(item.get("evidence_id") or "").strip()
        snapshot_id = str(item.get("snapshot_id") or "").strip()
        if not evidence_id or not snapshot_id:
            continue
        line_start = _optional_int(item.get("line_start"))
        line_end = _optional_int(item.get("line_end"))
        evidence.append(
            EvidenceItem(
                evidence_id=evidence_id,
                snapshot_id=snapshot_id,
                evidence_type=str(item.get("evidence_type") or "unknown"),
                title=_clean(item.get("title"), 240),
                excerpt=_clean(item.get("excerpt")),
                source_url=str(item.get("source_url") or ""),
                commit_sha=_valid_sha(item.get("commit_sha")),
                file_path=(
                    str(item.get("file_path"))
                    if item.get("file_path") is not None
                    else None
                ),
                line_start=line_start,
                line_end=line_end,
                captured_at=str(item.get("captured_at") or _now_iso()),
                content_hash=str(item.get("content_hash") or ""),
            )
        )

    claims: list[ResearchClaim] = []
    for item in claim_items:
        if not isinstance(item, Mapping):
            continue
        claim_id = str(item.get("claim_id") or "").strip()
        if not claim_id:
            continue
        claims.append(
            ResearchClaim(
                claim_id=claim_id,
                category=str(item.get("category") or "unknown"),
                statement=_clean(item.get("statement"), 1800),
                confidence=str(item.get("confidence") or "low"),
                evidence_ids=tuple(
                    entry for entry in _string_tuple(item.get("evidence_ids"))
                ),
                conflicting_evidence_ids=tuple(
                    entry for entry in _string_tuple(item.get("conflicting_evidence_ids"))
                ),
                limitations=_string_tuple(item.get("limitations")),
            )
        )

    coverage = CoverageResult(
        required_dimensions=_string_tuple(raw_coverage.get("required_dimensions")),
        covered_dimensions=_string_tuple(raw_coverage.get("covered_dimensions")),
        missing_dimensions=_string_tuple(raw_coverage.get("missing_dimensions")),
        weak_claims=_string_tuple(raw_coverage.get("weak_claims")),
        conflicting_claims=_string_tuple(raw_coverage.get("conflicting_claims")),
        coverage_score=_optional_float(raw_coverage.get("coverage_score")),
        allow_report=raw_coverage.get("allow_report") is True,
        gap_queries=_string_tuple(raw_coverage.get("gap_queries")),
        retry_count=_optional_int(raw_coverage.get("retry_count")) or 0,
    )
    report_spec = ReportSpec(
        title=_clean(raw_spec.get("title"), 240),
        executive_summary=_clean(raw_spec.get("executive_summary"), 2400),
        sections=_mapping_tuple(raw_spec.get("sections")),
        claim_ids=_string_tuple(raw_spec.get("claim_ids")),
        citation_ids=_string_tuple(raw_spec.get("citation_ids")),
        tables=_mapping_tuple(raw_spec.get("tables")),
        charts=_mapping_tuple(raw_spec.get("charts")),
        diagrams=_mapping_tuple(raw_spec.get("diagrams")),
        limitations=_string_tuple(raw_spec.get("limitations")),
    )
    artifacts: list[Artifact] = []
    raw_artifacts = value.get("artifacts")
    if not isinstance(raw_artifacts, (list, tuple)):
        raw_manifest = value.get("artifact_manifest")
        raw_artifacts = (
            raw_manifest.get("artifacts")
            if isinstance(raw_manifest, Mapping)
            else []
        )
    if isinstance(raw_artifacts, (list, tuple)):
        for item in raw_artifacts:
            if not isinstance(item, Mapping):
                continue
            artifact_id = str(item.get("artifact_id") or "").strip()
            if not artifact_id:
                continue
            artifacts.append(
                Artifact(
                    artifact_id=artifact_id,
                    artifact_type=str(item.get("artifact_type") or "unknown"),
                    mime_type=str(item.get("mime_type") or "text/plain"),
                    path=str(item.get("path") or ""),
                    title=_clean(item.get("title"), 240),
                    description=_clean(item.get("description"), 600),
                    source_ids=_string_tuple(item.get("source_ids")),
                    content=_clean(item.get("content"), 200_000),
                    checksum=str(item.get("checksum") or ""),
                )
            )
    return GitHubEvidenceBundle(
        snapshots=tuple(snapshots),
        evidence=tuple(evidence),
        claims=tuple(claims),
        coverage=coverage,
        report_spec=report_spec,
        artifacts=tuple(artifacts),
        evidence_frozen=value.get("evidence_frozen") is True,
    )


def supplement_github_evidence(
    bundle: GitHubEvidenceBundle,
    task_sources: Sequence[str],
) -> GitHubEvidenceBundle:
    """Perform the one permitted deterministic gap-enrichment pass.

    This pass only adds already-collected GitHub URLs from task source summaries;
    it never performs another network request and is a no-op after evidence is
    frozen or the retry budget has been consumed.
    """
    if bundle.evidence_frozen or bundle.coverage.retry_count >= 1 or not bundle.snapshots:
        return bundle
    extra: list[EvidenceItem] = list(bundle.evidence)
    snapshot = bundle.snapshots[0]
    existing_urls = {item.source_url for item in extra}
    for source in task_sources:
        for url in _URL_RE.findall(source or "")[:20]:
            normalized = url.rstrip(".,;")
            if "github.com" not in normalized or normalized in existing_urls:
                continue
            existing_urls.add(normalized)
            _append_evidence(
                extra,
                snapshot=snapshot,
                evidence_type="external_web",
                title="补查 GitHub 来源",
                excerpt=f"任务来源补充了 GitHub 证据：{normalized}",
                source_url=normalized,
            )
    extra = extra[:_MAX_EVIDENCE]
    claims = _claims_for_snapshot(snapshot, extra)
    if len(bundle.snapshots) > 1:
        compare_ids = tuple(
            item.evidence_id
            for repository_snapshot in bundle.snapshots
            for item in extra
            if item.snapshot_id == repository_snapshot.snapshot_id
        )[:12]
        claims.append(
            ResearchClaim(
                claim_id=_stable_id(
                    "claim", "comparison", *(item.repository for item in bundle.snapshots)
                ),
                category="comparison",
                statement="多个 GitHub 仓库可使用统一证据模型进行维度对齐比较。",
                confidence="medium" if extra else "low",
                evidence_ids=compare_ids,
            )
        )
    coverage = _coverage(bundle.snapshots, claims, retry_count=1)
    report_spec = ReportSpec(
        title=bundle.report_spec.title,
        executive_summary=bundle.report_spec.executive_summary,
        sections=bundle.report_spec.sections,
        claim_ids=tuple(item.claim_id for item in claims),
        citation_ids=tuple(item.evidence_id for item in extra),
        tables=bundle.report_spec.tables,
        charts=bundle.report_spec.charts,
        diagrams=bundle.report_spec.diagrams,
        limitations=tuple(coverage.missing_dimensions),
    )
    return GitHubEvidenceBundle(
        snapshots=bundle.snapshots,
        evidence=tuple(extra),
        claims=tuple(claims),
        coverage=coverage,
        report_spec=report_spec,
        artifacts=bundle.artifacts,
        evidence_frozen=False,
    )


def freeze_github_evidence(bundle: GitHubEvidenceBundle) -> GitHubEvidenceBundle:
    """Mark the bounded evidence set immutable for report rendering."""
    return GitHubEvidenceBundle(
        snapshots=bundle.snapshots,
        evidence=bundle.evidence,
        claims=bundle.claims,
        coverage=bundle.coverage,
        report_spec=bundle.report_spec,
        artifacts=bundle.artifacts,
        evidence_frozen=True,
    )


def deterministic_citation_block(bundle: GitHubEvidenceBundle) -> str:
    """Render citations exclusively from the frozen evidence records."""
    lines = ["## 参考证据"]
    for item in bundle.evidence[:40]:
        lines.append(f"- [{item.evidence_id}] {item.title} — {item.source_url}")
    return "\n".join(lines)


def canonicalize_github_report(report: str, bundle: GitHubEvidenceBundle) -> str:
    """Remove unlinked GitHub URLs and append evidence-backed citations."""
    allowed_urls = {item.source_url.rstrip(".,;)") for item in bundle.evidence}

    def replace_url(match: re.Match[str]) -> str:
        """Keep only URLs present in the evidence ledger."""
        candidate = match.group(0)
        normalized = candidate.rstrip(".,;)")
        return candidate if normalized in allowed_urls else "[未关联的 GitHub URL 已移除]"

    sanitized = re.sub(r"https?://github\.com/[^\s<>\"']+", replace_url, report)
    if len(sanitized) >= 160 and bundle.evidence and "## 参考证据" not in sanitized:
        return f"{sanitized.rstrip()}\n\n{deterministic_citation_block(bundle)}"
    return sanitized


def render_github_artifacts(
    bundle: GitHubEvidenceBundle,
    *,
    report_markdown: str = "",
) -> GitHubEvidenceBundle:
    """Render deterministic JSON, HTML, SVG, and Mermaid artifacts."""
    payload_bundle = GitHubEvidenceBundle(
        snapshots=bundle.snapshots,
        evidence=bundle.evidence,
        claims=bundle.claims,
        coverage=bundle.coverage,
        report_spec=bundle.report_spec,
        evidence_frozen=bundle.evidence_frozen,
    )
    payload = payload_bundle.as_dict()
    evidence_json = json.dumps(payload, ensure_ascii=False, indent=2)
    title = html.escape(bundle.report_spec.title)
    bounded_report = _clean(report_markdown or "报告尚未生成。", 200_000)
    report = html.escape(bounded_report)
    links = "\n".join(
        f'<li><a href="{html.escape(item.source_url)}" target="_blank" rel="noopener">'
        f"{html.escape(item.title)}</a></li>"
        for item in bundle.evidence[:30]
    )
    html_content = (
        "<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
        f"<title>{title}</title><main><h1>{title}</h1>"
        f"<pre>{report}</pre><h2>证据</h2><ul>{links}</ul>"
        f"<pre>{html.escape(deterministic_citation_block(bundle))}</pre></main>"
    )
    language_values = [
        f"{html.escape(snapshot.repository)} · {html.escape(str(name))}: {int(value)}"
        for snapshot in bundle.snapshots
        for name, value in snapshot.languages.items()
    ]
    svg_content = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="720" height="180">'
        '<rect width="100%" height="100%" fill="#f8fafc"/>'
        '<text x="24" y="32" font-family="sans-serif" font-size="18">语言规模</text>'
        + "".join(
            f'<text x="24" y="{62 + index * 22}" font-family="sans-serif" font-size="14">{value}</text>'
            for index, value in enumerate(language_values[:5])
        )
        + "</svg>"
    )
    mermaid_lines = ["graph TD"]
    for index, snapshot in enumerate(bundle.snapshots, start=1):
        node_id = f"R{index}"
        label = re.sub(r"[^A-Za-z0-9_.-]", "_", snapshot.repository)
        mermaid_lines.extend(
            [
                f"  {node_id}[{label}] --> {node_id}Readme[README]",
                f"  {node_id} --> {node_id}Source[Source files {len(snapshot.file_manifest)}]",
                f"  {node_id} --> {node_id}Activity[Commits/releases]",
            ]
        )
    mermaid = "\n".join(mermaid_lines)
    artifacts = (
        Artifact(
            artifact_id=_stable_id("artifact", "markdown", bundle.report_spec.title),
            artifact_type="report_markdown",
            mime_type="text/markdown",
            path="artifacts/report.md",
            title="Markdown 研究报告",
            description="与当前 Run 报告正文一致的 Markdown 产物。",
            source_ids=tuple(item.evidence_id for item in bundle.evidence[:20]),
            content=bounded_report,
        ),
        Artifact(
            artifact_id=_stable_id("artifact", "evidence", bundle.report_spec.title),
            artifact_type="evidence_json",
            mime_type="application/json",
            path="artifacts/evidence.json",
            title="Evidence JSON",
            description="可复核的 GitHub 证据和结论数据包。",
            source_ids=tuple(item.evidence_id for item in bundle.evidence[:20]),
            content=evidence_json,
        ),
        Artifact(
            artifact_id=_stable_id("artifact", "html", bundle.report_spec.title),
            artifact_type="report_html",
            mime_type="text/html",
            path="artifacts/report.html",
            title="HTML 研究报告",
            description="由同一份研究结果渲染的可下载 HTML 报告。",
            source_ids=tuple(item.evidence_id for item in bundle.evidence[:20]),
            content=html_content,
        ),
        Artifact(
            artifact_id=_stable_id("artifact", "chart", bundle.report_spec.title),
            artifact_type="chart_svg",
            mime_type="image/svg+xml",
            path="artifacts/languages.svg",
            title="语言规模图",
            description="由 GitHub languages 数据确定性生成。",
            source_ids=tuple(item.evidence_id for item in bundle.evidence if item.evidence_type == "repository_metadata")[:4],
            content=svg_content,
        ),
        Artifact(
            artifact_id=_stable_id("artifact", "diagram", bundle.report_spec.title),
            artifact_type="mermaid",
            mime_type="text/plain",
            path="artifacts/repository-structure.mmd",
            title="仓库结构图",
            description="由仓库树证据生成的 Mermaid 草图。",
            source_ids=tuple(item.evidence_id for item in bundle.evidence if item.evidence_type == "repository_tree")[:4],
            content=mermaid,
        ),
    )
    return GitHubEvidenceBundle(
        snapshots=bundle.snapshots,
        evidence=bundle.evidence,
        claims=bundle.claims,
        coverage=bundle.coverage,
        report_spec=bundle.report_spec,
        artifacts=artifacts,
        evidence_frozen=bundle.evidence_frozen,
    )


__all__ = [
    "Artifact",
    "ArtifactManifest",
    "CoverageResult",
    "EvidenceItem",
    "GitHubEvidenceBundle",
    "GITHUB_INTELLIGENCE_SCHEMA_VERSION",
    "ReportSpec",
    "RepositorySnapshot",
    "ResearchClaim",
    "build_github_evidence_bundle",
    "canonicalize_github_report",
    "deterministic_citation_block",
    "github_evidence_bundle_from_dict",
    "freeze_github_evidence",
    "render_github_artifacts",
    "supplement_github_evidence",
]
