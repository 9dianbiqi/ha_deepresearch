"""Compatibility adapters for canonical offline research assessment."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Protocol

from loguru import logger

from research.contracts import RunError, RunSnapshot, RunStatus
from research.evaluation import OfflineEvaluationService, ResearchAssessment

from .models import EvaluationFinding, HarnessRunRecord, RunContext


class _EvaluationService(Protocol):
    """Structural contract used to inject the canonical offline evaluator."""

    def evaluate(self, snapshot: RunSnapshot) -> ResearchAssessment:
        """Assess one immutable snapshot."""
        ...


@dataclass(kw_only=True)
class EvaluationResult:
    """Historical projection of one canonical research assessment."""

    score: float
    findings: list[EvaluationFinding] = field(default_factory=list)

    @classmethod
    def from_assessment(cls, assessment: ResearchAssessment) -> EvaluationResult:
        """Project an immutable assessment into the historical mutable shape."""
        return cls(
            score=assessment.score,
            findings=[
                EvaluationFinding(
                    severity=finding.severity,
                    message=finding.message,
                    code=finding.code,
                )
                for finding in assessment.findings
            ],
        )

    def as_dict(self) -> dict[str, object]:
        """Serialize the evaluation result for legacy persistence."""
        return {
            "score": self.score,
            "findings": [
                {
                    "severity": item.severity,
                    "message": item.message,
                    "code": item.code,
                }
                for item in self.findings
            ],
        }


class RuleBasedEvaluator:
    """Historical facade over the read-only offline evaluation service."""

    def __init__(self, service: _EvaluationService | None = None) -> None:
        """Initialize the facade with the canonical offline evaluator."""
        self._service = service or OfflineEvaluationService()

    def evaluate(self, context: RunContext) -> EvaluationResult:
        """Assess a live compatibility context through an immutable snapshot."""
        snapshot = context.to_snapshot()
        if context.result is None:
            snapshot = replace(snapshot, output={})
        assessment = self._service.evaluate(snapshot)
        result = EvaluationResult.from_assessment(assessment)
        logger.info(
            "Evaluation complete: run_id={} score={:.2f} findings={}",
            context.run_id,
            result.score,
            len(result.findings),
        )
        return result

    def evaluate_record(self, record: HarnessRunRecord) -> EvaluationResult:
        """Assess a persisted legacy record without re-running the workflow."""
        try:
            status = RunStatus(record.status)
        except ValueError:
            status = RunStatus.COMPLETED
        snapshot = RunSnapshot(
            run_id=record.run_id,
            topic=record.topic,
            status=status,
            started_at=record.started_at,
            completed_at=record.completed_at,
            parent_run_id=None,
            output=dict(record.output),
            followup_context=dict(record.compressed_context),
            metrics=dict(record.metrics),
            policy_decisions=tuple(dict(item) for item in record.policy_decisions),
            config_snapshot=dict(record.config_snapshot),
            events=(),
            error=(
                RunError(code="legacy_run_error", message=record.error)
                if record.error
                else None
            ),
        )
        return EvaluationResult.from_assessment(self._service.evaluate(snapshot))
