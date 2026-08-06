"""Real compatibility-facade and legacy SSE contract tests."""

from __future__ import annotations

import asyncio
import importlib
import json
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from queue import Queue
from typing import Any
from uuid import uuid4

import pytest

from config import Configuration
from harness.models import HarnessRunResult
from harness.runner import HarnessRunner, _HarnessStreamIterator, _Submission
from models import TodoItem
from research.application import ResearchApplicationService
from research.contracts import (
    EventKind,
    ResearchCommand,
    ResearchEvent,
    ResearchRunResult,
    RunError,
    RunSnapshot,
    RunStatus,
)
from research.legacy_sse import LegacySseProjector
from research.operations import OperationRejectedError, OperationSpec
from research.repository import RunNotFoundError
from research.session import CancellationToken, RunSession


class AllowingPolicy:
    """Permit deterministic application-service executions."""

    def evaluate_capability(
        self,
        capability: str,
        command: ResearchCommand,
    ) -> dict[str, str]:
        del command
        return {
            "capability": capability,
            "outcome": "allow",
            "reason": "allowed",
        }

    def evaluate(self, command: ResearchCommand) -> list[dict[str, str]]:
        return [self.evaluate_capability("research:run", command)]

    def assert_executable(self, decisions: object) -> None:
        del decisions


class CompletingCoordinator:
    """Produce one complete canonical run with compatibility metadata."""

    def execute(self, session: RunSession, prior_context: object) -> None:
        del prior_context
        session.install_plan(
            [
                TodoItem(
                    id=1,
                    title="Task",
                    intent="Intent",
                    query="query",
                    stream_token="task_1",
                    source_strategy="github_api_then_web",
                    repository="owner/repository",
                )
            ]
        )
        session.start_task(1)
        session.record_sources(
            1,
            context="worker-only context",
            latest_sources="- Source https://example.test",
            backend="fake",
            notices=["Search backend returned a notice."],
            notice_codes=["search_backend_notice"],
        )
        session.append_task_summary(1, "summary")
        session.complete_task(
            1,
            summary="summary",
            sources_summary="- Source https://example.test",
            original_query="query",
        )
        session.set_report("# Report")


class MinimalCompletingCoordinator:
    """Complete through the canonical lifecycle with the smallest valid state."""

    def execute(self, session: RunSession, prior_context: object) -> None:
        del prior_context
        session.set_report("# Report")


@dataclass
class MemoryRepository:
    """Small canonical repository that can fail required persistence."""

    fail_save: bool = False
    snapshots: dict[str, RunSnapshot] = field(default_factory=dict)
    save_count: int = 0
    load_count: int = 0

    def save(self, snapshot: RunSnapshot) -> None:
        self.save_count += 1
        if self.fail_save:
            raise RuntimeError("Authorization: Bearer persistence-secret")
        self.snapshots[snapshot.run_id] = snapshot

    def load(self, run_id: str) -> RunSnapshot:
        self.load_count += 1
        try:
            return self.snapshots[run_id]
        except KeyError as exc:
            raise RunNotFoundError(run_id) from exc


class CountingApplication:
    """Count calls while delegating to the real application lifecycle."""

    def __init__(
        self,
        delegate: ResearchApplicationService,
        repository: MemoryRepository,
    ) -> None:
        self._delegate = delegate
        self.repository = repository
        self.execute_count = 0

    def execute(self, *args: object, **kwargs: object) -> ResearchRunResult:
        self.execute_count += 1
        return self._delegate.execute(*args, **kwargs)  # type: ignore[arg-type]


def make_application(
    repository: MemoryRepository,
) -> ResearchApplicationService:
    return ResearchApplicationService(
        coordinator=CompletingCoordinator(),
        repository=repository,
        policy=AllowingPolicy(),
    )


def make_facade(
    *,
    repository: MemoryRepository | None = None,
    max_workers: int = 1,
    admission_capacity: int | None = None,
) -> tuple[HarnessRunner, CountingApplication, MemoryRepository]:
    canonical_repository = repository or MemoryRepository()
    application = CountingApplication(
        make_application(canonical_repository),
        canonical_repository,
    )
    facade = HarnessRunner(
        application=application,  # type: ignore[arg-type]
        repository=canonical_repository,
        max_workers=max_workers,
        queue_capacity=2,
        admission_capacity=admission_capacity,
    )
    return facade, application, canonical_repository


def make_command(topic: str = "topic") -> ResearchCommand:
    return ResearchCommand(topic=topic, config=Configuration.from_env())


def make_event(
    kind: EventKind,
    *,
    payload: dict[str, Any] | None = None,
    task_id: int | None = None,
) -> ResearchEvent:
    return ResearchEvent(
        kind=kind,
        run_id=uuid4().hex,
        sequence=7,
        occurred_at=datetime.now(timezone.utc),
        payload=payload or {},
        task_id=task_id,
        operation_id="internal-operation-marker",
    )


