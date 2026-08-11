"""Tests for terminal research-state validation."""

from __future__ import annotations

import pytest

from config import Configuration
from models import ResearchState, TodoItem
from research.contracts import ResearchCommand
from research.session import RunSession
from research.validation import TerminalStateError, validate_terminal_state


def make_session(*, task_statuses: list[str], report: str | None) -> RunSession:
    """Build a running session at the pre-terminal validation boundary."""
    command = ResearchCommand(topic="terminal topic", config=Configuration())
    session = RunSession(
        command=command,
        state=ResearchState(research_topic=command.topic),
    )
    session.start()
    session.install_plan(
        [
            TodoItem(
                id=index,
                title=f"Task {index}",
                intent="intent",
                query="query",
                status=status,
            )
            for index, status in enumerate(task_statuses, start=1)
        ]
    )
    session.state.structured_report = report
    session.state.running_summary = "Fallback text must not replace canonical report"
    return session


@pytest.mark.parametrize("status", ["pending", "in_progress"])
def test_terminal_validator_rejects_nonterminal_task(status: str) -> None:
    session = make_session(task_statuses=["completed", status], report="# Report")

    with pytest.raises(TerminalStateError, match=status):
        validate_terminal_state(session)


@pytest.mark.parametrize("report", [None, "", "   \n\t"])
def test_terminal_validator_rejects_empty_canonical_report(report: str | None) -> None:
    session = make_session(task_statuses=["completed"], report=report)

    with pytest.raises(TerminalStateError, match="report"):
        validate_terminal_state(session)


def test_terminal_validator_accepts_empty_task_list_with_report() -> None:
    session = make_session(task_statuses=[], report="# Report")

    assert validate_terminal_state(session) is None


@pytest.mark.parametrize("status", ["completed", "failed", "skipped", "cancelled"])
def test_terminal_validator_accepts_each_terminal_task_status(status: str) -> None:
    session = make_session(task_statuses=[status], report="# Report")

    assert validate_terminal_state(session) is None


def test_terminal_validator_does_not_score_summary_or_source_quality() -> None:
    session = make_session(
        task_statuses=["completed", "failed", "skipped", "cancelled"],
        report="# Report",
    )
    for task in session.state.todo_items:
        task.summary = None
        task.sources_summary = None

    assert validate_terminal_state(session) is None
