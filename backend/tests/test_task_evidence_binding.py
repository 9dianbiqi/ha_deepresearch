"""Focused acceptance tests for canonical task-evidence binding."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from research.evidence_normalization import normalize_collections
from research.pipeline import ResearchKernel
from research.profiles import (
    CitationPolicy,
    CoveragePolicy,
    ReportSectionSpec,
    ResearchDimension,
    ResearchMode,
    ResearchProfile,
    ResearchProfileRegistry,
    RetrievalBudget,
)
from research.sources import (
    DetectionResult,
    EnrichmentRequest,
    SourceCollection,
    SourceProviderRegistry,
    SourceSearchRequest,
    SourceSearchResult,
    SourceTarget,
)
from research.task_quality import TaskEvidence

CAPTURED_AT = "2026-09-22T00:00:00+00:00"


def _target(provider_id: str, source_id: str) -> SourceTarget:
    """Build a deterministic target without making a network request."""
    return SourceTarget(
        provider_id=provider_id,
        source_kind="repository" if provider_id == "github" else "web_page",
        source_id=source_id,
        canonical_url=(
            f"https://github.com/{source_id}"
            if provider_id == "github"
            else f"https://example.test/{source_id}"
        ),
        metadata=(
            {"owner": source_id.split("/", 1)[0], "repo": source_id.split("/", 1)[1]}
            if provider_id == "github"
            else {}
        ),
    )


def _record(
    target: SourceTarget,
    *,
    dimension: str,
    title: str,
    excerpt: str,
    paragraph: str,
) -> dict[str, object]:
    """Return one provider record with an explicit, stable paragraph locator."""
    return {
        "dimension": dimension,
        "evidence_type": "repository_metadata" if target.source_kind == "repository" else "web_paragraph",
        "evidence_level": "metadata" if target.source_kind == "repository" else "full_text",
        "title": title,
        "excerpt": excerpt,
        "url": target.canonical_url,
        "locator": {
            "locator_type": "paragraph",
            "url": target.canonical_url,
            "paragraph": paragraph,
        },
    }


def _collection(
    provider_id: str,
    source_id: str,
    records: tuple[dict[str, object], ...],
    *,
    resolved_version: str | None = "sha-a",
    payload: object | None = None,
) -> SourceCollection:
    """Build a bounded collection for a fake provider."""
    target = _target(provider_id, source_id)
    return SourceCollection(
        provider_id=provider_id,
        source_kind=target.source_kind,
        target=target,
        collection_status="complete",
        provider_payload=payload,
        records=records,
        resolved_version=resolved_version,
        captured_at=CAPTURED_AT,
    )


class _StaticProvider:
    """Provider fixture with deterministic collection and enrichment behavior."""

    def __init__(
        self,
        provider_id: str,
        mode: ResearchMode,
        collections: tuple[SourceCollection, ...],
        *,
        enrichment: SourceCollection | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.supported_modes = frozenset({mode})
        self.collections = collections
        self.enrichment = enrichment
        self.enrich_calls = 0

    def detect_target(self, request: object, context: object) -> DetectionResult:
        """Match the deterministic targets owned by this provider."""
        del request, context
        targets = tuple(item.target for item in self.collections)
        return DetectionResult(
            provider_id=self.provider_id,
            matched=bool(targets),
            targets=targets,
            confidence=1.0 if targets else 0.0,
        )

    def collect(self, target: SourceTarget, context: object) -> SourceCollection:
        """Return the collection corresponding to one target."""
        del context
        return next(item for item in self.collections if item.target.source_id == target.source_id)

    def search(self, request: SourceSearchRequest, context: object) -> SourceSearchResult:
        """Keep provider search empty; binding tests supply collection evidence."""
        del request, context
        return SourceSearchResult(provider_id=self.provider_id)

    def enrich(self, request: EnrichmentRequest, context: object) -> SourceCollection:
        """Return the configured one-pass enrichment result."""
        del request, context
        self.enrich_calls += 1
        if self.enrichment is None:
            raise ValueError("No enrichment fixture configured.")
        return self.enrichment


def _profile(
    provider_id: str,
    mode: ResearchMode,
    dimensions: tuple[str, ...] = ("overview",),
    *,
    max_evidence: int = 8,
) -> ResearchProfile:
    """Create a small profile whose gate still checks real source rules."""
    return ResearchProfile(
        profile_id=f"binding.{provider_id}.v1",
        version=1,
        mode=mode,
        dimensions=tuple(ResearchDimension(id=item, title=item) for item in dimensions),
        task_templates=(),
        source_priority=(provider_id,),
        retrieval_budget=RetrievalBudget(
            max_requests=20,
            max_results=20,
            max_tasks=8,
            max_evidence=max_evidence,
            max_enrich_passes=1,
            max_excerpt_chars=1200,
        ),
        coverage_policy=CoveragePolicy(
            required_dimensions=dimensions,
            min_coverage_score=1.0,
        ),
        citation_policy=CitationPolicy(require_locator=True),
        report_sections=tuple(
            ReportSectionSpec(id=item, title=item, dimension=item)
            for item in dimensions
        ),
    )


def _prepared(
    provider: _StaticProvider,
    profile: ResearchProfile,
) -> tuple[ResearchKernel, object]:
    """Prepare a real kernel with only the local fake provider registered."""
    kernel = ResearchKernel(
        profile_registry=ResearchProfileRegistry((profile,)),
        provider_registry=SourceProviderRegistry((provider,)),
    )
    prepared = kernel.prepare(
        "binding fixture",
        mode=profile.mode,
        profile_id=profile.profile_id,
        run_id="binding-run",
    )
    return kernel, prepared


def test_normalize_same_capture_across_dimensions_keeps_source_identity() -> None:
    """Dimension projection must not alter the source capture identity."""
    target = _target("web", "capture")
    payload = SimpleNamespace(snapshot=SimpleNamespace(content_hash="capture-sha"))
    overview = _collection(
        "web",
        "capture",
        (
            _record(
                target,
                dimension="overview",
                title="Capture paragraph",
                excerpt="The exact captured paragraph.",
                paragraph="p-1",
            ),
        ),
        resolved_version=None,
        payload=payload,
    )
    architecture = _collection(
        "web",
        "capture",
        (
            _record(
                target,
                dimension="architecture",
                title="Capture paragraph",
                excerpt="The exact captured paragraph.",
                paragraph="p-1",
            ),
        ),
        resolved_version=None,
        payload=payload,
    )

    first = normalize_collections((overview,), max_excerpt_chars=1200)[0]
    second = normalize_collections((architecture,), max_excerpt_chars=1200)[0]

    assert first.evidence_id == second.evidence_id
    assert first.source.as_dict() == second.source.as_dict()
    assert first.source.content_hash == "capture-sha"
    assert first.locator.as_dict() == second.locator.as_dict()
    assert first.attributes["dimension"] == "overview"
    assert second.attributes["dimension"] == "architecture"


def test_canonical_task_projection_preserves_text_locator_and_source() -> None:
    """Task projection must retain exact canonical text and provenance fields."""
    target = _target("github", "owner/repo")
    excerpt = "Exact source text with Unicode 证据 and punctuation."
    collection = _collection(
        "github",
        "owner/repo",
        (
            _record(
                target,
                dimension="overview",
                title="Repository metadata",
                excerpt=excerpt,
                paragraph="metadata-1",
            ),
        ),
        resolved_version="a" * 40,
    )
    record = normalize_collections((collection,), max_excerpt_chars=1200)[0]

    projected = TaskEvidence.from_record(record, "owner/repo overview")

    assert projected.evidence_id == record.evidence_id
    assert projected.excerpt == excerpt
    assert projected.locator == record.locator.as_dict()
    assert projected.url == record.locator.url
    assert projected.source_provenance == record.source.as_dict()
    assert projected.content_hash == record.source.content_hash
    assert projected.canonical is True


def test_same_evidence_across_task_dimensions_is_admitted_once_and_covers_both() -> None:
    """One shared record can ground two accepted dimensions without double charging."""
    target = _target("github", "owner/repo")
    collection = _collection(
        "github",
        "owner/repo",
        (
            _record(
                target,
                dimension="overview",
                title="Shared evidence",
                excerpt="The repository exposes one shared public architecture.",
                paragraph="shared-1",
            ),
        ),
        resolved_version="a" * 40,
    )
    provider = _StaticProvider("github", ResearchMode.GITHUB, (collection,))
    profile = _profile("github", ResearchMode.GITHUB, ("overview", "architecture"))
    kernel, prepared = _prepared(provider, profile)
    records = normalize_collections(prepared.collections, max_excerpt_chars=1200)
    shared_id = records[0].evidence_id

    overview = prepared.bind(
        task_id=1,
        task_attempt=1,
        dimension="overview",
        query="overview",
        evidence=records,
    )
    architecture = prepared.bind(
        task_id=2,
        task_attempt=1,
        dimension="architecture",
        query="architecture",
        evidence=records,
    )
    prepared.accept(task_id=1, task_attempt=overview.task_attempt)
    prepared.accept(task_id=2, task_attempt=architecture.task_attempt)

    bundle = kernel.finalize(
        prepared,
        task_results=(
            {
                "task_id": 1,
                "attempt": 1,
                "dimension": "overview",
                "summary": f"The overview is shared. [{shared_id}]",
            },
            {
                "task_id": 2,
                "attempt": 1,
                "dimension": "architecture",
                "summary": f"The architecture is shared. [{shared_id}]",
            },
        ),
        allow_enrichment=False,
        freeze=True,
    )

    assert prepared.provider_context.budget.snapshot()["evidence"] == 1
    assert len(bundle.evidence) == 1
    assert bundle.evidence[0].evidence_id == shared_id
    assert bundle.coverage.allow_report is True
    assert set(bundle.coverage.covered_dimensions) >= {"overview", "architecture"}
    assert bundle.evidence_frozen is True


def test_retry_accepts_b_and_old_attempt_summary_cannot_bind_b() -> None:
    """Final claims must use the accepted attempt's binding, not a stale attempt."""
    target = _target("github", "owner/repo")
    collection = _collection(
        "github",
        "owner/repo",
        (
            _record(
                target,
                dimension="overview",
                title="Attempt A",
                excerpt="Attempt A contains the stale explanation.",
                paragraph="attempt-a",
            ),
            _record(
                target,
                dimension="overview",
                title="Attempt B",
                excerpt="Attempt B contains the accepted explanation.",
                paragraph="attempt-b",
            ),
        ),
        resolved_version="a" * 40,
    )
    provider = _StaticProvider("github", ResearchMode.GITHUB, (collection,))
    profile = _profile("github", ResearchMode.GITHUB)
    kernel, prepared = _prepared(provider, profile)
    records = normalize_collections(prepared.collections, max_excerpt_chars=1200)
    record_a, record_b = records

    first = prepared.bind(
        task_id=1,
        task_attempt=1,
        dimension="overview",
        query="attempt-a",
        evidence=(record_a,),
    )
    second = prepared.bind(
        task_id=1,
        task_attempt=2,
        dimension="overview",
        query="attempt-b",
        evidence=(record_b,),
    )
    prepared.accept(task_id=1, task_attempt=second.task_attempt)
    assert prepared.read_accepted(task_id=1) is second
    assert prepared.read(task_id=1, task_attempt=1) is first

    bundle = kernel.finalize(
        prepared,
        task_results=(
            {
                "task_id": 1,
                "attempt": 1,
                "dimension": "overview",
                "summary": f"Old attempt statement is stale. [{record_a.evidence_id}]",
            },
            {
                "task_id": 1,
                "attempt": 2,
                "dimension": "overview",
                "summary": f"Accepted attempt statement is current. [{record_b.evidence_id}]",
            },
        ),
        allow_enrichment=False,
        freeze=True,
    )

    stale = next(claim for claim in bundle.claims if "Old attempt statement" in claim.statement)
    current = next(claim for claim in bundle.claims if "Accepted attempt statement" in claim.statement)
    assert stale.evidence_ids == ()
    assert stale.reportable is False
    assert current.evidence_ids == (record_b.evidence_id,)
    assert bundle.coverage.allow_report is False
    assert any(
        "citation_not_in_current_binding" in item
        for item in bundle.coverage.blockers
    )