def legacy_projector() -> Any:
    module = importlib.import_module("research.legacy_sse")
    return module.LegacySseProjector()


def test_application_exposes_read_only_canonical_repository_identity() -> None:
    repository = MemoryRepository()
    application = make_application(repository)

    assert application.repository is repository
    with pytest.raises(AttributeError):
        application.repository = MemoryRepository()  # type: ignore[misc]


def test_done_is_observed_only_after_completed_record_is_loadable() -> None:
    facade, application, repository = make_facade()
    command = make_command()
    seen_types: list[str] = []

    for event in facade.stream(command):
        seen_types.append(event["type"])
        if event["type"] == "done":
            record = facade.load_record(command.run_id)
            assert record["status"] == "completed"

    assert seen_types[-1] == "done"
    assert seen_types.count("done") == 1
    assert application.execute_count == 1
    assert repository.save_count == 1


def test_stream_done_waits_for_release_before_immediate_followup() -> None:
    repository = MemoryRepository()
    delegate = ResearchApplicationService(
        coordinator=MinimalCompletingCoordinator(),
        repository=repository,
        policy=AllowingPolicy(),
    )
    application = TerminalObserverPausingApplication(delegate, repository)
    facade = HarnessRunner(
        application=application,  # type: ignore[arg-type]
        repository=repository,
        max_workers=1,
    )
    parent = make_command("parent")
    returned_before_release = False

    try:
        with ThreadPoolExecutor(max_workers=1) as client_executor:
            parent_events_future = client_executor.submit(
                list,
                facade.stream(parent),
            )
            assert application.terminal_forwarded.wait(timeout=1)
            returned_before_release = parent_events_future.done()
            application.release_terminal.set()
            parent_events = parent_events_future.result(timeout=2)

        child = ResearchCommand(
            topic="follow-up",
            config=Configuration.from_env(),
            parent_run_id=parent.run_id,
        )
        child_events = list(facade.stream(child))
    finally:
        application.release_terminal.set()
        facade._executor.shutdown(wait=True, cancel_futures=True)

    assert not returned_before_release
    assert parent_events[-1]["type"] == "done"
    assert child_events[-1]["type"] == "done"
    assert not any(
        event.get("code") in {"parent_pending", "runner_busy"}
        for event in child_events
    )


def test_required_repository_failure_has_one_safe_error_and_no_done() -> None:
    facade, application, _repository = make_facade(
        repository=MemoryRepository(fail_save=True)
    )
    command = make_command()

    events = list(facade.stream(command))
    serialized = json.dumps(events, ensure_ascii=False)

    assert [event["type"] for event in events].count("error") == 1
    assert not any(event["type"] == "done" for event in events)
    assert events[-1]["code"] == "persistence_failed"
    assert "persistence-secret" not in serialized
    assert application.execute_count == 1


def test_run_and_stream_each_call_application_execute_exactly_once() -> None:
    facade, application, _repository = make_facade()

    sync = facade.run(make_command("sync"))
    assert sync.status == "completed"
    assert application.execute_count == 1

    stream_events = list(facade.stream(make_command("stream")))
    assert stream_events[-1]["type"] == "done"
    assert application.execute_count == 2


def test_sync_run_returns_only_after_capacity_release_before_next_run() -> None:
    repository = MemoryRepository()
    application = CountingApplication(
        make_application(repository),
        repository,
    )
    facade = HarnessRunner(
        application=application,  # type: ignore[arg-type]
        repository=repository,
        max_workers=1,
    )
    facade._executor.shutdown(wait=True, cancel_futures=True)
    controlled_executor = FirstCallbackDelayedExecutor()
    facade._executor = controlled_executor  # type: ignore[assignment]
    returned_before_release = False
    run_returned = threading.Event()

    def run_first() -> HarnessRunResult:
        result = facade.run(make_command("first"))
        run_returned.set()
        return result

    try:
        with ThreadPoolExecutor(max_workers=1) as client_executor:
            first_future = client_executor.submit(run_first)
            assert controlled_executor.first_submitted.wait(timeout=1)
            delayed_future = controlled_executor.first_future
            assert delayed_future is not None
            assert delayed_future.callback_registered.wait(timeout=1)
            returned_before_release = run_returned.wait(timeout=0.2)
            delayed_future.release_callbacks()
            first = first_future.result(timeout=2)

        second = facade.run(make_command("second"))
    finally:
        delayed_future = controlled_executor.first_future
        if delayed_future is not None:
            delayed_future.release_callbacks()
        facade._executor.shutdown(wait=True, cancel_futures=True)

    assert not returned_before_release
    assert first.status == "completed"
    assert second.status == "completed"
    assert second.error_code is None
    assert application.execute_count == 2


