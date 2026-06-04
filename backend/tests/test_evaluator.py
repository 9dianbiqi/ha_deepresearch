"""Tests for RuleBasedEvaluator scoring logic."""

from __future__ import annotations

import unittest

import conftest  # noqa: F401

from models import SummaryStateOutput, TodoItem
from harness.evaluator import EvaluationResult, RuleBasedEvaluator
from harness.models import HarnessRunRequest, RunContext

from config import Configuration


def _make_context(
    *,
    todo_items: list[TodoItem] | None = None,
    report_markdown: str = "Some report",
    compressed_context: dict | None = None,
    has_output: bool = True,
) -> RunContext:
    """Build a RunContext for evaluator testing."""
    config = Configuration.from_env()
    request = HarnessRunRequest(topic="test", config=config)
    context = RunContext(request=request, status="completed")

    if has_output:
        context.result = SummaryStateOutput(
            running_summary=report_markdown,
            report_markdown=report_markdown,
            todo_items=todo_items or [],
        )
    else:
        context.result = None

    context.compressed_context = compressed_context or {}
    return context


class TestEvaluatorScoring(unittest.TestCase):
    """Cover scoring paths of RuleBasedEvaluator."""

    def setUp(self) -> None:
        self.evaluator = RuleBasedEvaluator()

    def test_perfect_run_scores_one(self) -> None:
        tasks = [
            TodoItem(
                id=1,
                title="Task 1",
                intent="Intent",
                query="q",
                status="completed",
                summary="Summary",
            )
        ]
        context = _make_context(
            todo_items=tasks,
            report_markdown="# Report",
            compressed_context={"run_summary": {}},
        )
        result = self.evaluator.evaluate(context)

        self.assertEqual(result.score, 1.0)
        self.assertEqual(len(result.findings), 0)

    def test_no_output_scores_zero(self) -> None:
        context = _make_context(has_output=False)
        result = self.evaluator.evaluate(context)

        self.assertEqual(result.score, 0.0)
        codes = [f.code for f in result.findings]
        self.assertIn("missing_output", codes)

    def test_empty_report_deducts(self) -> None:
        tasks = [
            TodoItem(
                id=1,
                title="T",
                intent="I",
                query="q",
                status="completed",
                summary="S",
            )
        ]
        context = _make_context(
            todo_items=tasks,
            report_markdown="",
            compressed_context={"run_summary": {}},
        )
        result = self.evaluator.evaluate(context)

        self.assertLess(result.score, 1.0)
        codes = [f.code for f in result.findings]
        self.assertIn("missing_report", codes)

    def test_no_tasks_deducts(self) -> None:
        context = _make_context(
            todo_items=[],
            report_markdown="# Report",
            compressed_context={"x": 1},
        )
        result = self.evaluator.evaluate(context)

        self.assertLess(result.score, 1.0)
        codes = [f.code for f in result.findings]
        self.assertIn("missing_tasks", codes)

    def test_incomplete_tasks_deduct(self) -> None:
        tasks = [
            TodoItem(
                id=1,
                title="Incomplete",
                intent="I",
                query="q",
                status="in_progress",
                summary="S",
            )
        ]
        context = _make_context(
            todo_items=tasks,
            report_markdown="# Report",
            compressed_context={"x": 1},
        )
        result = self.evaluator.evaluate(context)

        codes = [f.code for f in result.findings]
        self.assertIn("incomplete_tasks", codes)

    def test_missing_summary_deducts(self) -> None:
        tasks = [
            TodoItem(
                id=1,
                title="No Summary",
                intent="I",
                query="q",
                status="completed",
                summary="",
            )
        ]
        context = _make_context(
            todo_items=tasks,
            report_markdown="# Report",
            compressed_context={"x": 1},
        )
        result = self.evaluator.evaluate(context)

        codes = [f.code for f in result.findings]
        self.assertIn("missing_summaries", codes)

    def test_missing_compressed_context_deducts(self) -> None:
        tasks = [
            TodoItem(
                id=1,
                title="T",
                intent="I",
                query="q",
                status="completed",
                summary="S",
            )
        ]
        context = _make_context(
            todo_items=tasks,
            report_markdown="# Report",
            compressed_context={},
        )
        result = self.evaluator.evaluate(context)

        codes = [f.code for f in result.findings]
        self.assertIn("missing_compressed_context", codes)

    def test_score_never_below_zero(self) -> None:
        """Stacking all penalties should not produce a negative score."""
        tasks = [
            TodoItem(
                id=1,
                title="Bad",
                intent="I",
                query="q",
                status="pending",
                summary="",
            )
        ]
        context = _make_context(
            todo_items=tasks,
            report_markdown="",
            compressed_context={},
        )
        result = self.evaluator.evaluate(context)

        self.assertGreaterEqual(result.score, 0.0)


if __name__ == "__main__":
    unittest.main()
