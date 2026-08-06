"""Thread-safe authoritative state transitions for a research run."""

from __future__ import annotations

import logging
import math
from collections import deque
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from threading import Event, RLock
from time import monotonic
from typing import TYPE_CHECKING, Any

from models import ResearchState, SummaryStateOutput, TodoItem

from .contracts import (
    EventKind,
    PreparedTerminal,
    ResearchCommand,
    ResearchEvent,
    RunError,
    RunSnapshot,
    RunStatus,
)

if TYPE_CHECKING:
    from .operations import OperationSpec


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


class InvalidTransitionError(RuntimeError):
    """Raised when a lifecycle or task transition is not allowed."""


class CancellationRequestedError(RuntimeError):
    """Raised at a cancellation checkpoint after cancellation is requested."""


class DeadlineExceededError(RuntimeError):
    """Raised when a run reaches its configured monotonic deadline."""


class CancellationToken:
    """Small thread-safe cancellation token backed by ``threading.Event``."""

    def __init__(self, event: Event | None = None) -> None:
        """Initialize the token with a private or caller-supplied event."""
        self._event = event or Event()
        self._lock = RLock()

    def cancel(self) -> None:
        """Request cancellation."""
        with self._lock:
            self._event.set()

    @property
    def is_cancelled(self) -> bool:
        """Return whether cancellation has been requested."""
        with self._lock:
            return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        """Raise when cancellation has been requested."""
        with self._lock:
            if self._event.is_set():
                raise CancellationRequestedError("Research run was cancelled.")

    def wait(self, timeout: float) -> bool:
        """Wait for cancellation for up to ``timeout`` seconds."""
        return self._event.wait(timeout)

    def _operation_guard(self) -> AbstractContextManager[bool | None]:
        """Serialize raw cancellation with an operation-start commit."""
        return self._lock


class _NeverCancelledToken(CancellationToken):
    """Cancellation token whose backing event is never set."""

    def cancel(self) -> None:
        """Ignore cancellation requests for the never-cancelled singleton."""

    @property
    def is_cancelled(self) -> bool:
        """Return false without serializing independent sessions."""
        return False

    def raise_if_cancelled(self) -> None:
        """Accept every cancellation checkpoint."""

    def _operation_guard(self) -> AbstractContextManager[bool | None]:
        """Avoid coupling independent sessions through the singleton token."""
        return nullcontext()


NEVER_CANCELLED: CancellationToken = _NeverCancelledToken()


Observer = Callable[[ResearchEvent], None]
_LOGGER = logging.getLogger(__name__)

_TERMINAL_EVENTS = {
    RunStatus.COMPLETED: EventKind.RUN_COMPLETED,
    RunStatus.FAILED: EventKind.RUN_FAILED,
    RunStatus.CANCELLED: EventKind.RUN_CANCELLED,
    RunStatus.REJECTED: EventKind.RUN_REJECTED,
}


