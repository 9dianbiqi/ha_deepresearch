"""Offline and benchmark fixture definitions for research assessment."""

from __future__ import annotations

from dataclasses import dataclass, field

from config import Configuration, SearchAPI
from research.contracts import ResearchCommand


@dataclass(kw_only=True)
class HarnessScenario:
    """Reusable offline benchmark fixture for smoke and batch evaluation."""

    name: str
    topic: str
    description: str = ""
    search_api: SearchAPI | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    def build_request(self, base_config: Configuration) -> ResearchCommand:
        """Create a canonical research command from the offline fixture."""
        overrides: dict[str, object] = {}
        if self.search_api is not None:
            overrides["search_api"] = self.search_api

        config = base_config.model_copy(update=overrides)
        return ResearchCommand(
            topic=self.topic,
            config=config,
            metadata={"scenario": self.name, **self.metadata},
        )


def build_default_scenarios() -> list[HarnessScenario]:
    """Return starter offline fixtures for repeatable benchmark runs."""
    return [
        HarnessScenario(
            name="smoke_single_topic",
            topic="local llm deep research workflow design",
            description="Offline benchmark smoke fixture for the happy path.",
        ),
        HarnessScenario(
            name="recent_news_style_query",
            topic="state of open-source agent frameworks in 2026",
            description="Offline benchmark fixture for a broad, time-sensitive topic.",
        ),
        HarnessScenario(
            name="narrow_technical_query",
            topic="how retrieval reranking improves research agents",
            description="Offline benchmark fixture for focused summarization quality.",
        ),
    ]