def test_accepted_record_wins_multi_repo_candidate_budget() -> None:
    """Final admission keeps an accepted record ahead of earlier repo candidates."""
    target_one = _target("github", "owner/first")
    target_two = _target("github", "owner/second")
    first = _collection(
        "github",
        "owner/first",
        (
            _record(target_one, dimension="overview", title="First 1", excerpt="First repository evidence one.", paragraph="p-1"),
            _record(target_one, dimension="overview", title="First 2", excerpt="First repository evidence two.", paragraph="p-2"),
        ),
        resolved_version="a" * 40,
    )
    second = _collection(
        "github",
        "owner/second",
        (
            _record(target_two, dimension="overview", title="Accepted", excerpt="Accepted second repository evidence.", paragraph="p-1"),
        ),
        resolved_version="b" * 40,
    )
    provider = _StaticProvider("github", ResearchMode.GITHUB, (first, second))
    profile = _profile("github", ResearchMode.GITHUB, max_evidence=2)
    kernel, prepared = _prepared(provider, profile)
    records = normalize_collections(prepared.collections, max_excerpt_chars=1200)
    accepted = next(item for item in records if item.source.source_id == "owner/second")
    binding = prepared.bind(
        task_id=1,
        task_attempt=1,
        dimension="overview",
        query="accepted repository",
        evidence=(accepted,),
    )
    prepared.accept(task_id=1, task_attempt=binding.task_attempt)

    bundle = kernel.finalize(
        prepared,
        allow_enrichment=False,
        freeze=True,
    )

    assert bundle.evidence
    assert bundle.evidence[0].evidence_id == accepted.evidence_id
    assert accepted.evidence_id in {item.evidence_id for item in bundle.evidence}
    assert len(bundle.evidence) == 2
    assert prepared.provider_context.budget.snapshot()["evidence"] == 2
    assert prepared.provider_context.budget.remaining()["evidence"] == 0


