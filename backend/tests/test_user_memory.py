"""Tests for explicit user memory candidates and confirmed planner context."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from config import Configuration
from main import create_app
from models import ResearchState, TodoItem
from research.application import ResearchApplicationService
from research.contracts import ResearchCommand, RunStatus
from research.memory import UserMemoryStore
from research.repository import FileRunRepository
from research.session import RunSession
from services.planner import PlanningService


def test_memory_candidate_requires_confirmation_and_can_be_deleted(tmp_path: Path) -> None:
    """Pending candidates stay out of planner context until confirmed."""
    store = UserMemoryStore(tmp_path)
    candidate = store.create_candidate(
        text="Prefer concise Chinese reports",
        kind="preference",
    )
    assert candidate.status == "pending"
    assert store.confirmed_context() == ()

    confirmed = store.confirm(candidate.memory_id)
    assert confirmed.status == "confirmed"
    assert store.confirmed_context()[0]["text"] == "Prefer concise Chinese reports"
    assert store.list(include_pending=False)[0].memory_id == candidate.memory_id

    assert store.delete(candidate.memory_id) is True
    assert store.confirmed_context() == ()
    assert store.delete(candidate.memory_id) is False


class AllowPolicy:
    """Minimal command policy for memory integration tests."""

    def evaluate(self, command: ResearchCommand) -> list[dict[str, str]]:
        del command
        return [{"capability": "research:run", "outcome": "allow", "reason": "ok"}]

    def assert_executable(self, decisions: object) -> None:
        del decisions


class MemoryCoordinator:
    """Complete runs while recording the context made available to planning."""

    def __init__(self) -> None:
        self.contexts: list[dict[str, Any]] = []

    def execute(self, session: RunSession, prior_context: object) -> None:
        del prior_context
        self.contexts.append(dict(session.user_memory_context))
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


def test_application_uses_confirmed_memory_and_records_metrics(tmp_path: Path) -> None:
    """Only confirmed memory reaches a run and its bounded metrics are durable."""
    repository = FileRunRepository(tmp_path / "runs")
    store = UserMemoryStore(repository.root)
    candidate = store.create_candidate(text="Use Chinese headings", kind="preference")
    coordinator = MemoryCoordinator()
    service = ResearchApplicationService(
        coordinator=coordinator,
        repository=repository,
        policy=AllowPolicy(),
        memory_store=store,
    )

    pending_result = service.execute(
        ResearchCommand(topic="pending", config=Configuration(enable_notes=False))
    )
    assert pending_result.status is RunStatus.COMPLETED
    assert coordinator.contexts[-1] == {}
    assert pending_result.metrics["user_memory"]["outcome"] == "miss"

    store.confirm(candidate.memory_id)
    confirmed_result = service.execute(
        ResearchCommand(topic="confirmed", config=Configuration(enable_notes=False))
    )
    assert confirmed_result.status is RunStatus.COMPLETED
    assert coordinator.contexts[-1]["memories"][0]["text"] == "Use Chinese headings"
    assert confirmed_result.metrics["user_memory"]["confirmed_count"] == 1
    assert confirmed_result.metrics["user_memory"]["outcome"] == "hit"
    snapshot = repository.load(confirmed_result.run_id)
    assert snapshot.metrics["user_memory"] == confirmed_result.metrics["user_memory"]


class MemoryApiRunner:
    """Small API facade exposing only the memory routes under test."""

    def __init__(self, root: Path) -> None:
        self.store = UserMemoryStore(root)

    def list_memories(self, *, scope="default", include_pending=True, limit=50):
        return {
            "items": [item.as_dict() for item in self.store.list(
                scope=scope,
                include_pending=include_pending,
                limit=limit,
            )],
            "scope": scope,
            "limit": limit,
        }

    def create_memory_candidate(self, *, text, kind, scope):
        return self.store.create_candidate(text=text, kind=kind, scope=scope)

    def confirm_memory(self, memory_id, *, scope):
        return self.store.confirm(memory_id, scope=scope)

    def delete_memory(self, memory_id, *, scope):
        return self.store.delete(memory_id, scope=scope)


def test_memory_api_enforces_candidate_confirm_delete_gate(tmp_path: Path) -> None:
    """The public API exposes explicit candidate, confirm, list, and delete actions."""
    client = TestClient(create_app(harness_runner=MemoryApiRunner(tmp_path)))
    created = client.post(
        "/memories/candidates",
        json={"text": "Prefer short reports", "kind": "preference"},
    )
    assert created.status_code == 200
    memory_id = created.json()["memory_id"]
    assert created.json()["status"] == "pending"

    pending = client.get("/memories?include_pending=false")
    assert pending.status_code == 200
    assert pending.json()["items"] == []

    confirmed = client.post(f"/memories/{memory_id}/confirm")
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"

    visible = client.get("/memories?include_pending=false")
    assert [item["memory_id"] for item in visible.json()["items"]] == [memory_id]

    deleted = client.delete(f"/memories/{memory_id}")
    assert deleted.status_code == 200
    assert client.delete(f"/memories/{memory_id}").status_code == 404

    missing = client.post("/memories/not-a-memory/confirm")
    assert missing.status_code == 404


class CapturingPlannerAgent:
    """Capture the planner prompt while returning an empty task list."""

    def __init__(self) -> None:
        self.prompt = ""

    def run(self, prompt: str, **kwargs: object) -> str:
        del kwargs
        self.prompt = prompt
        return "[]"

    def clear_history(self) -> None:
        return None


def test_planner_marks_confirmed_memory_as_constraint_not_evidence() -> None:
    """Confirmed memory is bounded and explicitly excluded from source claims."""
    agent = CapturingPlannerAgent()
    service = PlanningService(agent, Configuration(enable_notes=False))
    service.plan_todo_list(
        ResearchState(research_topic="memory topic"),
        user_memories={
            "memories": [
                {
                    "kind": "preference",
                    "text": "Prefer concise reports",
                }
            ]
        },
    )
    assert "Confirmed user preferences and facts" in agent.prompt
    assert "Prefer concise reports" in agent.prompt
    assert "do not cite them as sources" in agent.prompt
