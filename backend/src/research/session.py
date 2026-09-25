"""Thread-safe authoritative state transitions for a research run."""

from __future__ import annotations

import json
import logging
import math
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from threading import Event, RLock
from time import monotonic
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from config import Configuration
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
from .profiles import ResearchMode

if TYPE_CHECKING:
    from .operations import OperationSpec


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


class InvalidTransitionError(RuntimeError):
    """Raised when a lifecycle or task transition is not allowed."""


class CheckpointPersistenceError(RuntimeError):
    """Raised when a validated recovery checkpoint cannot be durably saved."""


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
_MAX_STRUCTURED_SUMMARY_BYTES = 256 * 1024
_MAX_QUALITY_ASSESSMENT_BYTES = 512 * 1024

_TERMINAL_EVENTS = {
    RunStatus.COMPLETED: EventKind.RUN_COMPLETED,
    RunStatus.REPORT_INCOMPLETE: EventKind.RUN_FAILED,
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
    checkpoint: str | None = None
    checkpoint_state: dict[str, Any] = field(default_factory=dict)
    checkpoint_writer: Callable[[RunSnapshot], None] | None = field(
        default=None,
        repr=False,
    )
    # Each in-process execution gets a fresh identity.  Recovery keeps the
    # run ID but creates a new execution identity so repeated restarts are
    # distinguishable in the persisted event/audit trail.
    execution_attempt_id: str = field(default_factory=lambda: uuid4().hex)
    last_resumable_parent: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    error: RunError | None = None
    followup_context: dict[str, Any] = field(default_factory=dict)
    related_history_context: dict[str, Any] = field(default_factory=dict)
    user_memory_context: dict[str, Any] = field(default_factory=dict)
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
    _recovery_attempt_id: str | None = field(
        default=None,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        """Fill the canonical topic from the immutable command when absent."""
        if self.state.research_topic is None:
            self.state.research_topic = self.command.topic
        if self.state.research_mode is None and self.command.research_mode is not None:
            self.state.research_mode = self.command.research_mode.value
        if (
            self.state.research_profile_id is None
            and self.command.research_profile_id is not None
        ):
            self.state.research_profile_id = self.command.research_profile_id
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

    @property
    def checkpoint_phase(self) -> str | None:
        """Return the phase represented by the last durable checkpoint."""
        with self._lock:
            phase = self.checkpoint_state.get("phase")
            return phase if isinstance(phase, str) else None

    @property
    def recovery_resumable(self) -> bool:
        """Return whether the last checkpoint is safe for failed-run recovery."""
        with self._lock:
            return bool(
                self.checkpoint_state.get("validated") is True
                and self.checkpoint_state.get("resumable") is True
            )

    def persist_checkpoint(
        self,
        phase: str,
        *,
        resumable: bool = True,
        evidence_recovery: Mapping[str, Any] | None = None,
        evidence_recovery_unavailable: bool = False,
    ) -> RunSnapshot:
        """Build and atomically persist one validated recovery checkpoint."""
        if not isinstance(phase, str) or not phase.strip():
            raise ValueError("Checkpoint phase must be non-empty text.")
        with self._lock:
            self._require_running_locked()
            checkpoint_state = self._build_checkpoint_state_locked(
                phase=phase.strip(),
                resumable=resumable,
                evidence_recovery=evidence_recovery,
                evidence_recovery_unavailable=evidence_recovery_unavailable,
            )
            if evidence_recovery is not None:
                from .evidence_recovery import validate_evidence_recovery
                validate_evidence_recovery(
                    evidence_recovery, run_id=self.run_id,
                    task_state=checkpoint_state["task_state"],
                )
            # A checkpoint that observes an uncertain side effect must never
            # replace the last trusted recovery target.  Keep the prior safe
            # checkpoint durable until a later boundary proves replay-safe.
            if (
                checkpoint_state.get("resumable") is False
                and checkpoint_state.get("recovery_blocked_reason")
                == "report_stream_incomplete"
                and self.checkpoint_state.get("validated") is True
                and self.checkpoint_state.get("resumable") is True
            ):
                prior_phase = self.checkpoint_state.get("phase")
                return self.to_snapshot(
                    status=RunStatus.RUNNING,
                    checkpoint=(
                        prior_phase
                        if isinstance(prior_phase, str)
                        else self.checkpoint
                    ),
                    checkpoint_state=self.checkpoint_state,
                    recovery_resumable=True,
                )
            snapshot = self.to_snapshot(
                status=RunStatus.RUNNING,
                checkpoint=phase.strip(),
                checkpoint_state=checkpoint_state,
                recovery_resumable=bool(checkpoint_state.get("resumable")),
            )
            writer = self.checkpoint_writer
            if writer is not None:
                try:
                    writer(snapshot)
                except Exception as exc:
                    raise CheckpointPersistenceError(
                        "Validated research checkpoint could not be persisted."
                    ) from exc
            self.checkpoint = phase.strip()
            self.checkpoint_state = checkpoint_state
            return snapshot

    def start_recovery(self) -> ResearchEvent:
        """Emit a recovery boundary after a durable checkpoint is restored."""
        with self._lock:
            self._require_running_locked()
            recovery_attempt_id = uuid4().hex
            self._recovery_attempt_id = recovery_attempt_id
            raw_attempts = self.metrics.get("execution_attempts")
            attempts = [
                dict(item)
                for item in raw_attempts
                if isinstance(item, Mapping)
            ] if isinstance(raw_attempts, list) else []
            attempts.append(
                {
                    "attempt_id": recovery_attempt_id,
                    "execution_attempt_id": self.execution_attempt_id,
                    "kind": "recovery",
                    "checkpoint_id": self.checkpoint_state.get("checkpoint_id"),
                    "phase": self.checkpoint_state.get("phase"),
                    "started_at": utc_now().isoformat(),
                }
            )
            self.metrics["execution_attempts"] = attempts[-32:]
            payload = {
                "checkpoint_id": self.checkpoint_state.get("checkpoint_id"),
                "phase": self.checkpoint_state.get("phase"),
                "attempt_id": recovery_attempt_id,
                "execution_attempt_id": self.execution_attempt_id,
            }
            event = self._validated_event_locked(
                EventKind.RUN_RECOVERY_STARTED,
                payload,
            )
            should_drain = self._commit_event_locked(event)
        if should_drain:
            self._drain_notifications()
        return event

    def _build_checkpoint_state_locked(
        self,
        *,
        phase: str,
        resumable: bool,
        evidence_recovery: Mapping[str, Any] | None = None,
        evidence_recovery_unavailable: bool = False,
    ) -> dict[str, Any]:
        """Capture only the detached state needed to replay the next phase."""
        from .operations import operation_replay_safety

        previous = self.checkpoint_state.get("checkpoint_id")
        sequence = max(0, self._next_sequence - 1)
        checkpoint_id = f"{self.run_id}:{sequence}:{phase}"
        operation_state: list[dict[str, object]] = []
        for key, state in self._operation_states.items():
            task_id, envelope = self._operation_envelopes.get(key, (None, {}))
            operation_name = envelope.get("operation_name")
            operation_state.append(
                {
                    "pairing_key": list(key),
                    "task_id": task_id,
                    "operation_name": operation_name,
                    "status": state,
                    "replay_safety": operation_replay_safety(operation_name),
                }
            )
        report_state: dict[str, object] = {
            "status": (
                "generated"
                if isinstance(self.state.structured_report, str)
                and self.state.structured_report.strip()
                else "not_started"
            ),
            "output_chars": len(self.state.structured_report or ""),
            "stream_completed": None,
        }
        report_validation = self.metrics.get("report_validation")
        if isinstance(report_validation, dict):
            final = report_validation.get("final")
            if isinstance(final, dict):
                report_state["validation"] = dict(final)
        llm = self.metrics.get("llm")
        if isinstance(llm, dict):
            calls = llm.get("calls")
            if isinstance(calls, list):
                for call in reversed(calls):
                    if not isinstance(call, dict) or call.get("role") != "reporter":
                        continue
                    if call.get("mode") == "stream":
                        report_state["stream_completed"] = call.get(
                            "stream_completed"
                        )
                    break
        active_operations = [
            (key, state)
            for key, state in self._operation_states.items()
            if state == "active"
        ]
        active_operations_safe = True
        for key, _state in active_operations:
            _task_id, envelope = self._operation_envelopes.get(key, (None, {}))
            if operation_replay_safety(envelope.get("operation_name")) != "safe_replay":
                active_operations_safe = False
                break
        if active_operations_safe:
            for key, state in self._operation_states.items():
                if state != "uncertain":
                    continue
                _task_id, envelope = self._operation_envelopes.get(key, (None, {}))
                if operation_replay_safety(envelope.get("operation_name")) != "safe_replay":
                    active_operations_safe = False
                    break
        report_stream_safe = report_state.get("stream_completed") is not False
        recovery_blocked_reason: str | None = None
        if not active_operations_safe:
            recovery_blocked_reason = "operation_outcome_uncertain"
        elif not report_stream_safe:
            recovery_blocked_reason = "report_stream_incomplete"
        elif evidence_recovery_unavailable:
            recovery_blocked_reason = "evidence_recovery_unavailable"
        effective_resumable = bool(
            resumable
            and active_operations_safe
            and report_stream_safe
            and not evidence_recovery_unavailable
        )
        task_quality = self.metrics.get("task_quality")
        if not isinstance(task_quality, Mapping):
            task_quality = {}
        saved_evidence_recovery = evidence_recovery
        if saved_evidence_recovery is None:
            prior_evidence_recovery = self.checkpoint_state.get("evidence_recovery")
            if isinstance(prior_evidence_recovery, Mapping):
                saved_evidence_recovery = prior_evidence_recovery
        checkpoint_state = {
            "schema_version": 1,
            "checkpoint_id": checkpoint_id,
            "run_id": self.run_id,
            "parent_checkpoint_id": previous if isinstance(previous, str) else None,
            "phase": phase,
            "created_at": utc_now().isoformat(),
            "task_state": [task.to_dict() for task in self.state.todo_items],
            "operation_state": operation_state,
            "report_state": report_state,
            "state": {
                "research_topic": self.state.research_topic,
                "research_loop_count": self.state.research_loop_count,
                "research_mode": (
                    self.state.research_mode
                    or (
                        self.command.research_mode.value
                        if self.command.research_mode is not None
                        else None
                    )
                ),
                "research_profile_id": (
                    self.state.research_profile_id
                    or self.command.research_profile_id
                ),
                "source_context": dict(self.state.source_context),
                "research_intelligence": dict(self.state.research_intelligence),
                "structured_summary": dict(self.state.structured_summary),
                "quality_assessment": dict(self.state.quality_assessment),
                "github_context": dict(self.state.github_context),
                "github_intelligence": dict(self.state.github_intelligence),
                "report_note_id": self.state.report_note_id,
                "report_note_path": self.state.report_note_path,
                "permission_mode": self.command.permission_mode,
                "caller_mode": self.command.caller_mode,
                "use_history_memory": self.command.use_history_memory,
                "memory_scope": self.command.memory_scope,
                "related_history_context": dict(self.related_history_context),
                "user_memory_context": dict(self.user_memory_context),
                "task_quality": dict(task_quality),
            },
            "validated": True,
            "resumable": effective_resumable,
        }
        if saved_evidence_recovery is not None:
            checkpoint_state["evidence_recovery"] = dict(saved_evidence_recovery)
        if recovery_blocked_reason is not None:
            checkpoint_state["recovery_blocked_reason"] = recovery_blocked_reason
        return checkpoint_state

    @classmethod
    def restore_from_snapshot(
        cls,
        snapshot: RunSnapshot,
        *,
        checkpoint_writer: Callable[[RunSnapshot], None] | None = None,
        cancellation_token: CancellationToken | None = None,
    ) -> RunSession:
        """Rebuild a running session exclusively from one validated snapshot."""
        detached_snapshot = snapshot.as_dict()
        config_values = {
            key: value
            for key, value in detached_snapshot["config_snapshot"].items()
            if isinstance(key, str)
        }
        raw_checkpoint_state = detached_snapshot.get("checkpoint_state")
        checkpoint_state = (
            dict(raw_checkpoint_state)
            if isinstance(raw_checkpoint_state, Mapping)
            else {}
        )
        continuation = checkpoint_state.get("state")
        continuation_state: Mapping[str, object] = (
            {
                str(key): value
                for key, value in continuation.items()
                if isinstance(key, str)
            }
            if isinstance(continuation, Mapping)
            else {}
        )
        raw_permission_mode = continuation_state.get("permission_mode")
        permission_mode = (
            raw_permission_mode
            if isinstance(raw_permission_mode, str)
            and raw_permission_mode in {"default", "strict"}
            else "default"
        )
        raw_caller_mode = continuation_state.get("caller_mode")
        caller_mode = (
            raw_caller_mode
            if isinstance(raw_caller_mode, str)
            and raw_caller_mode in {"public", "internal"}
            else "public"
        )
        use_history_memory = continuation_state.get("use_history_memory")
        if not isinstance(use_history_memory, bool):
            use_history_memory = True
        memory_scope = continuation_state.get("memory_scope")
        if not isinstance(memory_scope, str):
            memory_scope = "default"
        raw_research_mode = continuation_state.get("research_mode")
        research_mode: ResearchMode | None = None
        if isinstance(raw_research_mode, str):
            research_mode = ResearchMode(raw_research_mode)
        research_profile_id = continuation_state.get("research_profile_id")
        if not isinstance(research_profile_id, str):
            research_profile_id = None
        command = ResearchCommand(
            topic=snapshot.topic,
            config=Configuration.from_env(overrides=config_values),
            run_id=snapshot.run_id,
            parent_run_id=snapshot.parent_run_id,
            permission_mode=permission_mode,
            caller_mode=caller_mode,
            research_mode=research_mode,
            research_profile_id=research_profile_id,
            use_history_memory=use_history_memory,
            memory_scope=memory_scope,
        )
        raw_output = detached_snapshot["output"]
        output: Mapping[str, object] = (
            raw_output if isinstance(raw_output, Mapping) else {}
        )
        raw_loop_count = continuation_state.get("research_loop_count")
        loop_count = (
            raw_loop_count
            if isinstance(raw_loop_count, int) and not isinstance(raw_loop_count, bool)
            else 0
        )
        raw_github_context = continuation_state.get("github_context")
        typed_github_context = (
            cast(Mapping[str, object], raw_github_context)
            if isinstance(raw_github_context, Mapping)
            else None
        )
        github_context = (
            {
                str(key): value
                for key, value in typed_github_context.items()
                if isinstance(key, str)
            }
            if typed_github_context is not None
            else {}
        )
        raw_github_intelligence = continuation_state.get("github_intelligence")
        if not isinstance(raw_github_intelligence, Mapping):
            raw_github_intelligence = output.get("github_intelligence")
        github_intelligence = (
            {
                str(key): value
                for key, value in cast(Mapping[str, object], raw_github_intelligence).items()
                if isinstance(key, str)
            }
            if isinstance(raw_github_intelligence, Mapping)
            else {}
        )
        raw_source_context = continuation_state.get("source_context")
        source_context = (
            {
                str(key): value
                for key, value in cast(Mapping[str, object], raw_source_context).items()
                if isinstance(key, str)
            }
            if isinstance(raw_source_context, Mapping)
            else {}
        )
        raw_research_intelligence = continuation_state.get("research_intelligence")
        if not isinstance(raw_research_intelligence, Mapping):
            raw_research_intelligence = output.get("research_intelligence")
        research_intelligence = (
            {
                str(key): value
                for key, value in cast(
                    Mapping[str, object], raw_research_intelligence
                ).items()
                if isinstance(key, str)
            }
            if isinstance(raw_research_intelligence, Mapping)
            else {}
        )
        raw_structured_summary = continuation_state.get("structured_summary")
        if not isinstance(raw_structured_summary, Mapping):
            raw_structured_summary = output.get("structured_summary")
        structured_summary = (
            {
                str(key): value
                for key, value in cast(
                    Mapping[str, object], raw_structured_summary
                ).items()
                if isinstance(key, str)
            }
            if isinstance(raw_structured_summary, Mapping)
            else {}
        )
        raw_quality_assessment = continuation_state.get("quality_assessment")
        if not isinstance(raw_quality_assessment, Mapping):
            raw_quality_assessment = output.get("quality_assessment")
        quality_assessment = (
            {
                str(key): value
                for key, value in cast(
                    Mapping[str, object], raw_quality_assessment
                ).items()
                if isinstance(key, str)
            }
            if isinstance(raw_quality_assessment, Mapping)
            else {}
        )
        # Older checkpoints only carry the GitHub v1 projection.  Adapt it in
        # memory so recovery can use one canonical intelligence field without
        # rewriting the persisted checkpoint or changing the legacy output.
        if not research_intelligence and github_intelligence:
            try:
                from .compatibility import GitHubEvidenceV1Adapter

                research_intelligence = GitHubEvidenceV1Adapter.to_v2(
                    github_intelligence
                ).as_dict()
            except (TypeError, ValueError):
                research_intelligence = {}
        if research_mode is None:
            output_mode = output.get("research_mode")
            if isinstance(output_mode, str):
                try:
                    research_mode = ResearchMode(output_mode)
                except ValueError:
                    research_mode = None
        if research_profile_id is None:
            output_profile = output.get("research_profile_id")
            if isinstance(output_profile, str):
                research_profile_id = output_profile
        raw_research_topic = continuation_state.get("research_topic")
        research_topic = (
            raw_research_topic
            if isinstance(raw_research_topic, str)
            else snapshot.topic
        )
        raw_running_summary = output.get("running_summary")
        running_summary = (
            raw_running_summary if isinstance(raw_running_summary, str) else None
        )
        raw_report = output.get("report_markdown")
        structured_report = raw_report if isinstance(raw_report, str) else None
        raw_report_note_id = continuation_state.get("report_note_id")
        report_note_id = (
            raw_report_note_id if isinstance(raw_report_note_id, str) else None
        )
        raw_report_note_path = continuation_state.get("report_note_path")
        report_note_path = (
            raw_report_note_path if isinstance(raw_report_note_path, str) else None
        )
        raw_history_context = continuation_state.get("related_history_context")
        related_history_context = (
            {
                str(key): value
                for key, value in cast(Mapping[str, object], raw_history_context).items()
                if isinstance(key, str)
            }
            if isinstance(raw_history_context, Mapping)
            else {}
        )
        raw_memory_context = continuation_state.get("user_memory_context")
        user_memory_context = (
            {
                str(key): value
                for key, value in cast(Mapping[str, object], raw_memory_context).items()
                if isinstance(key, str)
            }
            if isinstance(raw_memory_context, Mapping)
            else {}
        )
        raw_tasks_value = checkpoint_state.get("task_state")
        if not isinstance(raw_tasks_value, (list, tuple)):
            raw_tasks_value = output.get("todo_items", [])
        raw_tasks = (
            raw_tasks_value
            if isinstance(raw_tasks_value, (list, tuple))
            else []
        )
        tasks: list[TodoItem] = []
        for item in raw_tasks:
            if not isinstance(item, Mapping):
                continue
            task = TodoItem(**dict(item))
            if task.status == "in_progress":
                # The previous stream was not durably terminal. Its partial
                # summary is deliberately discarded before safe replay.
                task.status = "pending"
                task.summary = None
                task.sources_summary = None
            tasks.append(task)
        state = ResearchState(
            research_topic=research_topic,
            research_loop_count=loop_count,
            running_summary=running_summary,
            structured_report=structured_report,
            todo_items=tasks,
            research_mode=(
                research_mode.value if research_mode is not None else None
            ),
            research_profile_id=research_profile_id,
            source_context=source_context,
            research_intelligence=research_intelligence,
            structured_summary=structured_summary,
            quality_assessment=quality_assessment,
            github_context=github_context,
            github_intelligence=github_intelligence,
            report_note_id=report_note_id,
            report_note_path=report_note_path,
        )
        restored_metrics = json.loads(json.dumps(detached_snapshot["metrics"]))
        checkpoint_task_quality = continuation_state.get("task_quality")
        if isinstance(checkpoint_task_quality, Mapping):
            restored_metrics["task_quality"] = {
                str(key): value
                for key, value in checkpoint_task_quality.items()
                if isinstance(key, str)
            }
        else:
            # Terminal metrics can contain quality judgments made after the
            # durable checkpoint.  Do not let those later values leak into a
            # recovered continuation.
            restored_metrics.pop("task_quality", None)
        session = cls(
            command=command,
            state=state,
            started_at=snapshot.started_at,
            checkpoint=snapshot.checkpoint,
            checkpoint_state=checkpoint_state,
            last_resumable_parent=snapshot.last_resumable_parent,
            metrics=restored_metrics,
            followup_context=dict(detached_snapshot["followup_context"]),
            related_history_context=related_history_context,
            user_memory_context=user_memory_context,
            policy_decisions=[
                dict(item) for item in detached_snapshot["policy_decisions"]
            ],
            checkpoint_writer=checkpoint_writer,
            cancellation_token=cancellation_token or CancellationToken(),
        )
        session.events = list(snapshot.events)
        session._next_sequence = max(
            (event.sequence for event in session.events),
            default=0,
        ) + 1
        session._status = RunStatus.RUNNING
        session._rebuild_operation_audit_from_events()
        return session

    def _rebuild_operation_audit_from_events(self) -> None:
        """Reconstruct operation pairing state from the persisted event ledger."""
        with self._lock:
            self._operation_states.clear()
            self._operation_envelopes.clear()
            for event in self.events:
                if event.operation_id is None:
                    continue
                payload = dict(event.payload)
                task_attempt = payload.get("task_attempt")
                operation_attempt = payload.get("operation_attempt")
                fallback_index = payload.get("fallback_index")
                if (
                    not isinstance(task_attempt, int)
                    or isinstance(task_attempt, bool)
                    or not isinstance(operation_attempt, int)
                    or isinstance(operation_attempt, bool)
                    or not isinstance(fallback_index, int)
                    or isinstance(fallback_index, bool)
                ):
                    continue
                key = (
                    event.operation_id,
                task_attempt,
                fallback_index,
                operation_attempt,
                )
                envelope = {
                    key: value
                    for key, value in payload.items()
                    if key not in {"duration_seconds", "code"}
                }
                if event.kind is EventKind.OPERATION_STARTED:
                    self._operation_states[key] = "active"
                    self._operation_envelopes[key] = (event.task_id, envelope)
                elif event.kind is EventKind.OPERATION_COMPLETED:
                    self._operation_states[key] = "completed"
                elif event.kind is EventKind.OPERATION_FAILED:
                    self._operation_states[key] = "failed"
                elif event.kind is EventKind.OPERATION_REJECTED:
                    self._operation_states[key] = "rejected"
            checkpoint_operations = self.checkpoint_state.get("operation_state")
            if isinstance(checkpoint_operations, (list, tuple)):
                for operation in checkpoint_operations:
                    if not isinstance(operation, Mapping):
                        continue
                    pairing_key = operation.get("pairing_key")
                    if not isinstance(pairing_key, (list, tuple)) or len(pairing_key) != 4:
                        continue
                    key = tuple(pairing_key)
                    if operation.get("status") == "active":
                        self._operation_states[key] = "uncertain"
                    if key not in self._operation_envelopes:
                        self._operation_envelopes[key] = (
                            operation.get("task_id")
                            if isinstance(operation.get("task_id"), int)
                            else None,
                            {
                                "operation_name": operation.get("operation_name"),
                                "task_attempt": key[1],
                                "fallback_index": key[2],
                                "operation_attempt": key[3],
                            },
                        )
            raw_metrics = self.metrics.get("operations")
            operation_metrics = (
                dict(raw_metrics) if isinstance(raw_metrics, Mapping) else {}
            )
            operation_metrics["active"] = sum(
                state == "active" for state in self._operation_states.values()
            )
            operation_metrics["uncertain"] = sum(
                state == "uncertain" for state in self._operation_states.values()
            )
            self.metrics["operations"] = operation_metrics

    def record_llm_telemetry(self, record: Mapping[str, object]) -> None:
        """Persist bounded, provider-safe metadata for one LLM call."""
        allowed = {
            "role",
            "provider",
            "model",
            "mode",
            "started_at",
            "duration_ms",
            "request_id",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "usage_available",
            "output_chars",
            "finish_reason",
            "exception_type",
            "stream_completed",
            "retry_count",
            "chunk_count",
            "first_chunk_latency_ms",
        }
        safe: dict[str, object] = {}
        for key in allowed:
            value = record.get(key)
            if isinstance(value, str):
                safe[key] = value[:128]
            elif value is None or isinstance(value, (bool, int, float)):
                safe[key] = value
        with self._lock:
            raw = self.metrics.get("llm")
            telemetry = dict(raw) if isinstance(raw, dict) else {}
            calls = list(telemetry.get("calls", []))
            calls = [item for item in calls if isinstance(item, dict)]
            if len(calls) >= 64:
                calls = calls[-63:]
                telemetry["dropped_calls"] = int(telemetry.get("dropped_calls", 0)) + 1
            calls.append(safe)
            telemetry["calls"] = calls
            telemetry["summary"] = self._summarize_llm_calls(calls)
            self.metrics["llm"] = telemetry

    def latest_llm_finish_reason(self, *, role: str | None = None) -> str | None:
        """Return the most recent provider finish reason for an optional role."""
        with self._lock:
            raw = self.metrics.get("llm")
            calls = raw.get("calls") if isinstance(raw, dict) else None
            if not isinstance(calls, list):
                return None
            for call in reversed(calls):
                if not isinstance(call, dict):
                    continue
                if role is not None and call.get("role") != role:
                    continue
                reason = call.get("finish_reason")
                if isinstance(reason, str) and reason:
                    return reason
        return None

    @staticmethod
    def _summarize_llm_calls(calls: Sequence[Mapping[str, object]]) -> dict[str, object]:
        """Aggregate bounded LLM call records without retaining prompt content."""
        finish_reasons: dict[str, int] = {}
        input_tokens = 0
        output_tokens = 0
        total_tokens = 0
        known_input = known_output = known_total = False
        output_chars = 0
        stream_calls = stream_completed = 0
        duration_ms = 0.0
        for call in calls:
            reason = call.get("finish_reason")
            if isinstance(reason, str) and reason:
                finish_reasons[reason] = finish_reasons.get(reason, 0) + 1
            for key, holder in (
                ("input_tokens", "input"),
                ("output_tokens", "output"),
                ("total_tokens", "total"),
            ):
                value = call.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    if holder == "input":
                        input_tokens += value
                        known_input = True
                    elif holder == "output":
                        output_tokens += value
                        known_output = True
                    else:
                        total_tokens += value
                        known_total = True
            value = call.get("output_chars")
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                output_chars += value
            value = call.get("duration_ms")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                duration_ms += max(0.0, float(value))
            if call.get("mode") == "stream":
                stream_calls += 1
                if call.get("stream_completed") is True:
                    stream_completed += 1
        return {
            "call_count": len(calls),
            "stream_call_count": stream_calls,
            "stream_completed_count": stream_completed,
            "stream_incomplete_count": max(0, stream_calls - stream_completed),
            "input_tokens": input_tokens if known_input else None,
            "output_tokens": output_tokens if known_output else None,
            "total_tokens": total_tokens if known_total else None,
            "output_chars": output_chars,
            "duration_ms": round(duration_ms, 3),
            "finish_reasons": finish_reasons,
        }

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
                {
                    "topic": self.command.topic,
                    "execution_attempt_id": self.execution_attempt_id,
                },
            )
            self._status = RunStatus.RUNNING
            should_drain = self._commit_event_locked(event)
        if should_drain:
            self._drain_notifications()
        return event

    def record_history_recall(
        self,
        *,
        matches: Sequence[Mapping[str, object]],
    ) -> ResearchEvent:
        """Record safe metadata for automatically recalled related research."""
        safe_matches: list[dict[str, object]] = []
        for match in matches:
            run_id = match.get("run_id")
            topic = match.get("topic")
            score = match.get("score")
            if not isinstance(run_id, str) or not isinstance(topic, str):
                continue
            if (
                isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(float(score))
            ):
                continue
            safe_matches.append(
                {
                    "run_id": run_id,
                    "topic": topic[:180],
                    "score": round(float(score), 4),
                }
            )
        payload = {
            "match_count": len(safe_matches),
            "matches": safe_matches,
        }
        with self._lock:
            self._require_running_locked()
            event = self._validated_event_locked(
                EventKind.HISTORY_RECALLED,
                payload,
            )
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
            self.state.research_mode = ResearchMode.GITHUB.value
            self.state.research_profile_id = (
                self.command.research_profile_id or "github.repository.v1"
            )
            self.state.source_context = dict(github_context)
            self.state.github_context = dict(github_context)
            should_drain = self._commit_event_locked(event)
        if should_drain:
            self._drain_notifications()
        return event

    def record_github_intelligence(
        self,
        bundle: Mapping[str, Any],
        *,
        artifact_count: int = 0,
    ) -> tuple[ResearchEvent, ResearchEvent]:
        """Persist bounded GitHub evidence metadata and coverage events."""
        snapshots = bundle.get("snapshots")
        evidence = bundle.get("evidence")
        claims = bundle.get("claims")
        coverage = bundle.get("coverage")
        snapshot_count = len(snapshots) if isinstance(snapshots, (list, tuple)) else 0
        evidence_count = len(evidence) if isinstance(evidence, (list, tuple)) else 0
        claim_count = len(claims) if isinstance(claims, (list, tuple)) else 0
        coverage_payload = dict(coverage) if isinstance(coverage, Mapping) else {}
        evidence_payload = {
            "snapshot_count": snapshot_count,
            "evidence_count": evidence_count,
            "claim_count": claim_count,
            "artifact_count": max(0, artifact_count),
            "bundle_schema_version": bundle.get("schema_version", 1),
        }
        with self._lock:
            self._require_running_locked()
            evidence_event = self._validated_event_locked(
                EventKind.EVIDENCE_COLLECTED,
                evidence_payload,
            )
            should_drain = self._commit_event_locked(evidence_event)
            coverage_event = self._validated_event_locked(
                EventKind.COVERAGE_UPDATED,
                {
                    "coverage_score": coverage_payload.get("coverage_score", 0.0),
                    "covered_dimensions": list(coverage_payload.get("covered_dimensions", [])),
                    "missing_dimensions": list(coverage_payload.get("missing_dimensions", [])),
                    "gap_queries": list(coverage_payload.get("gap_queries", [])),
                    "allow_report": bool(coverage_payload.get("allow_report", False)),
                },
            )
            self.state.github_intelligence = dict(bundle)
            self.state.research_mode = ResearchMode.GITHUB.value
            self.state.research_profile_id = (
                self.command.research_profile_id or "github.repository.v1"
            )
            try:
                from .compatibility import GitHubEvidenceV1Adapter

                self.state.research_intelligence = (
                    GitHubEvidenceV1Adapter.to_v2(bundle).as_dict()
                )
            except (TypeError, ValueError):
                # Keep the legacy projection available when a partial bundle
                # cannot be adapted during this compatibility transition.
                self.state.research_intelligence = {}
            should_drain = self._commit_event_locked(coverage_event) or should_drain
        if should_drain:
            self._drain_notifications()
        return evidence_event, coverage_event

    def record_research_intelligence(
        self,
        bundle: Mapping[str, Any],
        *,
        provider_ids: Sequence[str] = (),
    ) -> tuple[ResearchEvent, ResearchEvent]:
        """Persist a v2 intelligence bundle and emit safe generic events."""
        if not isinstance(bundle, Mapping):
            raise TypeError("Research intelligence must be a mapping.")
        try:
            detached_bundle = json.loads(json.dumps(dict(bundle)))
        except (TypeError, ValueError) as exc:
            raise TypeError("Research intelligence must be JSON serializable.") from exc
        sources = detached_bundle.get("sources")
        evidence = detached_bundle.get("evidence")
        claims = detached_bundle.get("claims")
        manifest = detached_bundle.get("artifact_manifest")
        coverage = detached_bundle.get("coverage")
        mode = detached_bundle.get("mode")
        profile_id = detached_bundle.get("profile_id")
        source_count = len(sources) if isinstance(sources, list) else 0
        evidence_count = len(evidence) if isinstance(evidence, list) else 0
        claim_count = len(claims) if isinstance(claims, list) else 0
        artifact_count = (
            len(manifest.get("artifacts", []))
            if isinstance(manifest, Mapping)
            and isinstance(manifest.get("artifacts"), list)
            else 0
        )
        inferred_providers = (
            [
                item.get("provider_id")
                for item in sources
                if isinstance(item, Mapping)
                and isinstance(item.get("provider_id"), str)
            ]
            if isinstance(sources, list)
            else []
        )
        safe_provider_ids = list(dict.fromkeys([*provider_ids, *inferred_providers]))
        evidence_payload = {
            "research_mode": mode,
            "profile_id": profile_id,
            "provider_ids": safe_provider_ids,
            "source_count": source_count,
            "evidence_count": evidence_count,
            "claim_count": claim_count,
            "artifact_count": artifact_count,
            "bundle_schema_version": detached_bundle.get("schema_version", 2),
        }
        coverage_payload = dict(coverage) if isinstance(coverage, Mapping) else {}
        coverage_event_payload = {
            "research_mode": mode,
            "profile_id": profile_id,
            "provider_ids": safe_provider_ids,
            "source_count": source_count,
            "coverage_score": coverage_payload.get("coverage_score", 0.0),
            "covered_dimensions": list(coverage_payload.get("covered_dimensions", [])),
            "missing_dimensions": list(coverage_payload.get("missing_dimensions", [])),
            "gap_queries": list(coverage_payload.get("gap_queries", [])),
            "allow_report": bool(coverage_payload.get("allow_report", False)),
        }
        with self._lock:
            self._require_running_locked()
            evidence_event = self._validated_event_locked(
                EventKind.EVIDENCE_COLLECTED,
                evidence_payload,
            )
            should_drain = self._commit_event_locked(evidence_event)
            coverage_event = self._validated_event_locked(
                EventKind.COVERAGE_UPDATED,
                coverage_event_payload,
            )
            self.state.research_intelligence = detached_bundle
            if isinstance(mode, str):
                self.state.research_mode = mode
            if isinstance(profile_id, str):
                self.state.research_profile_id = profile_id
            should_drain = self._commit_event_locked(coverage_event) or should_drain
        if should_drain:
            self._drain_notifications()
        return evidence_event, coverage_event

    def record_artifact(self, artifact: Mapping[str, Any]) -> ResearchEvent:
        """Persist one artifact manifest event without exposing its content in SSE."""
        payload = {
            "artifact_id": artifact.get("artifact_id"),
            "artifact_type": artifact.get("artifact_type"),
            "mime_type": artifact.get("mime_type"),
            "path": artifact.get("path"),
            "title": artifact.get("title"),
            "checksum": artifact.get("checksum"),
        }
        with self._lock:
            self._require_running_locked()
            event = self._validated_event_locked(EventKind.ARTIFACT_READY, payload)
            should_drain = self._commit_event_locked(event)
        if should_drain:
            self._drain_notifications()
        return event

    def record_summary_quality(
        self,
        document: Mapping[str, Any],
        assessment: Mapping[str, Any],
    ) -> ResearchEvent:
        """Persist bounded structured quality output and emit metadata only."""
        if not isinstance(document, Mapping) or not isinstance(assessment, Mapping):
            raise TypeError("Structured summary and quality assessment must be mappings.")
        try:
            detached_document = json.loads(json.dumps(dict(document)))
            detached_assessment = json.loads(json.dumps(dict(assessment)))
        except (TypeError, ValueError) as exc:
            raise TypeError("Structured quality output must be JSON serializable.") from exc
        if len(json.dumps(detached_document).encode("utf-8")) > _MAX_STRUCTURED_SUMMARY_BYTES:
            raise ValueError("Structured summary exceeds its persistence bound.")
        if len(json.dumps(detached_assessment).encode("utf-8")) > _MAX_QUALITY_ASSESSMENT_BYTES:
            raise ValueError("Quality assessment exceeds its persistence bound.")
        paragraphs = detached_document.get("paragraphs")
        paragraph_assessments = detached_assessment.get("paragraph_assessments")
        claim_assessments = detached_assessment.get("claim_assessments")
        blockers: set[str] = set()
        for item in (
            paragraph_assessments
            if isinstance(paragraph_assessments, list)
            else []
        ):
            if not isinstance(item, Mapping):
                continue
            item_blockers = item.get("blockers")
            if not isinstance(item_blockers, list):
                continue
            blockers.update(
                str(blocker).partition(":")[0]
                for blocker in item_blockers
                if isinstance(blocker, str)
            )
        score = detached_assessment.get("overall_score")
        payload = {
            "overall_score": (
                float(score)
                if isinstance(score, (int, float)) and not isinstance(score, bool)
                else 0.0
            ),
            "passed": bool(detached_assessment.get("passed", False)),
            "paragraph_count": len(paragraphs) if isinstance(paragraphs, list) else 0,
            "claim_count": (
                len(claim_assessments) if isinstance(claim_assessments, list) else 0
            ),
            "blocked_paragraph_count": sum(
                1
                for item in (
                    paragraph_assessments
                    if isinstance(paragraph_assessments, list)
                    else []
                )
                if isinstance(item, Mapping) and item.get("blockers")
            ),
            "blocker_codes": sorted(blockers),
        }
        with self._lock:
            self._require_running_locked()
            event = self._validated_event_locked(
                EventKind.SUMMARY_QUALITY_UPDATE,
                payload,
            )
            self.state.structured_summary = detached_document
            self.state.quality_assessment = detached_assessment
            should_drain = self._commit_event_locked(event)
        if should_drain:
            self._drain_notifications()
        return event

    def record_source_context(
        self,
        source_context: Mapping[str, Any],
        *,
        provider_ids: Sequence[str] = (),
        source_count: int = 0,
        research_mode: ResearchMode | str | None = None,
        profile_id: str | None = None,
    ) -> ResearchEvent:
        """Persist provider-neutral source context and emit safe metadata."""
        if not isinstance(source_context, Mapping):
            raise TypeError("Source context must be a mapping.")
        try:
            detached_context = json.loads(json.dumps(dict(source_context)))
        except (TypeError, ValueError) as exc:
            raise TypeError("Source context must be JSON serializable.") from exc
        normalized_mode = (
            ResearchMode(research_mode).value
            if research_mode is not None
            else (
                self.command.research_mode.value
                if self.command.research_mode is not None
                else None
            )
        )
        normalized_profile = profile_id or self.command.research_profile_id
        safe_provider_ids = list(
            dict.fromkeys(
                item.strip()
                for item in provider_ids
                if isinstance(item, str) and item.strip()
            )
        )
        if not source_count:
            raw_sources = detached_context.get("sources")
            source_count = len(raw_sources) if isinstance(raw_sources, list) else 0
        payload = {
            "research_mode": normalized_mode,
            "profile_id": normalized_profile,
            "provider_ids": safe_provider_ids,
            "source_count": max(0, int(source_count)),
        }
        with self._lock:
            self._require_running_locked()
            event = self._validated_event_locked(
                EventKind.REPOSITORY_DETECTED,
                payload,
            )
            self.state.research_mode = normalized_mode
            self.state.research_profile_id = normalized_profile
            self.state.source_context = detached_context
            should_drain = self._commit_event_locked(event)
        if should_drain:
            self._drain_notifications()
        return event

    def replace_github_intelligence(self, bundle: Mapping[str, Any]) -> None:
        """Replace rendered GitHub intelligence while preserving the event ledger."""
        with self._lock:
            self._require_running_locked()
            self.state.github_intelligence = dict(bundle)
            try:
                from .compatibility import GitHubEvidenceV1Adapter

                self.state.research_intelligence = (
                    GitHubEvidenceV1Adapter.to_v2(bundle).as_dict()
                )
            except (TypeError, ValueError):
                self.state.research_intelligence = {}

    def replace_legacy_github_intelligence(self, bundle: Mapping[str, Any]) -> None:
        """Replace only the legacy GitHub projection after a v2 update."""
        if not isinstance(bundle, Mapping):
            raise TypeError("Legacy GitHub intelligence must be a mapping.")
        try:
            detached_bundle = json.loads(json.dumps(dict(bundle)))
        except (TypeError, ValueError) as exc:
            raise TypeError(
                "Legacy GitHub intelligence must be JSON serializable."
            ) from exc
        with self._lock:
            self._require_running_locked()
            self.state.github_intelligence = detached_bundle

    def replace_research_intelligence(self, bundle: Mapping[str, Any]) -> None:
        """Replace canonical v2 intelligence without emitting an event."""
        if not isinstance(bundle, Mapping):
            raise TypeError("Research intelligence must be a mapping.")
        try:
            detached_bundle = json.loads(json.dumps(dict(bundle)))
        except (TypeError, ValueError) as exc:
            raise TypeError("Research intelligence must be JSON serializable.") from exc
        with self._lock:
            self._require_running_locked()
            self.state.research_intelligence = detached_bundle
            mode = detached_bundle.get("mode")
            profile_id = detached_bundle.get("profile_id")
            if isinstance(mode, str):
                self.state.research_mode = mode
            if isinstance(profile_id, str):
                self.state.research_profile_id = profile_id

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

    def record_task_quality(
        self,
        task_id: int,
        assessment: Mapping[str, Any],
    ) -> ResearchEvent:
        """Persist bounded task-quality telemetry and emit an ordered event."""
        try:
            detached = json.loads(json.dumps(dict(assessment)))
        except (TypeError, ValueError) as exc:
            raise TypeError("Task quality assessment must be JSON serializable.") from exc
        encoded = json.dumps(detached, ensure_ascii=False).encode("utf-8")
        if len(encoded) > 16 * 1024:
            raise ValueError("Task quality assessment exceeds its event bound.")
        with self._lock:
            self._require_running_locked()
            task = self._task_locked(task_id)
            payload = self._task_projection_locked(task)
            payload.update(detached)
            event = self._validated_event_locked(
                EventKind.TASK_QUALITY_EVALUATED,
                payload,
                task_id=task_id,
            )
            raw_quality = self.metrics.get("task_quality")
            quality = dict(raw_quality) if isinstance(raw_quality, Mapping) else {}
            raw_attempts = quality.get(str(task_id))
            attempts = list(raw_attempts) if isinstance(raw_attempts, list) else []
            attempts.append(detached)
            quality[str(task_id)] = attempts[-8:]
            self.metrics["task_quality"] = quality
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
            terminal_payload = dict(payload)
            terminal_payload.setdefault("checkpoint", kind.value)
            terminal_payload.setdefault(
                "resumable",
                status is RunStatus.COMPLETED,
            )
            terminal_payload.setdefault(
                "recovery_resumable",
                self.recovery_resumable,
            )
            if self.last_resumable_parent is not None:
                terminal_payload.setdefault(
                    "last_resumable_parent",
                    self.last_resumable_parent,
                )
            event = self._build_event_locked(kind, terminal_payload)
            event.as_dict()
            snapshot = self.to_snapshot(status=status, terminal_event=event)
            if status in {
                RunStatus.REPORT_INCOMPLETE,
                RunStatus.FAILED,
                RunStatus.REJECTED,
            }:
                message = payload.get("message")
                code = payload.get("code")
                if isinstance(message, str) and isinstance(code, str):
                    snapshot = replace(
                        snapshot,
                        error=RunError(code=code, message=message),
                        failure_reason=code,
                        resumable=False,
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
                research_mode=self.state.research_mode,
                research_profile_id=self.state.research_profile_id,
                source_context=dict(self.state.source_context),
                research_intelligence=dict(self.state.research_intelligence),
                github_intelligence=dict(self.state.github_intelligence),
                structured_summary=dict(self.state.structured_summary),
                quality_assessment=dict(self.state.quality_assessment),
            )

    def to_snapshot(
        self,
        *,
        status: RunStatus | None = None,
        terminal_event: ResearchEvent | None = None,
        checkpoint: str | None = None,
        checkpoint_state: Mapping[str, Any] | None = None,
        resumable: bool | None = None,
        recovery_resumable: bool | None = None,
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
                    "research_mode": output.research_mode,
                    "research_profile_id": output.research_profile_id,
                    "source_context": dict(output.source_context),
                    "research_intelligence": dict(output.research_intelligence),
                    "github_intelligence": dict(output.github_intelligence),
                    "structured_summary": dict(output.structured_summary),
                    "quality_assessment": dict(output.quality_assessment),
                },
                followup_context=dict(self.followup_context),
                metrics=dict(self.metrics),
                policy_decisions=tuple(dict(item) for item in self.policy_decisions),
                config_snapshot=self.command.config.safe_snapshot(),
                events=tuple(events),
                error=self.error,
                failure_reason=self.error.code if self.error else None,
                checkpoint=(
                    terminal_event.kind.value
                    if terminal_event is not None
                    else checkpoint if checkpoint is not None else self.checkpoint
                ),
                checkpoint_state=(
                    dict(checkpoint_state)
                    if checkpoint_state is not None
                    else (dict(self.checkpoint_state) if self.checkpoint_state else None)
                ),
                resumable=(
                    resumable
                    if resumable is not None
                    else snapshot_status is RunStatus.COMPLETED
                ),
                recovery_resumable=(
                    recovery_resumable
                    if recovery_resumable is not None
                    else self.recovery_resumable
                ),
                last_resumable_parent=(
                    self.last_resumable_parent
                    or (
                        self.command.parent_run_id
                        if snapshot_status is not RunStatus.COMPLETED
                        else None
                    )
                ),
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
        self.checkpoint = event.kind.value
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
