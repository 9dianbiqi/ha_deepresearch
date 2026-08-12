"""ResearchKernel preparation/finalization tests with a fake Paper provider."""

from __future__ import annotations

from dataclasses import dataclass

from config import Configuration
from research.pipeline import ResearchKernel
from research.profiles import (
    CoveragePolicy,
    ResearchDimension,
    ResearchMode,
    ResearchProfile,
    ResearchProfileRegistry,
)
from research.sources import (
    DetectionResult,
    EnrichmentRequest,
    ProviderContext,
    SourceCollection,
    SourceProviderRegistry,
    SourceTarget,
)


class _NeverCancelled:
    """Minimal cancellation surface for provider unit tests."""

    def raise_if_cancelled(self) -> None:
        """Allow the fake provider call."""


@dataclass
class _FakePaperProvider:
    """Small provider that records bounded collection/enrichment calls."""

    provider_id: str = "fake"
    supported_modes: frozenset[ResearchMode] = frozenset({ResearchMode.PAPER})
    collect_calls: int = 0
    enrich_calls: int = 0

    def detect_target(self, request, context) -> DetectionResult:
        """Detect one deterministic paper target."""
        del context
        target = SourceTarget(
            provider_id=self.provider_id,
            source_kind="paper",
            source_id="paper-1",
            canonical_url="https://example.test/paper-1",
        )
        return DetectionResult(
            provider_id=self.provider_id,
            matched=True,
            targets=(target,),
            confidence=1.0,
        )

    def search(self, request, context):
        """The kernel test does not need provider search."""
        del request, context
        raise AssertionError("search should not be called")

    def collect(self, target, context: ProviderContext) -> SourceCollection:
        """Return two dimension-tagged abstract records."""
        del context
        self.collect_calls += 1
        return SourceCollection(
            provider_id=self.provider_id,
            source_kind=target.source_kind,
            target=target,
            collection_status="complete",
            resolved_version="v1",
            records=(
                {
                    "dimension": "relevance",
                    "evidence_type": "abstract",
                    "title": "Relevance",
                    "excerpt": "The paper is relevant.",
                    "url": target.canonical_url,
                },
                {
                    "dimension": "method",
                    "evidence_type": "abstract",
                    "title": "Method",
                    "excerpt": "The paper describes a method.",
                    "url": target.canonical_url,
                },
            ),
        )

    def enrich(self, request: EnrichmentRequest, context: ProviderContext) -> SourceCollection:
        """Count at most one bounded enrichment pass."""
        del context
        self.enrich_calls += 1
        return SourceCollection(
            provider_id=self.provider_id,
            source_kind=request.target.source_kind,
            target=request.target,
            collection_status="complete",
            resolved_version="v1",
            records=(),
        )


def _profile() -> ResearchProfile:
    """Build a two-dimension fake Paper profile."""
    required = ("relevance", "method")
    return ResearchProfile(
        profile_id="paper.fake.v1",
        version=1,
        mode=ResearchMode.PAPER,
        dimensions=tuple(ResearchDimension(id=item, title=item) for item in required),
        task_templates=(),
        source_priority=("fake",),
        coverage_policy=CoveragePolicy(
            required_dimensions=required,
            min_coverage_score=1.0,
        ),
    )


def test_kernel_prepares_and_finalizes_without_touching_run_lifecycle() -> None:
    """A Fake Paper Provider can reach the frozen evidence gate."""
    provider = _FakePaperProvider()
    kernel = ResearchKernel(
        profile_registry=ResearchProfileRegistry((_profile(),)),
        provider_registry=SourceProviderRegistry((provider,)),
    )

    prepared = kernel.prepare(
        "paper methods",
        mode=ResearchMode.PAPER,
        profile_id="paper.fake.v1",
        run_id="run-1",
        config=Configuration.from_env(),
        cancellation=_NeverCancelled(),
    )
    bundle = kernel.finalize(prepared)

    assert prepared.profile.profile_id == "paper.fake.v1"
    assert prepared.tasks == ()
    assert provider.collect_calls == 1
    assert bundle.mode is ResearchMode.PAPER
    assert bundle.coverage.allow_report is True
    assert bundle.evidence_frozen is True


def test_kernel_limits_gap_enrichment_to_one_pass() -> None:
    """A failing gate cannot trigger unbounded provider enrichment."""
    provider = _FakePaperProvider()
    profile = _profile()
    kernel = ResearchKernel(
        profile_registry=ResearchProfileRegistry((profile,)),
        provider_registry=SourceProviderRegistry((provider,)),
    )
    prepared = kernel.prepare(
        "paper methods",
        mode=ResearchMode.PAPER,
        profile_id=profile.profile_id,
        run_id="run-2",
        config=Configuration.from_env(),
        cancellation=_NeverCancelled(),
    )
    bundle = kernel.finalize(prepared)

    assert provider.enrich_calls <= 1
    assert bundle.coverage.retry_count <= 1
