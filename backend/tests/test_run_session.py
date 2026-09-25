import json
import time
from threading import Barrier, Event, Thread

import pytest

from config import Configuration
from models import ResearchState, TodoItem
from research.contracts import EventKind, ResearchCommand, RunStatus
from research.operations import OperationRejectedError, OperationSpec
from research.session import (
    NEVER_CANCELLED,
    CancellationRequestedError,
    CancellationToken,
    DeadlineExceededError,
    InvalidTransitionError,
    RunSession,
)


def make_session() -> RunSession:
    command = ResearchCommand(topic="topic", config=Configuration.from_env())
    return RunSession(command=command, state=ResearchState(research_topic="topic"))


def test_state_changes_before_observer_receives_event() -> None:
    observed: list[tuple[RunStatus, EventKind]] = []
    session = make_session()
    session.add_observer(lambda event: observed.append((session.status, event.kind)))
    session.start()
    assert observed == [(RunStatus.RUNNING, EventKind.RUN_STARTED)]


def test_event_sequence_is_monotonic() -> None:
    session = make_session()
    session.start()
    session.install_plan([TodoItem(id=1, title="T", intent="I", query="q")])
    assert [event.sequence for event in session.events] == [1, 2]


def test_task_quality_decision_is_evented_and_persisted_in_metrics() -> None:
    """Task-quality diagnostics survive snapshot persistence without raw evidence."""
    session = make_session()
    session.start()
    session.install_plan([TodoItem(id=1, title="T", intent="I", query="q")])

    event = session.record_task_quality(
        1,
        {
            "action": "retrieve_gaps",
            "reason_codes": ["missing_primary_source"],
            "retrieval_relevance": 0.9,
            "claim_support": 0.4,
            "citation_integrity": 1.0,
            "checked_claim_ids": ["task-1-claim-1"],
            "retrieval_gaps": [
                {
                    "claim_id": "task-1-claim-1",
                    "gap_type": "missing_primary_source",
                    "topic": "Redis slot count",
                    "preferred_source_types": ["official_documentation"],
                    "time_constraint": None,
                }
            ],
        },
    )

    snapshot = session.to_snapshot().as_dict()
    assert event.kind is EventKind.TASK_QUALITY_EVALUATED
    assert event.payload["claim_support"] == 0.4
    assert snapshot["metrics"]["task_quality"]["1"][0]["action"] == "retrieve_gaps"
    assert "evidence" not in json.dumps(event.as_dict()).casefold()


def test_reentrant_observer_preserves_fifo_for_later_observers() -> None:
    session = make_session()
    observed_by_second: list[int] = []

    def submit_next_transition(event) -> None:
        if event.sequence == 1:
            session.install_plan([])

    session.add_observer(submit_next_transition)
    session.add_observer(
        lambda event: observed_by_second.append(event.sequence)
    )

    session.start()

    assert observed_by_second == [1, 2]


def test_task_failure_updates_canonical_task_before_event() -> None:
    session = make_session()
    session.start()
    session.install_plan([TodoItem(id=1, title="T", intent="I", query="q")])
    observed: list[str] = []
    session.add_observer(
        lambda event: observed.append(session.state.todo_items[0].status)
        if event.kind is EventKind.TASK_FAILED
        else None
    )
    session.fail_task(1, message="boom", code="task_failed")
    assert session.state.todo_items[0].status == "failed"
    assert observed == ["failed"]


def test_second_terminal_confirmation_is_rejected() -> None:
    session = make_session()
    session.start()
    prepared = session.prepare_terminal(RunStatus.COMPLETED, EventKind.RUN_COMPLETED)
    session.confirm_terminal(prepared)
    with pytest.raises(InvalidTransitionError):
        session.prepare_terminal(RunStatus.FAILED, EventKind.RUN_FAILED)


def test_invalid_event_payload_does_not_mutate_canonical_state() -> None:
    session = make_session()
    session.start()
    session.install_plan([TodoItem(id=1, title="T", intent="I", query="q")])
    event_count = len(session.events)

    with pytest.raises(TypeError):
        session.record_sources(
            1,
            sources_summary="must not stick",
            invalid=object(),
        )

    assert session.state.todo_items[0].sources_summary is None
    assert len(session.events) == event_count