@dataclass(kw_only=True)
class RunSession:
    """Own canonical state, lifecycle, events, and observers for one run."""

    command: ResearchCommand
    state: ResearchState
    started_at: datetime = field(default_factory=utc_now)
    completed_at: datetime | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    error: RunError | None = None
    followup_context: dict[str, Any] = field(default_factory=dict)
    policy_decisions: list[dict[str, Any]] = field(default_factory=list)
    cancellation_token: CancellationToken = field(default_factory=CancellationToken)
    monotonic_clock: Callable[[], float] = field(
        default=monotonic,
        repr=False,
    )
    events: list[ResearchEvent] = field(default_factory=list, init=False)
    _status: RunStatus = field(default=RunStatus.PENDING, init=False, repr=False)
    _next_sequence: int = field(default=1, init=False, repr=False)
    _observers: list[Observer] = field(default_factory=list, init=False, repr=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)
    _pending_notifications: deque[
        tuple[tuple[Observer, ...], ResearchEvent]
    ] = field(default_factory=deque, init=False, repr=False)
    _notification_draining: bool = field(default=False, init=False, repr=False)
    _prepared_terminal: PreparedTerminal | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _deadline_at: float | None = field(default=None, init=False, repr=False)
    _operation_states: dict[
        tuple[str, int, int, int], str
    ] = field(default_factory=dict, init=False, repr=False)
    _operation_envelopes: dict[
        tuple[str, int, int, int], tuple[int | None, dict[str, object]]
    ] = field(default_factory=dict, init=False, repr=False)
    _operation_admission_closed: bool = field(
        default=False,
        init=False,
        repr=False,
    )
    _first_rejection_operation_id: str | None = field(
        default=None,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        """Fill the canonical topic from the immutable command when absent."""
        if self.state.research_topic is None:
            self.state.research_topic = self.command.topic
        timeout = self.command.config.run_timeout_seconds
        if timeout is not None:
            self._deadline_at = self.monotonic_clock() + timeout

    @property
    def status(self) -> RunStatus:
        """Return the current lifecycle state."""
        with self._lock:
            return self._status

    @property
    def run_id(self) -> str:
        """Return the normalized run identifier."""
        return self.command.run_id

    @property
    def request(self) -> ResearchCommand:
        """Expose the historical harness name for the command."""
        return self.command

    @property
    def cancellation(self) -> CancellationToken:
        """Return the session cancellation token."""
        return self.cancellation_token

    @property
    def compressed_context(self) -> dict[str, Any]:
        """Expose the historical name for follow-up context."""
        return self.followup_context

    @compressed_context.setter
    def compressed_context(self, value: dict[str, Any]) -> None:
        self.followup_context = value

    @property
    def result(self) -> SummaryStateOutput | None:
        """Return a legacy result view when canonical output exists."""
        output = self.to_legacy_output()
        if not output.todo_items and not output.running_summary and not output.report_markdown:
            return None
        return output

    @property
    def duration_seconds(self) -> float | None:
        """Return elapsed duration once the session is complete."""
        if self.completed_at is None:
            return None
        return (self.completed_at - self.started_at).total_seconds()

    def add_observer(self, observer: Observer) -> None:
        """Register an observer for subsequently committed events."""
        with self._lock:
            self._observers.append(observer)

    def start(self) -> ResearchEvent:
        """Transition a pending session to running."""
        with self._lock:
            if self._status is not RunStatus.PENDING:
                raise InvalidTransitionError("Only a pending run can start.")
            event = self._validated_event_locked(
                EventKind.RUN_STARTED,
                {"topic": self.command.topic},
            )
            self._status = RunStatus.RUNNING
            should_drain = self._commit_event_locked(event)
        if should_drain:
            self._drain_notifications()
        return event

    def install_plan(self, tasks: Sequence[TodoItem]) -> ResearchEvent:
        """Install the canonical task plan and emit its normalized event."""
        planned = list(tasks)
        task_ids = [task.id for task in planned]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("Task IDs in a plan must be unique.")
        payload = {"tasks": [task.to_dict() for task in planned]}
        with self._lock:
            self._require_running_locked()
            event = self._validated_event_locked(EventKind.PLAN_CREATED, payload)
            self.state.todo_items = planned
            should_drain = self._commit_event_locked(event)
        if should_drain:
            self._drain_notifications()
        return event

    def record_repository(
        self,
        *,
        github_context: dict[str, Any],
        repository: dict[str, object],
        notices: Sequence[str],
        notice_codes: Sequence[str] = (),
    ) -> ResearchEvent:
        """Install canonical GitHub context while emitting only safe metadata."""
        payload = {
            "repository": dict(repository),
            "notices": list(notices),
            "notice_codes": list(notice_codes),
        }
        with self._lock:
            self._require_running_locked()
            event = self._validated_event_locked(
                EventKind.REPOSITORY_DETECTED,
                payload,
            )
            self.state.github_context = dict(github_context)
            should_drain = self._commit_event_locked(event)
        if should_drain:
            self._drain_notifications()
        return event

    def start_task(self, task_id: int) -> ResearchEvent:
        """Mark one canonical task as in progress."""
        with self._lock:
            self._require_running_locked()
            task = self._task_locked(task_id)
            if task.status != "pending":
                raise InvalidTransitionError(
                    f"Task {task_id} cannot start from status {task.status!r}."
                )
            payload = self._task_projection_locked(task)
            payload["status"] = "in_progress"
            event = self._validated_event_locked(
                EventKind.TASK_STARTED,
                payload,
                task_id=task_id,
            )
            task.status = "in_progress"
            should_drain = self._commit_event_locked(event)
        if should_drain:
            self._drain_notifications()
        return event

    def record_sources(
        self,
        task_id: int,
        *,
        context: str | None = None,
        **safe_payload: object,
    ) -> ResearchEvent:
        """Record safe source metadata for a task."""
        with self._lock:
            self._require_running_locked()
            task = self._task_locked(task_id)
            payload = self._task_projection_locked(task)
            payload.update(safe_payload)
            event = self._validated_event_locked(
                EventKind.SOURCES_COLLECTED,
                payload,
                task_id=task_id,
            )
            source_text = payload.get("sources_summary") or payload.get("latest_sources")
            if isinstance(source_text, str):
                task.sources_summary = source_text
                self.state.sources_gathered.append(source_text)
            if isinstance(context, str):
                self.state.web_research_results.append(context)
            notices = payload.get("notices")
            if isinstance(notices, (list, tuple)):
                task.notices = [
                    notice for notice in notices if isinstance(notice, str)
                ]
            notice_codes = payload.get("notice_codes")
            if isinstance(notice_codes, (list, tuple)):
                task.notice_codes = [
                    code for code in notice_codes if isinstance(code, str)
                ]
            sources = payload.get("sources")
            if isinstance(sources, (list, tuple)):
                self.state.sources_gathered.extend(
                    source for source in sources if isinstance(source, str)
                )
            should_drain = self._commit_event_locked(event)
        if should_drain:
            self._drain_notifications()
        return event

    def record_report_note(
        self,
        *,
        note_id: str,
        note_path: str | None,
        title: str,
    ) -> ResearchEvent:
        """Record conclusion-note coordinates before report publication."""
        payload = {
            "note_id": note_id,
            "note_path": note_path,
            "title": title,
        }
        with self._lock:
            self._require_running_locked()
            event = self._validated_event_locked(
                EventKind.REPORT_NOTE_CREATED,
                payload,
            )
            self.state.report_note_id = note_id
            self.state.report_note_path = note_path
            should_drain = self._commit_event_locked(event)
        if should_drain:
            self._drain_notifications()
        return event

    def append_task_summary(self, task_id: int, chunk: str) -> ResearchEvent:
        """Append one streamed summary chunk to the canonical task."""
        with self._lock:
            self._require_running_locked()
            task = self._task_locked(task_id)
            payload = self._task_projection_locked(task)
            payload["chunk"] = chunk
            event = self._validated_event_locked(
                EventKind.SUMMARY_DELTA,
                payload,
                task_id=task_id,
            )
            task.summary = f"{task.summary or ''}{chunk}"
            should_drain = self._commit_event_locked(event)
        if should_drain:
            self._drain_notifications()
        return event

    def record_retry(
        self,
        task_id: int,
        *,
        previous_query: str,
        refined_query: str,
        attempt: int,
        reason: str,
    ) -> ResearchEvent:
        """Record one retry and update canonical query history."""
        with self._lock:
            self._require_running_locked()
            task = self._task_locked(task_id)
            payload = self._task_projection_locked(task)
            payload.update(
                {
                    "previous_query": previous_query,
                    "refined_query": refined_query,
                    "attempt": attempt,
                    "reason": reason,
                }
            )
            event = self._validated_event_locked(
                EventKind.TASK_RETRY_SCHEDULED,
                payload,
                task_id=task_id,
            )
            task.retry_count = max(task.retry_count, attempt)
            task.refined_queries.append(refined_query)
            task.query = refined_query
            task.notices.append(reason)
            should_drain = self._commit_event_locked(event)
        if should_drain:
            self._drain_notifications()
        return event

    def complete_task(
        self,
        task_id: int,
        *,
        summary: str,
        sources_summary: str | None,
        original_query: str | None = None,
    ) -> ResearchEvent:
        """Complete a task with its final summary and sources."""
        with self._lock:
            self._require_running_locked()
            task = self._task_locked(task_id)
            payload = self._task_projection_locked(task)
            payload.update(
                {
                    "status": "completed",
                    "summary": summary,
                    "sources_summary": sources_summary,
                }
            )
            event = self._validated_event_locked(
                EventKind.TASK_COMPLETED,
                payload,
                task_id=task_id,
            )
            task.status = "completed"
            task.summary = summary
            task.sources_summary = sources_summary
            if original_query is not None:
                task.query = original_query
            should_drain = self._commit_event_locked(event)
        if should_drain:
            self._drain_notifications()
        return event

    def skip_task(
        self,
        task_id: int,
        *,
        reason: str,
        original_query: str | None = None,
    ) -> ResearchEvent:
        """Skip a task while retaining the reason in canonical state."""
        with self._lock:
            self._require_running_locked()
            task = self._task_locked(task_id)
            payload = self._task_projection_locked(task)
            payload.update({"status": "skipped", "reason": reason})
            event = self._validated_event_locked(
                EventKind.TASK_SKIPPED,
                payload,
                task_id=task_id,
            )
            task.status = "skipped"
            task.notices.append(reason)
            if original_query is not None:
                task.query = original_query
            should_drain = self._commit_event_locked(event)
        if should_drain:
            self._drain_notifications()
        return event

    def fail_task(
        self,
        task_id: int,
        *,
        message: str,
        code: str,
        original_query: str | None = None,
    ) -> ResearchEvent:
        """Fail a task and persist the failure before observers run."""
        with self._lock:
            self._require_running_locked()
            task = self._task_locked(task_id)
            payload = self._task_projection_locked(task)
            payload.update(
                {"status": "failed", "message": message, "code": code}
            )
            event = self._validated_event_locked(
                EventKind.TASK_FAILED,
                payload,
                task_id=task_id,
            )
            task.status = "failed"
            task.notices.append(message)
            if original_query is not None:
                task.query = original_query
            should_drain = self._commit_event_locked(event)
        if should_drain:
            self._drain_notifications()
        return event

    def set_report(
        self,
        report: str,
        *,
        note_id: str | None = None,
        note_path: str | None = None,
    ) -> ResearchEvent:
        """Install the generated report and optional note coordinates."""
        with self._lock:
            self._require_running_locked()
            event = self._validated_event_locked(
                EventKind.REPORT_GENERATED,
                {"report": report, "note_id": note_id, "note_path": note_path},
            )
            self.state.structured_report = report
            self.state.running_summary = report
            self.state.report_note_id = note_id
            self.state.report_note_path = note_path
            should_drain = self._commit_event_locked(event)
        if should_drain:
            self._drain_notifications()
        return event

    def append_policy_decisions(
        self,
        decisions: Sequence[dict[str, str]],
    ) -> None:
        """Append detached, allowlisted operation policy decisions atomically."""
        detached: list[dict[str, str]] = []
        for decision in decisions:
            if set(decision) != {"capability", "outcome", "reason"}:
                raise ValueError("Policy decision contains fields outside its allowlist.")
            if not all(isinstance(value, str) for value in decision.values()):
                raise TypeError("Policy decision fields must be text.")
            if decision["outcome"] not in {"allow", "deny", "ask"}:
                raise ValueError("Policy decision outcome is invalid.")
            detached.append(dict(decision))
        with self._lock:
            self._require_running_locked()
            self.policy_decisions.extend(detached)

    def start_operation(self, spec: OperationSpec) -> ResearchEvent:
        """Commit one uniquely paired operation start transition."""
        with self._lock:
            self._require_running_locked()
            if self._operation_admission_closed:
                from .operations import OperationRejectedError

                raise OperationRejectedError(self._first_rejection_operation_id)
            # A raw shared-token cancel (for example HarnessStream.close)
            # participates in the same ordering as the operation commit.
            with self.cancellation_token._operation_guard():
                self._raise_if_cancelled_locked()
                key, task_id, envelope = self._operation_identity_locked(spec)
                if key in self._operation_states:
                    raise InvalidTransitionError(
                        "An operation attempt with this identity already exists."
                    )
                event = self._validated_event_locked(
                    EventKind.OPERATION_STARTED,
                    envelope,
                    task_id=task_id,
                    operation_id=key[0],
                )
                operation_metrics = self._next_operation_metrics_locked("started")
                self._operation_states[key] = "active"
                self._operation_envelopes[key] = (task_id, envelope)
                self.metrics["operations"] = operation_metrics
                should_drain = self._commit_event_locked(event)
        if should_drain:
            self._drain_notifications()
        return event

    def complete_operation(
        self,
        spec: OperationSpec,
        *,
        duration_seconds: float,
    ) -> ResearchEvent:
        """Commit the single successful terminal for a started operation."""
        return self._terminal_operation(
            spec,
            kind=EventKind.OPERATION_COMPLETED,
            state="completed",
            duration_seconds=duration_seconds,
            code=None,
        )

    def fail_operation(
        self,
        spec: OperationSpec,
        *,
        duration_seconds: float,
        code: str,
    ) -> ResearchEvent:
        """Commit the single failed terminal for a started operation."""
        from .operations import validate_operation_error_code

        return self._terminal_operation(
            spec,
            kind=EventKind.OPERATION_FAILED,
            state="failed",
            duration_seconds=duration_seconds,
            code=validate_operation_error_code(code),
        )

    def reject_operation(
        self,
        spec: OperationSpec,
        *,
        code: str = "operation_rejected",
    ) -> ResearchEvent:
        """Commit one rejected terminal without first starting the callback."""
        from .operations import validate_operation_error_code

        safe_code = validate_operation_error_code(code)
        with self._lock:
            self._require_running_locked()
            key, task_id, envelope = self._operation_identity_locked(spec)
            if key in self._operation_states:
                raise InvalidTransitionError(
                    "An operation attempt with this identity already exists."
                )
            payload = {**envelope, "code": safe_code}
            event = self._validated_event_locked(
                EventKind.OPERATION_REJECTED,
                payload,
                task_id=task_id,
                operation_id=key[0],
            )
            operation_metrics = self._next_operation_metrics_locked("rejected")
            # Close start admission in the same critical section that commits
            # the rejection. Once observers can see OPERATION_REJECTED, no
            # later operation is allowed to publish OPERATION_STARTED.
            self._operation_admission_closed = True
            if self._first_rejection_operation_id is None:
                self._first_rejection_operation_id = key[0]
            self._operation_states[key] = "rejected"
            self._operation_envelopes[key] = (task_id, envelope)
            self.metrics["operations"] = operation_metrics
            should_drain = self._commit_event_locked(event)
        if should_drain:
            self._drain_notifications()
        return event

    def prepare_terminal(
        self,
        status: RunStatus,
        kind: EventKind,
        **payload: object,
    ) -> PreparedTerminal:
        """Prepare, but do not commit, one terminal transition."""
        with self._lock:
            self._require_running_status_locked()
            if any(
                state == "active" for state in self._operation_states.values()
            ):
                raise InvalidTransitionError(
                    "A run with an active operation cannot prepare a terminal."
                )
            expected_kind = _TERMINAL_EVENTS.get(status)
            if expected_kind is None or kind is not expected_kind:
                raise InvalidTransitionError(
                    f"{status.value!r} cannot be confirmed by {kind.value!r}."
                )
            event = self._build_event_locked(kind, dict(payload))
            event.as_dict()
            snapshot = self.to_snapshot(status=status, terminal_event=event)
            if status in {RunStatus.FAILED, RunStatus.REJECTED}:
                message = payload.get("message")
                code = payload.get("code")
                if isinstance(message, str) and isinstance(code, str):
                    snapshot = replace(
                        snapshot,
                        error=RunError(code=code, message=message),
                    )
            prepared = PreparedTerminal(
                status=status,
                event=event,
                snapshot=snapshot,
            )
            self._prepared_terminal = prepared
            return prepared

    def confirm_terminal(self, prepared: PreparedTerminal) -> ResearchEvent:
        """Commit a previously prepared terminal event exactly once."""
        with self._lock:
            self._require_running_status_locked()
            if prepared is not self._prepared_terminal:
                raise InvalidTransitionError(
                    "Terminal transition was not prepared by this session."
                )
            if prepared.event.sequence != self._next_sequence:
                raise InvalidTransitionError("Prepared terminal event is stale.")
            self._status = prepared.status
            self.completed_at = prepared.event.occurred_at
            self.error = prepared.snapshot.error
            self._prepared_terminal = None
            should_drain = self._commit_event_locked(prepared.event)
        if should_drain:
            self._drain_notifications()
        return prepared.event

    def request_cancellation(self) -> None:
        """Request cooperative cancellation."""
        with self._lock:
            self.cancellation_token.cancel()

    def raise_if_cancelled(self) -> None:
        """Raise at a cooperative cancellation or deadline checkpoint."""
        with self._lock:
            self._raise_if_cancelled_locked()

    def raise_if_run_controlled(self) -> None:
        """Raise run-level rejection before cancellation or deadline."""
        with self._lock:
            if self._first_rejection_operation_id is not None:
                from .operations import OperationRejectedError

                raise OperationRejectedError(self._first_rejection_operation_id)
            self._raise_if_cancelled_locked()

    def _raise_if_cancelled_locked(self) -> None:
        """Raise for cancellation/deadline while the session lock is held."""
        self.cancellation_token.raise_if_cancelled()
        deadline_at = self._deadline_at
        if deadline_at is not None and self.monotonic_clock() >= deadline_at:
            raise DeadlineExceededError("Research run deadline was exceeded.")

    def wait(self, timeout: float) -> bool:
        """Wait cooperatively while respecting cancellation and the run deadline."""
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout < 0
        ):
            raise ValueError("Wait timeout must be a finite non-negative number.")
        self.raise_if_cancelled()
        effective_timeout = float(timeout)
        deadline_at = self._deadline_at
        if deadline_at is not None:
            remaining = deadline_at - self.monotonic_clock()
            if remaining <= 0:
                self.raise_if_cancelled()
            effective_timeout = min(effective_timeout, remaining)
        if self.cancellation_token.wait(effective_timeout):
            return True
        self.raise_if_cancelled()
        return False

    def to_legacy_output(self) -> SummaryStateOutput:
        """Project canonical state into the legacy response shape."""
        with self._lock:
            return SummaryStateOutput(
                running_summary=self.state.running_summary,
                report_markdown=self.state.structured_report,
                todo_items=[TodoItem(**item.to_dict()) for item in self.state.todo_items],
            )

    def to_snapshot(
        self,
        *,
        status: RunStatus | None = None,
        terminal_event: ResearchEvent | None = None,
    ) -> RunSnapshot:
        """Capture an immutable persistence view without mutating the session."""
        with self._lock:
            snapshot_status = status or self._status
            output = self.to_legacy_output()
            events = list(self.events)
            if terminal_event is not None and terminal_event not in events:
                events.append(terminal_event)
            completed_at = self.completed_at
            if terminal_event is not None and snapshot_status in _TERMINAL_EVENTS:
                completed_at = terminal_event.occurred_at
            return RunSnapshot(
                run_id=self.run_id,
                topic=self.command.topic,
                status=snapshot_status,
                started_at=self.started_at,
                completed_at=completed_at,
                parent_run_id=self.command.parent_run_id,
                output={
                    "running_summary": output.running_summary,
                    "report_markdown": output.report_markdown,
                    "todo_items": [item.to_dict() for item in output.todo_items],
                },
                followup_context=dict(self.followup_context),
                metrics=dict(self.metrics),
                policy_decisions=tuple(dict(item) for item in self.policy_decisions),
                config_snapshot=self.command.config.safe_snapshot(),
                events=tuple(events),
                error=self.error,
            )

    def _terminal_operation(
        self,
        spec: OperationSpec,
        *,
        kind: EventKind,
        state: str,
        duration_seconds: float,
        code: str | None,
    ) -> ResearchEvent:
        if (
            isinstance(duration_seconds, bool)
            or not isinstance(duration_seconds, (int, float))
            or not math.isfinite(duration_seconds)
            or duration_seconds < 0
        ):
            raise ValueError("Operation duration must be a finite non-negative number.")
        duration = float(duration_seconds)
        with self._lock:
            self._require_running_locked()
            key, task_id, envelope = self._operation_identity_locked(spec)
            if self._operation_states.get(key) != "active":
                raise InvalidTransitionError(
                    "Operation terminal does not match one active start."
                )
            if self._operation_envelopes.get(key) != (task_id, envelope):
                raise InvalidTransitionError(
                    "Operation terminal metadata does not match its start."
                )
            payload: dict[str, object] = {
                **envelope,
                "duration_seconds": duration,
            }
            if code is not None:
                payload["code"] = code
            event = self._validated_event_locked(
                kind,
                payload,
                task_id=task_id,
                operation_id=key[0],
            )
            operation_metrics = self._next_operation_metrics_locked(
                state,
                duration_seconds=duration,
            )
            self._operation_states[key] = state
            self.metrics["operations"] = operation_metrics
            should_drain = self._commit_event_locked(event)
        if should_drain:
            self._drain_notifications()
        return event

    @staticmethod
    def _operation_identity_locked(
        spec: OperationSpec,
    ) -> tuple[
        tuple[str, int, int, int],
        int | None,
        dict[str, object],
    ]:
        key = spec.pairing_key
        envelope = spec.event_payload()
        if not isinstance(key, tuple) or len(key) != 4:
            raise TypeError("Operation spec pairing key is invalid.")
        if not isinstance(envelope, dict):
            raise TypeError("Operation spec event payload is invalid.")
        return key, spec.task_id, envelope

    def _next_operation_metrics_locked(
        self,
        transition: str,
        *,
        duration_seconds: float = 0.0,
    ) -> dict[str, int | float]:
        raw_metrics = self.metrics.get("operations")
        if raw_metrics is None:
            operation_metrics: dict[str, int | float] = {
                "started": 0,
                "completed": 0,
                "failed": 0,
                "rejected": 0,
                "active": 0,
                "duration_seconds": 0.0,
            }
        elif isinstance(raw_metrics, dict):
            operation_metrics = dict(raw_metrics)
        else:
            raise InvalidTransitionError("Operation audit metrics have invalid state.")

        if transition not in {"started", "completed", "failed", "rejected"}:
            raise InvalidTransitionError("Operation audit transition is invalid.")
        current = operation_metrics.get(transition, 0)
        if type(current) not in {int, float}:
            raise InvalidTransitionError("Operation audit counter is invalid.")
        operation_metrics[transition] = current + 1
        active = operation_metrics.get("active", 0)
        if type(active) not in {int, float}:
            raise InvalidTransitionError("Operation active counter is invalid.")
        if transition == "started":
            operation_metrics["active"] = active + 1
        elif transition in {"completed", "failed"}:
            if active < 1:
                raise InvalidTransitionError("Operation active counter underflow.")
            operation_metrics["active"] = active - 1
            total_duration = operation_metrics.get("duration_seconds", 0.0)
            if type(total_duration) not in {int, float}:
                raise InvalidTransitionError("Operation duration metric is invalid.")
            operation_metrics["duration_seconds"] = (
                float(total_duration) + duration_seconds
            )
        return operation_metrics

    def _require_running_locked(self) -> None:
        self._require_running_status_locked()
        if self._prepared_terminal is not None:
            raise InvalidTransitionError(
                "No nonterminal transition may follow terminal preparation."
            )

    def _require_running_status_locked(self) -> None:
        if self._status is not RunStatus.RUNNING:
            raise InvalidTransitionError(
                f"Run must be running, not {self._status.value!r}."
            )

    def _task_locked(self, task_id: int) -> TodoItem:
        for task in self.state.todo_items:
            if task.id == task_id:
                return task
        raise KeyError(f"Unknown task ID: {task_id}")

    def _task_projection_locked(self, task: TodoItem) -> dict[str, Any]:
        """Capture immutable legacy-facing task fields at transition time."""
        step = next(
            (
                index
                for index, candidate in enumerate(self.state.todo_items, start=1)
                if candidate.id == task.id
            ),
            None,
        )
        return {
            "title": task.title,
            "intent": task.intent,
            "status": task.status,
            "summary": task.summary,
            "sources_summary": task.sources_summary,
            "note_id": task.note_id,
            "note_path": task.note_path,
            "source_strategy": task.source_strategy,
            "repository": task.repository,
            "stream_token": task.stream_token,
            "step": step,
        }

    def _build_event_locked(
        self,
        kind: EventKind,
        payload: dict[str, Any],
        *,
        task_id: int | None = None,
        operation_id: str | None = None,
    ) -> ResearchEvent:
        return ResearchEvent(
            kind=kind,
            run_id=self.run_id,
            sequence=self._next_sequence,
            occurred_at=utc_now(),
            payload=payload,
            task_id=task_id,
            operation_id=operation_id,
        )

    def _validated_event_locked(
        self,
        kind: EventKind,
        payload: dict[str, Any],
        *,
        task_id: int | None = None,
        operation_id: str | None = None,
    ) -> ResearchEvent:
        event = self._build_event_locked(
            kind,
            payload,
            task_id=task_id,
            operation_id=operation_id,
        )
        event.as_dict()
        return event

    def _commit_event_locked(
        self,
        event: ResearchEvent,
    ) -> bool:
        self.events.append(event)
        self._next_sequence += 1
        self._pending_notifications.append((tuple(self._observers), event))
        if self._notification_draining:
            return False
        self._notification_draining = True
        return True

    def _drain_notifications(self) -> None:
        while True:
            with self._lock:
                if not self._pending_notifications:
                    self._notification_draining = False
                    return
                observers, event = self._pending_notifications.popleft()
            self._notify(observers, event)

    @staticmethod
    def _notify(observers: Sequence[Observer], event: ResearchEvent) -> None:
        for observer in observers:
            try:
                observer(event)
            except Exception:
                _LOGGER.error(
                    "Research event observer failure isolated: run_id=%s sequence=%s",
                    event.run_id,
                    event.sequence,
                )
