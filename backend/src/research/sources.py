"""Typed source-provider contracts, routing, and run-scoped retrieval budgets."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Protocol

from config import Configuration

from .operations import OperationScope
from .profiles import ResearchMode, RetrievalBudget


class CancellationCheckpoint(Protocol):
    """Minimal cancellation surface required by a ProviderContext."""

    def raise_if_cancelled(self) -> None:
        """Raise the run's cancellation or deadline exception when needed."""
        raise NotImplementedError


class BudgetExceededError(RuntimeError):
    """Raised when a provider would exceed its profile retrieval budget."""


class SourceProviderUnavailableError(RuntimeError):
    """Raised when no registered provider can serve a requested source mode."""


@dataclass(frozen=True, kw_only=True)
class SourceRequestSpec:
    """Stable input used by provider detection and routing."""

    topic: str
    mode: ResearchMode
    profile_id: str

    def __post_init__(self) -> None:
        """Validate the request fields at the router boundary."""
        if not isinstance(self.topic, str) or not self.topic.strip():
            raise ValueError("Source request topic must not be empty.")
        if not isinstance(self.profile_id, str) or not self.profile_id.strip():
            raise ValueError("Source request profile ID must not be empty.")
        try:
            normalized_mode = ResearchMode(self.mode)
        except (TypeError, ValueError) as exc:
            raise ValueError("Source request mode is unsupported.") from exc
        object.__setattr__(self, "topic", self.topic.strip())
        object.__setattr__(self, "profile_id", self.profile_id.strip())
        object.__setattr__(self, "mode", normalized_mode)


@dataclass(frozen=True, kw_only=True)
class SourceTarget:
    """Provider-neutral address of one source target."""

    provider_id: str
    source_kind: str
    source_id: str
    canonical_url: str
    requested_ref: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate bounded source identity fields."""
        for field_name in ("provider_id", "source_kind", "source_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Source target {field_name} must not be empty.")
            object.__setattr__(self, field_name, value.strip())
        if not isinstance(self.canonical_url, str):
            raise TypeError("Source target canonical_url must be text.")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("Source target metadata must be a mapping.")
        object.__setattr__(self, "metadata", dict(self.metadata))


@dataclass(frozen=True, kw_only=True)
class DetectionResult:
    """Safe provider target-detection output."""

    provider_id: str
    matched: bool
    targets: tuple[SourceTarget, ...] = ()
    confidence: float = 0.0
    notices: tuple[str, ...] = ()
    notice_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Normalize detection sequences and confidence bounds."""
        if not isinstance(self.matched, bool):
            raise TypeError("Detection matched flag must be boolean.")
        if not isinstance(self.confidence, (int, float)) or isinstance(self.confidence, bool):
            raise TypeError("Detection confidence must be numeric.")
        confidence = max(0.0, min(1.0, float(self.confidence)))
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "targets", tuple(self.targets))
        object.__setattr__(self, "notices", tuple(item for item in self.notices if isinstance(item, str)))
        object.__setattr__(self, "notice_codes", tuple(item for item in self.notice_codes if isinstance(item, str)))
        if self.matched and not self.targets:
            raise ValueError("A matched detection must contain at least one target.")


@dataclass(frozen=True, kw_only=True)
class SourceSearchRequest:
    """Typed search request shared by Web and future Paper providers."""

    query: str
    topic: str
    config: Configuration
    loop_count: int = 0
    use_cache: bool = False

    def __post_init__(self) -> None:
        """Validate query and retry-loop inputs."""
        if not isinstance(self.query, str) or not self.query.strip():
            raise ValueError("Source search query must not be empty.")
        if not isinstance(self.topic, str) or not self.topic.strip():
            raise ValueError("Source search topic must not be empty.")
        if not isinstance(self.config, Configuration):
            raise TypeError("Source search config must be Configuration.")
        if not isinstance(self.loop_count, int) or isinstance(self.loop_count, bool) or self.loop_count < 0:
            raise ValueError("Source search loop_count must be non-negative.")
        if not isinstance(self.use_cache, bool):
            raise TypeError("Source search use_cache flag must be boolean.")
        object.__setattr__(self, "query", self.query.strip())
        object.__setattr__(self, "topic", self.topic.strip())


