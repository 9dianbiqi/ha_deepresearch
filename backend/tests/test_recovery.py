"""Checkpoint recovery tests for failed and interrupted research runs."""

from __future__ import annotations

import json
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from config import Configuration
from harness import HarnessRunner
from main import create_app
from models import ResearchState, TodoItem
from research.application import RecoveryFailure, ResearchApplicationService
from research.contracts import EventKind, ResearchCommand, RunStatus
from research.operations import OperationSpec
from research.repository import FileRunRepository
from research.session import RunSession

VALID_REPORT = (
    "# Research report\n\n"
    "## Task 1\n"
    "The completed task includes a detailed summary with evidence and explicit "
    "limitations for the requested research topic. "
    "The conclusion is stable enough to support a follow-up question.\n\n"
    "## Sources\n"
    "Source: https://example.test/reference"
)


class AllowPolicy:
    """Allow every test command without external policy state."""

    def evaluate(self, command: ResearchCommand) -> list[dict[str, str]]:
        del command
        return [
            {
                "capability": "research:run",
                "outcome": "allow",
                "reason": "test",
            }
        ]

    def assert_executable(self, decisions: object) -> None:
        del decisions


class FailingThenRecoveringCoordinator:
    """Persist evidence, fail once, and recover without rerunning completed work."""

    def __init__(self, *, failure_phase: str = "research") -> None:
        self.failure_phase = failure_phase
        self.execute_calls = 0
        self.resume_calls = 0

    def execute(self, session: RunSession, prior_context: object) -> None:
        del prior_context
        self.execute_calls += 1
        session.install_plan(
            [TodoItem(id=1, title="Task 1", intent="intent", query="query")]
        )
        session.persist_checkpoint("planning_completed")
        session.start_task(1)
        session.complete_task(
            1,
            summary="durable evidence",
            sources_summary="source summary",
        )
        session.persist_checkpoint("evidence_completed")
        if self.failure_phase == "research":
            raise RuntimeError("simulated process failure")
        if self.failure_phase == "report":
            raise RuntimeError("simulated reporter failure")
        session.set_report(VALID_REPORT)

    def resume(self, session: RunSession, prior_context: object) -> None:
        del prior_context
        self.resume_calls += 1
        assert all(task.status == "completed" for task in session.state.todo_items)
        session.set_report(VALID_REPORT)


class TruncatedThenValidCoordinator:
    """Produce an incomplete streamed report followed by one valid retry."""

    def __init__(self) -> None:
        self.retry_calls = 0

    def execute(self, session: RunSession, prior_context: object) -> None:
        del prior_context
        session.install_plan(
            [TodoItem(id=1, title="Task 1", intent="intent", query="query")]
        )
        session.start_task(1)
        session.complete_task(1, summary="evidence", sources_summary="source")
        session.record_llm_telemetry(
            {
                "role": "reporter",
                "mode": "stream",
                "stream_completed": False,
            }
        )
        session.set_report("# Report")

    def retry_report(self, session: RunSession, prior_context: object) -> None:
        del prior_context
        self.retry_calls += 1
        session.record_llm_telemetry(
            {
                "role": "reporter",
                "mode": "stream",
                "stream_completed": True,
            }
        )
        session.set_report(VALID_REPORT)

def make_service(
    repository: FileRunRepository,
    coordinator: FailingThenRecoveringCoordinator,
) -> ResearchApplicationService:
    return ResearchApplicationService(
        coordinator=coordinator,
        repository=repository,
        policy=AllowPolicy(),
    )