def test_repository_transition_stores_context_but_emits_only_safe_summary() -> None:
    session = make_session()
    session.start()
    event = session.record_repository(
        github_context={"markdown": "RAW-GITHUB-CONTEXT", "target": {"full_name": "o/r"}},
        repository={"full_name": "o/r", "stars": 10},
        notices=["safe notice"],
    )

    assert session.state.github_context["markdown"] == "RAW-GITHUB-CONTEXT"
    assert event.kind is EventKind.REPOSITORY_DETECTED
    assert event.as_dict()["payload"] == {
        "repository": {"full_name": "o/r", "stars": 10},
        "notices": ["safe notice"],
        "notice_codes": [],
    }
    assert "RAW-GITHUB-CONTEXT" not in str(event.as_dict())


def test_source_transition_merges_worker_context_without_event_leakage() -> None:
    session = make_session()
    session.start()
    session.install_plan([TodoItem(id=1, title="T", intent="I", query="q")])
    event = session.record_sources(
        1,
        context="RAW-WEB-CONTEXT",
        latest_sources="- Safe source",
        backend="fake",
    )

    assert session.state.web_research_results == ["RAW-WEB-CONTEXT"]
    assert session.state.sources_gathered == ["- Safe source"]
    assert event.payload["latest_sources"] == "- Safe source"
    assert event.payload["backend"] == "fake"
    assert "RAW-WEB-CONTEXT" not in str(event.as_dict())


def test_report_note_transition_updates_metadata_before_observer() -> None:
    session = make_session()
    session.start()
    observed: list[tuple[str | None, str | None]] = []
    session.add_observer(
        lambda event: observed.append(
            (session.state.report_note_id, session.state.report_note_path)
        )
        if event.kind is EventKind.REPORT_NOTE_CREATED
        else None
    )

    event = session.record_report_note(
        note_id="report-note",
        note_path="safe/report-note.md",
        title="Report",
    )

    assert event.payload == {
        "note_id": "report-note",
        "note_path": "safe/report-note.md",
        "title": "Report",
    }
    assert observed == [("report-note", "safe/report-note.md")]


@pytest.mark.parametrize("terminal", ["completed", "skipped", "failed"])
def test_terminal_task_transition_restores_original_query(terminal: str) -> None:
    session = make_session()
    session.start()
    session.install_plan([TodoItem(id=1, title="T", intent="I", query="original")])
    session.start_task(1)
    session.record_retry(
        1,
        previous_query="original",
        refined_query="refined",
        attempt=1,
        reason="retry",
    )

    if terminal == "completed":
        session.complete_task(
            1,
            summary="summary",
            sources_summary="source",
            original_query="original",
        )
    elif terminal == "skipped":
        session.skip_task(1, reason="empty", original_query="original")
    else:
        session.fail_task(
            1,
            message="failed",
            code="task_failed",
            original_query="original",
        )

    task = session.state.todo_items[0]
    assert task.query == "original"
    assert task.retry_count == 1
    assert task.refined_queries == ["refined"]


def test_plan_event_is_not_retroactively_changed_by_retry_transition() -> None:
    session = make_session()
    session.start()
    plan = session.install_plan(
        [TodoItem(id=1, title="T", intent="I", query="original")]
    )

    session.start_task(1)
    session.record_retry(
        1,
        previous_query="original",
        refined_query="refined",
        attempt=1,
        reason="retry",
    )

    planned_task = plan.as_dict()["payload"]["tasks"][0]
    assert planned_task["query"] == "original"
    assert planned_task["notices"] == []
    assert planned_task["refined_queries"] == []


def test_todo_to_dict_detaches_mutable_list_fields() -> None:
    task = TodoItem(
        id=1,
        title="T",
        intent="I",
        query="q",
        notices=["notice"],
        refined_queries=["refined"],
    )

    payload = task.to_dict()
    payload["notices"].append("wire mutation")
    payload["refined_queries"].append("wire refinement")

    assert task.notices == ["notice"]
    assert task.refined_queries == ["refined"]


def test_legacy_output_and_snapshot_do_not_alias_canonical_tasks() -> None:
    session = make_session()
    session.start()
    session.install_plan(
        [
            TodoItem(
                id=1,
                title="Canonical",
                intent="I",
                query="q",
                notices=["notice"],
                refined_queries=["refined"],
            )
        ]
    )
    snapshot = session.to_snapshot()
    output = session.to_legacy_output()

    output.todo_items[0].title = "legacy mutation"
    output.todo_items[0].notices.append("legacy notice")
    output.todo_items[0].refined_queries.append("legacy refinement")
    output.todo_items.clear()

    canonical = session.state.todo_items[0]
    assert canonical.title == "Canonical"
    assert canonical.notices == ["notice"]
    assert canonical.refined_queries == ["refined"]
    assert snapshot.as_dict()["output"]["todo_items"][0] == {
        **canonical.to_dict(),
        "title": "Canonical",
    }

    wire_snapshot = snapshot.as_dict()
    wire_snapshot["output"]["todo_items"][0]["notices"].append("wire notice")
    wire_snapshot["output"]["todo_items"][0]["refined_queries"].append(
        "wire refinement"
    )

    with pytest.raises(TypeError):
        snapshot.output["todo_items"][0]["title"] = "snapshot mutation"

    assert canonical.notices == ["notice"]
    assert canonical.refined_queries == ["refined"]
    assert snapshot.output["todo_items"][0]["notices"] == ("notice",)
    assert snapshot.output["todo_items"][0]["refined_queries"] == ("refined",)


