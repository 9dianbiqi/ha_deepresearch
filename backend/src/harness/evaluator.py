"""Baseline evaluators for harness-controlled runs."""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import EvaluationFinding, HarnessRunRecord, RunContext


@dataclass(kw_only=True)
class EvaluationResult:
    """Structured evaluation output for a completed run."""

    score: float
    findings: list[EvaluationFinding] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        """Serialize the evaluation result for persistence."""
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
    """Simple evaluator that checks for obvious workflow failures."""

    def evaluate(self, context: RunContext) -> EvaluationResult:
        """Return baseline quality signals for the run."""
        output = context.result
        compressed_context = context.compressed_context
        return self._evaluate_payload(
            todo_items=output.todo_items if output else [],
            report_markdown=(output.report_markdown or "") if output else "",
            compressed_context=compressed_context,
            has_output=output is not None,
        )

    def evaluate_record(self, record: HarnessRunRecord) -> EvaluationResult:
        """Evaluate a persisted record without re-running the workflow."""
        output = record.output
        return self._evaluate_payload(
            todo_items=output.get("todo_items", []),
            report_markdown=output.get("report_markdown") or "",
            compressed_context=record.compressed_context,
            has_output=bool(output),
        )

    def _evaluate_payload(
        self,
        *,
        todo_items: list[object],
        report_markdown: str,
        compressed_context: dict[str, object],
        has_output: bool,
    ) -> EvaluationResult:
        findings: list[EvaluationFinding] = []
        score = 1.0

        if not has_output:
            findings.append(
                EvaluationFinding(
                    severity="error",
                    code="missing_output",
                    message="Run completed without a result payload.",
                )
            )
            return EvaluationResult(score=0.0, findings=findings)

        if not todo_items:
            findings.append(
                EvaluationFinding(
                    severity="warning",
                    code="missing_tasks",
                    message="Planner did not produce any explicit todo items.",
                )
            )
            score -= 0.2

        if not report_markdown.strip():
            findings.append(
                EvaluationFinding(
                    severity="error",
                    code="missing_report",
                    message="Final report markdown is empty.",
                )
            )
            score -= 0.5

        incomplete_tasks = []
        tasks_without_summary = []
        for item in todo_items:
            status = getattr(item, "status", None)
            title = getattr(item, "title", None)
            summary = getattr(item, "summary", None)
            if isinstance(item, dict):
                status = item.get("status")
                title = item.get("title")
                summary = item.get("summary")
            if status != "completed" and title:
                incomplete_tasks.append(str(title))
            if not (summary or "").strip() and title:
                tasks_without_summary.append(str(title))

        if incomplete_tasks:
            findings.append(
                EvaluationFinding(
                    severity="warning",
                    code="incomplete_tasks",
                    message=f"Some tasks did not complete: {', '.join(incomplete_tasks)}",
                )
            )
            score -= 0.2

        if tasks_without_summary:
            findings.append(
                EvaluationFinding(
                    severity="warning",
                    code="missing_summaries",
                    message=f"Some tasks are missing summaries: {', '.join(tasks_without_summary)}",
                )
            )
            score -= 0.1

        if not compressed_context:
            findings.append(
                EvaluationFinding(
                    severity="warning",
                    code="missing_compressed_context",
                    message="Compressed context payload is empty.",
                )
            )
            score -= 0.1

        return EvaluationResult(score=max(score, 0.0), findings=findings)
