"""Application lifecycle tests using deterministic in-memory ports."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Event, RLock
from typing import Any
from uuid import uuid4

import pytest

from config import Configuration
from models import TodoItem
from research.application import ResearchApplicationService
from research.context import FollowupContext
from research.contracts import (
    EventKind,
    ResearchCommand,
    ResearchEvent,
    RunSnapshot,
    RunStatus,
)
from research.observers import CompositeObserver
from research.operations import OperationRejectedError, OperationSpec
from research.repository import RunNotFoundError, RunRepositoryError
from research.session import (
    CancellationRequestedError,
    CancellationToken,
    DeadlineExceededError,
    InvalidTransitionError,
)


class RecordingRepository:
    """Record persistence calls while retaining typed snapshots."""

    def __init__(self, log: list[str] | None = None) -> None:
        self.log = log if log is not None else []
        self.snapshots: dict[str, RunSnapshot] = {}
        self.load_count = 0
        self.save_count = 0
        self._lock = RLock()

    def save(self, snapshot: RunSnapshot) -> None:
        with self._lock:
            self.save_count += 1
            self.log.append("save")
            self.snapshots[snapshot.run_id] = snapshot

    def load(self, run_id: str) -> RunSnapshot:
        with self._lock:
            self.load_count += 1
            try:
                return self.snapshots[run_id]
            except KeyError as exc:
                raise RunNotFoundError(run_id) from exc


class FailingSaveRepository(RecordingRepository):
    """Fail every required save while counting attempts."""

    def save(self, snapshot: RunSnapshot) -> None:
        self.save_count += 1
        raise RunRepositoryError("secret storage detail")


class CompletingCoordinator:
    """Drive one session through a valid canonical result."""

    def __init__(self) -> None:
        self.call_count = 0
        self.prior_contexts: list[object] = []
        self.sessions: list[object] = []

    def execute(self, session: Any, prior_context: object) -> None:
        self.call_count += 1
        self.prior_contexts.append(prior_context)
        self.sessions.append(session)
        complete_session(session)


def complete_session(session: Any) -> None:
    """Drive the supplied real session through valid domain transitions."""
    session.install_plan(
        [TodoItem(id=1, title="T", intent="I", query="q")]
    )
    session.start_task(1)
    session.complete_task(
        1,
        summary="summary",
        sources_summary="source",
    )
    session.metrics.update(
        {"source_count": 1, "secret_token": "metric-secret"}
    )
    session.set_report("# report")


@dataclass(frozen=True)
class FakeDecision:
    capability: str = "research:run"
    outcome: str = "deny"
    reason: str = "secret policy reason"

    def as_dict(self) -> dict[str, str]:
        return {
            "capability": self.capability,
            "outcome": self.outcome,
            "reason": self.reason,
            "api_token": "decision-secret",
        }


class AllowingPolicy:
    """Perform a successful command preflight and count both phases."""

    def __init__(self) -> None:
        self.evaluate_count = 0
        self.assert_count = 0

    def evaluate(self, command: ResearchCommand) -> list[FakeDecision]:
        self.evaluate_count += 1
        return [FakeDecision(outcome="allow", reason="secret allow reason")]

    def assert_executable(self, decisions: object) -> None:
        self.assert_count += 1


class RejectingPolicy:
    """Reject after returning one serializable decision."""

    def evaluate(self, command: ResearchCommand) -> list[FakeDecision]:
        return [FakeDecision()]

    def assert_executable(self, decisions: object) -> None:
        raise PermissionError("secret policy detail")


class RaisingCoordinator(CompletingCoordinator):
    """Raise a secret-bearing coordinator exception."""

    def execute(self, session: Any, prior_context: object) -> None:
        self.call_count += 1
        self.sessions.append(session)
        raise RuntimeError("secret coordinator detail")


class DynamicRejectingCoordinator(CompletingCoordinator):
    """Raise the dedicated operation policy control-flow exception."""

    def execute(self, session: Any, prior_context: object) -> None:
        self.call_count += 1
        self.sessions.append(session)
        raise OperationRejectedError()


class DeadlineCoordinator(CompletingCoordinator):
    """Raise the dedicated monotonic deadline control-flow exception."""

    def execute(self, session: Any, prior_context: object) -> None:
        self.call_count += 1
        self.sessions.append(session)
        raise DeadlineExceededError("secret timing detail")


class RejectionThenControlCoordinator(CompletingCoordinator):
    """Commit a dynamic rejection before a competing control exception."""

    def __init__(self, control_error: BaseException) -> None:
        super().__init__()
        self.control_error = control_error
        self.rejection_operation_id: str | None = None

    def execute(self, session: Any, prior_context: object) -> None:
        del prior_context
        self.call_count += 1
        self.sessions.append(session)
        spec = OperationSpec(
            operation_name="search.execute",
            capabilities=("search:web",),
            resource={"query_hash": "e" * 64, "backend": "fake"},
        )
        self.rejection_operation_id = spec.operation_id
        session.reject_operation(spec)
        session.cancellation.cancel()
        raise self.control_error


class InvalidTerminalCoordinator(CompletingCoordinator):
    """Return with one canonical task still nonterminal."""

    def execute(self, session: Any, prior_context: object) -> None:
        self.call_count += 1
        self.sessions.append(session)
        session.install_plan(
            [TodoItem(id=1, title="T", intent="I", query="q")]
        )
        session.set_report("# report")


class CancellingCoordinator(CompletingCoordinator):
    """Request cancellation after producing otherwise valid state."""

    def __init__(self, token: CancellationToken) -> None:
        super().__init__()
        self.token = token

    def execute(self, session: Any, prior_context: object) -> None:
        super().execute(session, prior_context)
        self.token.cancel()


class BlockingCoordinator(CompletingCoordinator):
    """Hold selected runs so duplicate and parent races are deterministic."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = Event()
        self.release = Event()
        self._call_lock = RLock()

    def execute(self, session: Any, prior_context: object) -> None:
        with self._call_lock:
            self.call_count += 1
            self.prior_contexts.append(prior_context)
            self.sessions.append(session)
        if session.command.topic.startswith("blocked"):
            self.entered.set()
            if not self.release.wait(timeout=5):
                raise RuntimeError("blocking test coordinator timed out")
        complete_session(session)


