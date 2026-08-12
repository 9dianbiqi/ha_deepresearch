"""Run-scoped research kernel that composes profiles, providers, and the gate."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from config import Configuration

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
    SourceSearchResult,
    SourceTarget,
)

_URL_RE = re.compile(r"https?://[^\s<>\"']+")


class _NeverCancelled:
    """Default cancellation surface for direct kernel use."""

    def raise_if_cancelled(self) -> None:
        """Allow the current provider call."""


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
        rendered_tasks = profile.render_tasks(
            repository=repository,
            comparison_repositories=comparison_repositories,
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
        if not decision.allow_report and decision.gap_queries and prepared.targets:
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
                all_collections.append(enrichment)
                bundle = self._build_bundle(
                    prepared,
                    collections=all_collections,
                    search_results=search_results,
                    task_text=task_text,
                    retry_count=1,
                )
                decision = self.quality_gate.evaluate(bundle, prepared.profile)
        bundle = replace(bundle, coverage=decision)
        if decision.allow_report:
            bundle = replace(bundle, evidence_frozen=True)
        return bundle

    @staticmethod
    def _split_task_results(
        task_results: Sequence[object],
    ) -> tuple[list[SourceCollection], list[SourceSearchResult], list[str]]:
        """Separate provider collections, search results, and legacy text summaries."""
        collections: list[SourceCollection] = []
        search_results: list[SourceSearchResult] = []
        text_results: list[str] = []
        for result in task_results:
            if isinstance(result, SourceCollection):
                collections.append(result)
            elif isinstance(result, SourceSearchResult):
                search_results.append(result)
            elif isinstance(result, str) and result.strip():
                text_results.append(result.strip())
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
                    text_results.append(str(result["summary"]))
        return collections, search_results, text_results

    def _build_bundle(
        self,
        prepared: PreparedResearch,
        *,
        collections: Sequence[SourceCollection],
        search_results: Sequence[SourceSearchResult],
        task_text: Sequence[str],
        retry_count: int,
    ) -> ResearchIntelligenceBundle:
        """Build a referentially complete v2 bundle from bounded provider data."""
        sources: list[SourceReference] = []
        source_by_key: dict[tuple[str, str], SourceReference] = {}
        evidence: list[EvidenceRecord] = []
        dimension_by_evidence: dict[str, str] = {}
        for collection in collections:
            target = collection.target
            key = (collection.provider_id, target.source_id)
            source = source_by_key.get(key)
            if source is None:
                source = SourceReference(
                    provider_id=collection.provider_id,
                    source_kind=collection.source_kind,
                    source_id=target.source_id,
                    canonical_url=target.canonical_url,
                    requested_ref=target.requested_ref,
                    resolved_version=collection.resolved_version,
                    captured_at=collection.captured_at,
                    content_hash=_hash(collection.records),
                )
                source_by_key[key] = source
                sources.append(source)
            for index, record in enumerate(collection.records):
                dimension = _text(record.get("dimension"), "overview")
                excerpt = _text(
                    record.get("excerpt") or record.get("summary") or record.get("text"),
                    "Provider record",
                )[: prepared.profile.retrieval_budget.max_excerpt_chars]
                url = _text(
                    record.get("url") or record.get("source_url"),
                    target.canonical_url,
                )
                locator = self._locator(record, url=url)
                evidence_id = stable_evidence_id(source, locator=locator, excerpt=excerpt)
                if evidence_id in {item.evidence_id for item in evidence}:
                    continue
                attributes = _safe_attributes(record)
                attributes["dimension"] = dimension
                attributes["provider_collection_status"] = collection.collection_status
                attributes["notice_codes"] = list(collection.notice_codes)
                evidence.append(
                    EvidenceRecord(
                        evidence_id=evidence_id,
                        source=source,
                        evidence_type=_text(record.get("evidence_type"), "provider_record"),
                        evidence_level=_text(
                            record.get("evidence_level"),
                            "abstract" if collection.source_kind == "paper" else "metadata",
                        ),
                        title=_text(record.get("title"), f"{target.source_id} record {index + 1}"),
                        excerpt=excerpt,
                        locator=locator,
                        attributes=attributes,
                    )
                )
                dimension_by_evidence[evidence_id] = dimension
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
                excerpt = _text(result.get("snippet") or result.get("summary"), "Search result")[: prepared.profile.retrieval_budget.max_excerpt_chars]
                locator = EvidenceLocator(locator_type="search_result", url=url)
                evidence_id = stable_evidence_id(source, locator=locator, excerpt=excerpt)
                if evidence_id in {item.evidence_id for item in evidence}:
                    continue
                dimension = _text(result.get("dimension"), "overview")
                evidence.append(
                    EvidenceRecord(
                        evidence_id=evidence_id,
                        source=source,
                        evidence_type="search_result",
                        evidence_level="metadata",
                        title=_text(result.get("title"), url),
                        excerpt=excerpt,
                        locator=locator,
                        attributes={"dimension": dimension, "backend": search.backend},
                    )
                )
                dimension_by_evidence[evidence_id] = dimension
        for text_result in task_text:
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
                excerpt = text_result[: prepared.profile.retrieval_budget.max_excerpt_chars]
                locator = EvidenceLocator(locator_type="task_source", url=source_id)
                evidence_id = stable_evidence_id(source, locator=locator, excerpt=excerpt)
                if evidence_id in {item.evidence_id for item in evidence}:
                    continue
                evidence.append(
                    EvidenceRecord(
                        evidence_id=evidence_id,
                        source=source,
                        evidence_type="task_source",
                        evidence_level="derived",
                        title=f"Task source {index + 1}",
                        excerpt=excerpt,
                        locator=locator,
                        attributes={"dimension": "overview"},
                    )
                )
                dimension_by_evidence[evidence_id] = "overview"

        # Apply the profile evidence budget before deriving claims so no claim
        # can point at an Evidence ID that is removed from the final bundle.
        evidence = evidence[: prepared.profile.retrieval_budget.max_evidence]
        dimension_by_evidence = {
            evidence_id: dimension
            for evidence_id, dimension in dimension_by_evidence.items()
            if evidence_id in {item.evidence_id for item in evidence}
        }
        claims: list[ClaimRecord] = []
        for profile_dimension in prepared.profile.dimensions:
            evidence_ids = tuple(
                item.evidence_id
                for item in evidence
                if dimension_by_evidence.get(item.evidence_id) == profile_dimension.id
            )
            if not evidence_ids:
                continue
            statement = (
                f"Evidence was collected for the {profile_dimension.id} dimension."
            )
            claims.append(
                ClaimRecord(
                    claim_id=stable_claim_id(
                        profile_id=prepared.profile.profile_id,
                        dimension=profile_dimension.id,
                        statement=statement,
                    ),
                    dimension=profile_dimension.id,
                    statement=statement,
                    confidence="high" if len(evidence_ids) > 1 else "medium",
                    evidence_ids=evidence_ids,
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
            limitations=missing,
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
    def _locator(record: Mapping[str, Any], *, url: str) -> EvidenceLocator:
        """Build a locator from a provider record with safe fallback semantics."""
        raw_locator = record.get("locator")
        if isinstance(raw_locator, Mapping):
            try:
                return EvidenceLocator.from_dict(raw_locator)
            except (TypeError, ValueError):
                pass
        file_path = record.get("file_path") if isinstance(record.get("file_path"), str) else None
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


__all__ = ["PreparedResearch", "ResearchKernel"]