@dataclass(frozen=True, kw_only=True)
class SourceSearchResult:
    """Bounded, provider-neutral search result."""

    provider_id: str
    results: tuple[Mapping[str, Any], ...] = ()
    answer: str | None = None
    backend: str = "none"
    notices: tuple[str, ...] = ()
    notice_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Detach mappings and retain only textual notice fields."""
        object.__setattr__(self, "results", tuple(dict(item) for item in self.results if isinstance(item, Mapping)))
        object.__setattr__(self, "notices", tuple(item for item in self.notices if isinstance(item, str)))
        object.__setattr__(self, "notice_codes", tuple(item for item in self.notice_codes if isinstance(item, str)))


@dataclass(frozen=True, kw_only=True)
class EnrichmentRequest:
    """Hints for a bounded second provider pass."""

    target: SourceTarget
    hints: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class SourceCollection:
    """Typed provider collection result before Evidence normalization."""

    provider_id: str
    source_kind: str
    target: SourceTarget
    collection_status: str = "partial"
    provider_payload: object | None = None
    records: tuple[Mapping[str, Any], ...] = ()
    resolved_version: str | None = None
    captured_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    notices: tuple[str, ...] = ()
    notice_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Detach bounded mapping records and stable notice fields."""
        object.__setattr__(self, "records", tuple(dict(item) for item in self.records if isinstance(item, Mapping)))
        object.__setattr__(self, "notices", tuple(item for item in self.notices if isinstance(item, str)))
        object.__setattr__(self, "notice_codes", tuple(item for item in self.notice_codes if isinstance(item, str)))


@dataclass(frozen=True, kw_only=True)
class ProviderContext:
    """Run-bound, non-persistent dependencies passed to every provider call."""

    run_id: str
    profile_id: str
    mode: ResearchMode
    operation_scope: OperationScope | None
    cancellation: CancellationCheckpoint
    budget: RetrievalBudgetTracker

    def __post_init__(self) -> None:
        """Validate the immutable identity portion of provider context."""
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("Provider context run ID must not be empty.")
        if not isinstance(self.profile_id, str) or not self.profile_id.strip():
            raise ValueError("Provider context profile ID must not be empty.")
        object.__setattr__(self, "mode", ResearchMode(self.mode))


class RetrievalBudgetTracker:
    """Thread-safe counters enforcing one profile's retrieval budget."""

    _FIELDS = ("requests", "results", "evidence", "enrich_passes")

    def __init__(self, budget: RetrievalBudget) -> None:
        """Initialize counters for one immutable profile budget."""
        if not isinstance(budget, RetrievalBudget):
            raise TypeError("Budget tracker requires a RetrievalBudget.")
        self._budget = budget
        self._used = {field_name: 0 for field_name in self._FIELDS}
        self._lock = RLock()

    def reserve(
        self,
        *,
        requests: int = 0,
        results: int = 0,
        evidence: int = 0,
        enrich_passes: int = 0,
    ) -> None:
        """Atomically reserve non-negative units or change nothing on overflow."""
        increments = {
            "requests": requests,
            "results": results,
            "evidence": evidence,
            "enrich_passes": enrich_passes,
        }
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in increments.values()
        ):
            raise ValueError("Budget reservations must be non-negative integers.")
        limits = {
            "requests": self._budget.max_requests,
            "results": self._budget.max_results,
            "evidence": self._budget.max_evidence,
            "enrich_passes": self._budget.max_enrich_passes,
        }
        with self._lock:
            if any(
                self._used[field_name] + increments[field_name] > limits[field_name]
                for field_name in self._FIELDS
            ):
                raise BudgetExceededError("Research retrieval budget exceeded.")
            for field_name in self._FIELDS:
                self._used[field_name] += increments[field_name]

    def snapshot(self) -> dict[str, int]:
        """Return detached usage counters suitable for metrics."""
        with self._lock:
            return dict(self._used)

    def remaining(self) -> dict[str, int]:
        """Return the currently available units in every budget dimension."""
        limits = {
            "requests": self._budget.max_requests,
            "results": self._budget.max_results,
            "evidence": self._budget.max_evidence,
            "enrich_passes": self._budget.max_enrich_passes,
        }
        with self._lock:
            return {
                field_name: limits[field_name] - self._used[field_name]
                for field_name in self._FIELDS
            }


