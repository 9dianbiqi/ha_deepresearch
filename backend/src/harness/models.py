"""Typed models for the harness orchestration layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from config import Configuration
from models import SummaryStateOutput


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


@dataclass(kw_only=True)
class HarnessRunRequest:
    """Input contract for a harness-controlled research run."""

    topic: str
    config: Configuration
    run_id: str = field(default_factory=lambda: uuid4().hex)
    metadata: dict[str, Any] = field(default_factory=dict)
    permission_mode: str = "default"
    caller_mode: str = "public"


@dataclass(kw_only=True)
class HarnessEvent:
    """Normalized event emitted during a harness run."""

    event_type: str
    run_id: str
    created_at: datetime = field(default_factory=utc_now)
    payload: dict[str, Any] = field(default_factory=dict)
    sequence: int = 0

    def as_dict(self) -> dict[str, Any]:
        """Convert the event into a JSON-serializable payload."""
        return {
            "event_type": self.event_type,
            "run_id": self.run_id,
            "created_at": self.created_at.isoformat(),
            "payload": self.payload,
            "sequence": self.sequence,
        }


@dataclass(kw_only=True)
class RunContext:
    """Mutable execution context shared across the harness lifecycle."""

    request: HarnessRunRequest
    started_at: datetime = field(default_factory=utc_now)
    completed_at: datetime | None = None
    status: str = "pending"
    events: list[HarnessEvent] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    result: SummaryStateOutput | None = None
    policy_decisions: list[dict[str, Any]] = field(default_factory=list)
    compressed_context: dict[str, Any] = field(default_factory=dict)

    @property
    def run_id(self) -> str:
        """Expose the current run identifier."""
        return self.request.run_id

    @property
    def duration_seconds(self) -> float | None:
        """Return the run duration when both timestamps are available."""
        if self.completed_at is None:
            return None
        return (self.completed_at - self.started_at).total_seconds()


@dataclass(kw_only=True)
class EvaluationFinding:
    """Single evaluation finding produced by a harness evaluator."""

    severity: str
    message: str
    code: str | None = None


@dataclass(kw_only=True)
class HarnessRunResult:
    """Final result returned by the harness runner."""

    run_id: str
    status: str
    output: SummaryStateOutput | None = None
    error: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    findings: list[EvaluationFinding] = field(default_factory=list)
    compressed_context: dict[str, Any] = field(default_factory=dict)
    policy_decisions: list[dict[str, Any]] = field(default_factory=list)


@dataclass(kw_only=True)
class HarnessRunRecord:
    """Persisted snapshot of a harness run."""

    run_id: str
    topic: str
    started_at: datetime
    completed_at: datetime | None
    status: str
    config_snapshot: dict[str, Any]
    metrics: dict[str, Any]
    error: str | None
    events: list[dict[str, Any]] = field(default_factory=list)
    output: dict[str, Any] = field(default_factory=dict)
    compressed_context: dict[str, Any] = field(default_factory=dict)
    policy_decisions: list[dict[str, Any]] = field(default_factory=list)
    evaluation: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Convert the record into a JSON-serializable payload."""
        return {
            "run_id": self.run_id,
            "topic": self.topic,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "status": self.status,
            "config_snapshot": self.config_snapshot,
            "metrics": self.metrics,
            "error": self.error,
            "events": self.events,
            "output": self.output,
            "compressed_context": self.compressed_context,
            "policy_decisions": self.policy_decisions,
            "evaluation": self.evaluation,
        }

    @classmethod
    def from_context(
        cls,
        context: RunContext,
        *,
        evaluation: dict[str, Any] | None = None,
    ) -> "HarnessRunRecord":
        """Build a persisted record from the in-memory run context."""
        output = context.result
        serialized_output = {
            "running_summary": output.running_summary if output else None,
            "report_markdown": output.report_markdown if output else None,
            "todo_items": [
                {
                    "id": item.id,
                    "title": item.title,
                    "intent": item.intent,
                    "query": item.query,
                    "status": item.status,
                    "summary": item.summary,
                    "sources_summary": item.sources_summary,
                    "note_id": item.note_id,
                    "note_path": item.note_path,
                }
                for item in (output.todo_items if output else [])
            ],
        }

        return cls(
            run_id=context.run_id,
            topic=context.request.topic,
            started_at=context.started_at,
            completed_at=context.completed_at,
            status=context.status,
            config_snapshot=context.request.config.model_dump(),
            metrics=dict(context.metrics),
            error=context.error,
            events=[event.as_dict() for event in context.events],
            output=serialized_output,
            compressed_context=dict(context.compressed_context),
            policy_decisions=list(context.policy_decisions),
            evaluation=evaluation or {},
        )


@dataclass(kw_only=True)
class RecorderConfig:
    """Filesystem settings used by harness recorders."""

    base_path: Path
