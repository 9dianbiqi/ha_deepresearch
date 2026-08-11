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
from .history import HistoryCursorError, HistoryPage, ResearchHistoryStore
from .memory import (
    MemoryNotFoundError,
    MemoryStateError,
    MemoryStoreUnavailableError,
    MemoryValidationError,
    UserMemory,
    UserMemoryStore,
)
from .session import (
    NEVER_CANCELLED,
    CancellationRequestedError,
    CancellationToken,
    CheckpointPersistenceError,
    InvalidTransitionError,
    RunSession,
)

__all__ = [
    "NEVER_CANCELLED",
    "CancellationRequestedError",
    "CheckpointPersistenceError",
    "CancellationToken",
    "EventKind",
    "HistoryCursorError",
    "HistoryPage",
    "InvalidTransitionError",
    "PreparedTerminal",
    "ResearchCommand",
    "ResearchEvent",
    "ResearchRunResult",
    "RunError",
    "RunSession",
    "RunSnapshot",
    "RunStatus",
    "ResearchHistoryStore",
    "MemoryNotFoundError",
    "MemoryStateError",
    "MemoryStoreUnavailableError",
    "MemoryValidationError",
    "UserMemory",
    "UserMemoryStore",
]
