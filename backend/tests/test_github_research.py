"""Tests for GitHub-specific research helpers."""

from __future__ import annotations

import unittest
from typing import Any

import conftest  # noqa: F401

from research.session import CancellationRequestedError
from services.github_research import (
    GitHubRepositoryTarget,
    GitHubResearchClient,
    parse_github_repository,
)


class FakeResponse:
    """Small requests.Response stand-in for GitHub client tests."""

    def __init__(
        self,
        *,
        status_code: int = 200,
        payload: Any = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> Any:
        return self._payload


class FakeSession:
    """Routes GitHub API paths to deterministic fake responses."""

    def __init__(self, routes: dict[str, FakeResponse]) -> None:
        self.routes = routes
        self.calls: list[dict[str, Any]] = []

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
        timeout: int,
    ) -> FakeResponse:
        self.calls.append(
            {"url": url, "headers": headers, "params": params, "timeout": timeout}
        )
        path = url.replace("https://api.github.test", "")
        return self.routes.get(
            path,
            FakeResponse(status_code=404, payload={"message": "Not Found"}),
        )


class ParseGitHubRepositoryTests(unittest.TestCase):
    """Cover GitHub repository target detection."""

    def test_parses_github_url_inside_topic(self) -> None:
        target = parse_github_repository(
            "请分析 https://github.com/bytedance/deer-flow 的架构"
        )

        self.assertEqual(
            target,
            GitHubRepositoryTarget(owner="bytedance", repo="deer-flow"),
        )
        self.assertEqual(target.full_name, "bytedance/deer-flow")
        self.assertEqual(target.html_url, "https://github.com/bytedance/deer-flow")

    def test_parses_owner_repo_shorthand(self) -> None:
        target = parse_github_repository("bytedance/deer-flow")

        self.assertEqual(target.owner, "bytedance")
        self.assertEqual(target.repo, "deer-flow")

    def test_ignores_non_repository_topics(self) -> None:
        self.assertIsNone(parse_github_repository("local llm deep research workflow"))


class GitHubResearchClientTests(unittest.TestCase):
    """Verify GitHub API aggregation and graceful degradation."""

    def test_collects_repository_context_from_github_api(self) -> None:
        session = FakeSession(
            {
                "/repos/bytedance/deer-flow": FakeResponse(
                    payload={
                        "full_name": "bytedance/deer-flow",
                        "description": "Deep research framework",
                        "html_url": "https://github.com/bytedance/deer-flow",
                        "stargazers_count": 100,
                        "forks_count": 10,
                        "open_issues_count": 5,
                        "language": "Python",
                        "license": {"spdx_id": "MIT"},
                        "created_at": "2025-01-01T00:00:00Z",
                        "updated_at": "2026-01-01T00:00:00Z",
                        "pushed_at": "2026-01-02T00:00:00Z",
                        "default_branch": "main",
                        "topics": ["deep-research"],
                    }
                ),
                "/repos/bytedance/deer-flow/readme": FakeResponse(
                    text="# DeerFlow\nResearch agent"
                ),
                "/repos/bytedance/deer-flow/git/trees/main": FakeResponse(
                    payload={
                        "tree": [
                            {"path": "backend", "type": "tree"},
                            {"path": "backend/src/main.py", "type": "blob"},
                        ]
                    }
                ),
                "/repos/bytedance/deer-flow/languages": FakeResponse(
                    payload={"Python": 10, "TypeScript": 5}
                ),
                "/repos/bytedance/deer-flow/contributors": FakeResponse(
                    payload=[{"login": "alice", "contributions": 12}]
                ),
                "/repos/bytedance/deer-flow/commits": FakeResponse(
                    payload=[
                        {
                            "sha": "abc123456",
                            "html_url": "https://github.com/c",
                            "commit": {
                                "message": "feat: add research mode",
                                "author": {
                                    "name": "Alice",
                                    "date": "2026-01-02T00:00:00Z",
                                },
                            },
                        }
                    ]
                ),
                "/repos/bytedance/deer-flow/issues": FakeResponse(
                    payload=[
                        {
                            "number": 1,
                            "title": "Roadmap",
                            "state": "open",
                            "html_url": "https://github.com/i",
                            "pull_request": None,
                            "created_at": "2026-01-03T00:00:00Z",
                            "updated_at": "2026-01-04T00:00:00Z",
                        }
                    ]
                ),
                "/repos/bytedance/deer-flow/pulls": FakeResponse(payload=[]),
                "/repos/bytedance/deer-flow/releases": FakeResponse(
                    payload=[
                        {
                            "tag_name": "v1.0.0",
                            "name": "First release",
                            "published_at": "2026-01-05T00:00:00Z",
                            "html_url": "https://github.com/r",
                        }
                    ]
                ),
            }
        )
        client = GitHubResearchClient(
            token="token-123",
            base_url="https://api.github.test",
            session=session,
        )

        context = client.collect_repository_context(
            GitHubRepositoryTarget(owner="bytedance", repo="deer-flow")
        )

        self.assertEqual(context.target.full_name, "bytedance/deer-flow")
        self.assertEqual(context.repository["stars"], 100)
        self.assertIn("# DeerFlow", context.readme_excerpt)
        self.assertIn("backend/src/main.py", context.tree_excerpt)
        self.assertEqual(context.languages["Python"], 10)
        self.assertEqual(context.contributors[0]["login"], "alice")
        self.assertEqual(context.commits[0]["message"], "feat: add research mode")
        self.assertEqual(context.issues[0]["title"], "Roadmap")
        self.assertEqual(context.releases[0]["tag"], "v1.0.0")
        self.assertEqual(context.notices, [])
        self.assertTrue(
            all(
                call["headers"]["Authorization"] == "Bearer token-123"
                for call in session.calls
            )
        )

    def test_collects_notices_when_rate_limited_without_token(self) -> None:
        session = FakeSession(
            {
                "/repos/bytedance/deer-flow": FakeResponse(
                    status_code=403,
                    payload={"message": "API rate limit exceeded"},
                    text="rate limit",
                )
            }
        )
        client = GitHubResearchClient(
            token=None,
            base_url="https://api.github.test",
            session=session,
        )

        context = client.collect_repository_context(
            GitHubRepositoryTarget(owner="bytedance", repo="deer-flow")
        )

        self.assertEqual(context.repository, {})
        self.assertTrue(
            any("GITHUB_TOKEN" in notice and "rate limit" in notice for notice in context.notices)
        )
        self.assertFalse(
            any("Authorization" in call["headers"] for call in session.calls)
        )

    def test_checks_cancellation_before_and_after_each_http_get(self) -> None:
        """Aggregate authorization still permits cancellation between requests."""
        session = FakeSession(
            {
                "/repos/owner/repo": FakeResponse(
                    payload={
                        "full_name": "owner/repo",
                        "default_branch": "main",
                    }
                )
            }
        )
        checkpoints = 0

        def checkpoint() -> None:
            nonlocal checkpoints
            checkpoints += 1
            if checkpoints == 2:
                raise CancellationRequestedError("cancelled after request")

        client = GitHubResearchClient(
            base_url="https://api.github.test",
            session=session,
            checkpoint=checkpoint,
        )

        with self.assertRaises(CancellationRequestedError):
            client.collect_repository_context(
                GitHubRepositoryTarget(owner="owner", repo="repo")
            )

        self.assertEqual(checkpoints, 2)
        self.assertEqual(len(session.calls), 1)


if __name__ == "__main__":
    unittest.main()
