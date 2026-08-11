"""Validation at terminal and checkpoint recovery boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING

from .contracts import EventKind, RunSnapshot

if TYPE_CHECKING:
    from .session import RunSession

TERMINAL_TASK_STATUSES = frozenset(
    {"completed", "failed", "skipped", "cancelled"}
)


class TerminalStateError(RuntimeError):
    """Raised when canonical state cannot be finalized durably."""


class CheckpointValidationError(ValueError):
    """Raised when a persisted checkpoint is not safe to recover."""


def validate_terminal_state(session: RunSession) -> None:
    """Require a canonical report and terminal status for every planned task."""
    report = session.state.structured_report
    if not isinstance(report, str) or not report.strip():
        raise TerminalStateError("Canonical structured report must not be empty.")

    for task in session.state.todo_items:
        if task.status not in TERMINAL_TASK_STATUSES:
            raise TerminalStateError(
                f"Task {task.id} has nonterminal status {task.status!r}."
            )


_CHECKPOINT_SCHEMA_VERSION = 1
_CHECKPOINT_PHASES = frozenset(
    {
        "run_created",
        "planning_completed",
        "research_tasks_progress",
        "evidence_completed",
        "report_before_generation",
        "report_generated",
        "report_retry",
        "report_retry_completed",
        "recovery_started",
    }
)
_TASK_STATUSES = frozenset(
    {"pending", "in_progress", "completed", "failed", "skipped", "cancelled"}
)
_OPERATION_STATES = frozenset({"active", "completed", "failed", "rejected", "uncertain"})
_CHECKPOINT_BLOCK_REASONS = frozenset(
    {"operation_outcome_uncertain", "report_stream_incomplete"}
)


def _checkpoint_text(value: object, *, field_name: str, limit: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise CheckpointValidationError(
            f"Checkpoint {field_name} must be bounded non-empty text."
        )
    return value.strip()


def _checkpoint_datetime(value: object) -> None:
    if not isinstance(value, str):
        raise CheckpointValidationError("Checkpoint created_at must be an ISO timestamp.")
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise CheckpointValidationError(
            "Checkpoint created_at must be an ISO timestamp."
        ) from exc


def validate_checkpoint_snapshot(snapshot: RunSnapshot) -> None:
    """Validate checkpoint metadata before it becomes a recovery target."""
    state = snapshot.checkpoint_state
    if state is None:
        if snapshot.recovery_resumable:
            raise CheckpointValidationError(
                "Recovery resumable snapshot is missing checkpoint state."
            )
        return
    if not isinstance(state, Mapping):
        raise CheckpointValidationError("Checkpoint state must be an object.")

    schema_version = state.get("schema_version")
    if schema_version != _CHECKPOINT_SCHEMA_VERSION:
        raise CheckpointValidationError("Checkpoint schema version is unsupported.")
    checkpoint_id = _checkpoint_text(state.get("checkpoint_id"), field_name="checkpoint_id")
    run_id = _checkpoint_text(state.get("run_id"), field_name="run_id")
    if run_id != snapshot.run_id:
        raise CheckpointValidationError("Checkpoint run_id does not match snapshot.")
    parent_checkpoint_id = state.get("parent_checkpoint_id")
    if parent_checkpoint_id is not None:
        _checkpoint_text(parent_checkpoint_id, field_name="parent_checkpoint_id")
    phase = _checkpoint_text(state.get("phase"), field_name="phase", limit=64)
    if phase not in _CHECKPOINT_PHASES:
        raise CheckpointValidationError("Checkpoint phase is unsupported.")
    _checkpoint_datetime(state.get("created_at"))

    validated = state.get("validated")
    resumable = state.get("resumable")
    if not isinstance(validated, bool) or not isinstance(resumable, bool):
        raise CheckpointValidationError(
            "Checkpoint validated and resumable flags must be boolean."
        )
    if validated is not True and resumable is True:
        raise CheckpointValidationError(
            "An unvalidated checkpoint cannot be resumable."
        )
    if snapshot.recovery_resumable is True and not (validated and resumable):
        raise CheckpointValidationError(
            "Snapshot recovery flag disagrees with checkpoint state."
        )
    if snapshot.recovery_resumable is False and resumable:
        raise CheckpointValidationError(
            "Snapshot recovery flag disables a resumable checkpoint."
        )
    blocked_reason = state.get("recovery_blocked_reason")
    if blocked_reason is not None and blocked_reason not in _CHECKPOINT_BLOCK_REASONS:
        raise CheckpointValidationError("Checkpoint recovery block reason is invalid.")
    if resumable and blocked_reason is not None:
        raise CheckpointValidationError(
            "A resumable checkpoint cannot carry a recovery block reason."
        )

    task_state = state.get("task_state")
    # RunSnapshot freezes nested arrays to tuples in memory; repository input
    # is a list.  Both are valid representations of the JSON array contract.
    if not isinstance(task_state, (list, tuple)):
        raise CheckpointValidationError("Checkpoint task_state must be a list.")
    for task in task_state:
        if not isinstance(task, Mapping):
            raise CheckpointValidationError("Checkpoint task state must be objects.")
        task_id = task.get("id")
        status = task.get("status")
        if not isinstance(task_id, int) or isinstance(task_id, bool):
            raise CheckpointValidationError("Checkpoint task ID is invalid.")
        if status not in _TASK_STATUSES:
            raise CheckpointValidationError("Checkpoint task status is invalid.")

    operation_state = state.get("operation_state")
    if not isinstance(operation_state, (list, tuple)):
        raise CheckpointValidationError(
            "Checkpoint operation_state must be a list."
        )
    safe_replay_operation_ids: set[str] = set()
    safe_replay_task_ids: set[int] = set()
    for operation in operation_state:
        if not isinstance(operation, Mapping):
            raise CheckpointValidationError(
                "Checkpoint operation state must be objects."
            )
        if not isinstance(operation.get("pairing_key"), (list, tuple)):
            raise CheckpointValidationError("Checkpoint operation pairing key is invalid.")
        if operation.get("status") not in _OPERATION_STATES:
            raise CheckpointValidationError("Checkpoint operation status is invalid.")
        replay_safety = operation.get("replay_safety")
        if replay_safety not in {"safe_replay", "side_effecting", "uncertain"}:
            raise CheckpointValidationError("Checkpoint operation replay safety is invalid.")
        pairing_key = operation.get("pairing_key")
        if (
            replay_safety == "safe_replay"
            and isinstance(pairing_key, (list, tuple))
            and len(pairing_key) == 4
            and isinstance(pairing_key[0], str)
            and all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in pairing_key[1:]
            )
        ):
            safe_replay_operation_ids.add(pairing_key[0])
            task_id = operation.get("task_id")
            if isinstance(task_id, int) and not isinstance(task_id, bool):
                safe_replay_task_ids.add(task_id)
        elif not (
            isinstance(pairing_key, (list, tuple))
            and len(pairing_key) == 4
            and isinstance(pairing_key[0], str)
            and all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in pairing_key[1:]
            )
        ):
            raise CheckpointValidationError("Checkpoint operation pairing key is invalid.")

    report_state = state.get("report_state")
    if not isinstance(report_state, Mapping):
        raise CheckpointValidationError("Checkpoint report_state must be an object.")
    stream_completed = report_state.get("stream_completed")
    if stream_completed is not None and not isinstance(stream_completed, bool):
        raise CheckpointValidationError("Checkpoint report stream state is invalid.")

    checkpoint_payload = state.get("state")
    if not isinstance(checkpoint_payload, Mapping):
        raise CheckpointValidationError("Checkpoint continuation state is missing.")

    if resumable:
        if any(
            task.get("status") == "in_progress"
            and task.get("id") not in safe_replay_task_ids
            for task in task_state
        ):
            raise CheckpointValidationError(
                "An in-progress task must have only safe-to-replay operations."
            )
        if any(
            (
                operation.get("status") in {"active", "uncertain"}
                and operation.get("replay_safety") != "safe_replay"
            )
            or operation.get("replay_safety") == "uncertain"
            for operation in operation_state
        ):
            raise CheckpointValidationError(
                "A resumable checkpoint cannot contain uncertain operations."
            )
        if stream_completed is False:
            raise CheckpointValidationError(
                "An incomplete report stream is not resumable as completed work."
            )

    checkpoint_parts = checkpoint_id.split(":", 2)
    if len(checkpoint_parts) != 3 or checkpoint_parts[0] != run_id:
        raise CheckpointValidationError("Checkpoint ID is not bound to its run.")
    try:
        checkpoint_sequence = int(checkpoint_parts[1])
    except ValueError as exc:
        raise CheckpointValidationError("Checkpoint ID sequence is invalid.") from exc
    if checkpoint_sequence < 0 or checkpoint_parts[2] != phase:
        raise CheckpointValidationError("Checkpoint ID is not bound to its phase.")
    if snapshot.events and checkpoint_sequence > snapshot.events[-1].sequence:
        raise CheckpointValidationError("Checkpoint ID sequence exceeds the event ledger.")

    active_operation_ids: set[str] = set()
    completed_operation_ids: set[str] = set()
    for event in snapshot.events:
        # The terminal snapshot may contain events emitted after this
        # checkpoint.  Replay safety is evaluated against the checkpoint
        # prefix, never against later terminalization or uncertain work.
        if event.sequence > checkpoint_sequence:
            continue
        if event.kind is EventKind.OPERATION_STARTED:
            if event.operation_id is None or event.operation_id in active_operation_ids:
                raise CheckpointValidationError("Operation audit contains duplicate starts.")
            active_operation_ids.add(event.operation_id)
        elif event.kind in {
            EventKind.OPERATION_COMPLETED,
            EventKind.OPERATION_FAILED,
        }:
            if event.operation_id is None or event.operation_id not in active_operation_ids:
                raise CheckpointValidationError("Operation audit terminal is unmatched.")
            active_operation_ids.remove(event.operation_id)
            completed_operation_ids.add(event.operation_id)
    if resumable and active_operation_ids - safe_replay_operation_ids:
        raise CheckpointValidationError(
            "A resumable checkpoint has an active operation audit."
        )
