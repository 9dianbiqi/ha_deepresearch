"""Contract tests for run-bound governed side-effect operations."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from threading import Barrier, Event, Thread
from typing import Any

import pytest

from config import Configuration
from harness.policy import PolicyDecision
from models import ResearchState
from research.contracts import EventKind, ResearchCommand
from research.operations import (
    GovernedOperations,
    OperationRejectedError,
    OperationScope,
    OperationSpec,
)
from research.session import (
    CancellationRequestedError,
    DeadlineExceededError,
    RunSession,
)


class ManualClock:
    """Deterministic monotonic clock used by deadline and duration tests."""

    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class RecordingPolicy:
    """Return deterministic per-capability decisions and record evaluation order."""

    def __init__(self, outcomes: dict[str, str] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.evaluated: list[str] = []

    def evaluate_capability(
        self,
        capability: str,
        command: ResearchCommand,
    ) -> PolicyDecision:
        self.evaluated.append(capability)
        return PolicyDecision(
            capability=capability,
            outcome=self.outcomes.get(capability, "allow"),
            reason=f"policy-secret reason for {capability}",
        )


class ClosingIterator:
    """Iterator exposing whether explicit stream cleanup reached the delegate."""

    def __init__(self) -> None:
        self.closed = False
        self._values = iter(("first", "second"))

    def __iter__(self) -> ClosingIterator:
        return self

    def __next__(self) -> str:
        return next(self._values)

    def close(self) -> None:
        self.closed = True


class CloseFailingIterator(ClosingIterator):
    """Delegate whose cleanup failure must not corrupt a completed audit."""

    def close(self) -> None:
        self.closed = True
        raise RuntimeError("secret delegate close detail")


def make_session(
    *,
    timeout: float | None = None,
    clock: ManualClock | None = None,
) -> RunSession:
    configuration = Configuration(
        enable_notes=False,
        run_timeout_seconds=timeout,
    )
    command = ResearchCommand(topic="governed topic", config=configuration)
    session = RunSession(
        command=command,
        state=ResearchState(research_topic=command.topic),
        monotonic_clock=clock or ManualClock(),
    )
    session.start()
    return session


def make_spec(**overrides: Any) -> OperationSpec:
    values: dict[str, Any] = {
        "operation_name": "planner.complete",
        "capabilities": ("llm:invoke",),
        "resource": {
            "role": "planner",
            "model_id": "safe-model",
            "prompt_hash": "a" * 64,
        },
        "task_attempt": 1,
        "operation_attempt": 1,
        "fallback_index": 1,
    }
    values.update(overrides)
    return OperationSpec(**values)


def operation_events(session: RunSession) -> list[Any]:
    return [
        event
        for event in session.events
        if event.kind
        in {
            EventKind.OPERATION_STARTED,
            EventKind.OPERATION_COMPLETED,
            EventKind.OPERATION_FAILED,
            EventKind.OPERATION_REJECTED,
        }
    ]


def test_operation_spec_is_immutable_deduplicated_and_scope_is_immutable() -> None:
    operations = GovernedOperations(make_session(), RecordingPolicy())
    spec = make_spec(capabilities=("llm:invoke", "llm:invoke"))
    scope = OperationScope(
        operations=operations,
        task_id=3,
        task_attempt=2,
        fallback_index=1,
    )

    assert spec.capabilities == ("llm:invoke",)
    assert len(spec.operation_id) == 32
    assert scope.spec(
        operation_name="summarizer.stream",
        capabilities=("llm:invoke",),
        resource={"role": "summarizer", "prompt_hash": "b" * 64},
        operation_attempt=4,
    ).task_id == 3
    with pytest.raises(FrozenInstanceError):
        spec.operation_name = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        scope.task_attempt = 9  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_attempt", 0),
        ("task_attempt", True),
        ("operation_attempt", 0),
        ("operation_attempt", False),
        ("fallback_index", 0),
        ("fallback_index", True),
    ],
)
def test_operation_spec_requires_strictly_one_based_integer_counters(
    field: str,
    value: object,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        make_spec(**{field: value})


@pytest.mark.parametrize(
    "resource",
    [
        {"prompt": "raw prompt"},
        {"prompt_hash": "not-a-sha256"},
        {"role": 42},
        {"query_hash": "a" * 64},
    ],
)
def test_operation_spec_rejects_unknown_or_invalid_resource_fields(
    resource: dict[str, object],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        make_spec(resource=resource)


def test_denial_evaluates_every_capability_and_prevents_callback() -> None:
    session = make_session()
    policy = RecordingPolicy({"search:premium": "ask"})
    operations = GovernedOperations(session, policy)
    callback_count = 0
    spec = make_spec(
        operation_name="search.execute",
        capabilities=("search:web", "search:premium", "search:web"),
        resource={"query_hash": "c" * 64, "backend": "perplexity"},
    )

    def callback() -> str:
        nonlocal callback_count
        callback_count += 1
        return "must not run"

    with pytest.raises(OperationRejectedError):
        operations.call(spec, callback)

    assert callback_count == 0
    assert policy.evaluated == ["search:web", "search:premium"]
    assert [event.kind for event in operation_events(session)] == [
        EventKind.OPERATION_REJECTED
    ]
    assert session.policy_decisions[-2:] == [
        {
            "capability": "search:web",
            "outcome": "allow",
            "reason": "Capability allowed by policy.",
        },
        {
            "capability": "search:premium",
            "outcome": "ask",
            "reason": "Capability requires explicit approval.",
        },
    ]


def test_rejection_closes_admission_before_observer_releases_contender() -> None:
    session = make_session()
    operations = GovernedOperations(
        session,
        RecordingPolicy({"notes:write": "deny"}),
    )
    rejection_visible = Event()
    contender_done = Event()
    callback_count = 0
    contender_errors: list[BaseException] = []
    contender_spec = make_spec()

    def observe(event: Any) -> None:
        if event.kind is EventKind.OPERATION_REJECTED:
            rejection_visible.set()
            assert contender_done.wait(timeout=2)

    def contender_callback() -> str:
        nonlocal callback_count
        callback_count += 1
        return "must not run"

    def contend() -> None:
        assert rejection_visible.wait(timeout=2)
        try:
            operations.call(contender_spec, contender_callback)
        except BaseException as exc:
            contender_errors.append(exc)
        finally:
            contender_done.set()

    session.add_observer(observe)
    contender = Thread(target=contend)
    contender.start()
    rejected_spec = make_spec(
        operation_name="notes.update",
        capabilities=("notes:write",),
        resource={
            "action": "update",
            "note_kind": "task",
            "note_id": "task-note-1",
        },
    )

    with pytest.raises(OperationRejectedError) as rejected:
        operations.call(rejected_spec, lambda: "must not run")

    contender.join(timeout=2)
    assert not contender.is_alive()
    assert rejected.value.operation_id == rejected_spec.operation_id
    assert len(contender_errors) == 1
    assert isinstance(contender_errors[0], OperationRejectedError)
    assert contender_errors[0].operation_id == rejected_spec.operation_id
    assert callback_count == 0
    assert [event.kind for event in operation_events(session)] == [
        EventKind.OPERATION_REJECTED
    ]


def test_operation_started_before_rejection_may_complete_after_it() -> None:
    session = make_session()
    operations = GovernedOperations(
        session,
        RecordingPolicy({"notes:write": "deny"}),
    )
    callback_started = Event()
    release_callback = Event()
    result: list[str] = []
    errors: list[BaseException] = []

    def active_callback() -> str:
        callback_started.set()
        assert release_callback.wait(timeout=2)
        return "completed"

    def invoke_active() -> None:
        try:
            result.append(operations.call(make_spec(), active_callback))
        except BaseException as exc:
            errors.append(exc)

    active = Thread(target=invoke_active)
    active.start()
    assert callback_started.wait(timeout=2)
    rejected_spec = make_spec(
        operation_name="notes.update",
        capabilities=("notes:write",),
        resource={
            "action": "update",
            "note_kind": "task",
            "note_id": "task-note-1",
        },
    )

    with pytest.raises(OperationRejectedError):
        operations.call(rejected_spec, lambda: "must not run")
    release_callback.set()
    active.join(timeout=2)

    assert not active.is_alive()
    assert errors == []
    assert result == ["completed"]
    audit = operation_events(session)
    assert [event.kind for event in audit] == [
        EventKind.OPERATION_STARTED,
        EventKind.OPERATION_REJECTED,
        EventKind.OPERATION_COMPLETED,
    ]
    assert audit[0].operation_id == audit[2].operation_id
    assert audit[0].operation_id != audit[1].operation_id


def test_allowed_call_runs_once_and_updates_metrics_before_observers() -> None:
    session = make_session()
    observed: list[tuple[EventKind, dict[str, object]]] = []
    session.add_observer(
        lambda event: observed.append(
            (event.kind, dict(session.metrics.get("operations", {})))
        )
        if event.kind in {EventKind.OPERATION_STARTED, EventKind.OPERATION_COMPLETED}
        else None
    )
    operations = GovernedOperations(session, RecordingPolicy())
    callback_count = 0

    def callback() -> str:
        nonlocal callback_count
        callback_count += 1
        return "provider result"

    assert operations.call(make_spec(), callback) == "provider result"
    assert callback_count == 1
    assert [kind for kind, _metrics in observed] == [
        EventKind.OPERATION_STARTED,
        EventKind.OPERATION_COMPLETED,
    ]
    assert observed[0][1]["started"] == 1
    assert observed[0][1]["active"] == 1
    assert observed[1][1]["completed"] == 1
    assert observed[1][1]["active"] == 0


def test_provider_permission_failure_is_re_raised_and_never_serialized() -> None:
    session = make_session()
    operations = GovernedOperations(session, RecordingPolicy())
    secret = "provider-secret-permission-detail"

    def callback() -> None:
        raise PermissionError(secret)

    with pytest.raises(PermissionError, match=secret):
        operations.call(make_spec(), callback)

    assert [event.kind for event in operation_events(session)] == [
        EventKind.OPERATION_STARTED,
        EventKind.OPERATION_FAILED,
    ]
    serialized = json.dumps(session.to_snapshot().as_dict())
    assert secret not in serialized
    assert "policy-secret" not in serialized
    assert "operation_rejected" not in serialized


def test_precancelled_call_never_authorizes_or_invokes_callback() -> None:
    session = make_session()
    session.request_cancellation()
    policy = RecordingPolicy()
    operations = GovernedOperations(session, policy)
    called = False

    def callback() -> None:
        nonlocal called
        called = True

    with pytest.raises(CancellationRequestedError):
        operations.call(make_spec(), callback)

    assert not called
    assert policy.evaluated == []
    assert operation_events(session) == []


def test_cancellation_during_call_emits_failed_and_never_completed() -> None:
    session = make_session()
    operations = GovernedOperations(session, RecordingPolicy())

    def callback() -> str:
        session.request_cancellation()
        return "ignored"

    with pytest.raises(CancellationRequestedError):
        operations.call(make_spec(), callback)

    assert [event.kind for event in operation_events(session)] == [
        EventKind.OPERATION_STARTED,
        EventKind.OPERATION_FAILED,
    ]
    assert operation_events(session)[-1].payload["code"] == "cancelled"


@pytest.mark.parametrize("streaming", [False, True])
def test_cancellation_after_start_event_prevents_delegate_invocation(
    streaming: bool,
) -> None:
    session = make_session()
    operations = GovernedOperations(session, RecordingPolicy())
    delegate_calls = 0

    def cancel_after_start(event: Any) -> None:
        if event.kind is EventKind.OPERATION_STARTED:
            session.cancellation.cancel()

    def callback() -> str:
        nonlocal delegate_calls
        delegate_calls += 1
        return "must not run"

    def stream_factory() -> tuple[str, ...]:
        nonlocal delegate_calls
        delegate_calls += 1
        return ("must not run",)

    session.add_observer(cancel_after_start)

    with pytest.raises(CancellationRequestedError):
        if streaming:
            list(
                operations.stream(
                    make_spec(operation_name="summarizer.stream"),
                    stream_factory,
                )
            )
        else:
            operations.call(make_spec(), callback)

    assert delegate_calls == 0
    assert [event.kind for event in operation_events(session)] == [
        EventKind.OPERATION_STARTED,
        EventKind.OPERATION_FAILED,
    ]
    assert operation_events(session)[-1].payload["code"] == "cancelled"


def test_deadline_before_and_during_calls_never_complete() -> None:
    before_clock = ManualClock()
    before_session = make_session(timeout=1, clock=before_clock)
    before_operations = GovernedOperations(
        before_session,
        RecordingPolicy(),
        monotonic_clock=before_clock,
    )
    before_clock.advance(1)
    with pytest.raises(DeadlineExceededError):
        before_operations.call(make_spec(), lambda: "never")
    assert operation_events(before_session) == []

    during_clock = ManualClock()
    during_session = make_session(timeout=1, clock=during_clock)
    during_operations = GovernedOperations(
        during_session,
        RecordingPolicy(),
        monotonic_clock=during_clock,
    )

    def expire() -> str:
        during_clock.advance(1)
        return "ignored"

    with pytest.raises(DeadlineExceededError):
        during_operations.call(make_spec(), expire)
    assert [event.kind for event in operation_events(during_session)] == [
        EventKind.OPERATION_STARTED,
        EventKind.OPERATION_FAILED,
    ]
    assert operation_events(during_session)[-1].payload["code"] == (
        "deadline_exceeded"
    )


def test_stream_is_lazy_and_close_closes_delegate_with_one_failed_terminal() -> None:
    session = make_session()
    policy = RecordingPolicy()
    operations = GovernedOperations(session, policy)
    delegate = ClosingIterator()
    factory_count = 0

    def factory() -> ClosingIterator:
        nonlocal factory_count
        factory_count += 1
        return delegate

    stream = operations.stream(
        make_spec(operation_name="summarizer.stream"),
        factory,
    )
    assert policy.evaluated == []
    assert factory_count == 0
    assert operation_events(session) == []

    assert next(stream) == "first"
    assert factory_count == 1
    stream.close()
    stream.close()

    assert delegate.closed
    assert [event.kind for event in operation_events(session)] == [
        EventKind.OPERATION_STARTED,
        EventKind.OPERATION_FAILED,
    ]
    assert operation_events(session)[-1].payload["code"] == "stream_closed"


def test_exhausted_stream_completes_once() -> None:
    session = make_session()
    operations = GovernedOperations(session, RecordingPolicy())
    delegate = ClosingIterator()

    assert list(
        operations.stream(
            make_spec(operation_name="summarizer.stream"),
            lambda: delegate,
        )
    ) == ["first", "second"]
    assert delegate.closed
    assert [event.kind for event in operation_events(session)] == [
        EventKind.OPERATION_STARTED,
        EventKind.OPERATION_COMPLETED,
    ]


def test_stream_close_failure_does_not_replace_success_or_leak_text() -> None:
    session = make_session()
    operations = GovernedOperations(session, RecordingPolicy())
    delegate = CloseFailingIterator()

    assert list(
        operations.stream(
            make_spec(operation_name="summarizer.stream"),
            lambda: delegate,
        )
    ) == ["first", "second"]
    assert delegate.closed
    assert [event.kind for event in operation_events(session)] == [
        EventKind.OPERATION_STARTED,
        EventKind.OPERATION_COMPLETED,
    ]
    assert "secret delegate close detail" not in json.dumps(
        [event.as_dict() for event in session.events]
    )


def test_concurrent_operations_have_unique_ids_and_global_fifo_sequences() -> None:
    session = make_session()
    observed: list[int] = []
    session.add_observer(lambda event: observed.append(event.sequence))
    operations = GovernedOperations(session, RecordingPolicy())
    barrier = Barrier(4)
    specs = [make_spec() for _index in range(4)]

    def invoke(spec: OperationSpec) -> str:
        def callback() -> str:
            barrier.wait(timeout=2)
            return spec.operation_id

        return operations.call(spec, callback)

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(invoke, specs))

    assert len(set(results)) == 4
    audit = operation_events(session)
    assert len(audit) == 8
    assert len({event.sequence for event in audit}) == 8
    assert observed == sorted(observed)
    assert [event.sequence for event in session.events] == list(
        range(1, len(session.events) + 1)
    )
