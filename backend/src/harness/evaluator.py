"""Baseline evaluators for harness-controlled runs."""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import EvaluationFinding, RunContext


@dataclass(kw_only=True)
class EvaluationResult:
    """Structured evaluation output for a completed run."""

    score: float
    findings: list[EvaluationFinding] = field(default_factory=list)


class RuleBasedEvaluator:
    """Simple evaluator that checks for obvious workflow failures."""

    def evaluate(self, context: RunContext) -> EvaluationResult:
        """Return baseline quality signals for the run."""
        findings: list[EvaluationFinding] = []
        score = 1.0

        output = context.result
        if output is None:
            findings.append(
                EvaluationFinding(
                    severity="error",
                    code="missing_output",
                    message="Run completed without a result payload.",
                )
            )
            return EvaluationResult(score=0.0, findings=findings)

        if not output.todo_items:
            findings.append(
                EvaluationFinding(
                    severity="warning",
                    code="missing_tasks",
                    message="Planner did not produce any explicit todo items.",
                )
            )
            score -= 0.2

        if not (output.report_markdown or "").strip():
            findings.append(
                EvaluationFinding(
                    severity="error",
                    code="missing_report",
                    message="Final report markdown is empty.",
                )
            )
            score -= 0.5

        incomplete_tasks = [item.title for item in output.todo_items if item.status != "completed"]
        if incomplete_tasks:
            findings.append(
                EvaluationFinding(
                    severity="warning",
                    code="incomplete_tasks",
                    message=f"Some tasks did not complete: {', '.join(incomplete_tasks)}",
                )
            )
            score -= 0.2

        tasks_without_summary = [item.title for item in output.todo_items if not (item.summary or "").strip()]
        if tasks_without_summary:
            findings.append(
                EvaluationFinding(
                    severity="warning",
                    code="missing_summaries",
                    message=f"Some tasks are missing summaries: {', '.join(tasks_without_summary)}",
                )
            )
            score -= 0.1

        return EvaluationResult(score=max(score, 0.0), findings=findings)