def test_load_record_returns_detached_json_ready_canonical_snapshot() -> None:
    facade, _application, _repository = make_facade()
    command = make_command()
    facade.run(command)

    first = facade.load_record(command.run_id)
    json.dumps(first)
    first["output"]["todo_items"][0]["title"] = "mutated"
    second = facade.load_record(command.run_id)

    assert second["status"] == "completed"
    assert second["output"]["todo_items"][0]["title"] == "Task"


class BlockingApplication:
    """Wait cooperatively so close and admission behavior are observable."""

    def __init__(self, repository: MemoryRepository) -> None:
        self.repository = repository
        self.calls = 0
        self.entered = threading.Event()
        self.exited = threading.Event()
        self.tokens: list[CancellationToken] = []

    def execute(
        self,
        command: ResearchCommand,
        *,
        observer: Any,
        cancellation: CancellationToken,
    ) -> ResearchRunResult:
        self.calls += 1
        self.tokens.append(cancellation)
        observer(
            ResearchEvent(
                kind=EventKind.RUN_STARTED,
                run_id=command.run_id,
                sequence=1,
                occurred_at=datetime.now(timezone.utc),
                payload={"topic": command.topic},
            )
        )
        self.entered.set()
        try:
            while not cancellation.wait(0.01):
                pass
            observer(
                ResearchEvent(
                    kind=EventKind.RUN_CANCELLED,
                    run_id=command.run_id,
                    sequence=2,
                    occurred_at=datetime.now(timezone.utc),
                    payload={
                        "code": "cancelled",
                        "message": "Research run was cancelled.",
                    },
                )
            )
            return ResearchRunResult(
                run_id=command.run_id,
                status=RunStatus.CANCELLED,
                output=None,
                error=RunError(
                    code="cancelled",
                    message="Research run was cancelled.",
                ),
                metrics={},
                followup_context={},
                policy_decisions=(),
            )
        finally:
            self.exited.set()


class TerminalPausingApplication:
    """Pause after terminal observation so generator-close races are visible."""

    def __init__(self, repository: MemoryRepository, kind: EventKind) -> None:
        self.repository = repository
        self.kind = kind
        self.observed = threading.Event()
        self.release = threading.Event()
        self.exited = threading.Event()
        self.tokens: list[CancellationToken] = []

    def execute(
        self,
        command: ResearchCommand,
        *,
        observer: Any,
        cancellation: CancellationToken,
    ) -> ResearchRunResult:
        self.tokens.append(cancellation)
        is_completed = self.kind is EventKind.RUN_COMPLETED
        error = None if is_completed else RunError(code="run_failed", message="failed")
        observer(
            ResearchEvent(
                kind=self.kind,
                run_id=command.run_id,
                sequence=1,
                occurred_at=datetime.now(timezone.utc),
                payload={} if error is None else {"code": error.code},
            )
        )
        self.observed.set()
        try:
            while not self.release.wait(0.01):
                if cancellation.is_cancelled:
                    break
            return ResearchRunResult(
                run_id=command.run_id,
                status=RunStatus.COMPLETED if is_completed else RunStatus.FAILED,
                output=None,
                error=error,
                metrics={},
                followup_context={},
                policy_decisions=(),
            )
        finally:
            self.exited.set()


class TerminalObserverPausingApplication:
    """Pause the real application after forwarding its canonical terminal."""

    def __init__(
        self,
        delegate: ResearchApplicationService,
        repository: MemoryRepository,
    ) -> None:
        self._delegate = delegate
        self.repository = repository
        self.terminal_forwarded = threading.Event()
        self.release_terminal = threading.Event()

    def execute(
        self,
        command: ResearchCommand,
        *,
        observer: Any,
        cancellation: CancellationToken,
    ) -> ResearchRunResult:
        def pausing_observer(event: ResearchEvent) -> None:
            observer(event)
            if event.kind in {
                EventKind.RUN_COMPLETED,
                EventKind.RUN_FAILED,
                EventKind.RUN_CANCELLED,
                EventKind.RUN_REJECTED,
            }:
                self.terminal_forwarded.set()
                self.release_terminal.wait(timeout=2)

        return self._delegate.execute(
            command,
            observer=pausing_observer,
            cancellation=cancellation,
        )

    def is_run_active(self, run_id: str) -> bool:
        """Expose the delegate's active registry to the compatibility facade."""
        return self._delegate.is_run_active(run_id)


class DelayedCallbackFuture(Future[ResearchRunResult]):
    """Expose a completed result while retaining callbacks until released."""

    def __init__(self) -> None:
        super().__init__()
        self.callback_registered = threading.Event()
        self._delayed_callbacks: list[Any] = []

    def add_done_callback(self, fn: Any) -> None:
        self._delayed_callbacks.append(fn)
        self.callback_registered.set()

    def release_callbacks(self) -> None:
        callbacks = list(self._delayed_callbacks)
        self._delayed_callbacks.clear()
        for callback in callbacks:
            callback(self)


