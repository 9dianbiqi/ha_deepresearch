"""Stable input, event, snapshot, and result contracts for research runs."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4

from config import Configuration
from models import SummaryStateOutput

_MEMORY_SCOPE_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


def _thaw_json(value: Any) -> Any:
    """Return a detached JSON-compatible copy of immutable payload data."""
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json(item) for item in value]
    return value


def _freeze_json(value: Any) -> Any:
    """Recursively freeze JSON-compatible containers against mutation."""
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def normalize_run_id(value: str) -> str:
    """Validate a path-safe canonical UUID and return its lowercase hex form."""
    if not isinstance(value, str):
        raise ValueError("Run ID must be a canonical UUID string.")
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("Run ID must be a canonical UUID string.") from exc
    if value not in {str(parsed), parsed.hex}:
        raise ValueError("Run ID must be a canonical UUID string.")
    return parsed.hex


class RunStatus(str, Enum):
    """Lifecycle states for a research run."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    REPORT_INCOMPLETE = "report_incomplete"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class EventKind(str, Enum):
    """Normalized event kinds emitted by integrated research execution."""

    RUN_STARTED = "run_started"
    POLICY_CHECKED = "policy_checked"
    REPOSITORY_DETECTED = "repository_detected"
    PLAN_CREATED = "plan_created"
    TASK_STARTED = "task_started"
    SOURCES_COLLECTED = "sources_collected"
    SUMMARY_DELTA = "summary_delta"
    TASK_RETRY_SCHEDULED = "task_retry_scheduled"
    TASK_COMPLETED = "task_completed"
    TASK_SKIPPED = "task_skipped"
    TASK_FAILED = "task_failed"
    REPORT_NOTE_CREATED = "report_note_created"
    REPORT_GENERATED = "report_generated"
    OPERATION_STARTED = "operation_started"
    OPERATION_COMPLETED = "operation_completed"
    OPERATION_FAILED = "operation_failed"
    OPERATION_REJECTED = "operation_rejected"
    RUN_COMPLETED = "run_completed"
    RUN_RECOVERY_STARTED = "run_recovery_started"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"
    RUN_REJECTED = "run_rejected"
    HISTORY_RECALLED = "history_recalled"


@dataclass(frozen=True, kw_only=True)
class ResearchCommand:
    """Immutable command that starts one research run."""

    topic: str
    config: Configuration
    run_id: str = field(default_factory=lambda: uuid4().hex)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    permission_mode: str = "default"
    caller_mode: str = "public"
    parent_run_id: str | None = None
    use_history_memory: bool = True
    memory_scope: str = "default"

    def __post_init__(self) -> None:
        """Validate topic and normalize identifiers at the trust boundary."""
        if not self.topic.strip():
            raise ValueError("Research topic must not be empty.")
        if self.permission_mode not in {"default", "strict"}:
            raise ValueError("Permission mode is not supported.")
        if self.caller_mode not in {"public", "internal"}:
            raise ValueError("Caller mode is not supported.")
        if not isinstance(self.use_history_memory, bool):
            raise TypeError("History memory switch must be boolean.")
        if (
            not isinstance(self.memory_scope, str)
            or _MEMORY_SCOPE_RE.fullmatch(self.memory_scope.strip().casefold()) is None
        ):
            raise ValueError("Memory scope is invalid.")
        object.__setattr__(self, "memory_scope", self.memory_scope.strip().casefold())
        if not isinstance(self.config, Configuration):
            raise TypeError("Research command config must be Configuration.")
        object.__setattr__(self, "config", self.config.model_copy(deep=True))
        if not isinstance(self.metadata, Mapping):
            raise TypeError("Research command metadata must be a mapping.")
        detached_metadata = _thaw_json(self.metadata)
        json.dumps(detached_metadata)
        object.__setattr__(self, "metadata", _freeze_json(detached_metadata))
        object.__setattr__(self, "run_id", normalize_run_id(self.run_id))
        if self.parent_run_id is not None:
            object.__setattr__(
                self,
                "parent_run_id",
                normalize_run_id(self.parent_run_id),
            )


@dataclass(frozen=True, kw_only=True)
class ResearchEvent:
    """One ordered, JSON-serializable fact emitted during a run."""

    kind: EventKind
    run_id: str
    sequence: int
    occurred_at: datetime
    payload: Mapping[str, Any] = field(default_factory=dict)
    task_id: int | None = None
    operation_id: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        """Validate, detach, and recursively freeze the event payload."""
        detached = _thaw_json(self.payload)
        json.dumps(detached)
        object.__setattr__(self, "payload", _freeze_json(detached))

    def as_dict(self) -> dict[str, Any]:
        """Return the wire representation after validating the payload."""
        payload = _thaw_json(self.payload)
        json.dumps(payload)
        return {
            "schema_version": self.schema_version,
            "type": self.kind.value,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "operation_id": self.operation_id,
            "sequence": self.sequence,
            "occurred_at": self.occurred_at.isoformat(),
            "payload": payload,
        }