class SourceProvider(Protocol):
    """Typed source provider boundary used by the future research kernel."""

    provider_id: str
    supported_modes: frozenset[ResearchMode]

    def detect_target(
        self,
        request: SourceRequestSpec,
        context: ProviderContext,
    ) -> DetectionResult:
        """Detect provider-specific targets without performing collection."""
        raise NotImplementedError

    def search(
        self,
        request: SourceSearchRequest,
        context: ProviderContext,
    ) -> SourceSearchResult:
        """Search the provider under the supplied run scope and budget."""
        raise NotImplementedError

    def collect(
        self,
        target: SourceTarget,
        context: ProviderContext,
    ) -> SourceCollection:
        """Collect one bounded target."""
        raise NotImplementedError

    def enrich(
        self,
        request: EnrichmentRequest,
        context: ProviderContext,
    ) -> SourceCollection:
        """Perform one bounded enrichment pass."""
        raise NotImplementedError


class SourceProviderRegistry:
    """Explicit process-local registry with immutable provider identities."""

    def __init__(self, providers: Sequence[SourceProvider] = ()) -> None:
        """Register an optional initial provider list."""
        self._providers: dict[str, SourceProvider] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: SourceProvider) -> None:
        """Add one provider and reject duplicate or malformed identities."""
        provider_id = getattr(provider, "provider_id", None)
        supported_modes = getattr(provider, "supported_modes", None)
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise ValueError("Source provider ID must not be empty.")
        if not isinstance(supported_modes, (set, frozenset, tuple, list)):
            raise TypeError("Source provider supported_modes must be a collection.")
        normalized_modes = frozenset(ResearchMode(mode) for mode in supported_modes)
        if not normalized_modes:
            raise ValueError("Source provider must support at least one mode.")
        if provider_id in self._providers:
            raise ValueError(f"Source provider {provider_id!r} is already registered.")
        self._providers[provider_id] = provider

    def get(self, provider_id: str) -> SourceProvider:
        """Return one provider or raise a stable lookup error."""
        try:
            return self._providers[provider_id]
        except (KeyError, TypeError) as exc:
            raise KeyError(f"Unknown source provider: {provider_id!r}.") from exc

    def all(self) -> tuple[SourceProvider, ...]:
        """Return providers in deterministic registration order."""
        return tuple(self._providers.values())


class SourceRouter:
    """Route detection through a Profile's provider priority list."""

    def __init__(self, registry: SourceProviderRegistry) -> None:
        """Store the explicit provider registry without Run state."""
        self._registry = registry

    def route(
        self,
        request: SourceRequestSpec,
        *,
        source_priority: Sequence[str],
        context: ProviderContext,
    ) -> tuple[SourceProvider, DetectionResult]:
        """Return the first matching provider or a stable unavailable error."""
        attempted = False
        for provider_id in source_priority:
            try:
                provider = self._registry.get(provider_id)
            except KeyError:
                continue
            if request.mode not in provider.supported_modes:
                continue
            attempted = True
            detection = provider.detect_target(request, context)
            if detection.matched:
                return provider, detection
        if attempted:
            raise SourceProviderUnavailableError(
                f"No source provider matched research mode {request.mode.value!r}."
            )
        raise SourceProviderUnavailableError(
            f"No source provider is registered for research mode {request.mode.value!r}."
        )


__all__ = [
    "BudgetExceededError",
    "CancellationCheckpoint",
    "DetectionResult",
    "EnrichmentRequest",
    "ProviderContext",
    "RetrievalBudgetTracker",
    "SourceCollection",
    "SourceProvider",
    "SourceProviderRegistry",
    "SourceProviderUnavailableError",
    "SourceRequestSpec",
    "SourceRouter",
    "SourceSearchRequest",
    "SourceSearchResult",
    "SourceTarget",
]
