"""Run-scoped research kernel that composes profiles, providers, and the gate."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from threading import RLock
from typing import Any

from config import Configuration

from .evidence_normalization import normalize_collections
from .intelligence import (
    ClaimRecord,
    CoverageDecision,
    EvidenceLocator,
    EvidenceRecord,
    GenericReportSpec,
    ResearchIntelligenceBundle,
    SourceReference,
    stable_claim_id,
    stable_evidence_id,
)
from .operations import OperationScope
from .profiles import (
    RenderedResearchTask,
    ResearchMode,
    ResearchProfile,
    ResearchProfileRegistry,
    built_in_profile_registry,
)
from .providers import GitHubSourceProvider, WebSourceProvider
from .quality import EvidenceQualityGate
from .sources import (
    BudgetExceededError,
    DetectionResult,
    EnrichmentRequest,
    ProviderContext,
    RetrievalBudgetTracker,
    SourceCollection,
    SourceProvider,
    SourceProviderRegistry,
    SourceRequestSpec,
    SourceRouter,
    SourceSearchRequest,
    SourceSearchResult,
    SourceTarget,
)
from .web_evidence import RunWebEvidence

_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_CLAIM_SPLIT_RE = re.compile(r"(?<=[.!?\u3002\uff01\uff1f])\s+(?!\s*\[)|[\r\n]+")
_MARKDOWN_PREFIX_RE = re.compile(r"^\s*(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+)")


class _NeverCancelled:
    """Default cancellation surface for direct kernel use."""

    def raise_if_cancelled(self) -> None:
        """Allow the current provider call."""


@dataclass(frozen=True, kw_only=True)
class TaskEvidenceBinding:
    """Immutable evidence snapshot bound to one task attempt and dimension."""

    task_id: int
    task_attempt: int
    dimension: str
    query: str
    evidence: tuple[EvidenceRecord, ...] = ()

    def __post_init__(self) -> None:
        """Validate the run-local binding identity and detach evidence."""
        for name in ("task_id", "task_attempt"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer.")
        for name in ("dimension", "query"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must not be empty.")
            object.__setattr__(self, name, value.strip())
        object.__setattr__(self, "evidence", tuple(self.evidence))

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        """Return the stable IDs visible to this attempt."""
        return tuple(item.evidence_id for item in self.evidence)


@dataclass(frozen=True, kw_only=True)
class PreparedResearch:
    """Provider and profile inputs prepared inside an existing Run."""

    request: SourceRequestSpec
    profile: ResearchProfile
    provider: SourceProvider
    detection: DetectionResult
    targets: tuple[SourceTarget, ...]
    collections: tuple[SourceCollection, ...]
    tasks: tuple[RenderedResearchTask, ...]
    provider_context: ProviderContext
    source_context: Mapping[str, Any] = field(default_factory=dict)
    web_evidence: RunWebEvidence = field(default_factory=RunWebEvidence, compare=False, repr=False)
    _binding_lock: RLock = field(default_factory=RLock, compare=False, repr=False)
    _task_bindings: dict[tuple[int, int], TaskEvidenceBinding] = field(
        default_factory=dict, compare=False, repr=False
    )
    _accepted_attempts: dict[int, int] = field(
        default_factory=dict, compare=False, repr=False
    )
    _attempt_highwater: dict[int, int] = field(default_factory=dict, compare=False, repr=False)
    _admitted_evidence: dict[str, EvidenceRecord] = field(
        default_factory=dict, compare=False, repr=False
    )
    _enrichment_limitations: list[str] = field(
        default_factory=list, compare=False, repr=False
    )

    def bind(
        self,
        *,
        task_id: int,
        task_attempt: int,
        dimension: str,
        query: str,
        evidence: Sequence[EvidenceRecord],
    ) -> TaskEvidenceBinding:
        """Bind one immutable candidate set and admit budgeted records once."""
        binding = TaskEvidenceBinding(
            task_id=task_id,
            task_attempt=task_attempt,
            dimension=dimension,
            query=query,
            evidence=tuple(evidence),
        )
        retained: list[EvidenceRecord] = []
        with self._binding_lock:
            for record in binding.evidence:
                if record.evidence_id in self._admitted_evidence:
                    retained.append(self._admitted_evidence[record.evidence_id])
                    continue
                # Web paragraph admission is already charged atomically by
                # RunWebEvidence.read.  Initial GitHub records are candidates
                # and charge only when first admitted to a task binding.
                if record.source.provider_id != "web":
                    try:
                        self.provider_context.budget.reserve(evidence=1)
                    except BudgetExceededError:
                        continue
                self._admitted_evidence[record.evidence_id] = record
                retained.append(record)
            binding = replace(binding, evidence=tuple(retained))
            self._task_bindings[(task_id, task_attempt)] = binding
        return binding

    def export_recovery_state(self) -> dict[str, Any]:
        """Copy one consistent ledger without holding locks during artifact IO."""
        from .evidence_recovery import _budget_limits, profile_fingerprint

        with self._binding_lock, self.web_evidence._lock, self.provider_context.budget._lock:
            return {
                "run_id": self.provider_context.run_id,
                "profile_id": self.profile.profile_id,
                "version": self.profile.version,
                "profile_fingerprint": profile_fingerprint(self.profile),
                "mode": self.profile.mode.value,
                "provider_id": self.provider.provider_id,
                "request": self.request,
                "detection": self.detection,
                "targets": self.targets,
                "tasks": self.tasks,
                "initial_collections": self.collections,
                "budget_limits": _budget_limits(self.profile.retrieval_budget),
                "budget": self.provider_context.budget.snapshot(),
                "web_captures": tuple(self.web_evidence._captures.items()),
                "selected_web_collections": tuple(self.web_evidence._collections.values()),
                "web_record_keys": tuple(sorted(self.web_evidence._record_keys)),
                "admitted_records": [record.as_dict() for record in self._admitted_evidence.values()],
                "task_attempt_bindings": [
                    {"task_id": b.task_id, "task_attempt": b.task_attempt,
                     "dimension": b.dimension, "query": b.query,
                     "evidence_ids": list(b.evidence_ids)}
                    for _, b in sorted(self._task_bindings.items())
                ],
                "accepted_attempts": {str(k): v for k, v in self._accepted_attempts.items()},
                "attempt_highwater": {
                    str(task): max(self._attempt_highwater.get(task, 0), *(attempt for t, attempt in self._task_bindings if t == task), 0)
                    for task in set(self._attempt_highwater) | {t for t, _ in self._task_bindings}
                },
                "enrichment_limitations": tuple(self._enrichment_limitations),
            }

    def start_attempt(self, task_id: int, attempt: int) -> None:
        """Record an attempt before work starts so saved in-flight work is charged."""
        with self._binding_lock:
            self._attempt_highwater[task_id] = max(attempt, self._attempt_highwater.get(task_id, 0))

    def attempt_high_water(self, task_id: int) -> int:
        """Return the saved attempt counter for one task."""
        with self._binding_lock:
            return self._attempt_highwater.get(task_id, 0)

    def accept(self, *, task_id: int, task_attempt: int) -> TaskEvidenceBinding | None:
        """Accept a completed binding on the coordinator thread."""
        with self._binding_lock:
            binding = self._task_bindings.get((task_id, task_attempt))
            if binding is None:
                return None
            prior = self._accepted_attempts.get(task_id)
            if prior is None or task_attempt >= prior:
                self._accepted_attempts[task_id] = task_attempt
            accepted_attempt = self._accepted_attempts.get(task_id)
            return (
                self._task_bindings.get((task_id, accepted_attempt))
                if accepted_attempt is not None
                else binding
            )

    def read(
        self,
        *,
        task_id: int,
        task_attempt: int | None = None,
    ) -> TaskEvidenceBinding | None:
        """Read one binding, preferring its accepted attempt when omitted."""
        with self._binding_lock:
            attempt = task_attempt
            if attempt is None:
                attempt = self._accepted_attempts.get(task_id)
            if attempt is not None:
                return self._task_bindings.get((task_id, attempt))
            candidates = [
                binding
                for (bound_task, _), binding in self._task_bindings.items()
                if bound_task == task_id
            ]
            return max(candidates, key=lambda item: item.task_attempt) if candidates else None

    def read_accepted(self, *, task_id: int) -> TaskEvidenceBinding | None:
        """Read only the attempt accepted after coordinator completion."""
        with self._binding_lock:
            attempt = self._accepted_attempts.get(task_id)
            return (
                self._task_bindings.get((task_id, attempt))
                if attempt is not None
                else None
            )

    def accepted_bindings(self) -> tuple[TaskEvidenceBinding, ...]:
        """Return accepted task bindings in deterministic task order."""
        with self._binding_lock:
            values = []
            for task_id, attempt in self._accepted_attempts.items():
                binding = self._task_bindings.get((task_id, attempt))
                if binding is not None:
                    values.append(binding)
            return tuple(sorted(values, key=lambda item: item.task_id))

    def admitted_records(self) -> tuple[EvidenceRecord, ...]:
        """Return all budget-admitted records without exposing the runtime dict."""
        with self._binding_lock:
            return tuple(self._admitted_evidence.values())

    def admit_records(
        self,
        records: Sequence[EvidenceRecord],
    ) -> tuple[EvidenceRecord, ...]:
        """Admit remaining final candidates under the run budget exactly once."""
        retained: list[EvidenceRecord] = []
        with self._binding_lock:
            for record in records:
                prior = self._admitted_evidence.get(record.evidence_id)
                if prior is not None:
                    retained.append(prior)
                    continue
                if record.source.provider_id != "web":
                    try:
                        self.provider_context.budget.reserve(evidence=1)
                    except BudgetExceededError:
                        continue
                self._admitted_evidence[record.evidence_id] = record
                retained.append(record)
        return tuple(retained)

    def add_enrichment_limitation(self, message: str) -> None:
        """Record a bounded explanation for rejected provider enrichment."""
        if not isinstance(message, str) or not message.strip():
            return
        with self._binding_lock:
            if message not in self._enrichment_limitations:
                self._enrichment_limitations.append(message)

    def enrichment_limitations(self) -> tuple[str, ...]:
        """Return immutable enrichment limitations for bundle construction."""
        with self._binding_lock:
            return tuple(self._enrichment_limitations)

    # Explicit aliases keep the seam readable to callers while retaining the
    # short bind/accept/read vocabulary used by the run-local coordinator.
    bind_task_evidence = bind
    accept_task_evidence = accept
    read_task_evidence = read
    read_accepted_task_evidence = read_accepted


def _now_iso() -> str:
    """Return one capture timestamp for normalized records."""
    return datetime.now(timezone.utc).isoformat()


def _hash(value: object) -> str:
    """Build a deterministic content hash for bounded provider records."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _text(value: object, fallback: str) -> str:
    """Normalize one provider field without exposing arbitrary objects."""
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def _safe_attributes(value: Mapping[str, Any]) -> dict[str, Any]:
    """Detach only JSON-compatible provider attributes."""
    try:
        detached = json.loads(json.dumps(dict(value), ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return {}
    return detached if isinstance(detached, dict) else {}


def _canonical_excerpt_limit(profile: ResearchProfile) -> int:
    """Use one bounded excerpt size shared by IDs, summaries, and judges."""
    return min(profile.retrieval_budget.max_excerpt_chars, 2000)


def _candidate_terms(text: str) -> set[str]:
    """Tokenize English and CJK intent text for deterministic local selection."""
    words = set(re.findall(r"[a-z0-9][a-z0-9_.+-]+", text.casefold()))
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        words.update(run[index : index + 2] for index in range(len(run) - 1))
    return words - {"the", "and", "for", "with", "what", "how", "this", "that"}


def _record_dimension(record: EvidenceRecord) -> str:
    """Read a provider dimension without mutating the canonical record."""
    value = record.attributes.get("dimension")
    return value.strip() if isinstance(value, str) and value.strip() else "overview"


def _rank_task_records(
    records: Sequence[EvidenceRecord],
    *,
    query: str,
    intent: str,
    dimension: str,
) -> list[EvidenceRecord]:
    """Rank candidates by dimension and lexical intent with stable tie breaks."""
    expected = _candidate_terms(f"{query} {intent}")

    def score(index_and_record: tuple[int, EvidenceRecord]) -> tuple[float, int, int, str]:
        index, record = index_and_record
        observed = _candidate_terms(f"{record.title} {record.excerpt}")
        dimension_score = 3 if _record_dimension(record) == dimension else 0
        overlap = len(expected & observed)
        level = 1 if record.evidence_level == "full_text" else 0
        return (dimension_score + overlap, level, -index, record.evidence_id)

    return [
        record
        for _, record in sorted(
            enumerate(records), key=score, reverse=True
        )
    ]


def _merge_task_records(
    records: Sequence[EvidenceRecord],
    *,
    query: str,
    intent: str,
    dimension: str,
    limit: int = 12,
) -> tuple[EvidenceRecord, ...]:
    """Keep relevant candidates from each source family before filling slots."""
    if limit <= 0:
        return ()
    ranked = _rank_task_records(
        records, query=query, intent=intent, dimension=dimension
    )
    by_provider: dict[str, list[EvidenceRecord]] = {}
    for record in ranked:
        by_provider.setdefault(record.source.provider_id, []).append(record)
    providers = sorted(by_provider)
    selected: list[EvidenceRecord] = []
    seen: set[str] = set()
    if len(providers) > 1:
        # Reserve a deterministic share for every available provider family so
        # a large repository collection cannot permanently starve Web evidence.
        share = max(1, limit // len(providers))
        for offset in range(share):
            for provider_id in providers:
                family = by_provider[provider_id]
                if offset >= len(family):
                    continue
                record = family[offset]
                if record.evidence_id not in seen:
                    selected.append(record)
                    seen.add(record.evidence_id)
    for record in ranked:
        if len(selected) >= limit:
            break
        if record.evidence_id not in seen:
            selected.append(record)
            seen.add(record.evidence_id)
    return tuple(selected[:limit])


class ResearchKernel:
    """Coordinate research preparation/finalization without owning Run state."""

    def __init__(
        self,
        *,
        profile_registry: ResearchProfileRegistry | None = None,
        provider_registry: SourceProviderRegistry | None = None,
        quality_gate: EvidenceQualityGate | None = None,
    ) -> None:
        """Store explicit registries; defaults remain process-local and immutable."""
        self.profile_registry = profile_registry or built_in_profile_registry()
        self.provider_registry = provider_registry or SourceProviderRegistry(
            (GitHubSourceProvider(), WebSourceProvider())
        )
        self.router = SourceRouter(self.provider_registry)
        self.quality_gate = quality_gate or EvidenceQualityGate()

    def restore_prepared(self, payload, *, run_id, cancellation, operation_scope, artifact_store):
        """Rebuild the saved runtime using fresh providers without collection."""
        from .evidence_recovery import EvidenceRecoveryError, restore_prepared_research

        try:
            return restore_prepared_research(
                self, payload, run_id=run_id, cancellation=cancellation,
                operation_scope=operation_scope, artifact_store=artifact_store,
            )
        except EvidenceRecoveryError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise EvidenceRecoveryError("evidence_recovery_invalid", "Saved evidence state is malformed.") from exc

    def prepare(
        self,
        topic_or_request: str | SourceRequestSpec,
        *,
        mode: ResearchMode | str | None = None,
        profile_id: str | None = None,
        run_id: str = "kernel-run",
        config: Configuration | None = None,
        cancellation: Any | None = None,
        operation_scope: OperationScope | None = None,
        repository: str | None = None,
        comparison_repositories: Sequence[str] = (),
    ) -> PreparedResearch:
        """Resolve a profile, route/detect a provider, and collect initial targets."""
        if isinstance(topic_or_request, SourceRequestSpec):
            request = topic_or_request
            profile = self.profile_registry.resolve(
                profile_id=request.profile_id,
                mode=request.mode,
            )
        else:
            if not isinstance(topic_or_request, str) or not topic_or_request.strip():
                raise ValueError("Research kernel topic must not be empty.")
            profile = self.profile_registry.resolve(profile_id=profile_id, mode=mode)
            request = SourceRequestSpec(
                topic=topic_or_request,
                mode=profile.mode,
                profile_id=profile.profile_id,
            )
        tracker = RetrievalBudgetTracker(profile.retrieval_budget)
        provider_context = ProviderContext(
            run_id=run_id,
            profile_id=profile.profile_id,
            mode=profile.mode,
            operation_scope=operation_scope,
            cancellation=cancellation or _NeverCancelled(),
            budget=tracker,
        )
        provider, detection = self.router.route(
            request,
            source_priority=profile.source_priority,
            context=provider_context,
        )
        collections: list[SourceCollection] = []
        for target in detection.targets:
            collections.append(provider.collect(target, provider_context))
        detected_repositories = tuple(
            target.source_id
            for target in detection.targets
            if target.source_id
        )
        rendered_tasks = profile.render_tasks(
            repository=repository or (detected_repositories[0] if detected_repositories else None),
            comparison_repositories=(
                tuple(comparison_repositories)
                or detected_repositories[1:]
            ),
        )
        source_context = {
            "provider_ids": [provider.provider_id],
            "targets": [
                {
                    "provider_id": target.provider_id,
                    "source_kind": target.source_kind,
                    "source_id": target.source_id,
                    "canonical_url": target.canonical_url,
                }
                for target in detection.targets
            ],
            "collection_count": len(collections),
            "captured_at": _now_iso(),
        }
        return PreparedResearch(
            request=request,
            profile=profile,
            provider=provider,
            detection=detection,
            targets=detection.targets,
            collections=tuple(collections),
            tasks=tuple(rendered_tasks),
            provider_context=provider_context,
            source_context=source_context,
        )

    def finalize(
        self,
        prepared: PreparedResearch,
        *,
        task_results: Sequence[object] = (),
        collections: Sequence[SourceCollection] = (),
        allow_enrichment: bool = True,
        freeze: bool = True,
    ) -> ResearchIntelligenceBundle:
        """Normalize provider/task outputs, gate coverage, and freeze on success."""
        if not isinstance(prepared, PreparedResearch):
            raise TypeError("Research kernel finalize requires PreparedResearch.")
        all_collections = list(prepared.collections)
        all_collections.extend(item for item in collections if isinstance(item, SourceCollection))
        task_collections, search_results, task_text = self._split_task_results(task_results)
        all_collections.extend(task_collections)
        bundle = self._build_bundle(
            prepared,
            collections=all_collections,
            search_results=search_results,
            task_text=task_text,
            retry_count=0,
        )
        decision = self.quality_gate.evaluate(bundle, prepared.profile)
        if allow_enrichment and not decision.allow_report and decision.gap_queries and prepared.targets:
            try:
                enrichment = prepared.provider.enrich(
                    EnrichmentRequest(
                        target=prepared.targets[0],
                        hints=decision.gap_queries,
                    ),
                    prepared.provider_context,
                )
            except (BudgetExceededError, ValueError):
                enrichment = None
            if enrichment is not None:
                source_key = (enrichment.provider_id, enrichment.target.source_id)
                initial_versions = {
                    (item.provider_id, item.target.source_id): item.resolved_version
                    for item in prepared.collections
                }
                initial_version = initial_versions.get(source_key)
                if (
                    source_key in initial_versions
                    and initial_version != enrichment.resolved_version
                ):
                    prepared.add_enrichment_limitation(
                        "Enrichment rejected: source version changed from "
                        f"{initial_version or 'unknown'} to "
                        f"{enrichment.resolved_version or 'unknown'} for "
                        f"{source_key[0]}:{source_key[1]}."
                    )
                else:
                    all_collections.append(enrichment)
                bundle = self._build_bundle(
                    prepared,
                    collections=all_collections,
                    search_results=search_results,
                    task_text=task_text,
                    retry_count=1,
                )
                decision = self.quality_gate.evaluate(bundle, prepared.profile)
        if freeze:
            # Candidate records only enter the run budget on the final path;
            # baseline construction stays a pure build/evaluate operation.
            bundle = self._build_bundle(
                prepared,
                collections=all_collections,
                search_results=search_results,
                task_text=task_text,
                retry_count=bundle.coverage.retry_count,
                admit_candidates=True,
            )
            decision = self.quality_gate.evaluate(bundle, prepared.profile)
        limitations = prepared.enrichment_limitations()
        if limitations:
            bundle = replace(
                bundle,
                report_spec=replace(
                    bundle.report_spec,
                    limitations=tuple(
                        dict.fromkeys((*bundle.report_spec.limitations, *limitations))
                    ),
                ),
            )
        bundle = replace(bundle, coverage=decision)
        if freeze and decision.allow_report:
            bundle = replace(bundle, evidence_frozen=True)
        return bundle

    def collect_search_evidence(
        self,
        prepared: PreparedResearch,
        search_results: Sequence[SourceSearchResult],
    ) -> tuple[SourceCollection, ...]:
        """Capture search results through the Web provider without persisting bodies."""
        if not isinstance(prepared, PreparedResearch):
            raise TypeError("Search evidence collection requires PreparedResearch.")
        try:
            provider = self.provider_registry.get("web")
        except KeyError:
            return ()
        if not isinstance(provider, WebSourceProvider):
            return ()
        collections: list[SourceCollection] = []
        for search in search_results:
            for result in search.results:
                dimension = _text(result.get("dimension"), "overview")
                collections.append(
                    provider.collect_search_result(
                        result,
                        prepared.provider_context,
                        dimension=dimension,
                    )
                )
        return tuple(collections)

    def collect_task_evidence(
        self,
        prepared: PreparedResearch,
        results: Sequence[Mapping[str, Any]],
        *,
        intent: str,
        dimension: str,
        operation_scope: OperationScope | None,
    ) -> tuple[SourceCollection, ...]:
        """Read task evidence before summarization using the run-local ledger."""
        provider = self.provider_registry.get("web")
        if not isinstance(provider, WebSourceProvider):
            raise TypeError("Task Web evidence requires WebSourceProvider.")
        context = replace(prepared.provider_context, operation_scope=operation_scope)
        return prepared.web_evidence.read(
            provider, results, context, intent=intent, dimension=dimension,
            max_excerpt_chars=_canonical_excerpt_limit(prepared.profile),
        )

    def bind_task_evidence(
        self,
        prepared: PreparedResearch,
        *,
        task_id: int,
        task_attempt: int,
        query: str,
        intent: str,
        dimension: str,
        search_results: Sequence[Mapping[str, Any]] = (),
        operation_scope: OperationScope | None = None,
    ) -> TaskEvidenceBinding:
        """Select, admit, and bind repository plus Web evidence for one attempt."""
        if not isinstance(prepared, PreparedResearch):
            raise TypeError("Task evidence binding requires PreparedResearch.")
        canonical_limit = _canonical_excerpt_limit(prepared.profile)
        candidates = list(
            normalize_collections(prepared.collections, canonical_limit)
        )
        web_collections: tuple[SourceCollection, ...]
        if search_results:
            try:
                self.provider_registry.get("web")
            except KeyError:
                web_collections = ()
            else:
                try:
                    web_collections = self.collect_task_evidence(
                        prepared,
                        search_results,
                        intent=f"{query} {intent}",
                        dimension=dimension,
                        operation_scope=operation_scope or prepared.provider_context.operation_scope,
                    )
                except BudgetExceededError:
                    # Repository evidence remains usable when Web capture is
                    # exhausted; cancellation/control and implementation
                    # errors remain visible to the coordinator.
                    web_collections = ()
            candidates.extend(normalize_collections(web_collections, canonical_limit))
        selected = _merge_task_records(
            candidates,
            query=query,
            intent=intent,
            dimension=dimension,
            limit=12,
        )
        return prepared.bind(
            task_id=task_id,
            task_attempt=task_attempt,
            dimension=dimension,
            query=query,
            evidence=selected,
        )

    def search(
        self,
        prepared: PreparedResearch,
        *,
        query: str,
        topic: str,
        config: Configuration,
        loop_count: int = 0,
        use_cache: bool = False,
        operation_scope: OperationScope | None = None,
    ) -> SourceSearchResult:
        """Run one task search through the prepared profile's provider route."""
        if not isinstance(prepared, PreparedResearch):
            raise TypeError("Research kernel search requires PreparedResearch.")
        request = SourceSearchRequest(
            query=query,
            topic=topic,
            config=(
                config.model_copy(update={"fetch_full_page": False})
                if prepared.profile.profile_id == "web.evidence.v1"
                else config
            ),
            loop_count=loop_count,
            use_cache=use_cache,
        )
        fallback: SourceSearchResult | None = None
        for provider_id in prepared.profile.source_priority:
            try:
                provider = self.provider_registry.get(provider_id)
            except KeyError:
                continue
            if prepared.profile.mode not in provider.supported_modes:
                continue
            context = replace(
                prepared.provider_context,
                operation_scope=operation_scope or prepared.provider_context.operation_scope,
            )
            result = provider.search(request, context)
            if result.results or result.answer:
                return result
            fallback = result
        return fallback or SourceSearchResult(
            provider_id=prepared.provider.provider_id,
            notices=("No configured source provider returned search results.",),
            notice_codes=("source_search_empty",),
        )

    @staticmethod
    def _split_task_results(
        task_results: Sequence[object],
    ) -> tuple[
        list[SourceCollection],
        list[SourceSearchResult],
        list[tuple[Any, ...]],
    ]:
        """Separate provider collections, search results, and legacy text summaries."""
        collections: list[SourceCollection] = []
        search_results: list[SourceSearchResult] = []
        text_results: list[tuple[Any, ...]] = []
        for result in task_results:
            if isinstance(result, SourceCollection):
                collections.append(result)
            elif isinstance(result, SourceSearchResult):
                search_results.append(result)
            elif isinstance(result, str) and result.strip():
                text_results.append(("legacy", "overview", result.strip()))
            elif isinstance(result, Mapping):
                raw_results = result.get("results")
                if isinstance(raw_results, (list, tuple)):
                    search_results.append(
                        SourceSearchResult(
                            provider_id=str(result.get("provider_id") or "web"),
                            results=tuple(
                                item for item in raw_results if isinstance(item, Mapping)
                            ),
                            answer=result.get("answer") if isinstance(result.get("answer"), str) else None,
                            backend=str(result.get("backend") or "none"),
                        )
                    )
                elif isinstance(result.get("summary"), str):
                    summary = str(result["summary"]).strip()
                    raw_task_id = result.get("task_id")
                    raw_attempt = result.get("attempt", result.get("task_attempt"))
                    if (
                        isinstance(raw_task_id, int)
                        and not isinstance(raw_task_id, bool)
                        and raw_task_id > 0
                        and isinstance(raw_attempt, int)
                        and not isinstance(raw_attempt, bool)
                        and raw_attempt > 0
                    ):
                        text_results.append(
                            (
                                "canonical",
                                raw_task_id,
                                raw_attempt,
                                _text(result.get("dimension"), "overview"),
                                summary,
                            )
                        )
                    else:
                        text_results.append(
                            ("legacy", _text(result.get("dimension"), "overview"), summary)
                        )
        return collections, search_results, text_results

    def _build_bundle(
        self,
        prepared: PreparedResearch,
        *,
        collections: Sequence[SourceCollection],
        search_results: Sequence[SourceSearchResult],
        task_text: Sequence[tuple[Any, ...]],
        retry_count: int,
        admit_candidates: bool = False,
    ) -> ResearchIntelligenceBundle:
        """Build a referentially complete v2 bundle from bounded provider data."""
        canonical_limit = _canonical_excerpt_limit(prepared.profile)
        candidate_records = list(normalize_collections(collections, canonical_limit))
        source_by_key: dict[tuple[str, str], SourceReference] = {}
        source: SourceReference | None
        sources: list[SourceReference] = []
        for record in candidate_records:
            key = (record.source.provider_id, record.source.source_id)
            if key not in source_by_key:
                source_by_key[key] = record.source
                sources.append(record.source)
        # Preserve source identity even for an empty collection.
        for collection in collections:
            key = (collection.provider_id, collection.target.source_id)
            if key not in source_by_key:
                source = SourceReference(
                    provider_id=collection.provider_id,
                    source_kind=collection.source_kind,
                    source_id=collection.target.source_id,
                    canonical_url=collection.target.canonical_url,
                    requested_ref=collection.target.requested_ref,
                    resolved_version=collection.resolved_version,
                    captured_at=collection.captured_at,
                    content_hash=_hash(collection.records),
                )
                source_by_key[key] = source
                sources.append(source)

        search_records: list[EvidenceRecord] = []
        for search in search_results:
            for index, result in enumerate(search.results):
                url = _text(result.get("url"), f"search://{search.provider_id}/{index}")
                source_id = _text(result.get("source_id"), url)
                source = source_by_key.get((search.provider_id, source_id))
                if source is None:
                    source = SourceReference(
                        provider_id=search.provider_id,
                        source_kind="search_result",
                        source_id=source_id,
                        canonical_url=url,
                        captured_at=_now_iso(),
                        content_hash=_hash(result),
                    )
                    source_by_key[(search.provider_id, source_id)] = source
                    sources.append(source)
                excerpt = _text(result.get("snippet") or result.get("summary"), "Search result")[:canonical_limit]
                locator = EvidenceLocator(locator_type="search_result", url=url)
                evidence_id = stable_evidence_id(source, locator=locator, excerpt=excerpt)
                if any(item.evidence_id == evidence_id for item in (*candidate_records, *search_records)):
                    continue
                search_records.append(
                    EvidenceRecord(
                        evidence_id=evidence_id,
                        source=source,
                        evidence_type="search_result",
                        evidence_level="metadata",
                        title=_text(result.get("title"), url),
                        excerpt=excerpt,
                        locator=locator,
                        attributes={"dimension": _text(result.get("dimension"), "overview"), "backend": search.backend},
                    )
                )

        legacy_records: list[EvidenceRecord] = []
        legacy_text: list[tuple[str, str]] = []
        for item in task_text:
            if len(item) == 3 and item[0] == "legacy":
                legacy_text.append((str(item[1]), str(item[2])))
            elif len(item) == 2:
                legacy_text.append((str(item[0]), str(item[1])))
        for dimension, text_result in legacy_text:
            urls = _URL_RE.findall(text_result)
            for index, url in enumerate(urls[: prepared.profile.retrieval_budget.max_evidence]):
                source_id = url.rstrip(".,;)")
                source = source_by_key.get(("web", source_id))
                if source is None:
                    source = SourceReference(
                        provider_id="web",
                        source_kind="task_source",
                        source_id=source_id,
                        canonical_url=source_id,
                        captured_at=_now_iso(),
                        content_hash=_hash(text_result),
                    )
                    source_by_key[("web", source_id)] = source
                    sources.append(source)
                excerpt = text_result[:canonical_limit]
                locator = EvidenceLocator(locator_type="task_source", url=source_id)
                evidence_id = stable_evidence_id(source, locator=locator, excerpt=excerpt)
                if any(item.evidence_id == evidence_id for item in (*candidate_records, *search_records, *legacy_records)):
                    continue
                legacy_records.append(
                    EvidenceRecord(
                        evidence_id=evidence_id,
                        source=source,
                        evidence_type="task_source",
                        evidence_level="derived",
                        title=f"Task source {index + 1}",
                        excerpt=excerpt,
                        locator=locator,
                        attributes={"dimension": dimension},
                    )
                )

        if admit_candidates:
            prepared.admit_records(candidate_records)
        # Accepted records are always first.  Previously admitted candidates
        # follow, then bounded unadmitted candidates; this keeps an accepted
        # source from being evicted by the initial repository collection.
        accepted_bindings = prepared.accepted_bindings()
        canonical_mode = any(
            len(item) == 5 and item[0] == "canonical" for item in task_text
        )
        accepted_records = [
            record
            for binding in accepted_bindings
            for record in binding.evidence
        ]
        admitted_records = list(prepared.admitted_records())
        ordered: list[EvidenceRecord] = []
        seen_ids: set[str] = set()
        final_candidates = (
            (*accepted_records, *admitted_records, *candidate_records, *legacy_records)
            if canonical_mode
            else (
                *accepted_records,
                *admitted_records,
                *candidate_records,
                *search_records,
                *legacy_records,
            )
        )
        for record in final_candidates:
            if record.evidence_id not in seen_ids:
                seen_ids.add(record.evidence_id)
                ordered.append(record)
        accepted_unique: list[EvidenceRecord] = []
        accepted_seen: set[str] = set()
        for record in accepted_records:
            if record.evidence_id not in accepted_seen:
                accepted_seen.add(record.evidence_id)
                accepted_unique.append(record)
        evidence = accepted_unique
        remaining_capacity = max(
            0, prepared.profile.retrieval_budget.max_evidence - len(evidence)
        )
        for record in ordered:
            if record.evidence_id in accepted_seen:
                continue
            if remaining_capacity <= 0:
                break
            evidence.append(record)
            remaining_capacity -= 1
        evidence_ids = {item.evidence_id for item in evidence}
        dimension_by_evidence = {
            item.evidence_id: _record_dimension(item) for item in evidence
        }
        binding_dimensions: dict[str, list[str]] = {}
        for accepted_binding in accepted_bindings:
            for record in accepted_binding.evidence:
                if record.evidence_id in evidence_ids:
                    dimension_ids = binding_dimensions.setdefault(accepted_binding.dimension, [])
                    if record.evidence_id not in dimension_ids:
                        dimension_ids.append(record.evidence_id)

        claims: list[ClaimRecord] = []
        canonical_dimensions = {
            str(item[3])
            for item in task_text
            if len(item) == 5 and item[0] == "canonical"
        }
        for item in task_text:
            if len(item) != 5 or item[0] != "canonical":
                continue
            _, task_id, _task_attempt, dimension, text_result = item
            binding = prepared.read_accepted(task_id=int(task_id))
            if binding is not None and (
                binding.task_attempt != int(_task_attempt)
                or binding.dimension != str(dimension)
            ):
                binding = None
            binding_ids = set(binding.evidence_ids) if binding is not None else set()
            current_evidence = tuple(record for record in evidence if record.evidence_id in binding_ids)
            for statement in self._atomic_claim_statements(str(text_result)):
                cited = tuple(dict.fromkeys(re.findall(r"\[(ev_[A-Za-z0-9]+)\]", statement)))
                valid_cited = tuple(item for item in cited if item in binding_ids and item in evidence_ids)
                unknown_cited = tuple(item for item in cited if item not in binding_ids)
                if cited:
                    claim_evidence = valid_cited
                    limitations = (
                        ("citation_not_in_current_binding",) if unknown_cited else ()
                    )
                    reportable = not unknown_cited
                else:
                    claim_evidence = self._candidate_evidence_ids(statement, current_evidence)
                    limitations = () if claim_evidence else ("no_accepted_binding",)
                    reportable = bool(claim_evidence)
                claim_id = stable_claim_id(
                    profile_id=prepared.profile.profile_id,
                    dimension=str(dimension),
                    statement=statement,
                )
                if any(existing.claim_id == claim_id for existing in claims):
                    continue
                claims.append(
                    ClaimRecord(
                        claim_id=claim_id,
                        dimension=str(dimension),
                        statement=statement,
                        confidence="unverified" if claim_evidence else "weak",
                        evidence_ids=claim_evidence,
                        limitations=limitations,
                        reportable=reportable,
                    )
                )
                if len(claims) >= 24:
                    break
            if len(claims) >= 24:
                break

        for dimension, text_result in legacy_text:
            dimension_evidence = tuple(
                item for item in evidence if dimension_by_evidence.get(item.evidence_id) == dimension
            )
            if not dimension_evidence:
                continue
            for statement in self._atomic_claim_statements(text_result):
                claim_id = stable_claim_id(
                    profile_id=prepared.profile.profile_id,
                    dimension=dimension,
                    statement=statement,
                )
                if any(item.claim_id == claim_id for item in claims):
                    continue
                claims.append(
                    ClaimRecord(
                        claim_id=claim_id,
                        dimension=dimension,
                        statement=statement,
                        confidence="unverified",
                        evidence_ids=self._candidate_evidence_ids(statement, dimension_evidence),
                    )
                )
                if len(claims) >= 24:
                    break
            if len(claims) >= 24:
                break
        for profile_dimension in prepared.profile.dimensions:
            if any(item.dimension == profile_dimension.id for item in claims):
                continue
            ids = tuple(binding_dimensions.get(profile_dimension.id, ()))
            if not ids and profile_dimension.id not in canonical_dimensions:
                ids = tuple(
                    item.evidence_id
                    for item in evidence
                    if dimension_by_evidence.get(item.evidence_id) == profile_dimension.id
                )
            if not ids:
                continue
            statement = f"Evidence was collected for the {profile_dimension.id} dimension."
            claims.append(
                ClaimRecord(
                    claim_id=stable_claim_id(
                        profile_id=prepared.profile.profile_id,
                        dimension=profile_dimension.id,
                        statement=statement,
                    ),
                    dimension=profile_dimension.id,
                    statement=statement,
                    confidence="high" if len(ids) > 1 else "medium",
                    evidence_ids=ids,
                )
            )
        covered = tuple(dict.fromkeys(claim.dimension for claim in claims))
        required = tuple(prepared.profile.coverage_policy.required_dimensions)
        missing = tuple(item for item in required if item not in covered)
        raw_coverage = CoverageDecision(
            required_dimensions=required,
            covered_dimensions=covered,
            missing_dimensions=missing,
            coverage_score=len(covered) / len(required) if required else 1.0,
            allow_report=not missing,
            gap_queries=tuple(f"{item} evidence" for item in missing),
            retry_count=retry_count,
        )
        report_spec = GenericReportSpec(
            title=f"{prepared.request.topic} research",
            sections=tuple(
                {
                    "id": section.id,
                    "title": section.title,
                    "dimension": section.dimension,
                    "required": section.required,
                }
                for section in prepared.profile.report_sections
            ),
            claim_ids=tuple(item.claim_id for item in claims),
            citation_ids=tuple(item.evidence_id for item in evidence),
            limitations=tuple(
                dict.fromkeys(
                    (
                        *missing,
                        *prepared.enrichment_limitations(),
                        *(
                            limitation
                            for claim in claims
                            for limitation in claim.limitations
                        ),
                    )
                )
            ),
        )
        return ResearchIntelligenceBundle(
            mode=prepared.profile.mode,
            profile_id=prepared.profile.profile_id,
            profile_version=prepared.profile.version,
            sources=tuple(sources),
            evidence=tuple(evidence),
            claims=tuple(claims),
            coverage=raw_coverage,
            report_spec=report_spec,
            evidence_frozen=False,
        )

    @staticmethod
    def _atomic_claim_statements(text: str) -> tuple[str, ...]:
        """Extract a bounded set of factual candidate sentences from one task summary."""
        statements: list[str] = []
        for raw_part in _CLAIM_SPLIT_RE.split(text):
            normalized = _MARKDOWN_PREFIX_RE.sub("", raw_part).strip()
            normalized = " ".join(normalized.split())
            if len(normalized) < 20:
                continue
            if len(normalized) > 600:
                normalized = normalized[:600].rstrip()
            if normalized in statements:
                continue
            statements.append(normalized)
            if len(statements) >= 3:
                break
        if statements:
            return tuple(statements)
        fallback = " ".join(text.split()).strip()
        return (fallback[:600],) if len(fallback) >= 20 else ()

    @staticmethod
    def _candidate_evidence_ids(
        statement: str,
        evidence: Sequence[EvidenceRecord],
        *,
        limit: int = 8,
    ) -> tuple[str, ...]:
        """Rank a bounded candidate set without claiming factual support."""
        cited = set(re.findall(r"\[(ev_[A-Za-z0-9]+)\]", statement))
        explicit = tuple(item.evidence_id for item in evidence if item.evidence_id in cited)
        if explicit:
            return explicit[:limit]
        claim_terms = set(statement.casefold()) - set(" \t\r\n,.;:!?，。；：！？")

        def rank(item: EvidenceRecord) -> tuple[float, int, str]:
            evidence_terms = set(item.excerpt.casefold())
            overlap = (
                len(claim_terms & evidence_terms) / len(claim_terms)
                if claim_terms
                else 0.0
            )
            level = 1 if item.evidence_level == "full_text" else 0
            return (overlap, level, item.evidence_id)

        ranked = sorted(evidence, key=rank, reverse=True)
        return tuple(item.evidence_id for item in ranked[:limit])

    @staticmethod
    def _locator(record: Mapping[str, Any], *, url: str) -> EvidenceLocator:
        """Build a locator from a provider record with safe fallback semantics."""
        raw_locator = record.get("locator")
        if isinstance(raw_locator, Mapping):
            try:
                return EvidenceLocator.from_dict(raw_locator)
            except (TypeError, ValueError):
                pass
        file_path = record.get("file_path") if isinstance(record.get("file_path"), str) else None
        line_start = record.get("line_start") if isinstance(record.get("line_start"), int) else None
        line_end = record.get("line_end") if isinstance(record.get("line_end"), int) else None
        if file_path and line_start is not None and line_end is not None:
            try:
                return EvidenceLocator(
                    locator_type="line",
                    url=url,
                    file_path=file_path,
                    line_start=line_start,
                    line_end=line_end,
                )
            except ValueError:
                pass
        page_start = record.get("page_start") if isinstance(record.get("page_start"), int) else None
        page_end = record.get("page_end") if isinstance(record.get("page_end"), int) else None
        if (page_start is None) != (page_end is None) or (
            page_start is not None and page_end is not None and page_end < page_start
        ):
            page_start = None
            page_end = None
        return EvidenceLocator(
            locator_type=_text(record.get("locator_type"), "record"),
            url=url,
            file_path=file_path,
            page_start=page_start,
            page_end=page_end,
        )


__all__ = ["PreparedResearch", "ResearchKernel", "TaskEvidenceBinding"]