class CancelOnSaveRepository(RecordingRepository):
    """Cancel during save to prove a successful save commits completion."""

    def __init__(self, token: CancellationToken) -> None:
        super().__init__()
        self.token = token

    def save(self, snapshot: RunSnapshot) -> None:
        super().save(snapshot)
        self.token.cancel()


class LateMutationRepository(RecordingRepository):
    """Attempt a stale session transition while completion is prepared."""

    def __init__(self, coordinator: CompletingCoordinator) -> None:
        super().__init__()
        self.coordinator = coordinator
        self.mutation_error: Exception | None = None

    def save(self, snapshot: RunSnapshot) -> None:
        try:
            self.coordinator.sessions[-1].set_report("# stale report")
        except Exception as exc:
            self.mutation_error = exc
        super().save(snapshot)


class SequencedParentRepository(RecordingRepository):
    """Miss once and expose a durable parent on the mandatory recheck."""

    def __init__(self, parent: RunSnapshot) -> None:
        super().__init__()
        self.parent = parent

    def load(self, run_id: str) -> RunSnapshot:
        self.load_count += 1
        if self.load_count == 1:
            raise RunNotFoundError(run_id)
        return self.parent


class BrokenLoadRepository(RecordingRepository):
    """Raise a repository error while resolving a parent."""

    def load(self, run_id: str) -> RunSnapshot:
        self.load_count += 1
        raise RunRepositoryError("secret repository detail")


@pytest.fixture
def configuration() -> Configuration:
    return Configuration.from_env(overrides={"enable_notes": False})


def make_service(
    *,
    repository: object,
    coordinator: object,
    policy: object | None = None,
) -> ResearchApplicationService:
    return ResearchApplicationService(
        coordinator=coordinator,
        repository=repository,
        policy=policy if policy is not None else AllowingPolicy(),
    )


def make_parent_snapshot(
    *,
    run_id: str,
    followup_context: dict[str, object] | None = None,
) -> RunSnapshot:
    now = datetime.now(timezone.utc)
    context = followup_context or {
        "schema_version": 1,
        "source_run_id": run_id,
        "key_findings": ["finding"],
        "key_sources": ["source"],
        "open_questions": ["question"],
    }
    return RunSnapshot(
        run_id=run_id,
        topic="parent",
        status=RunStatus.COMPLETED,
        started_at=now,
        completed_at=now,
        parent_run_id=None,
        output={
            "running_summary": "# parent",
            "report_markdown": "# parent",
            "todo_items": [],
        },
        followup_context=context,
        metrics={},
        policy_decisions=(),
        config_snapshot={},
        events=(),
    )


