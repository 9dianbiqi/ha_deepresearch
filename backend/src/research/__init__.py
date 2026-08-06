"""Authoritative contracts and state transitions for research runs."""

from .contracts import (
    EventKind,
    PreparedTerminal,
    ResearchCommand,
    ResearchEvent,
    ResearchRunResult,
    RunError,
    RunSnapshot,
    RunStatus,
)
from .session import (
    NEVER_CANCELLED,
    CancellationRequestedError,
    CancellationToken,
    InvalidTransitionError,
    RunSession,
)

__all__ = [
    "NEVER_CANCELLED",
    "CancellationRequestedError",
    "CancellationToken",
    "EventKind",
    "InvalidTransitionError",
    "PreparedTerminal",
    "ResearchCommand",
    "ResearchEvent",
    "ResearchRunResult",
    "RunError",
    "RunSession",
    "RunSnapshot",
    "RunStatus",
]