@dataclass(frozen=True, kw_only=True)
class PreparedTerminal:
    """Terminal transition prepared for durable persistence and confirmation."""

    status: RunStatus
    event: ResearchEvent
    snapshot: RunSnapshot


@dataclass(frozen=True, kw_only=True)
class RunError:
    """Stable machine and human-readable run error."""

    code: str
    message: str


@dataclass(frozen=True, kw_only=True)
class RunSnapshot:
    """Immutable persistence snapshot of a research run."""

    run_id: str
    topic: str
    status: RunStatus
    started_at: datetime
    completed_at: datetime | None
    parent_run_id: str | None
    output: Mapping[str, Any]
    followup_context: Mapping[str, Any]
    metrics: Mapping[str, Any]
    policy_decisions: tuple[Mapping[str, Any], ...]
    config_snapshot: Mapping[str, Any]
    events: tuple[ResearchEvent, ...]
    error: RunError | None = None
    failure_reason: str | None = None
    checkpoint: str | None = None
    checkpoint_state: Mapping[str, Any] | None = None
    resumable: bool | None = None
    recovery_resumable: bool | None = None
    last_resumable_parent: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        """Detach and recursively freeze every JSON container in the snapshot."""
        for field_name in (
            "output",
            "followup_context",
            "metrics",
            "config_snapshot",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, Mapping):
                raise TypeError(f"Snapshot {field_name} must be a mapping.")
            detached = _thaw_json(value)
            json.dumps(detached)
            object.__setattr__(self, field_name, _freeze_json(detached))
        if self.checkpoint_state is not None:
            if not isinstance(self.checkpoint_state, Mapping):
                raise TypeError("Snapshot checkpoint_state must be a mapping or null.")
            detached_checkpoint = _thaw_json(self.checkpoint_state)
            json.dumps(detached_checkpoint)
            object.__setattr__(
                self,
                "checkpoint_state",
                _freeze_json(detached_checkpoint),
            )

        frozen_decisions: list[Mapping[str, Any]] = []
        for decision in self.policy_decisions:
            if not isinstance(decision, Mapping):
                raise TypeError("Snapshot policy decisions must be mappings.")
            detached_decision = _thaw_json(decision)
            json.dumps(detached_decision)
            frozen_decisions.append(_freeze_json(detached_decision))
        object.__setattr__(self, "policy_decisions", tuple(frozen_decisions))
        object.__setattr__(self, "events", tuple(self.events))
        if self.resumable is None:
            object.__setattr__(self, "resumable", self.status is RunStatus.COMPLETED)
        if self.recovery_resumable is None:
            checkpoint_state = self.checkpoint_state
            object.__setattr__(
                self,
                "recovery_resumable",
                bool(
                    isinstance(checkpoint_state, Mapping)
                    and checkpoint_state.get("resumable") is True
                    and checkpoint_state.get("validated") is True
                ),
            )

    def as_dict(self) -> dict[str, Any]:
        """Return a fully detached JSON-ready snapshot representation."""
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "topic": self.topic,
            "status": self.status.value,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "parent_run_id": self.parent_run_id,
            "output": _thaw_json(self.output),
            "followup_context": _thaw_json(self.followup_context),
            "metrics": _thaw_json(self.metrics),
            "policy_decisions": [
                _thaw_json(decision) for decision in self.policy_decisions
            ],
            "config_snapshot": _thaw_json(self.config_snapshot),
            "events": [event.as_dict() for event in self.events],
            "error": asdict(self.error) if self.error else None,
            "failure_reason": self.failure_reason,
            "checkpoint": self.checkpoint,
            "checkpoint_state": (
                _thaw_json(self.checkpoint_state)
                if self.checkpoint_state is not None
                else None
            ),
            "resumable": self.resumable,
            "recovery_resumable": self.recovery_resumable,
            "last_resumable_parent": self.last_resumable_parent,
        }


@dataclass(frozen=True, kw_only=True)
class ResearchRunResult:
    """Final in-process result returned to research callers."""

    run_id: str
    status: RunStatus
    output: SummaryStateOutput | None
    error: RunError | None
    metrics: dict[str, Any]
    followup_context: dict[str, Any]
    policy_decisions: tuple[dict[str, Any], ...]
    evaluation_status: str = "pending"
    resumable: bool = False
    recovery_resumable: bool = False
    last_resumable_parent: str | None = None