def test_changed_enrichment_sha_is_rejected_and_baseline_can_disable_enrichment() -> None:
    """A changed enrichment snapshot becomes a limitation and is never merged."""
    target = _target("github", "owner/repo")
    initial = _collection(
        "github",
        "owner/repo",
        (
            _record(target, dimension="overview", title="Initial", excerpt="Initial overview evidence.", paragraph="initial"),
        ),
        resolved_version="a" * 40,
    )
    changed = _collection(
        "github",
        "owner/repo",
        (
            _record(target, dimension="architecture", title="Changed", excerpt="Changed architecture evidence.", paragraph="changed"),
        ),
        resolved_version="b" * 40,
    )
    profile = _profile("github", ResearchMode.GITHUB, ("overview", "architecture"))

    provider = _StaticProvider(
        "github",
        ResearchMode.GITHUB,
        (initial,),
        enrichment=changed,
    )
    kernel, prepared = _prepared(provider, profile)
    bundle = kernel.finalize(prepared, allow_enrichment=True, freeze=True)

    assert provider.enrich_calls == 1
    assert bundle.coverage.allow_report is False
    assert bundle.evidence_frozen is False
    assert any("source version changed" in item for item in bundle.report_spec.limitations)
    assert all(item.source.resolved_version == "a" * 40 for item in bundle.evidence)

    baseline_provider = _StaticProvider(
        "github",
        ResearchMode.GITHUB,
        (initial,),
        enrichment=changed,
    )
    baseline_kernel, baseline_prepared = _prepared(baseline_provider, profile)
    baseline = baseline_kernel.finalize(
        baseline_prepared,
        allow_enrichment=False,
        freeze=True,
    )

    assert baseline_provider.enrich_calls == 0
    assert baseline.coverage.allow_report is False
    assert not any("source version changed" in item for item in baseline.report_spec.limitations)