def test_observer_cannot_mutate_event_seen_by_later_observer() -> None:
    session = make_session()
    observed: list[str] = []

    def attempt_mutation(event) -> None:
        event.payload["topic"] = "mutated"

    def observe(event) -> None:
        observed.append(event.payload["topic"])

    session.add_observer(attempt_mutation)
    session.add_observer(observe)

    event = session.start()
    wire = event.as_dict()
    wire["payload"]["topic"] = "wire mutation"

    assert observed == ["topic"]
    assert event.payload["topic"] == "topic"
    assert session.events[0].payload["topic"] == "topic"


def test_concurrent_observers_receive_events_in_sequence() -> None:
    session = make_session()
    session.start()
    session.install_plan(
        [
            TodoItem(id=1, title="One", intent="I", query="q1"),
            TodoItem(id=2, title="Two", intent="I", query="q2"),
        ]
    )
    first_entered = Event()
    release_first = Event()
    second_entered = Event()
    observed: list[int] = []

    def observe(event) -> None:
        if event.sequence == 3:
            first_entered.set()
            assert release_first.wait(timeout=1)
        elif event.sequence == 4:
            second_entered.set()
        observed.append(event.sequence)

    session.add_observer(observe)
    first = Thread(target=session.start_task, args=(1,))
    second = Thread(target=session.start_task, args=(2,))
    first.start()
    assert first_entered.wait(timeout=1)
    second.start()
    second_overtook = second_entered.wait(timeout=0.1)
    release_first.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert not second_overtook
    assert observed == [3, 4]


def test_observer_failure_does_not_fail_committed_transition() -> None:
    session = make_session()
    observed: list[EventKind] = []

    def fail_observer(_event) -> None:
        raise RuntimeError("observer failed")

    session.add_observer(fail_observer)
    session.add_observer(lambda event: observed.append(event.kind))

    event = session.start()

    assert event.kind is EventKind.RUN_STARTED
    assert session.status is RunStatus.RUNNING
    assert observed == [EventKind.RUN_STARTED]


def test_observer_failure_log_omits_exception_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = r"session-observer-secret::E:\workspace\credentials.json"
    session = make_session()
    observed: list[EventKind] = []

    def fail_observer(_event) -> None:
        raise RuntimeError(secret)

    caplog.set_level("ERROR", logger="research.session")
    session.add_observer(fail_observer)
    session.add_observer(lambda event: observed.append(event.kind))

    event = session.start()

    records = [
        record
        for record in caplog.records
        if record.name == "research.session"
    ]
    assert event.kind is EventKind.RUN_STARTED
    assert session.status is RunStatus.RUNNING
    assert observed == [EventKind.RUN_STARTED]
    assert records
    assert secret not in caplog.text
    assert r"E:\workspace\credentials.json" not in caplog.text
    assert all(record.exc_info is None for record in records)


def test_never_cancelled_token_cannot_be_cancelled() -> None:
    NEVER_CANCELLED.cancel()
    assert not NEVER_CANCELLED.is_cancelled
    NEVER_CANCELLED.raise_if_cancelled()


class AdvancingCancellationToken(CancellationToken):
    """Advance a deterministic clock instead of blocking the test thread."""

    def __init__(self, clock) -> None:
        super().__init__()
        self.clock = clock
        self.waits: list[float] = []

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        self.clock.value += timeout
        return False


