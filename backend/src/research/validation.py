"""Validation at the durable terminal transition boundary."""

from __future__ import annotations

from .session import RunSession

TERMINAL_TASK_STATUSES = frozenset(
    {"completed", "failed", "skipped", "cancelled"}
)


class TerminalStateError(RuntimeError):
    """Raised when canonical state cannot be finalized durably."""


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