def test_completion_is_observed_after_required_save(
    configuration: Configuration,
) -> None:
    log: list[str] = []
    repository = RecordingRepository(log)
    coordinator = CompletingCoordinator()
    policy = AllowingPolicy()
    service = make_service(
        repository=repository,
        coordinator=coordinator,
        policy=policy,
    )

    result = service.execute(
        ResearchCommand(topic="topic", config=configuration),
        observer=lambda event: log.append(event.kind.value),
    )

    assert result.status is RunStatus.COMPLETED
    assert result.evaluation_status == "pending"
    assert coordinator.call_count == 1
    assert policy.evaluate_count == 1
    assert policy.assert_count == 1
    assert repository.save_count == 1
    assert log.index("save") < log.index(EventKind.RUN_COMPLETED.value)
    assert service._active_run_ids == set()


def test_persistence_failure_emits_one_failure_and_never_completion(
    configuration: Configuration,
) -> None:
    events: list[EventKind] = []
    repository = FailingSaveRepository()
    service = make_service(
        repository=repository,
        coordinator=CompletingCoordinator(),
    )

    result = service.execute(
        ResearchCommand(topic="topic", config=configuration),
        observer=lambda event: events.append(event.kind),
    )

    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert result.error.code == "persistence_failed"
    assert "secret" not in result.error.message
    assert repository.save_count == 1
    assert EventKind.RUN_COMPLETED not in events
    assert events.count(EventKind.RUN_FAILED) == 1
    assert service._active_run_ids == set()


def test_missing_parent_never_calls_coordinator(
    configuration: Configuration,
) -> None:
    repository = RecordingRepository()
    coordinator = CompletingCoordinator()
    service = make_service(repository=repository, coordinator=coordinator)

    result = service.execute(
        ResearchCommand(
            topic="follow-up",
            config=configuration,
            parent_run_id=uuid4().hex,
        )
    )

    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert result.error.code == "parent_not_found"
    assert coordinator.call_count == 0
    assert repository.load_count == 3
    assert service._active_run_ids == set()


def test_policy_rejection_never_loads_saves_or_calls_coordinator(
    configuration: Configuration,
) -> None:
    repository = RecordingRepository()
    coordinator = CompletingCoordinator()
    events: list[EventKind] = []
    service = make_service(
        repository=repository,
        coordinator=coordinator,
        policy=RejectingPolicy(),
    )

    result = service.execute(
        ResearchCommand(topic="topic", config=configuration),
        observer=lambda event: events.append(event.kind),
    )

    assert result.status is RunStatus.REJECTED
    assert result.error is not None
    assert result.error.code == "policy_rejected"
    assert "secret" not in result.error.message
    assert result.policy_decisions == (
        {
            "capability": "research:run",
            "outcome": "deny",
            "reason": "Capability denied by policy.",
        },
    )
    assert repository.load_count == 0
    assert repository.save_count == 0
    assert coordinator.call_count == 0
    assert events == [EventKind.RUN_REJECTED]
    assert service._active_run_ids == set()


def test_durable_parent_supplies_strict_typed_context(
    configuration: Configuration,
) -> None:
    parent_id = uuid4().hex
    repository = RecordingRepository()
    repository.snapshots[parent_id] = make_parent_snapshot(run_id=parent_id)
    coordinator = CompletingCoordinator()
    service = make_service(repository=repository, coordinator=coordinator)

    result = service.execute(
        ResearchCommand(
            topic="follow-up",
            config=configuration,
            parent_run_id=parent_id,
        )
    )

    assert result.status is RunStatus.COMPLETED
    assert coordinator.call_count == 1
    assert coordinator.prior_contexts == [
        FollowupContext(
            source_run_id=parent_id,
            key_findings=("finding",),
            key_sources=("source",),
            open_questions=("question",),
        )
    ]


