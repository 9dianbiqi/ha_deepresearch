"""Provider, router, and governed boundary contracts for Task 2."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agent import DeepResearchAgent
from config import Configuration
from models import ResearchState
from research.contracts import ResearchCommand
from research.operations import GovernedOperations, OperationScope
from research.profiles import ResearchMode, RetrievalBudget
from research.providers.github import GitHubSourceProvider
from research.providers.web import WebSourceProvider
from research.session import (
    CancellationRequestedError,
    RunSession,
)
from research.sources import (
    BudgetExceededError,
    EnrichmentRequest,
    ProviderContext,
    RetrievalBudgetTracker,
    SourceProviderRegistry,
    SourceRequestSpec,
    SourceRouter,
    SourceSearchRequest,
    SourceTarget,
)


class AllowPolicy:
    """Allow provider operation tests to focus on adapter boundaries."""

    def evaluate_capability(self, capability: str, command: ResearchCommand) -> dict[str, str]:
        del command
        return {"capability": capability, "outcome": "allow", "reason": "test"}


def make_context(*, mode: ResearchMode = ResearchMode.GITHUB) -> ProviderContext:
    """Build a provider context with a real run scope and bounded budget."""
    command = ResearchCommand(
        topic="https://github.com/owner/repo",
        config=Configuration(enable_notes=False),
        research_mode=mode,
        research_profile_id=(
            "github.repository.v1" if mode is ResearchMode.GITHUB else "web.default.v1"
        ),
    )
    session = RunSession(
        command=command,
        state=ResearchState(research_topic=command.topic),
    )
    session.start()
    scope = OperationScope(
        operations=GovernedOperations(session, AllowPolicy()),
        task_attempt=1,
    )
    return ProviderContext(
        run_id=command.run_id,
        profile_id=command.research_profile_id or "test.profile.v1",
        mode=mode,
        operation_scope=scope,
        cancellation=session.cancellation,
        budget=RetrievalBudgetTracker(RetrievalBudget(max_requests=10)),
    )


def test_budget_tracker_reserves_atomically_under_concurrency() -> None:
    """Concurrent workers cannot reserve more requests than the profile budget."""
    from concurrent.futures import ThreadPoolExecutor

    tracker = RetrievalBudgetTracker(RetrievalBudget(max_requests=10))

    def reserve() -> bool:
        try:
            tracker.reserve(requests=1)
        except BudgetExceededError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=24) as executor:
        results = list(executor.map(lambda _: reserve(), range(50)))

    assert sum(results) == 10
    assert tracker.snapshot()["requests"] == 10
    assert tracker.remaining()["requests"] == 0


def test_router_uses_profile_priority_and_github_detection() -> None:
    """A GitHub target is selected by the profile-prioritized provider registry."""
    provider = GitHubSourceProvider(adapter=SimpleNamespace())
    registry = SourceProviderRegistry([provider])
    router = SourceRouter(registry)
    context = make_context()
    request = SourceRequestSpec(
        topic="Please inspect https://github.com/owner/repo",
        mode=ResearchMode.GITHUB,
        profile_id="github.repository.v1",
    )

    selected, detection = router.route(
        request,
        source_priority=("github",),
        context=context,
    )

    assert selected is provider
    assert detection.matched is True
    assert detection.targets[0].source_id == "owner/repo"
    assert detection.targets[0].canonical_url == "https://github.com/owner/repo"


def test_github_provider_collects_under_the_supplied_operation_scope() -> None:
    """Provider collection passes the run-bound scope to the existing adapter."""
    context = make_context()
    observed: list[object] = []

    class Adapter:
        def collect_repository_context(
            self,
            target: object,
            *,
            operation_scope: OperationScope,
        ) -> object:
            observed.append(operation_scope)
            return {"target": target, "repository": {"full_name": "owner/repo"}}

    provider = GitHubSourceProvider(adapter=Adapter())
    target = SourceTarget(
        provider_id="github",
        source_kind="repository",
        source_id="owner/repo",
        canonical_url="https://github.com/owner/repo",
        metadata={"owner": "owner", "repo": "repo"},
    )

    collected = provider.collect(target, context)

    assert observed == [context.operation_scope]
    assert collected.collection_status == "complete"
    assert collected.provider_payload is not None
    assert collected.target.source_id == "owner/repo"


def test_github_provider_preserves_cancellation_and_hides_untrusted_errors() -> None:
    """Control exceptions propagate while ordinary failures become stable notices."""
    context = make_context()
    target = SourceTarget(
        provider_id="github",
        source_kind="repository",
        source_id="owner/repo",
        canonical_url="https://github.com/owner/repo",
        metadata={"owner": "owner", "repo": "repo"},
    )

    class Cancelled:
        def collect_repository_context(self, target: object, **kwargs: object) -> object:
            del target, kwargs
            raise CancellationRequestedError("secret cancellation detail")

    with pytest.raises(CancellationRequestedError):
        GitHubSourceProvider(adapter=Cancelled()).collect(target, context)

    class Broken:
        def collect_repository_context(self, target: object, **kwargs: object) -> object:
            del target, kwargs
            raise RuntimeError("Authorization: Bearer provider-secret")

    failed = GitHubSourceProvider(adapter=Broken()).collect(target, context)
    assert failed.collection_status == "failed"
    assert failed.notice_codes == ("github_provider_unavailable",)
    assert "provider-secret" not in " ".join(failed.notices)


def test_web_provider_wraps_existing_dispatcher_and_passes_scope() -> None:
    """Web searches remain delegated to the existing retry/cache dispatcher."""
    context = make_context(mode=ResearchMode.WEB)
    calls: list[dict[str, Any]] = []

    def dispatcher(
        query: str,
        config: Configuration,
        loop_count: int,
        *,
        use_cache: bool,
        cancellation: object,
        operation_scope: OperationScope,
        search_adapter: object | None,
    ) -> tuple[dict[str, Any], list[str], str | None, str]:
        calls.append(
            {
                "query": query,
                "config": config,
                "loop_count": loop_count,
                "use_cache": use_cache,
                "cancellation": cancellation,
                "operation_scope": operation_scope,
                "search_adapter": search_adapter,
            }
        )
        return (
            {
                "results": [{"title": "Result", "url": "https://example.test"}],
                "notice_codes": [],
            },
            [],
            "answer",
            "duckduckgo",
        )

    provider = WebSourceProvider(dispatcher=dispatcher, search_adapter="runner")
    result = provider.search(
        SourceSearchRequest(
            query="research query",
            topic="topic",
            config=Configuration(enable_notes=False),
            loop_count=2,
            use_cache=True,
        ),
        context,
    )

    assert result.results[0]["title"] == "Result"
    assert result.answer == "answer"
    assert result.backend == "duckduckgo"
    assert calls[0]["operation_scope"] is context.operation_scope
    assert calls[0]["cancellation"] is context.cancellation
    assert calls[0]["search_adapter"] == "runner"


def test_web_provider_turns_unexpected_dispatcher_failures_into_stable_notice() -> None:
    """Provider boundaries never expose raw backend exception text."""
    context = make_context(mode=ResearchMode.WEB)

    def broken(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("raw backend secret")

    result = WebSourceProvider(dispatcher=broken).search(
        SourceSearchRequest(
            query="query",
            topic="topic",
            config=Configuration(enable_notes=False),
        ),
        context,
    )

    assert result.results == ()
    assert result.notice_codes == ("web_provider_unavailable",)
    assert "raw backend secret" not in " ".join(result.notices)


def test_provider_enrich_requires_a_target_and_consumes_a_bounded_budget() -> None:
    """Enrichment uses the same typed target path and budget accounting."""
    context = make_context()
    provider = GitHubSourceProvider(adapter=SimpleNamespace())
    target = SourceTarget(
        provider_id="github",
        source_kind="repository",
        source_id="owner/repo",
        canonical_url="https://github.com/owner/repo",
        metadata={"owner": "owner", "repo": "repo"},
    )

    with pytest.raises(ValueError, match="hints"):
        provider.enrich(EnrichmentRequest(target=target, hints=()), context)


def test_agent_accepts_an_injected_provider_registry_without_changing_execution() -> None:
    """The coordinator exposes a provider seam before the kernel migration."""
    registry = SourceProviderRegistry([WebSourceProvider(dispatcher=lambda *args, **kwargs: None)])
    agent = DeepResearchAgent(
        config=Configuration(enable_notes=False),
        planner=object(),
        summarizer=object(),
        reporting=object(),
        source_provider_registry=registry,
    )

    assert agent.source_provider_registry is registry
    assert agent.research_kernel is not None
    assert agent.research_kernel.provider_registry is registry