class FirstCallbackDelayedExecutor:
    """Run inline and delay only the first submitted Future callback."""

    def __init__(self) -> None:
        self.first_future: DelayedCallbackFuture | None = None
        self.first_submitted = threading.Event()
        self._submission_count = 0

    def submit(self, callback: Any, *args: object, **kwargs: object) -> Any:
        self._submission_count += 1
        future: Future[ResearchRunResult]
        if self._submission_count == 1:
            delayed = DelayedCallbackFuture()
            self.first_future = delayed
            self.first_submitted.set()
            future = delayed
        else:
            future = Future()
        try:
            result = callback(*args, **kwargs)
        except BaseException as exc:
            future.set_exception(exc)
        else:
            future.set_result(result)
        return future

    def shutdown(self, *args: object, **kwargs: object) -> None:
        del args, kwargs


class DuplicateTrackingApplication:
    """Expose whether the facade submits the same in-flight run twice."""

    def __init__(self, repository: MemoryRepository) -> None:
        self.repository = repository
        self.calls = 0
        self.entered = threading.Event()

    def execute(
        self,
        command: ResearchCommand,
        *,
        observer: Any,
        cancellation: CancellationToken,
    ) -> ResearchRunResult:
        self.calls += 1
        call_number = self.calls
        observer(
            ResearchEvent(
                kind=EventKind.RUN_STARTED,
                run_id=command.run_id,
                sequence=1,
                occurred_at=datetime.now(timezone.utc),
            )
        )
        self.entered.set()
        if call_number == 1:
            while not cancellation.wait(0.01):
                pass
            status = RunStatus.CANCELLED
            kind = EventKind.RUN_CANCELLED
            error = RunError(code="cancelled", message="cancelled")
        else:
            status = RunStatus.COMPLETED
            kind = EventKind.RUN_COMPLETED
            error = None
        observer(
            ResearchEvent(
                kind=kind,
                run_id=command.run_id,
                sequence=2,
                occurred_at=datetime.now(timezone.utc),
                payload={} if error is None else {"code": error.code},
            )
        )
        return ResearchRunResult(
            run_id=command.run_id,
            status=status,
            output=None,
            error=error,
            metrics={},
            followup_context={},
            policy_decisions=(),
        )


class EventThenExplodingApplication:
    """Emit one high-sequence event before its future raises."""

    def __init__(self, repository: MemoryRepository) -> None:
        self.repository = repository

    def execute(
        self,
        command: ResearchCommand,
        *,
        observer: Any,
        cancellation: CancellationToken,
    ) -> ResearchRunResult:
        del cancellation
        observer(
            ResearchEvent(
                kind=EventKind.RUN_STARTED,
                run_id=command.run_id,
                sequence=7,
                occurred_at=datetime.now(timezone.utc),
            )
        )
        raise RuntimeError("Authorization: Bearer application-secret")


class ControlledTerminalCoordinator:
    """Commit audit gaps before cancellation or rejection reaches Application."""

    def __init__(self, terminal: RunStatus) -> None:
        self.terminal = terminal
        self.session: RunSession | None = None

    def execute(self, session: RunSession, prior_context: object) -> None:
        del prior_context
        self.session = session
        session.install_plan(
            [TodoItem(id=1, title="Task", intent="Intent", query="query")]
        )
        completed = OperationSpec(
            operation_name="search.execute",
            capabilities=("search:web",),
            resource={"query_hash": "0" * 64, "backend": "duckduckgo"},
        )
        session.start_operation(completed)
        session.complete_operation(completed, duration_seconds=0.0)

        if self.terminal is RunStatus.REJECTED:
            rejected = OperationSpec(
                operation_name="search.execute",
                capabilities=("search:web",),
                resource={"query_hash": "1" * 64, "backend": "duckduckgo"},
            )
            session.reject_operation(rejected)
            session.request_cancellation()
            raise OperationRejectedError(rejected.operation_id)

        session.request_cancellation()
        session.raise_if_cancelled()