@pytest.mark.parametrize(
    ("provider_id", "mode", "resolved_version", "expect_allowed"),
    [
        ("web", ResearchMode.WEB, None, True),
        ("github", ResearchMode.GITHUB, None, False),
    ],
)
def test_web_without_sha_is_allowed_but_github_without_sha_is_blocked(
    provider_id: str,
    mode: ResearchMode,
    resolved_version: str | None,
    expect_allowed: bool,
) -> None:
    """Version requirements are provider-specific while locator checks stay shared."""
    source_id = "owner/repo" if provider_id == "github" else "mixed-web"
    target = _target(provider_id, source_id)
    collection = _collection(
        provider_id,
        source_id,
        (
            _record(
                target,
                dimension="overview",
                title="Mixed source",
                excerpt="A source with a valid locator but no resolved version.",
                paragraph="mixed-1",
            ),
        ),
        resolved_version=resolved_version,
    )
    provider = _StaticProvider(provider_id, mode, (collection,))
    profile = _profile(provider_id, mode)
    kernel, prepared = _prepared(provider, profile)
    bundle = kernel.finalize(
        prepared,
        allow_enrichment=False,
        freeze=True,
    )

    assert bundle.coverage.allow_report is expect_allowed
    assert bundle.evidence_frozen is expect_allowed
    if expect_allowed:
        assert bundle.coverage.blockers == ()
    else:
        assert any("missing_version" in item for item in bundle.coverage.blockers)
