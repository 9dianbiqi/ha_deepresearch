"""Tests for the P0 history browser and P1 local related-run recall."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from config import Configuration
from harness.runner import HarnessRunner
from main import create_app
from models import ResearchState, TodoItem
from research.application import ResearchApplicationService
from research.contracts import EventKind, ResearchCommand, RunStatus
from research.history import ResearchHistoryStore
from research.repository import FileRunRepository
from research.session import RunSession


def make_snapshot(
    topic: str,
    finding: str,
    *,
    parent_run_id: str | None = None,
    offset_minutes: int = 0,
):
    """Build one canonical completed snapshot with bounded follow-up memory."""
    command = ResearchCommand(
        topic=topic,
        config=Configuration(enable_notes=False),
        parent_run_id=parent_run_id,
    )
    session = RunSession(
        command=command,
        state=ResearchState(research_topic=topic),
    )
    session.start()
    session.install_plan(
        [TodoItem(id=1, title="Task", intent="Intent", query=topic)]
    )
    session.start_task(1)
    session.complete_task(1, summary=finding, sources_summary="Source summary")
    session.set_report(f"# {finding}")
    session.followup_context = {
        "schema_version": 1,
        "source_run_id": session.run_id,
        "key_findings": [finding],
        "key_sources": ["Source summary"],
        "open_questions": [],
    }
    prepared = session.prepare_terminal(RunStatus.COMPLETED, EventKind.RUN_COMPLETED)
    session.confirm_terminal(prepared)
    moment = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(
        minutes=offset_minutes
    )
    return replace(
        session.to_snapshot(),
        started_at=moment,
        completed_at=moment + timedelta(seconds=1),
    )


def test_repository_history_page_is_newest_first_and_cursor_stable(tmp_path) -> None:
    """P0 exposes bounded completed summaries without loading report bodies."""
    repository = FileRunRepository(tmp_path)
    snapshots = [
        make_snapshot("older topic", "older finding", offset_minutes=1),
        make_snapshot("newer topic", "newer finding", offset_minutes=2),
        make_snapshot("newest topic", "newest finding", offset_minutes=3),
    ]
    for snapshot in snapshots:
        repository.save(snapshot)

    first = repository.list_summaries(limit=2)
    assert [item["topic"] for item in first.items] == [
        "newest topic",
        "newer topic",
    ]
    assert first.next_cursor
    second = repository.list_summaries(limit=2, cursor=first.next_cursor)
    assert [item["topic"] for item in second.items] == ["older topic"]
    assert second.next_cursor is None
    assert first.items[0]["task_count"] == 1
    assert first.items[0]["report_excerpt"] == "# newest finding"


def test_history_store_recalls_chinese_and_english_clues(tmp_path) -> None:
    """P1 uses local token overlap and returns only bounded related context."""
    repository = FileRunRepository(tmp_path)
    snapshot = make_snapshot("Python 中文搜索优化", "中文搜索应优先验证来源")
    repository.save(snapshot)
    store = ResearchHistoryStore(repository)

    matches = store.recall("中文搜索优化")
    assert len(matches) == 1
    assert matches[0]["run_id"] == snapshot.run_id
    assert matches[0]["key_findings"] == ["中文搜索应优先验证来源"]
    assert 0.35 <= matches[0]["score"] <= 1


def test_corrupt_derived_index_is_quarantined_and_rebuilt(tmp_path) -> None:
    """A damaged SQLite derivative never blocks history or research startup."""
    repository = FileRunRepository(tmp_path)
    snapshot = make_snapshot("rebuildable index", "safe finding")
    repository.save(snapshot)
    index_path = tmp_path / "research-history.db"
    index_path.write_bytes(b"not a sqlite database")

    store = ResearchHistoryStore(repository)

    assert store.list_runs().items[0]["run_id"] == snapshot.run_id
    assert list(tmp_path.glob("research-history.db.corrupt-*"))


def test_history_recall_excludes_exact_parent_run(tmp_path) -> None:
    """Related recall never duplicates the explicit exact-follow-up parent."""
    repository = FileRunRepository(tmp_path)
    parent = make_snapshot("SQLite history retrieval", "parent finding")
    child = make_snapshot(
        "SQLite history retrieval follow-up",
        "child finding",
        parent_run_id=parent.run_id,
        offset_minutes=1,
    )
    repository.save(parent)
    repository.save(child)
    store = ResearchHistoryStore(repository)

    matches = store.recall(
        "SQLite history retrieval",
        exclude_run_ids=(parent.run_id,),
    )
    assert matches
    assert all(match["run_id"] != parent.run_id for match in matches)


class AllowPolicy:
    """Minimal policy for application-level history wiring tests."""

    def evaluate(self, command):
        del command
        return [{"capability": "research:run", "outcome": "allow", "reason": "ok"}]

    def assert_executable(self, decisions):
        del decisions


class CompletingCoordinator:
    """Complete a run while exposing the attached related history context."""

    def __init__(self) -> None:
        self.related_history: list[dict[str, object]] = []

    def execute(self, session, prior_context):
        del prior_context
        self.related_history.append(dict(session.related_history_context))
        session.install_plan(
            [TodoItem(id=1, title="Task", intent="Intent", query=session.command.topic)]
        )
        session.start_task(1)
        session.complete_task(
            1,
            summary="finding",
            sources_summary="Source: https://example.test/reference",
        )
        session.set_report(
            "# Report\n\n"
            "## Task 1: Task\n"
            "The completed task produced a detailed finding and records the "
            "evidence and its limitations for follow-up.\n\n"
            "## Findings and sources\n"
            "The finding is traceable to the collected evidence. "
            "Source: https://example.test/reference."
        )


def test_application_attaches_recall_and_allows_per_run_disable(tmp_path) -> None:
    """P1 recall is visible to planning and isolated from the exact parent path."""
    repository = FileRunRepository(tmp_path)
    related = make_snapshot("SQLite memory retrieval", "related finding")
    repository.save(related)
    history = ResearchHistoryStore(repository)
    coordinator = CompletingCoordinator()
    service = ResearchApplicationService(
        coordinator=coordinator,
        repository=repository,
        policy=AllowPolicy(),
        history_store=history,
    )

    events = []
    result = service.execute(
        ResearchCommand(topic="SQLite memory retrieval", config=Configuration(enable_notes=False)),
        observer=events.append,
    )
    assert result.status is RunStatus.COMPLETED
    assert coordinator.related_history[0]["matches"]
    assert result.metrics["history_recall"]["match_count"] == 1
    assert result.metrics["history_recall"]["outcome"] == "hit"
    assert result.metrics["history_recall"]["context_chars"] > 0
    assert result.metrics["history_recall"]["latency_ms"] >= 0
    assert any(event.kind is EventKind.HISTORY_RECALLED for event in events)

    disabled = service.execute(
        ResearchCommand(
            topic="SQLite memory retrieval",
            config=Configuration(enable_notes=False),
            use_history_memory=False,
        )
    )
    assert disabled.status is RunStatus.COMPLETED
    assert coordinator.related_history[-1] == {}
    assert disabled.metrics["history_recall"]["enabled"] is False
    assert disabled.metrics["history_recall"]["outcome"] == "disabled"
    assert disabled.metrics["history_recall"]["context_chars"] == 0


class HistoryApiRunner:
    """Small API double exposing the new history route."""

    def list_history(self, *, limit=20, cursor=None):
        return {
            "items": [{"run_id": "a", "topic": "topic", "task_count": 1}],
            "next_cursor": cursor,
            "limit": limit,
        }


def test_history_api_returns_bounded_page() -> None:
    """P0 exposes history through a stable GET endpoint."""
    client = TestClient(
        create_app(harness_runner=HistoryApiRunner()),
        headers={"Authorization": "Bearer test-app-key"},
    )
    response = client.get("/runs?limit=5")
    assert response.status_code == 200
    assert response.json()["items"][0]["run_id"] == "a"


def test_http_history_followup_and_memory_toggle_end_to_end(tmp_path: Path) -> None:
    """The browser, report lookup, follow-up, and per-run toggle compose over HTTP."""
    repository = FileRunRepository(tmp_path / "runs")
    history = ResearchHistoryStore(repository)
    coordinator = CompletingCoordinator()
    application = ResearchApplicationService(
        coordinator=coordinator,
        repository=repository,
        policy=AllowPolicy(),
        history_store=history,
    )
    runner = HarnessRunner(
        application=application,
        repository=repository,
        history_store=history,
    )

    def stream(client: TestClient, endpoint: str, payload: dict[str, object]) -> list[dict[str, object]]:
        with client.stream("POST", endpoint, json=payload) as response:
            assert response.status_code == 200, response.text
            return [
                json.loads(line[5:].strip())
                for line in response.iter_lines()
                if line and line.startswith("data:")
            ]

    with TestClient(
        create_app(harness_runner=runner),
        headers={"Authorization": "Bearer test-app-key"},
    ) as client:
        first = stream(client, "/research/stream", {"topic": "SQLite memory retrieval"})
        first_run_id = first[0]["run_id"]
        assert first[-1]["type"] == "done"

        second = stream(
            client,
            "/research/stream",
            {"topic": "SQLite memory retrieval benchmarks"},
        )
        second_run_id = second[0]["run_id"]
        assert any(event["type"] == "history_recalled" for event in second)

        page = client.get("/runs?limit=10")
        assert page.status_code == 200
        assert {first_run_id, second_run_id} <= {
            item["run_id"] for item in page.json()["items"]
        }

        report = client.get(f"/runs/{first_run_id}")
        assert report.status_code == 200
        assert report.json()["output"]["report_markdown"].startswith("# Report")

        followup = stream(
            client,
            "/research/continue/stream",
            {
                "topic": "SQLite memory retrieval follow-up",
                "parent_run_id": second_run_id,
                "use_history_memory": True,
            },
        )
        followup_run_id = followup[0]["run_id"]
        assert followup[-1]["type"] == "done"
        assert any(event["type"] == "history_recalled" for event in followup)
        followup_record = client.get(f"/runs/{followup_run_id}").json()
        assert followup_record["metrics"]["followup"]["success"] is True

        disabled = stream(
            client,
            "/research/continue/stream",
            {
                "topic": "SQLite memory retrieval without memory",
                "parent_run_id": second_run_id,
                "use_history_memory": False,
            },
        )
        disabled_run_id = disabled[0]["run_id"]
        assert disabled[-1]["type"] == "done"
        assert not any(event["type"] == "history_recalled" for event in disabled)
        disabled_record = client.get(f"/runs/{disabled_run_id}").json()
        assert disabled_record["metrics"]["history_recall"]["outcome"] == "disabled"
        assert coordinator.related_history[-1] == {}

    runner._executor.shutdown(wait=True)
