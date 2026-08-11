"""Historical model names retained at the compatibility boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from models import SummaryStateOutput
from research.contracts import ResearchCommand, ResearchEvent
from research.session import RunSession as RunContext

HarnessRunRequest = ResearchCommand
HarnessEvent = ResearchEvent


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


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
    error_code: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    findings: list[EvaluationFinding] = field(default_factory=list)
    compressed_context: dict[str, Any] = field(default_factory=dict)
    policy_decisions: list[dict[str, Any]] = field(default_factory=list)
    resumable: bool = False
    recovery_resumable: bool = False
    last_resumable_parent: str | None = None


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
    ) -> HarnessRunRecord:
        """Build a persisted record from the in-memory run context."""
        output = context.to_legacy_output()
        serialized_output = {
            "running_summary": output.running_summary,
            "report_markdown": output.report_markdown,
            "todo_items": [
                item.to_dict() for item in output.todo_items
            ],
        }

        return cls(
            run_id=context.run_id,
            topic=context.command.topic,
            started_at=context.started_at,
            completed_at=context.completed_at,
            status=context.status.value,
            config_snapshot=context.command.config.safe_snapshot(),
            metrics=dict(context.metrics),
            error=context.error.message if context.error else None,
            events=[event.as_dict() for event in context.events],
            output=serialized_output,
            compressed_context=dict(context.followup_context),
            policy_decisions=list(context.policy_decisions),
            evaluation=evaluation or {},
        )


@dataclass(kw_only=True)
class RecorderConfig:
    """Filesystem settings used by harness recorders."""

    base_path: Path