class SessionClock:
    def __init__(self, value: float = 10.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def test_session_wait_clamps_backoff_to_remaining_deadline() -> None:
    clock = SessionClock()
    token = AdvancingCancellationToken(clock)
    command = ResearchCommand(
        topic="topic",
        config=Configuration(run_timeout_seconds=2),
    )
    session = RunSession(
        command=command,
        state=ResearchState(research_topic="topic"),
        cancellation_token=token,
        monotonic_clock=clock,
    )

    with pytest.raises(DeadlineExceededError):
        session.wait(10)

    assert token.waits == [2]


def test_session_wait_checks_explicit_cancellation_before_deadline() -> None:
    clock = SessionClock()
    token = AdvancingCancellationToken(clock)
    token.cancel()
    command = ResearchCommand(
        topic="topic",
        config=Configuration(run_timeout_seconds=2),
    )
    session = RunSession(
        command=command,
        state=ResearchState(research_topic="topic"),
        cancellation_token=token,
        monotonic_clock=clock,
    )

    with pytest.raises(CancellationRequestedError):
        session.wait(10)

    assert token.waits == []


def make_operation_spec(**overrides) -> OperationSpec:
    values = {
        "operation_name": "search.execute",
        "capabilities": ("search:web",),
        "resource": {"query_hash": "d" * 64, "backend": "duckduckgo"},
        "task_id": 1,
        "task_attempt": 1,
        "operation_attempt": 1,
        "fallback_index": 1,
    }
    values.update(overrides)
    return OperationSpec(**values)


def test_operation_pairing_rejects_duplicates_and_mismatched_attempts() -> None:
    session = make_session()
    session.start()
    spec = make_operation_spec()
    session.start_operation(spec)

    with pytest.raises(InvalidTransitionError):
        session.start_operation(spec)
    with pytest.raises(InvalidTransitionError):
        session.complete_operation(
            make_operation_spec(
                operation_id=spec.operation_id,
                operation_attempt=2,
            ),
            duration_seconds=0.1,
        )

    session.complete_operation(spec, duration_seconds=0.1)
    with pytest.raises(InvalidTransitionError):
        session.fail_operation(
            spec,
            duration_seconds=0.2,
            code="operation_failed",
        )


def test_rejection_closes_operation_admission_with_rejection_priority() -> None:
    session = make_session()
    session.start()
    rejected_spec = make_operation_spec()
    late_spec = make_operation_spec()

    rejected = session.reject_operation(rejected_spec)
    session.request_cancellation()

    with pytest.raises(OperationRejectedError) as late:
        session.start_operation(late_spec)

    assert rejected.kind is EventKind.OPERATION_REJECTED
    assert late.value.operation_id == rejected_spec.operation_id
    assert [
        event.kind
        for event in session.events
        if event.kind.name.startswith("OPERATION_")
    ] == [EventKind.OPERATION_REJECTED]


def test_run_control_checkpoint_prioritizes_first_rejection_over_cancel() -> None:
    session = make_session()
    session.start()
    first_rejection = make_operation_spec()
    later_rejection = make_operation_spec()

    session.reject_operation(first_rejection)
    session.reject_operation(later_rejection)
    session.cancellation.cancel()

    with pytest.raises(OperationRejectedError) as rejected:
        session.raise_if_run_controlled()
    with pytest.raises(CancellationRequestedError):
        session.raise_if_cancelled()

    assert rejected.value.operation_id == first_rejection.operation_id


def test_start_operation_checks_cancellation_and_deadline_before_commit() -> None:
    cancelled = make_session()
    cancelled.start()
    cancelled.request_cancellation()

    with pytest.raises(CancellationRequestedError):
        cancelled.start_operation(make_operation_spec())

    clock = SessionClock()
    command = ResearchCommand(
        topic="topic",
        config=Configuration(run_timeout_seconds=1),
    )
    expired = RunSession(
        command=command,
        state=ResearchState(research_topic="topic"),
        monotonic_clock=clock,
    )
    expired.start()
    clock.value += 1

    with pytest.raises(DeadlineExceededError):
        expired.start_operation(make_operation_spec())

    assert not any(
        event.kind is EventKind.OPERATION_STARTED
        for event in (*cancelled.events, *expired.events)
    )


def test_raw_shared_token_cancel_is_atomic_with_operation_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    class PausingCancellationToken(CancellationToken):
        def __init__(self) -> None:
            super().__init__()
            self.check_observed = Event()
            self.release_check = Event()
            self.cancel_attempted = Event()

        def raise_if_cancelled(self) -> None:
            super().raise_if_cancelled()
            self.check_observed.set()
            assert self.release_check.wait(timeout=2)

        def cancel(self) -> None:
            self.cancel_attempted.set()
            super().cancel()
            order.append("cancelled")

    token = PausingCancellationToken()
    command = ResearchCommand(topic="topic", config=Configuration())
    session = RunSession(
        command=command,
        state=ResearchState(research_topic="topic"),
        cancellation_token=token,
    )
    session.start()
    spec = make_operation_spec()
    original_commit = session._commit_event_locked

    def record_commit(event):
        if event.kind is EventKind.OPERATION_STARTED:
            order.append("started")
        return original_commit(event)

    monkeypatch.setattr(session, "_commit_event_locked", record_commit)
    start_errors: list[BaseException] = []

    def start_operation() -> None:
        try:
            session.start_operation(spec)
        except BaseException as exc:
            start_errors.append(exc)

    starter = Thread(target=start_operation)
    starter.start()
    assert token.check_observed.wait(timeout=2)
    canceller = Thread(target=token.cancel)
    canceller.start()
    assert token.cancel_attempted.wait(timeout=2)
    time.sleep(0.05)
    token.release_check.set()
    starter.join(timeout=2)
    canceller.join(timeout=2)

    assert not starter.is_alive()
    assert not canceller.is_alive()
    assert start_errors == []
    assert order == ["started", "cancelled"]


def test_never_cancelled_token_does_not_serialize_operation_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = Barrier(2)
    sessions = [
        RunSession(
            command=ResearchCommand(topic=f"topic-{index}", config=Configuration()),
            state=ResearchState(research_topic=f"topic-{index}"),
            cancellation_token=NEVER_CANCELLED,
        )
        for index in range(2)
    ]
    for session in sessions:
        session.start()
        original_commit = session._commit_event_locked

        def synchronized_commit(event, commit=original_commit):
            if event.kind is EventKind.OPERATION_STARTED:
                barrier.wait(timeout=1)
            return commit(event)

        monkeypatch.setattr(session, "_commit_event_locked", synchronized_commit)

    errors: list[BaseException] = []

    def start_operation(session: RunSession) -> None:
        try:
            session.start_operation(make_operation_spec())
        except BaseException as exc:
            errors.append(exc)

    workers = [
        Thread(target=start_operation, args=(session,))
        for session in sessions
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=2)

    assert all(not worker.is_alive() for worker in workers)
    assert errors == []


def test_active_operation_prevents_run_terminal_preparation() -> None:
    session = make_session()
    session.start()
    spec = make_operation_spec()
    session.start_operation(spec)

    with pytest.raises(InvalidTransitionError, match="active operation"):
        session.prepare_terminal(RunStatus.COMPLETED, EventKind.RUN_COMPLETED)

    session.fail_operation(
        spec,
        duration_seconds=0.1,
        code="operation_failed",
    )
    prepared = session.prepare_terminal(
        RunStatus.FAILED,
        EventKind.RUN_FAILED,
        code="coordinator_failed",
        message="Research coordination failed.",
    )
    assert prepared.status is RunStatus.FAILED


def test_invalid_operation_metrics_do_not_partially_commit_pairing_state() -> None:
    session = make_session()
    session.start()
    spec = make_operation_spec()
    session.metrics["operations"] = "invalid"

    with pytest.raises(InvalidTransitionError):
        session.start_operation(spec)

    session.metrics.pop("operations")
    started = session.start_operation(spec)
    assert started.kind is EventKind.OPERATION_STARTED

    valid_metrics = dict(session.metrics["operations"])
    session.metrics["operations"] = "invalid"
    with pytest.raises(InvalidTransitionError):
        session.complete_operation(spec, duration_seconds=0.1)

    session.metrics["operations"] = valid_metrics
    completed = session.complete_operation(spec, duration_seconds=0.1)
    assert completed.kind is EventKind.OPERATION_COMPLETED


def test_snapshot_configuration_is_allowlisted_before_repository_boundary() -> None:
    configuration = Configuration(
        llm_provider="custom",
        llm_model_id="safe-model",
        llm_api_key="secret-llm-key",
        llm_base_url="https://private-llm.invalid/v1",
        github_token="secret-github-token",
        github_api_base_url="https://private-github.invalid",
        notes_workspace="private-workspace",
        run_timeout_seconds=321,
    )
    command = ResearchCommand(topic="topic", config=configuration)
    session = RunSession(command=command, state=ResearchState(research_topic="topic"))

    snapshot = session.to_snapshot()
    config_snapshot = snapshot.as_dict()["config_snapshot"]
    serialized = json.dumps(config_snapshot)

    assert config_snapshot["run_timeout_seconds"] == 321
    for unsafe in (
        "llm_api_key",
        "llm_base_url",
        "github_token",
        "github_api_base_url",
        "notes_workspace",
        "secret-llm-key",
        "secret-github-token",
        "private-llm.invalid",
        "private-github.invalid",
        "private-workspace",
    ):
        assert unsafe not in serialized
