"""GitHub SourceProvider adapter over the existing governed REST client."""

from __future__ import annotations

from collections.abc import Mapping

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
        return SourceCollection(
            provider_id=self.provider_id,
            source_kind=target.source_kind,
            target=target,
            collection_status="complete",
            provider_payload=payload,
            resolved_version=resolved_version,
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
