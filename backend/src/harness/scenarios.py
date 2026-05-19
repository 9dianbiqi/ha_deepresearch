"""Scenario definitions used by harness test suites."""

from __future__ import annotations

from dataclasses import dataclass, field

from config import Configuration, SearchAPI

from .models import HarnessRunRequest


@dataclass(kw_only=True)
class HarnessScenario:
    """Reusable harness scenario for smoke tests and batch evaluation."""

    name: str
    topic: str
    description: str = ""
    search_api: SearchAPI | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    def build_request(self, base_config: Configuration) -> HarnessRunRequest:
        """Create a runnable request from the scenario."""
        overrides: dict[str, object] = {}
        if self.search_api is not None:
            overrides["search_api"] = self.search_api

        config = base_config.model_copy(update=overrides)
        return HarnessRunRequest(
            topic=self.topic,
            config=config,
            metadata={"scenario": self.name, **self.metadata},
        )


def build_default_scenarios() -> list[HarnessScenario]:
    """Return a small starter set of research scenarios."""
    return [
        HarnessScenario(
            name="smoke_single_topic",
            topic="local llm deep research workflow design",
            description="Basic smoke test covering the happy path.",
        ),
        HarnessScenario(
            name="recent_news_style_query",
            topic="state of open-source agent frameworks in 2026",
            description="Checks behavior on a broad, time-sensitive topic.",
        ),
        HarnessScenario(
            name="narrow_technical_query",
            topic="how retrieval reranking improves research agents",
            description="Checks summarization quality for a focused technical topic.",
        ),
    ]
