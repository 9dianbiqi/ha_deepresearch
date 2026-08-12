"""GitHub SourceProvider adapter over the existing governed REST client."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import quote

from research.adapters import GovernedGitHubAdapter, invoke_with_operation_scope
from research.profiles import ResearchMode
from research.session import CancellationRequestedError, DeadlineExceededError
from services.github_research import (
    GitHubRepositoryTarget,
    parse_github_repositories,
)

from ..operations import OperationRejectedError
from ..sources import (
    BudgetExceededError,
    DetectionResult,
    EnrichmentRequest,
    ProviderContext,
    SourceCollection,
    SourceRequestSpec,
    SourceSearchRequest,
    SourceSearchResult,
    SourceTarget,
)

_NOTICE_CODE = "github_provider_unavailable"
_NOTICE_MESSAGE = "GitHub source provider unavailable."
_API_NOTICE_CODE = "github_api_notice"
_API_NOTICE_MESSAGE = "GitHub API returned a notice."
_CONTEXT_FAILED_CODE = "github_api_context_failed"
_CONTEXT_FAILED_MESSAGE = "GitHub API context collection failed."
_KNOWN_NOTICE_MESSAGES = {
    _NOTICE_CODE: _NOTICE_MESSAGE,
    _API_NOTICE_CODE: _API_NOTICE_MESSAGE,
    _CONTEXT_FAILED_CODE: _CONTEXT_FAILED_MESSAGE,
}
_MAX_PROVIDER_RECORDS = 180
_MAX_SOURCE_CHARS = 1100
_MAX_SOURCE_LINES = 40


def _payload_value(payload: object, name: str, default: object = None) -> object:
    """Read one public field from either the real context or a test mapping."""
    if isinstance(payload, Mapping):
        return payload.get(name, default)
    return getattr(payload, name, default)


def _mapping(payload: object, name: str) -> Mapping[str, object]:
    """Return one detached public mapping field."""
    value = _payload_value(payload, name, {})
    return dict(value) if isinstance(value, Mapping) else {}


def _items(payload: object, name: str) -> tuple[Mapping[str, object], ...]:
    """Return bounded mapping entries from one GitHub collection field."""
    value = _payload_value(payload, name, ())
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(dict(item) for item in value if isinstance(item, Mapping))


def _text(value: object, fallback: str = "") -> str:
    """Normalize one bounded provider excerpt."""
    if not isinstance(value, str):
        return fallback
    normalized = value.strip()
    if len(normalized) > _MAX_SOURCE_CHARS:
        return normalized[:_MAX_SOURCE_CHARS]
    return normalized


def _github_url(target: SourceTarget, *, sha: str | None = None, path: str | None = None) -> str:
    """Build a public GitHub locator pinned to the collected commit when possible."""
    if sha and path:
        return f"{target.canonical_url}/blob/{sha}/{quote(path, safe='/')}"
    if sha:
        return f"{target.canonical_url}/commit/{sha}"
    return target.canonical_url


def _append_record(
    records: list[dict[str, object]],
    *,
    target: SourceTarget,
    sha: str | None,
    dimension: str,
    evidence_type: str,
    title: str,
    excerpt: str,
    url: str | None = None,
    file_path: str | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
) -> None:
    """Append one bounded provider-neutral evidence record."""
    normalized = _text(excerpt)
    if not normalized or len(records) >= _MAX_PROVIDER_RECORDS:
        return
    locator_type = "line" if file_path and line_start and line_end else "record"
    record: dict[str, object] = {
        "dimension": dimension,
        "evidence_type": evidence_type,
        "evidence_level": "metadata" if evidence_type == "repository_metadata" else "full_text",
        "title": _text(title, evidence_type)[:512],
        "excerpt": normalized,
        "url": url or _github_url(target, sha=sha),
        "locator_type": locator_type,
    }
    if sha:
        record["commit_sha"] = sha
    if file_path:
        record["file_path"] = file_path
    if line_start is not None and line_end is not None:
        record["line_start"] = line_start
        record["line_end"] = line_end
    records.append(record)


def _source_chunks(content: str) -> tuple[tuple[int, int, str], ...]:
    """Split one bounded source file into line-addressable excerpts."""
    lines = content.splitlines()
    chunks: list[tuple[int, int, str]] = []
    for offset in range(0, len(lines), _MAX_SOURCE_LINES):
        selected = lines[offset : offset + _MAX_SOURCE_LINES]
        rendered = "\n".join(
            f"{offset + index + 1} | {line}"
            for index, line in enumerate(selected)
        )
        chunks.append(
            (
                offset + 1,
                offset + len(selected),
                rendered[:_MAX_SOURCE_CHARS],
            )
        )
        if len(chunks) >= 6:
            break
    return tuple(chunks)


def _normalize_context_records(
    target: SourceTarget,
    payload: object,
    *,
    resolved_version: str | None,
) -> tuple[Mapping[str, object], ...]:
    """Normalize GitHub repository context into bounded schema-v2 inputs."""
    records: list[dict[str, object]] = []
    repository = _mapping(payload, "repository")
    metadata_excerpt = "; ".join(
        f"{key}={value}"
        for key, value in repository.items()
        if isinstance(key, str) and value not in (None, "", [], {})
    )
    _append_record(
        records,
        target=target,
        sha=resolved_version,
        dimension="overview",
        evidence_type="repository_metadata",
        title=f"{target.source_id} repository metadata",
        excerpt=metadata_excerpt or f"GitHub repository {target.source_id}",
    )
    license_value = repository.get("license")
    _append_record(
        records,
        target=target,
        sha=resolved_version,
        dimension="license",
        evidence_type="repository_license",
        title=f"{target.source_id} license metadata",
        excerpt=f"license={license_value or 'not declared in repository metadata'}",
        url=f"{_github_url(target, sha=resolved_version)}#license",
    )

    readme = _payload_value(payload, "readme_excerpt", "")
    _append_record(
        records,
        target=target,
        sha=resolved_version,
        dimension="overview",
        evidence_type="readme",
        title=f"{target.source_id} README",
        excerpt=_text(readme),
        url=_github_url(target, sha=resolved_version, path="README.md"),
        file_path="README.md",
    )
    tree = _payload_value(payload, "tree_excerpt", "")
    _append_record(
        records,
        target=target,
        sha=resolved_version,
        dimension="architecture",
        evidence_type="repository_tree",
        title=f"{target.source_id} repository tree",
        excerpt=_text(tree),
    )

    for manifest_item in _items(payload, "file_manifest")[:80]:
        path = manifest_item.get("path")
        if not isinstance(path, str) or not path.strip():
            continue
        _append_record(
            records,
            target=target,
            sha=resolved_version,
            dimension="architecture",
            evidence_type="source_file",
            title=path,
            excerpt=f"File exists in the pinned repository snapshot: {path}",
            url=_github_url(target, sha=resolved_version, path=path),
            file_path=path,
        )

    for file_item in _items(payload, "file_contents")[:12]:
        path = file_item.get("path")
        content = file_item.get("content")
        if not isinstance(path, str) or not path.strip() or not isinstance(content, str):
            continue
        for line_start, line_end, excerpt in _source_chunks(content):
            _append_record(
                records,
                target=target,
                sha=resolved_version,
                dimension="architecture",
                evidence_type="source_code",
                title=f"{path}:{line_start}-{line_end}",
                excerpt=excerpt,
                url=(
                    f"{_github_url(target, sha=resolved_version, path=path)}"
                    f"#L{line_start}-L{line_end}"
                ),
                file_path=path,
                line_start=line_start,
                line_end=line_end,
            )

    for item in _items(payload, "commits")[:20]:
        item_url = item.get("url")
        _append_record(
            records,
            target=target,
            sha=resolved_version,
            dimension="maintenance",
            evidence_type="commit",
            title=str(item.get("message") or item.get("sha") or "commit"),
            excerpt="; ".join(f"{key}={value}" for key, value in item.items()),
            url=item_url if isinstance(item_url, str) else None,
        )
    for item in _items(payload, "releases")[:20]:
        item_url = item.get("url")
        _append_record(
            records,
            target=target,
            sha=resolved_version,
            dimension="maintenance",
            evidence_type="release",
            title=str(item.get("name") or item.get("tag") or "release"),
            excerpt="; ".join(f"{key}={value}" for key, value in item.items()),
            url=item_url if isinstance(item_url, str) else None,
        )
    for field_name, evidence_type in (("issues", "issue"), ("pull_requests", "pull_request")):
        for item in _items(payload, field_name)[:20]:
            item_url = item.get("url")
            _append_record(
                records,
                target=target,
                sha=resolved_version,
                dimension="community",
                evidence_type=evidence_type,
                title=str(item.get("title") or item.get("number") or evidence_type),
                excerpt="; ".join(f"{key}={value}" for key, value in item.items()),
                url=item_url if isinstance(item_url, str) else None,
            )
    return tuple(records)


class GitHubSourceProvider:
    """Adapt existing GitHub repository collection to the provider contract."""

    provider_id = "github"
    supported_modes = frozenset({ResearchMode.GITHUB})

    def __init__(
        self,
        *,
        adapter: object | None = None,
        token: str | None = None,
        base_url: str = "https://api.github.com",
    ) -> None:
        """Store a governed adapter and optional API configuration."""
        self._adapter = adapter or GovernedGitHubAdapter()
        self._token = token
        self._base_url = base_url

    def detect_target(
        self,
        request: SourceRequestSpec,
        context: ProviderContext,
    ) -> DetectionResult:
        """Detect all bounded GitHub repositories named by the topic."""
        del context
        if request.mode is not ResearchMode.GITHUB:
            return DetectionResult(provider_id=self.provider_id, matched=False)
        targets = tuple(
            SourceTarget(
                provider_id=self.provider_id,
                source_kind="repository",
                source_id=target.full_name,
                canonical_url=target.html_url,
                metadata={"owner": target.owner, "repo": target.repo},
            )
            for target in parse_github_repositories(request.topic)
        )
        return DetectionResult(
            provider_id=self.provider_id,
            matched=bool(targets),
            targets=targets,
            confidence=1.0 if targets else 0.0,
        )

    def search(
        self,
        request: SourceSearchRequest,
        context: ProviderContext,
    ) -> SourceSearchResult:
        """Return a stable unsupported-search result for the repository provider."""
        del request, context
        return SourceSearchResult(
            provider_id=self.provider_id,
            notices=("GitHub repository collection is used for this provider.",),
            notice_codes=("github_collection_only",),
        )

    def collect(
        self,
        target: SourceTarget,
        context: ProviderContext,
    ) -> SourceCollection:
        """Collect one repository through the existing governed adapter."""
        context.cancellation.raise_if_cancelled()
        context.budget.reserve(requests=1)
        operation_scope = context.operation_scope
        if operation_scope is None:
            raise ValueError("GitHub provider requires an operation scope.")
        owner = target.metadata.get("owner")
        repo = target.metadata.get("repo")
        if not isinstance(owner, str) or not isinstance(repo, str):
            owner, _, repo = target.source_id.partition("/")
        if not owner or not repo:
            raise ValueError("GitHub source target must contain owner and repo.")
        repository_target = GitHubRepositoryTarget(owner=owner, repo=repo)
        callback = getattr(self._adapter, "collect_repository_context", None)
        if not callable(callback):
            raise TypeError("GitHub adapter does not expose collection.")
        try:
            payload = invoke_with_operation_scope(
                callback,  # type: ignore[arg-type]
                repository_target,
                operation_scope=operation_scope,
                token=self._token,
                base_url=self._base_url,
            )
        except (
            OperationRejectedError,
            CancellationRequestedError,
            DeadlineExceededError,
            BudgetExceededError,
        ):
            raise
        except Exception:
            return SourceCollection(
                provider_id=self.provider_id,
                source_kind=target.source_kind,
                target=target,
                collection_status="failed",
                notices=(_NOTICE_MESSAGE,),
                notice_codes=(_NOTICE_CODE,),
            )
        resolved_version = getattr(payload, "commit_sha", None)
        if not isinstance(resolved_version, str) and isinstance(payload, Mapping):
            candidate = payload.get("commit_sha")
            resolved_version = candidate if isinstance(candidate, str) else None
        if isinstance(resolved_version, str):
            resolved_version = resolved_version.strip() or None
        raw_notices = _payload_value(payload, "notices", ())
        raw_notice_items = (
            tuple(item for item in raw_notices if isinstance(item, str))
            if isinstance(raw_notices, (list, tuple))
            else ()
        )
        raw_notice_codes = _payload_value(payload, "notice_codes", ())
        candidate_codes = (
            tuple(
                item
                for item in raw_notice_codes
                if isinstance(item, str) and item in _KNOWN_NOTICE_MESSAGES
            )
            if isinstance(raw_notice_codes, (list, tuple))
            else ()
        )
        notice_codes = tuple(dict.fromkeys(candidate_codes))
        if raw_notice_items and not notice_codes:
            notice_codes = (_API_NOTICE_CODE,)
        notices = tuple(_KNOWN_NOTICE_MESSAGES[code] for code in notice_codes)
        return SourceCollection(
            provider_id=self.provider_id,
            source_kind=target.source_kind,
            target=target,
            collection_status="complete" if not notices else "partial",
            provider_payload=payload,
            resolved_version=resolved_version,
            records=_normalize_context_records(
                target,
                payload,
                resolved_version=resolved_version,
            ),
            notices=notices,
            notice_codes=notice_codes,
        )

    def enrich(
        self,
        request: EnrichmentRequest,
        context: ProviderContext,
    ) -> SourceCollection:
        """Perform one bounded follow-up collection using explicit hints."""
        if not request.hints:
            raise ValueError("GitHub enrichment hints must not be empty.")
        context.budget.reserve(enrich_passes=1)
        return self.collect(request.target, context)


__all__ = ["GitHubSourceProvider"]