@pytest.mark.parametrize(
    "invalid_field",
    ["schema", "source", "list_type", "item_type", "budget", "extra"],
)
def test_corrupt_parent_context_is_rejected_before_coordination(
    configuration: Configuration,
    invalid_field: str,
) -> None:
    parent_id = uuid4().hex
    context: dict[str, object] = {
        "schema_version": 1,
        "source_run_id": parent_id,
        "key_findings": ["finding"],
        "key_sources": ["source"],
        "open_questions": ["question"],
    }
    if invalid_field == "schema":
        context["schema_version"] = 2
    elif invalid_field == "source":
        context["source_run_id"] = uuid4().hex
    elif invalid_field == "list_type":
        context["key_findings"] = "finding"
    elif invalid_field == "item_type":
        context["key_sources"] = [42]
    elif invalid_field == "budget":
        context["open_questions"] = [f"q-{index}" for index in range(11)]
    else:
        context["raw_context"] = "must not be accepted"

    repository = RecordingRepository()
    repository.snapshots[parent_id] = make_parent_snapshot(
        run_id=parent_id,
        followup_context=context,
    )
    coordinator = CompletingCoordinator()
    events: list[ResearchEvent] = []
    service = make_service(repository=repository, coordinator=coordinator)

    result = service.execute(
        ResearchCommand(
            topic="follow-up",
            config=configuration,
            parent_run_id=parent_id,
        ),
        observer=events.append,
    )

    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert result.error.code == "parent_corrupt"
    assert coordinator.call_count == 0
    assert [event.kind for event in events] == [EventKind.RUN_FAILED]
    assert service._active_run_ids == set()


def test_parent_miss_is_rechecked_and_second_durable_snapshot_wins(
    configuration: Configuration,
) -> None:
    parent_id = uuid4().hex
    repository = SequencedParentRepository(
        make_parent_snapshot(run_id=parent_id)
    )
    coordinator = CompletingCoordinator()
    service = make_service(repository=repository, coordinator=coordinator)

    result = service.execute(
        ResearchCommand(
            topic="follow-up",
            config=configuration,
            parent_run_id=parent_id,
        )
    )

    assert result.status is RunStatus.COMPLETED
    assert repository.load_count == 2
    assert isinstance(coordinator.prior_contexts[0], FollowupContext)