def test_failed_run_persists_checkpoint_and_recovers_after_service_restart() -> None:
    """A failed run is durable and recovery reuses completed task evidence."""
    with TemporaryDirectory() as directory:
        repository = FileRunRepository(directory)
        coordinator = FailingThenRecoveringCoordinator()
        command = ResearchCommand(
            topic="checkpoint recovery",
            config=Configuration(enable_notes=False),
        )
        first = make_service(repository, coordinator).execute(command)

        assert first.status is RunStatus.FAILED
        assert first.error is not None
        assert first.error.code == "coordinator_failed"
        failed_snapshot = repository.load(command.run_id)
        assert failed_snapshot.recovery_resumable is True
        assert failed_snapshot.checkpoint_state is not None
        assert failed_snapshot.checkpoint_state["phase"] == "evidence_completed"

        restarted = make_service(repository, coordinator)
        recovered = restarted.resume(command.run_id)

        assert recovered.status is RunStatus.COMPLETED
        assert recovered.error is None
        assert coordinator.execute_calls == 1
        assert coordinator.resume_calls == 1
        final_snapshot = repository.load(command.run_id)
        assert final_snapshot.status is RunStatus.COMPLETED
        assert sum(
            event.kind is EventKind.TASK_STARTED for event in final_snapshot.events
        ) == 1
        assert any(
            event.kind is EventKind.RUN_RECOVERY_STARTED
            for event in final_snapshot.events
        )


def test_report_only_recovery_does_not_repeat_research() -> None:
    """A reporter failure resumes from evidence and skips task execution."""
    with TemporaryDirectory() as directory:
        repository = FileRunRepository(directory)
        coordinator = FailingThenRecoveringCoordinator(failure_phase="report")
        command = ResearchCommand(
            topic="report-only recovery",
            config=Configuration(enable_notes=False),
        )
        first = make_service(repository, coordinator).execute(command)
        assert first.status is RunStatus.FAILED
        checkpoint = repository.load(command.run_id).checkpoint_state
        assert checkpoint is not None
        assert checkpoint["phase"] == "evidence_completed"

        recovered = make_service(repository, coordinator).resume(command.run_id)
        assert recovered.status is RunStatus.COMPLETED
        assert coordinator.execute_calls == 1
        assert coordinator.resume_calls == 1


def test_truncated_report_retry_keeps_previous_safe_checkpoint() -> None:
    """An incomplete first stream does not prevent the single valid retry."""
    with TemporaryDirectory() as directory:
        repository = FileRunRepository(directory)
        coordinator = TruncatedThenValidCoordinator()
        service = ResearchApplicationService(
            coordinator=coordinator,
            repository=repository,
            policy=AllowPolicy(),
        )
        result = service.execute(
            ResearchCommand(
                topic="truncated report",
                config=Configuration(enable_notes=False),
            )
        )
        assert result.status is RunStatus.COMPLETED
        assert coordinator.retry_calls == 1


def test_corrupt_checkpoint_has_explicit_recovery_error() -> None:
    """Corrupt checkpoint metadata is not replayed as an ordinary application error."""
    with TemporaryDirectory() as directory:
        repository = FileRunRepository(directory)
        coordinator = FailingThenRecoveringCoordinator()
        command = ResearchCommand(
            topic="corrupt checkpoint",
            config=Configuration(enable_notes=False),
        )
        make_service(repository, coordinator).execute(command)
        path = repository.runs_dir / f"{command.run_id}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["snapshot"]["checkpoint_state"]["validated"] = False
        path.write_text(json.dumps(raw), encoding="utf-8")

        with pytest.raises(RecoveryFailure) as raised:
            make_service(repository, coordinator).resume(command.run_id)
        assert raised.value.code == "checkpoint_corrupt"


def test_completed_run_is_not_accepted_by_recovery_api() -> None:
    """Recovery is reserved for failed or interrupted runs."""
    with TemporaryDirectory() as directory:
        repository = FileRunRepository(directory)
        coordinator = FailingThenRecoveringCoordinator(failure_phase="none")
        command = ResearchCommand(
            topic="already complete",
            config=Configuration(enable_notes=False),
        )
        result = make_service(repository, coordinator).execute(command)
        assert result.status is RunStatus.COMPLETED

        with pytest.raises(RecoveryFailure) as raised:
            make_service(repository, coordinator).resume(command.run_id)
        assert raised.value.code == "run_not_resumable"


