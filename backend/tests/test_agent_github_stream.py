"""Streaming behavior tests for GitHub research mode."""

from __future__ import annotations

import unittest
from typing import Any

import conftest  # noqa: F401

from agent import DeepResearchAgent
from config import Configuration
from models import SummaryState, TodoItem
from services.github_research import GitHubRepositoryContext, GitHubRepositoryTarget


class FakePlanner:
    """Planner stub that produces one ordinary web-search task."""

    def plan_todo_list(
        self,
        state: SummaryState,
        prior_context: dict[str, Any] | None = None,
    ) -> list[TodoItem]:
        return [
            TodoItem(
                id=1,
                title="Repository overview",
                intent="Summarize repository purpose and activity",
                query=state.research_topic or "",
            )
        ]

    def create_fallback_task(self, state: SummaryState) -> TodoItem:
        return self.plan_todo_list(state)[0]


class FakeSummarizer:
    """Summarizer stub for deterministic stream output."""

    def stream_summary(self, request: object):
        del request

        collected: list[str] = []

        def chunks():
            collected.append("GitHub summary")
            yield "GitHub summary"

        return chunks(), lambda: "".join(collected)


class FakeReporter:
    """Reporter stub for deterministic final output."""

    def generate_report(
        self,
        state: SummaryState,
        notes_context: dict[str, Any] | None = None,
    ) -> str:
        return "GitHub final report"


class FakeGitHubResearchClient:
    """GitHub client stub returning structured repository context."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def collect_repository_context(
        self,
        target: GitHubRepositoryTarget,
    ) -> GitHubRepositoryContext:
        return GitHubRepositoryContext(
            target=target,
            repository={
                "name": target.full_name,
                "stars": 100,
                "default_branch": "main",
            },
            readme_excerpt="# DeerFlow",
            tree_excerpt="README.md\nbackend/src/main.py",
        )


def fake_dispatch_search(
    query: str,
    config: Configuration,
    loop_count: int,
    *,
    cancellation: object | None = None,
) -> tuple[dict[str, Any], list[str], str | None, str]:
    """Return one fake web source."""
    del query, config, loop_count, cancellation
    return (
        {
            "results": [
                {
                    "title": "GitHub repository",
                    "url": "https://github.com/bytedance/deer-flow",
                    "content": "Repository content",
                }
            ]
        },
        [],
        None,
        "duckduckgo",
    )


def fake_prepare_research_context(
    search_result: dict[str, Any] | None,
    answer_text: str | None,
    config: Configuration,
) -> tuple[str, str]:
    """Return deterministic safe source metadata and worker-only context."""
    del search_result, answer_text, config
    return (
        "- GitHub repository https://github.com/bytedance/deer-flow",
        "Repository content that must not enter events",
    )


class AgentGitHubStreamTests(unittest.TestCase):
    """Verify GitHub repository topics surface repo-aware stream events."""

    def test_stream_emits_github_repository_event_and_final_report(self) -> None:
        config = Configuration.from_env(
            overrides={
                "enable_notes": False,
                "enable_quality_gate": False,
                "enable_github_research": True,
            }
        )
        agent = DeepResearchAgent(
            config=config,
            planner=FakePlanner(),
            search_adapter=fake_dispatch_search,
            context_preparer=fake_prepare_research_context,
            summarizer=FakeSummarizer(),
            reporting=FakeReporter(),
            note_agent=None,
            github_adapter=FakeGitHubResearchClient(),
        )

        events = list(agent.run_stream("https://github.com/bytedance/deer-flow"))

        github_events = [item for item in events if item.get("type") == "github_repository"]
        self.assertEqual(len(github_events), 1)
        self.assertEqual(github_events[0]["repository"]["full_name"], "bytedance/deer-flow")
        self.assertEqual(github_events[0]["repository"]["url"], "https://github.com/bytedance/deer-flow")
        self.assertEqual(github_events[0]["repository"]["stars"], 100)
        todo_event = next(item for item in events if item.get("type") == "todo_list")
        self.assertEqual(len(todo_event["tasks"]), 4)
        self.assertTrue(
            all(
                task["source_strategy"] == "github_api_then_web"
                and task["repository"] == "bytedance/deer-flow"
                for task in todo_event["tasks"]
            )
        )
        self.assertNotIn("raw_context", str(events))
        self.assertEqual(events[-2]["type"], "final_report")
        self.assertEqual(events[-2]["report"], "GitHub final report")
        self.assertEqual(events[-1]["type"], "done")


if __name__ == "__main__":
    unittest.main()