def test_parent_published_after_two_misses_wins_before_not_found(
    configuration: Configuration,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_id = uuid4().hex
    parent = make_parent_snapshot(run_id=parent_id)
    repository = RecordingRepository()
    coordinator = CompletingCoordinator()
    service = make_service(repository=repository, coordinator=coordinator)
    service._active_run_ids.add(parent_id)
    original_is_active = service._is_active

    def publish_then_report_inactive(run_id: str) -> bool:
        assert run_id == parent_id
        assert original_is_active(parent_id)
        repository.save(parent)
        service._unregister(parent_id)
        return False

    monkeypatch.setattr(service, "_is_active", publish_then_report_inactive)

    result = service.execute(
        ResearchCommand(
            topic="follow-up",
            config=configuration,
            parent_run_id=parent_id,
        )
    )

    assert result.status is RunStatus.COMPLETED
    assert repository.load_count == 3
    assert repository.save_count == 2
    assert coordinator.call_count == 1
    assert isinstance(coordinator.prior_contexts[0], FollowupContext)
    assert service._active_run_ids == set()


def test_inactive_parent_after_two_misses_is_rechecked_then_not_found(
    configuration: Configuration,
) -> None:
    parent_id = uuid4().hex
    repository = RecordingRepository()
    coordinator = CompletingCoordinator()
    service = make_service(repository=repository, coordinator=coordinator)
    service._active_run_ids.add(parent_id)

    def miss_and_stop_parent(run_id: str) -> RunSnapshot:
        repository.load_count += 1
        if repository.load_count == 2:
            service._unregister(parent_id)
        raise RunNotFoundError(run_id)

    repository.load = miss_and_stop_parent  # type: ignore[method-assign]

    result = service.execute(
        ResearchCommand(
            topic="follow-up",
            config=configuration,
            parent_run_id=parent_id,
        )
    )

    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert result.error.code == "parent_not_found"
    assert repository.load_count == 3
    assert coordinator.call_count == 0


def test_persisted_parent_long_open_question_is_typed_prior_context(
    configuration: Configuration,
) -> None:
    parent_id = uuid4().hex
    long_question = "Q" * 240
    repository = RecordingRepository()
    repository.snapshots[parent_id] = make_parent_snapshot(
        run_id=parent_id,
        followup_context={
            "schema_version": 1,
            "source_run_id": parent_id,
            "key_findings": ["finding"],
            "key_sources": ["source"],
            "open_questions": [long_question],
        },
    )
    coordinator = CompletingCoordinator()
    service = make_service(repository=repository, coordinator=coordinator)

    result = service.execute(
        ResearchCommand(
            topic="follow-up",
            config=configuration,
            parent_run_id=parent_id,
        )
    )

    assert result.status is RunStatus.COMPLETED
    assert isinstance(coordinator.prior_contexts[0], FollowupContext)
    assert coordinator.prior_contexts[0].open_questions == (long_question,)


def test_repository_load_error_is_typed_and_safe(
    configuration: Configuration,
) -> None:
    coordinator = CompletingCoordinator()
    service = make_service(
        repository=BrokenLoadRepository(),
        coordinator=coordinator,
    )

    result = service.execute(
        ResearchCommand(
            topic="follow-up",
            config=configuration,
            parent_run_id=uuid4().hex,
        )
    )

    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert result.error.code == "repository_error"
    assert "secret" not in result.error.message
    assert coordinator.call_count == 0


def test_prestart_terminal_event_is_typed_safe_and_observer_isolated(
    configuration: Configuration,
) -> None:
    order: list[str] = []
    observed: list[ResearchEvent] = []

    def record_first(event: ResearchEvent) -> None:
        order.append("first")
        observed.append(event)

    def explode(event: ResearchEvent) -> None:
        order.append("explode")
        raise RuntimeError("observer failure")

    def record_last(event: ResearchEvent) -> None:
        order.append("last")

    service = make_service(
        repository=RecordingRepository(),
        coordinator=CompletingCoordinator(),
        policy=RejectingPolicy(),
    )
    result = service.execute(
        ResearchCommand(topic="topic", config=configuration),
        observer=CompositeObserver([record_first, explode, record_last]),
    )

    assert result.status is RunStatus.REJECTED
    assert order == ["first", "explode", "last"]
    assert len(observed) == 1
    event = observed[0]
    assert event.run_id == result.run_id
    assert event.kind is EventKind.RUN_REJECTED
    assert event.sequence == 1
    assert event.occurred_at.utcoffset() == timezone.utc.utcoffset(event.occurred_at)
    json.dumps(event.as_dict())


def test_prestart_observer_failure_log_omits_exception_secret(
    configuration: Configuration,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = r"application-observer-secret::C:\private\vault\token.txt"

    def explode(_event: ResearchEvent) -> None:
        raise RuntimeError(secret)

    caplog.set_level("ERROR", logger="research.application")
    result = make_service(
        repository=RecordingRepository(),
        coordinator=CompletingCoordinator(),
        policy=RejectingPolicy(),
    ).execute(
        ResearchCommand(topic="topic", config=configuration),
        observer=explode,
    )

    records = [
        record
        for record in caplog.records
        if record.name == "research.application"
    ]
    assert result.status is RunStatus.REJECTED
    assert records
    assert secret not in caplog.text
    assert r"C:\private\vault" not in caplog.text
    assert all(record.exc_info is None for record in records)


def test_composite_observer_failure_log_omits_exception_secret(
    configuration: Configuration,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = r"composite-observer-secret::D:\notes\private-key.txt"
    observed: list[EventKind] = []

    def explode(_event: ResearchEvent) -> None:
        raise RuntimeError(secret)

    caplog.set_level("ERROR", logger="research.observers")
    result = make_service(
        repository=RecordingRepository(),
        coordinator=CompletingCoordinator(),
        policy=RejectingPolicy(),
    ).execute(
        ResearchCommand(topic="topic", config=configuration),
        observer=CompositeObserver(
            [explode, lambda event: observed.append(event.kind)]
        ),
    )

    records = [
        record
        for record in caplog.records
        if record.name == "research.observers"
    ]
    assert result.status is RunStatus.REJECTED
    assert observed == [EventKind.RUN_REJECTED]
    assert records
    assert secret not in caplog.text
    assert r"D:\notes\private-key.txt" not in caplog.text
    assert all(record.exc_info is None for record in records)


def test_coordinator_exception_maps_to_one_failed_terminal(
    configuration: Configuration,
) -> None:
    coordinator = RaisingCoordinator()
    events: list[EventKind] = []
    service = make_service(
        repository=RecordingRepository(),
        coordinator=coordinator,
    )

    result = service.execute(
        ResearchCommand(topic="topic", config=configuration),
        observer=lambda event: events.append(event.kind),
    )

    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert result.error.code == "coordinator_failed"
    assert "secret" not in result.error.message
    assert events.count(EventKind.RUN_FAILED) == 1
    assert EventKind.RUN_COMPLETED not in events
    assert service._active_run_ids == set()


def test_dynamic_operation_rejection_maps_to_rejected_terminal(
    configuration: Configuration,
) -> None:
    events: list[ResearchEvent] = []
    result = make_service(
        repository=RecordingRepository(),
        coordinator=DynamicRejectingCoordinator(),
    ).execute(
        ResearchCommand(topic="topic", config=configuration),
        observer=events.append,
    )

    assert result.status is RunStatus.REJECTED
    assert result.error is not None
    assert result.error.code == "operation_rejected"
    assert events[-1].kind is EventKind.RUN_REJECTED
    assert events[-1].payload["code"] == "operation_rejected"
    assert "secret" not in json.dumps([event.as_dict() for event in events])


@pytest.mark.parametrize(
    "control_error",
    [
        CancellationRequestedError("competing cancellation"),
        DeadlineExceededError("competing deadline"),
    ],
)
def test_committed_rejection_precedes_competing_coordinator_control_error(
    configuration: Configuration,
    control_error: BaseException,
) -> None:
    events: list[ResearchEvent] = []
    coordinator = RejectionThenControlCoordinator(control_error)

    result = make_service(
        repository=RecordingRepository(),
        coordinator=coordinator,
    ).execute(
        ResearchCommand(topic="topic", config=configuration),
        observer=events.append,
    )

    assert coordinator.rejection_operation_id is not None
    assert result.status is RunStatus.REJECTED
    assert result.error is not None
    assert result.error.code == "operation_rejected"
    assert [event.kind for event in events][-2:] == [
        EventKind.OPERATION_REJECTED,
        EventKind.RUN_REJECTED,
    ]


def test_direct_rejection_error_is_arbitrated_against_first_latch(
    configuration: Configuration,
) -> None:
    class LaterRejectionCoordinator(CompletingCoordinator):
        def __init__(self) -> None:
            super().__init__()
            self.arbitrated_ids: list[str | None] = []

        def execute(self, session: Any, prior_context: object) -> None:
            del prior_context
            first = OperationSpec(
                operation_name="search.execute",
                capabilities=("search:web",),
                resource={"query_hash": "a" * 64, "backend": "first"},
            )
            second = OperationSpec(
                operation_name="search.execute",
                capabilities=("search:web",),
                resource={"query_hash": "b" * 64, "backend": "second"},
            )
            session.reject_operation(first)
            original_checkpoint = session.raise_if_run_controlled

            def record_arbitration() -> None:
                try:
                    original_checkpoint()
                except OperationRejectedError as exc:
                    self.arbitrated_ids.append(exc.operation_id)
                    raise

            session.raise_if_run_controlled = record_arbitration
            raise OperationRejectedError(second.operation_id)

    coordinator = LaterRejectionCoordinator()
    result = make_service(
        repository=RecordingRepository(),
        coordinator=coordinator,
    ).execute(ResearchCommand(topic="topic", config=configuration))

    assert result.status is RunStatus.REJECTED
    assert coordinator.arbitrated_ids


def test_deadline_expiry_maps_to_cancelled_terminal(
    configuration: Configuration,
) -> None:
    events: list[ResearchEvent] = []
    result = make_service(
        repository=RecordingRepository(),
        coordinator=DeadlineCoordinator(),
    ).execute(
        ResearchCommand(topic="topic", config=configuration),
        observer=events.append,
    )

    assert result.status is RunStatus.CANCELLED
    assert result.error is not None
    assert result.error.code == "deadline_exceeded"
    assert events[-1].kind is EventKind.RUN_CANCELLED
    assert events[-1].payload["code"] == "deadline_exceeded"
    assert "secret timing detail" not in json.dumps(
        [event.as_dict() for event in events]
    )


def test_terminal_validation_failure_maps_to_one_failed_terminal(
    configuration: Configuration,
) -> None:
    events: list[EventKind] = []
    service = make_service(
        repository=RecordingRepository(),
        coordinator=InvalidTerminalCoordinator(),
    )

    result = service.execute(
        ResearchCommand(topic="topic", config=configuration),
        observer=lambda event: events.append(event.kind),
    )

    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert result.error.code == "terminal_validation_failed"
    assert events.count(EventKind.RUN_FAILED) == 1
    assert EventKind.RUN_COMPLETED not in events
    assert service._active_run_ids == set()


def test_precancelled_command_never_calls_coordinator_or_repository(
    configuration: Configuration,
) -> None:
    token = CancellationToken()
    token.cancel()
    policy = AllowingPolicy()
    repository = RecordingRepository()
    coordinator = CompletingCoordinator()
    events: list[ResearchEvent] = []
    service = make_service(
        repository=repository,
        coordinator=coordinator,
        policy=policy,
    )

    result = service.execute(
        ResearchCommand(topic="topic", config=configuration),
        observer=events.append,
        cancellation=token,
    )

    assert result.status is RunStatus.CANCELLED
    assert result.error is not None
    assert result.error.code == "cancelled"
    assert coordinator.call_count == 0
    assert repository.save_count == 0
    assert events[0].kind is EventKind.RUN_CANCELLED
    assert events[0].sequence == 1
    assert service._active_run_ids == set()


def test_cancellation_before_save_uses_same_token_and_emits_no_completion(
    configuration: Configuration,
) -> None:
    token = CancellationToken()
    coordinator = CancellingCoordinator(token)
    repository = RecordingRepository()
    events: list[EventKind] = []
    service = make_service(repository=repository, coordinator=coordinator)

    result = service.execute(
        ResearchCommand(topic="topic", config=configuration),
        observer=lambda event: events.append(event.kind),
        cancellation=token,
    )

    assert result.status is RunStatus.CANCELLED
    assert coordinator.sessions[0].cancellation_token is token
    assert repository.save_count == 0
    assert events.count(EventKind.RUN_CANCELLED) == 1
    assert EventKind.RUN_COMPLETED not in events
    assert service._active_run_ids == set()


def test_cancellation_during_successful_save_does_not_override_completion(
    configuration: Configuration,
) -> None:
    token = CancellationToken()
    repository = CancelOnSaveRepository(token)
    service = make_service(
        repository=repository,
        coordinator=CompletingCoordinator(),
    )

    result = service.execute(
        ResearchCommand(topic="topic", config=configuration),
        cancellation=token,
    )

    assert token.is_cancelled
    assert result.status is RunStatus.COMPLETED
    assert repository.save_count == 1


def test_duplicate_callers_cannot_remove_the_registered_active_run(
    configuration: Configuration,
) -> None:
    coordinator = BlockingCoordinator()
    service = make_service(
        repository=RecordingRepository(),
        coordinator=coordinator,
    )
    command = ResearchCommand(topic="blocked topic", config=configuration)

    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(service.execute, command)
        assert coordinator.entered.wait(timeout=2)
        try:
            second = service.execute(command)
            third = service.execute(command)
            assert second.status is RunStatus.REJECTED
            assert third.status is RunStatus.REJECTED
            assert second.error is not None
            assert third.error is not None
            assert second.error.code == "run_already_active"
            assert third.error.code == "run_already_active"
            assert command.run_id in service._active_run_ids
            assert coordinator.call_count == 1
        finally:
            coordinator.release.set()
        assert first.result(timeout=5).status is RunStatus.COMPLETED

    assert service._active_run_ids == set()


def test_active_undurable_parent_returns_pending_without_coordination(
    configuration: Configuration,
) -> None:
    coordinator = BlockingCoordinator()
    repository = RecordingRepository()
    service = make_service(repository=repository, coordinator=coordinator)
    parent = ResearchCommand(topic="blocked parent", config=configuration)

    with ThreadPoolExecutor(max_workers=1) as executor:
        parent_future = executor.submit(service.execute, parent)
        assert coordinator.entered.wait(timeout=2)
        try:
            child = service.execute(
                ResearchCommand(
                    topic="follow-up",
                    config=configuration,
                    parent_run_id=parent.run_id,
                )
            )
            assert child.status is RunStatus.FAILED
            assert child.error is not None
            assert child.error.code == "parent_pending"
            assert coordinator.call_count == 1
            assert repository.load_count == 2
        finally:
            coordinator.release.set()
        assert parent_future.result(timeout=5).status is RunStatus.COMPLETED


def test_durable_parent_wins_while_same_run_id_remains_active(
    configuration: Configuration,
) -> None:
    parent_id = uuid4().hex
    coordinator = BlockingCoordinator()
    repository = RecordingRepository()
    repository.snapshots[parent_id] = make_parent_snapshot(run_id=parent_id)
    service = make_service(repository=repository, coordinator=coordinator)
    parent = ResearchCommand(
        topic="blocked parent",
        config=configuration,
        run_id=parent_id,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        parent_future = executor.submit(service.execute, parent)
        assert coordinator.entered.wait(timeout=2)
        try:
            child = service.execute(
                ResearchCommand(
                    topic="follow-up",
                    config=configuration,
                    parent_run_id=parent_id,
                )
            )
            assert child.status is RunStatus.COMPLETED
            assert coordinator.call_count == 2
            assert isinstance(coordinator.prior_contexts[1], FollowupContext)
        finally:
            coordinator.release.set()
        assert parent_future.result(timeout=5).status is RunStatus.COMPLETED


def test_result_and_prepared_snapshot_include_only_safe_projections(
    configuration: Configuration,
) -> None:
    repository = RecordingRepository()
    policy = AllowingPolicy()
    command = ResearchCommand(topic="topic", config=configuration)
    service = make_service(
        repository=repository,
        coordinator=CompletingCoordinator(),
        policy=policy,
    )

    result = service.execute(command)
    snapshot = repository.snapshots[command.run_id]
    snapshot_wire = snapshot.as_dict()

    assert result.metrics == {"source_count": 1}
    assert snapshot.metrics == result.metrics
    assert result.followup_context == snapshot_wire["followup_context"]
    assert result.followup_context["source_run_id"] == command.run_id
    assert result.policy_decisions == snapshot.policy_decisions
    assert result.policy_decisions == (
        {
            "capability": "research:run",
            "outcome": "allow",
            "reason": "Capability allowed by policy.",
        },
    )
    selected = json.dumps(
        {
            "metrics": snapshot_wire["metrics"],
            "policy": snapshot_wire["policy_decisions"],
            "followup": snapshot_wire["followup_context"],
        }
    )
    assert "metric-secret" not in selected
    assert "decision-secret" not in selected
    assert "secret allow reason" not in selected
    assert result.output is not None
    assert result.output.report_markdown == snapshot.output["report_markdown"]


def test_observer_exception_does_not_change_committed_success(
    configuration: Configuration,
) -> None:
    def explode(event: ResearchEvent) -> None:
        raise RuntimeError("observer failure")

    result = make_service(
        repository=RecordingRepository(),
        coordinator=CompletingCoordinator(),
    ).execute(
        ResearchCommand(topic="topic", config=configuration),
        observer=explode,
    )

    assert result.status is RunStatus.COMPLETED


def test_prepared_terminal_freezes_late_nonterminal_transitions(
    configuration: Configuration,
) -> None:
    coordinator = CompletingCoordinator()
    repository = LateMutationRepository(coordinator)
    service = make_service(repository=repository, coordinator=coordinator)

    result = service.execute(
        ResearchCommand(topic="topic", config=configuration)
    )

    assert result.status is RunStatus.COMPLETED
    assert isinstance(repository.mutation_error, InvalidTransitionError)
    assert result.output is not None
    assert result.output.report_markdown == "# report"


@pytest.mark.parametrize("terminal_status", ["failed", "skipped"])
def test_current_long_open_question_title_does_not_fail_completion(
    configuration: Configuration,
    terminal_status: str,
) -> None:
    long_title = "Q" * 240

    class LongOpenQuestionCoordinator:
        def execute(self, session: Any, prior_context: object) -> None:
            session.install_plan(
                [TodoItem(id=1, title=long_title, intent="I", query="q")]
            )
            session.start_task(1)
            if terminal_status == "failed":
                session.fail_task(1, message="failed", code="task_failed")
            else:
                session.skip_task(1, reason="skipped")
            session.set_report("# report")

    result = make_service(
        repository=RecordingRepository(),
        coordinator=LongOpenQuestionCoordinator(),
    ).execute(ResearchCommand(topic="topic", config=configuration))

    assert result.status is RunStatus.COMPLETED
    assert result.followup_context["open_questions"] == [long_title]


def test_application_exposes_no_second_execution_lifecycle(
    configuration: Configuration,
) -> None:
    service = make_service(
        repository=RecordingRepository(),
        coordinator=CompletingCoordinator(),
    )

    assert callable(service.execute)
    assert not hasattr(service, "stream")
    assert not hasattr(service, "run")