def test_uncertain_side_effect_fails_closed_until_it_is_terminal() -> None:
    """An unknown side effect blocks recovery instead of replaying blindly."""
    with TemporaryDirectory() as directory:
        repository = FileRunRepository(directory)
        command = ResearchCommand(
            topic="unsafe checkpoint",
            config=Configuration(enable_notes=False),
        )
        session = RunSession(
            command=command,
            state=ResearchState(research_topic=command.topic),
            checkpoint_writer=repository.save_checkpoint,
        )
        session.start()
        session.persist_checkpoint("run_created")
        spec = OperationSpec(
            operation_name="notes.create",
            capabilities=("notes:write",),
            resource={"action": "create", "note_kind": "task"},
            operation_id=uuid4().hex,
        )
        session.start_operation(spec)
        session.persist_checkpoint("research_tasks_progress")
        assert session.checkpoint_phase == "research_tasks_progress"
        checkpoint = repository.load(command.run_id).checkpoint_state
        assert checkpoint is not None
        assert checkpoint["resumable"] is False
        assert checkpoint["recovery_blocked_reason"] == "operation_outcome_uncertain"
        with pytest.raises(RecoveryFailure) as raised:
            make_service(
                repository,
                FailingThenRecoveringCoordinator(),
            ).resume(command.run_id)
        assert raised.value.code == "run_not_resumable"

        session.fail_operation(spec, duration_seconds=0, code="operation_failed")
        session.persist_checkpoint("research_tasks_progress")
        assert repository.load(command.run_id).checkpoint_state["phase"] == (
            "research_tasks_progress"
        )


def test_restore_discards_partial_task_output_and_marks_active_operation_uncertain() -> None:
    """Recovery never trusts a partial stream or an in-flight operation result."""
    with TemporaryDirectory() as directory:
        repository = FileRunRepository(directory)
        command = ResearchCommand(
            topic="partial task",
            config=Configuration(enable_notes=False),
        )
        session = RunSession(
            command=command,
            state=ResearchState(research_topic=command.topic),
            checkpoint_writer=repository.save_checkpoint,
        )
        session.start()
        session.install_plan(
            [TodoItem(id=1, title="Task 1", intent="intent", query="query")]
        )
        session.start_task(1)
        spec = OperationSpec(
            operation_name="search.execute",
            capabilities=("search:web",),
            resource={"query_hash": "a" * 64, "backend": "fake"},
            operation_id=uuid4().hex,
            task_id=1,
        )
        session.start_operation(spec)
        session.persist_checkpoint("research_tasks_progress")
        restored = RunSession.restore_from_snapshot(repository.load(command.run_id))
        assert restored.state.todo_items[0].status == "pending"
        assert restored.metrics["operations"]["uncertain"] == 1


def test_recovery_http_endpoint_uses_same_run_id() -> None:
    """The public recovery endpoint returns the recovered run, not a child run."""
    with TemporaryDirectory() as directory:
        repository = FileRunRepository(directory)
        coordinator = FailingThenRecoveringCoordinator()
        service = make_service(repository, coordinator)
        runner = HarnessRunner(application=service, repository=repository)
        request = ResearchCommand(
            topic="HTTP recovery",
            config=Configuration(enable_notes=False),
        )
        failed = runner.run(request)
        assert failed.status == "failed"

        with TestClient(create_app(harness_runner=runner)) as client:
            response = client.post("/research/recover", json={"run_id": request.run_id})
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["run_id"] == request.run_id
        assert payload["status"] == "completed"


def test_recovery_sse_emits_recovery_boundary_and_terminal_event() -> None:
    """Recovery streaming keeps the original ID and exposes its boundary."""
    with TemporaryDirectory() as directory:
        repository = FileRunRepository(directory)
        coordinator = FailingThenRecoveringCoordinator()
        service = make_service(repository, coordinator)
        runner = HarnessRunner(application=service, repository=repository)
        request = ResearchCommand(
            topic="SSE recovery",
            config=Configuration(enable_notes=False),
        )
        assert runner.run(request).status == "failed"

        with TestClient(create_app(harness_runner=runner)) as client:
            with client.stream(
                "POST",
                "/research/recover/stream",
                json={"run_id": request.run_id},
            ) as response:
                assert response.status_code == 200
                events = [
                    json.loads(line[5:].strip())
                    for line in response.iter_lines()
                    if line and line.startswith("data:")
                ]
        assert events[0]["run_id"] == request.run_id
        assert any(event["type"] == "status" for event in events)
        assert events[-1]["type"] == "done"
        assert events[-1]["run_id"] == request.run_id
