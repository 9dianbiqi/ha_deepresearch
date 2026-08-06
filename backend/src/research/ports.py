"""Focused structural ports used by the research application core."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

from .context import FollowupContext
from .contracts import ResearchCommand, ResearchEvent, RunSnapshot
from .session import RunSession


class ResearchCoordinator(Protocol):
    """Execute research transitions against one authoritative session."""

    def execute(
        self,
        session: RunSession,
        prior_context: FollowupContext | None,
    ) -> None:
        """Run the coordinator exactly once for ``session``."""
        raise NotImplementedError


class RunRepository(Protocol):
    """Durably store and reconstruct canonical run snapshots."""

    def save(self, snapshot: RunSnapshot) -> None:
        """Persist one canonical snapshot."""
        raise NotImplementedError

    def load(self, run_id: str) -> RunSnapshot:
        """Load one canonical snapshot by normalized run ID."""
        raise NotImplementedError


class ResearchEventObserver(Protocol):
    """Receive committed typed research events."""

    def __call__(self, event: ResearchEvent) -> None:
        """Observe one committed event without mutating its session."""
        raise NotImplementedError


class OperationPolicy(Protocol):
    """Authorize one capability immediately adjacent to a side effect."""

    def evaluate_capability(
        self,
        capability: str,
        command: ResearchCommand,
    ) -> Any:
        """Return one serializable decision for ``capability``."""
        raise NotImplementedError


class CommandPolicy(OperationPolicy, Protocol):
    """Structurally match command-level policy preflight implementations."""

    def evaluate(self, command: ResearchCommand) -> Iterable[Any]:
        """Return policy decision objects for ``command``."""
        raise NotImplementedError

    def assert_executable(self, decisions: Iterable[Any]) -> None:
        """Raise ``PermissionError`` when execution is not permitted."""
        raise NotImplementedError


__all__ = [
    "CommandPolicy",
    "OperationPolicy",
    "ResearchCoordinator",
    "ResearchEventObserver",
    "RunRepository",
]