class PausingCoordinator:
    """Keep a canonical parent active until a concurrency assertion completes."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def execute(self, session: RunSession, prior_context: object) -> None:
        del session, prior_context
        self.entered.set()
        self.release.wait(timeout=2)
        raise RuntimeError("expected test stop")


class TerminalOnDoneFuture(Future[ResearchRunResult]):
    """Enqueue a typed terminal exactly when the consumer observes completion."""

    def __init__(
        self,
        event_queue: Queue[ResearchEvent],
        event: ResearchEvent,
        result: ResearchRunResult,
    ) -> None:
        super().__init__()
        self._event_queue = event_queue
        self._event = event
        self._terminal_result = result

    def done(self) -> bool:
        if not super().done():
            self._event_queue.put_nowait(self._event)
            self.set_result(self._terminal_result)
        return True


def make_blocking_facade() -> tuple[HarnessRunner, BlockingApplication]:
    repository = MemoryRepository()
    application = BlockingApplication(repository)
    facade = HarnessRunner(
        application=application,  # type: ignore[arg-type]
        repository=repository,
        max_workers=1,
        queue_capacity=1,
    )
    return facade, application


def test_stream_close_is_prompt_and_cancels_active_application() -> None:
    facade, application = make_blocking_facade()
    stream = facade.stream(make_command())
    assert next(stream)["type"] == "status"
    assert application.entered.wait(timeout=1)

    started = time.monotonic()
    stream.close()
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert application.exited.wait(timeout=1)
    assert application.tokens[0].is_cancelled


def test_busy_facade_rejects_without_submitting_beyond_admission_bound() -> None:
    facade, application = make_blocking_facade()
    assert facade.admission_capacity == facade.max_workers
    first = facade.stream(make_command("first"))
    assert next(first)["type"] == "status"
    assert application.entered.wait(timeout=1)

    busy_events = list(facade.stream(make_command("second")))

    assert application.calls == 1
    assert busy_events == [
        {
            "type": "error",
            "run_id": busy_events[0]["run_id"],
            "schema_version": 1,
            "sequence": 1,
            "code": "runner_busy",
            "detail": "Research execution capacity is busy.",
        }
    ]
    first.close()
    assert application.exited.wait(timeout=1)


def test_active_parent_followup_reports_parent_pending_before_busy_capacity() -> None:
    repository = MemoryRepository()
    coordinator = PausingCoordinator()
    application = ResearchApplicationService(
        coordinator=coordinator,
        repository=repository,
        policy=AllowingPolicy(),
    )
    facade = HarnessRunner(
        application=application,
        repository=repository,
        max_workers=1,
    )
    parent = make_command("parent")
    parent_stream = facade.stream(parent)

    try:
        assert next(parent_stream)["type"] == "status"
        assert coordinator.entered.wait(timeout=1)
        child = ResearchCommand(
            topic="child",
            config=Configuration.from_env(),
            parent_run_id=parent.run_id,
        )

        events = list(facade.stream(child))

        assert [event["type"] for event in events] == ["error"]
        assert events[0]["code"] == "parent_pending"
        assert events[0]["detail"] == "Parent run is active but not yet durable."
    finally:
        coordinator.release.set()
        list(parent_stream)
        facade._executor.shutdown(wait=True, cancel_futures=True)


def test_facade_rejects_admission_capacity_above_active_worker_bound() -> None:
    repository = MemoryRepository()
    application = make_application(repository)

    with pytest.raises(ValueError, match="cannot exceed max_workers"):
        HarnessRunner(
            application=application,
            repository=repository,
            max_workers=1,
            admission_capacity=2,
        )


@pytest.mark.parametrize("max_workers", [1, 2])
def test_facade_reserves_same_run_id_before_executor_submission(
    max_workers: int,
) -> None:
    repository = MemoryRepository()
    application = DuplicateTrackingApplication(repository)
    facade = HarnessRunner(
        application=application,  # type: ignore[arg-type]
        repository=repository,
        max_workers=max_workers,
    )
    command = make_command()
    first = facade.stream(command)
    assert next(first)["type"] == "status"
    assert application.entered.wait(timeout=1)

    try:
        duplicate_events = list(facade.stream(command))

        assert application.calls == 1
        assert [event["type"] for event in duplicate_events] == ["error"]
        assert duplicate_events[0]["code"] == "run_already_active"
    finally:
        first.close()
        facade._executor.shutdown(wait=True, cancel_futures=True)


def test_synthetic_stream_error_sequence_follows_observed_events() -> None:
    repository = MemoryRepository()
    application = EventThenExplodingApplication(repository)
    facade = HarnessRunner(
        application=application,  # type: ignore[arg-type]
        repository=repository,
        max_workers=1,
    )

    events = list(facade.stream(make_command()))

    assert [event["sequence"] for event in events] == [7, 8]
    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "application_error"
    assert "application-secret" not in json.dumps(events)


@pytest.mark.parametrize(
    ("terminal_status", "terminal_kind", "terminal_code"),
    [
        (RunStatus.CANCELLED, EventKind.RUN_CANCELLED, "cancelled"),
        (RunStatus.REJECTED, EventKind.RUN_REJECTED, "operation_rejected"),
    ],
)
def test_controlled_terminal_keeps_canonical_sequence_in_facade_stream(
    terminal_status: RunStatus,
    terminal_kind: EventKind,
    terminal_code: str,
) -> None:
    repository = MemoryRepository()
    coordinator = ControlledTerminalCoordinator(terminal_status)
    application = ResearchApplicationService(
        coordinator=coordinator,
        repository=repository,
        policy=AllowingPolicy(),
    )
    facade = HarnessRunner(
        application=application,
        repository=repository,
        max_workers=1,
    )

    try:
        events = list(facade.stream(make_command()))
    finally:
        facade._executor.shutdown(wait=True, cancel_futures=True)

    assert coordinator.session is not None
    canonical_terminal = coordinator.session.events[-1]
    assert canonical_terminal.kind is terminal_kind
    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == terminal_code
    assert events[-1]["sequence"] == canonical_terminal.sequence


def test_future_completion_drains_enqueued_terminal_before_synthetic_error() -> None:
    run_id = uuid4().hex
    event_queue: Queue[ResearchEvent] = Queue()
    terminal = ResearchEvent(
        kind=EventKind.RUN_COMPLETED,
        run_id=run_id,
        sequence=9,
        occurred_at=datetime.now(timezone.utc),
    )
    result = ResearchRunResult(
        run_id=run_id,
        status=RunStatus.COMPLETED,
        output=None,
        error=None,
        metrics={},
        followup_context={},
        policy_decisions=(),
    )
    future = TerminalOnDoneFuture(event_queue, terminal, result)
    submission = _Submission(future=future)
    submission.released.set()
    submission.terminal_wakeup.set()
    stream = _HarnessStreamIterator(
        run_id=run_id,
        cancellation=CancellationToken(),
        closed=threading.Event(),
        event_queue=event_queue,
        projector=LegacySseProjector(),
        submission=submission,
    )

    assert next(stream) == {
        "run_id": run_id,
        "schema_version": 1,
        "sequence": 9,
        "type": "done",
    }


def test_build_default_serializes_shared_coordinator_runs(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("NOTES_WORKSPACE", str(tmp_path / "notes"))
    facade = HarnessRunner.build_default(base_path=tmp_path)
    try:
        assert facade.max_workers == 1
        assert facade.admission_capacity == 1
        assert facade.application.repository is facade.repository
        coordinator = facade.application._coordinator
        assert coordinator._operation_authorizer is facade.application._policy
    finally:
        facade._executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize("code", ["operation_rejected", "deadline_exceeded"])
def test_synthetic_facade_error_preserves_governed_terminal_code(code: str) -> None:
    event = HarnessRunner._error_event(
        uuid4().hex,
        code=code,
        detail="Safe terminal detail.",
    )

    assert event["code"] == code


@pytest.mark.parametrize(
    ("kind", "legacy_type"),
    [
        (EventKind.RUN_COMPLETED, "done"),
        (EventKind.RUN_FAILED, "error"),
    ],
)
def test_terminal_waits_for_release_and_close_does_not_cancel_application(
    kind: EventKind,
    legacy_type: str,
) -> None:
    repository = MemoryRepository()
    application = TerminalPausingApplication(repository, kind)
    facade = HarnessRunner(
        application=application,  # type: ignore[arg-type]
        repository=repository,
        max_workers=1,
    )
    stream = facade.stream(make_command())
    visible_before_release = False

    try:
        with ThreadPoolExecutor(max_workers=1) as client_executor:
            terminal_future = client_executor.submit(next, stream)
            assert application.observed.wait(timeout=1)
            visible_before_release = terminal_future.done()
            application.release.set()
            terminal = terminal_future.result(timeout=2)
        stream.close()
        assert not application.tokens[0].is_cancelled
    finally:
        application.release.set()
        facade._executor.shutdown(wait=True, cancel_futures=True)

    assert not visible_before_release
    assert terminal["type"] == legacy_type
    assert application.exited.wait(timeout=1)


def test_asgi_disconnect_promptly_cancels_active_application() -> None:
    from main import _streaming_response

    facade, application = make_blocking_facade()
    command = make_command()
    response = _streaming_response(facade, command)
    sent: list[dict[str, Any]] = []
    request_delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal request_delivered
        if not request_delivered:
            request_delivered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.to_thread(application.entered.wait, 1)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/research/stream",
        "raw_path": b"/research/stream",
        "query_string": b"",
        "headers": [],
        "client": ("test", 1),
        "server": ("test", 80),
        "root_path": "",
    }

    try:
        asyncio.run(asyncio.wait_for(response(scope, receive, send), timeout=1))
        assert application.entered.wait(timeout=1)
        token = application.tokens[0]
        cancelled_before_cleanup = token.is_cancelled
        exited_before_cleanup = application.exited.wait(timeout=0.25)
    finally:
        if application.tokens:
            application.tokens[0].cancel()
        application.exited.wait(timeout=1)
        facade._executor.shutdown(wait=True, cancel_futures=True)

    assert any(message["type"] == "http.response.start" for message in sent)
    assert cancelled_before_cleanup
    assert exited_before_cleanup


@pytest.mark.parametrize(
    ("kind", "payload", "task_id", "legacy_type"),
    [
        (EventKind.RUN_STARTED, {"topic": "topic"}, None, "status"),
        (
            EventKind.REPOSITORY_DETECTED,
            {"repository": {"full_name": "owner/repository"}},
            None,
            "github_repository",
        ),
        (EventKind.PLAN_CREATED, {"tasks": []}, None, "todo_list"),
        (EventKind.TASK_STARTED, {"step": 1}, 1, "task_status"),
        (EventKind.TASK_COMPLETED, {"step": 1}, 1, "task_status"),
        (EventKind.TASK_SKIPPED, {"step": 1}, 1, "task_status"),
        (EventKind.TASK_FAILED, {"step": 1}, 1, "task_status"),
        (EventKind.SOURCES_COLLECTED, {"step": 1}, 1, "sources"),
        (EventKind.SUMMARY_DELTA, {"chunk": "x", "step": 1}, 1, "task_summary_chunk"),
        (EventKind.TASK_RETRY_SCHEDULED, {"step": 1}, 1, "task_retry"),
        (EventKind.REPORT_NOTE_CREATED, {"note_id": "n"}, None, "report_note"),
        (EventKind.REPORT_GENERATED, {"report": "# Report"}, None, "final_report"),
        (EventKind.RUN_COMPLETED, {}, None, "done"),
        (EventKind.RUN_FAILED, {"code": "failed"}, None, "error"),
        (EventKind.RUN_REJECTED, {"code": "rejected"}, None, "error"),
        (EventKind.RUN_CANCELLED, {"code": "cancelled"}, None, "error"),
    ],
)
def test_legacy_projector_golden_event_kind_mapping(
    kind: EventKind,
    payload: dict[str, Any],
    task_id: int | None,
    legacy_type: str,
) -> None:
    event = make_event(kind, payload=payload, task_id=task_id)

    projected = legacy_projector().project(event)

    assert projected is not None
    assert projected["type"] == legacy_type
    assert projected["run_id"] == event.run_id
    assert projected["schema_version"] == event.schema_version
    assert projected["sequence"] == event.sequence
    assert "operation_id" not in projected


@pytest.mark.parametrize(
    "kind",
    [
        EventKind.POLICY_CHECKED,
        EventKind.OPERATION_STARTED,
        EventKind.OPERATION_COMPLETED,
        EventKind.OPERATION_FAILED,
        EventKind.OPERATION_REJECTED,
    ],
)
def test_legacy_projector_suppresses_internal_event_kinds(kind: EventKind) -> None:
    assert legacy_projector().project(make_event(kind)) is None


def test_legacy_projector_preserves_github_and_task_compatibility_fields() -> None:
    projector = legacy_projector()
    github = projector.project(
        make_event(
            EventKind.REPOSITORY_DETECTED,
            payload={
                "repository": {
                    "owner": "owner",
                    "repo": "repository",
                    "full_name": "owner/repository",
                    "url": "https://github.com/owner/repository",
                    "stars": 7,
                    "raw_body": "must-not-project",
                },
                "notices": ["GitHub API returned a notice."],
                "notice_codes": ["github_api_notice"],
            },
        )
    )
    source = projector.project(
        make_event(
            EventKind.SOURCES_COLLECTED,
            task_id=1,
            payload={
                "latest_sources": "- source",
                "backend": "fake",
                "step": 2,
                "stream_token": "task_1",
                "note_id": "note-1",
                "note_path": "safe/note-1.md",
                "source_strategy": "github_api_then_web",
                "repository": "owner/repository",
                "notices": ["Search backend returned a notice."],
                "notice_codes": ["search_backend_notice"],
                "raw_context": "must-not-project",
            },
        )
    )

    assert github is not None
    assert github["repository"]["full_name"] == "owner/repository"
    assert "raw_body" not in github["repository"]
    assert source is not None
    assert source["source_strategy"] == "github_api_then_web"
    assert source["repository"] == "owner/repository"
    assert source["note_id"] == "note-1"
    assert source["step"] == 2
    assert source["stream_token"] == "task_1"
    assert "raw_context" not in source


def test_legacy_projector_whitelists_payload_and_redacts_terminal_messages() -> None:
    sentinel = "Authorization: Bearer private-token RAW_PROVIDER_BODY"
    projector = legacy_projector()
    failed = projector.project(
        make_event(
            EventKind.RUN_FAILED,
            payload={
                "code": "persistence_failed",
                "message": sentinel,
                "raw_body": sentinel,
                "operation_marker": sentinel,
            },
        )
    )
    planned = projector.project(
        make_event(
            EventKind.PLAN_CREATED,
            payload={
                "tasks": [
                    {
                        "id": 1,
                        "title": "Task",
                        "intent": "Intent",
                        "query": "query",
                        "source_strategy": "web",
                        "repository": "owner/repository",
                        "raw_source_body": sentinel,
                    }
                ],
                "raw_context": sentinel,
            },
        )
    )

    serialized = json.dumps([failed, planned], ensure_ascii=False)
    assert sentinel not in serialized
    assert failed is not None
    assert failed["code"] == "persistence_failed"
    assert failed["detail"] == "Research run failed."
    assert planned is not None
    assert "raw_source_body" not in planned["tasks"][0]


def test_legacy_projector_sanitizes_nested_repository_and_notice_fields() -> None:
    sentinel = "Authorization: Bearer nested-provider-secret"
    projector = legacy_projector()
    planned = projector.project(
        make_event(
            EventKind.PLAN_CREATED,
            payload={
                "tasks": [
                    {
                        "id": 1,
                        "title": "Task",
                        "notices": [sentinel],
                        "notice_codes": ["untrusted_provider_message"],
                        "repository": {
                            "full_name": "owner/repository",
                            "raw_body": sentinel,
                        },
                    }
                ]
            },
        )
    )
    sources = projector.project(
        make_event(
            EventKind.SOURCES_COLLECTED,
            task_id=1,
            payload={
                "repository": {
                    "full_name": "owner/repository",
                    "raw_body": sentinel,
                }
            },
        )
    )

    assert planned is not None
    task = planned["tasks"][0]
    assert task["notices"] == []
    assert task["notice_codes"] == []
    assert task.get("repository") is None
    assert sources is not None
    assert sources["repository"] is None
    assert sentinel not in json.dumps([planned, sources], ensure_ascii=False)


def test_legacy_projector_rejects_non_scalar_values_under_safe_keys() -> None:
    sentinel = "Authorization: Bearer nested-scalar-secret"
    projector = legacy_projector()
    repository = projector.project(
        make_event(
            EventKind.REPOSITORY_DETECTED,
            payload={
                "repository": {
                    "owner": "owner",
                    "full_name": {"raw_body": sentinel},
                    "stars": True,
                    "forks": {"raw_body": sentinel},
                }
            },
        )
    )
    task = projector.project(
        make_event(
            EventKind.TASK_STARTED,
            task_id=1,
            payload={
                "title": {"provider_message": sentinel},
                "step": True,
                "note_id": {"raw_body": sentinel},
            },
        )
    )
    sources = projector.project(
        make_event(
            EventKind.SOURCES_COLLECTED,
            task_id=1,
            payload={
                "latest_sources": {"raw_body": sentinel},
                "note_path": {"raw_body": sentinel},
            },
        )
    )
    chunk = projector.project(
        make_event(
            EventKind.SUMMARY_DELTA,
            task_id=1,
            payload={"chunk": {"provider_message": sentinel}},
        )
    )
    report = projector.project(
        make_event(
            EventKind.REPORT_GENERATED,
            payload={"report": {"raw_body": sentinel}},
        )
    )

    assert repository is not None
    assert repository["repository"] == {"owner": "owner"}
    assert task is not None
    assert task["title"] is None
    assert task["step"] is None
    assert task["note_id"] is None
    assert sources is not None
    assert sources["latest_sources"] is None
    assert sources["note_path"] is None
    assert chunk is not None
    assert chunk["content"] == ""
    assert report is not None
    assert report["report"] == ""
    assert sentinel not in json.dumps(
        [repository, task, sources, chunk, report],
        ensure_ascii=False,
    )


def test_legacy_projector_allowlists_backend_and_terminal_codes() -> None:
    projector = legacy_projector()
    unknown_backend = projector.project(
        make_event(
            EventKind.SOURCES_COLLECTED,
            task_id=1,
            payload={"backend": "Authorization: Bearer backend-secret"},
        )
    )
    known_backends = [
        projector.project(
            make_event(
                EventKind.SOURCES_COLLECTED,
                task_id=1,
                payload={"backend": backend},
            )
        )
        for backend in ("duckduckgo", "advanced", "none")
    ]
    failed = projector.project(
        make_event(
            EventKind.RUN_FAILED,
            payload={"code": "authorization_bearer_private_token"},
        )
    )
    governed_terminals = [
        projector.project(make_event(kind, payload={"code": code}))
        for kind, code in (
            (EventKind.RUN_REJECTED, "operation_rejected"),
            (EventKind.RUN_CANCELLED, "deadline_exceeded"),
        )
    ]

    assert unknown_backend is not None
    assert unknown_backend["backend"] is None
    assert [event["backend"] for event in known_backends if event is not None] == [
        "duckduckgo",
        "advanced",
        "none",
    ]
    assert failed is not None
    assert failed["code"] == "run_failed"
    assert [
        event["code"] for event in governed_terminals if event is not None
    ] == ["operation_rejected", "deadline_exceeded"]


def test_harness_result_exposes_stable_compatibility_error_code() -> None:
    result = HarnessRunResult(
        run_id=uuid4().hex,
        status="failed",
        error="Research execution failed.",
        error_code="application_error",
    )

    assert result.error_code == "application_error"
