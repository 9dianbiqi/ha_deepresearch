"""Offline assessment and historical evaluator compatibility tests."""

from __future__ import annotations

import json
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from uuid import uuid4

import conftest  # noqa: F401

import harness
from config import Configuration
from harness.evaluator import EvaluationResult, RuleBasedEvaluator
from harness.models import (
    EvaluationFinding,
    HarnessRunRecord,
    HarnessRunRequest,
    RunContext,
)
from harness.scenarios import HarnessScenario
from models import ResearchState, TodoItem
from research.contracts import ResearchCommand, RunSnapshot, RunStatus
from research.evaluation import (
    AssessmentFinding,
    OfflineEvaluationService,
    ResearchAssessment,
)


def _make_context(
    *,
    todo_items: list[TodoItem] | None = None,
    report_markdown: str = "Some report",
    compressed_context: dict[str, object] | None = None,
    has_output: bool = True,
    config: Configuration | None = None,
) -> RunContext:
    """Build a canonical run session for compatibility-evaluator testing."""
    request = HarnessRunRequest(
        topic="test",
        config=config or Configuration.from_env(),
    )
    context = RunContext(
        command=request,
        state=ResearchState(research_topic=request.topic),
    )
    context.start()

    if has_output:
        context.install_plan(todo_items or [])
        context.set_report(report_markdown)

    context.followup_context = compressed_context or {}
    return context


def _make_snapshot(
    *,
    output: dict[str, object],
    followup_context: dict[str, object],
) -> RunSnapshot:
    """Build an isolated persisted snapshot for offline assessment."""
    return RunSnapshot(
        run_id=uuid4().hex,
        topic="offline fixture",
        status=RunStatus.COMPLETED,
        started_at=datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc),
        completed_at=datetime(2026, 7, 19, 8, 1, tzinfo=timezone.utc),
        parent_run_id=None,
        output=output,
        followup_context=followup_context,
        metrics={"duration_seconds": 60.0},
        policy_decisions=(),
        config_snapshot={"llm_provider": "ollama"},
        events=(),
    )


class TestResearchAssessment(unittest.TestCase):
    """Define the immutable, detached offline assessment contract."""

    def test_values_are_frozen_bounded_and_timezone_aware(self) -> None:
        finding = AssessmentFinding(
            severity="warning",
            code="example",
            message="Example finding.",
        )
        assessment = ResearchAssessment(
            run_id=uuid4().hex,
            evaluated_at=datetime.now(timezone.utc),
            score=0.75,
            findings=[finding],
        )

        self.assertIsInstance(assessment.findings, tuple)
        self.assertEqual(assessment.schema_version, 1)
        with self.assertRaises(FrozenInstanceError):
            finding.message = "changed"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            assessment.score = 0.0  # type: ignore[misc]

        for invalid_score in (-0.01, 1.01, float("nan"), float("inf"), True):
            with self.subTest(score=invalid_score), self.assertRaises(ValueError):
                ResearchAssessment(
                    run_id=uuid4().hex,
                    evaluated_at=datetime.now(timezone.utc),
                    score=invalid_score,  # type: ignore[arg-type]
                )

        with self.assertRaises(ValueError):
            ResearchAssessment(
                run_id=uuid4().hex,
                evaluated_at=datetime(2026, 7, 19, 8, 0),
                score=1.0,
            )
        with self.assertRaises(ValueError):
            ResearchAssessment(
                run_id=uuid4().hex,
                evaluated_at=datetime.now(timezone.utc),
                score=1.0,
                schema_version=2,
            )

    def test_serialization_is_json_ready_and_detached(self) -> None:
        assessment = ResearchAssessment(
            run_id=uuid4().hex,
            evaluated_at=datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc),
            score=0.5,
            findings=(
                AssessmentFinding(
                    severity="warning",
                    code="example",
                    message="Example finding.",
                ),
            ),
        )

        serialized = assessment.as_dict()
        json.dumps(serialized, ensure_ascii=False)
        serialized["findings"][0]["message"] = "mutated"
        serialized["findings"].append({"severity": "error", "message": "new"})

        self.assertEqual(assessment.findings[0].message, "Example finding.")
        self.assertEqual(len(assessment.findings), 1)
        self.assertEqual(assessment.as_dict()["schema_version"], 1)
        self.assertEqual(
            assessment.as_dict()["evaluated_at"],
            "2026-07-19T08:00:00+00:00",
        )


class TestOfflineEvaluation(unittest.TestCase):
    """Prove persisted evaluation is deterministic and side-effect free."""

    def test_evaluation_preserves_snapshot_byte_for_byte_and_by_equality(self) -> None:
        snapshot = _make_snapshot(
            output={
                "running_summary": "summary",
                "report_markdown": "# Report",
                "todo_items": [
                    {
                        "id": 1,
                        "title": "Task",
                        "status": "completed",
                        "summary": "Finding",
                    }
                ],
            },
            followup_context={"key_findings": ["Finding"]},
        )
        before = snapshot.as_dict()
        before_bytes = json.dumps(
            before,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

        assessment = OfflineEvaluationService().evaluate(snapshot)

        after = snapshot.as_dict()
        after_bytes = json.dumps(
            after,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertEqual(after, before)
        self.assertEqual(after_bytes, before_bytes)
        self.assertEqual(assessment.run_id, snapshot.run_id)
        self.assertEqual(assessment.score, 1.0)
        self.assertEqual(assessment.findings, ())

    def test_offline_service_reproduces_all_deduction_rules(self) -> None:
        snapshot = _make_snapshot(
            output={
                "running_summary": None,
                "report_markdown": "",
                "todo_items": [
                    {
                        "id": 1,
                        "title": "Bad task",
                        "status": "pending",
                        "summary": "",
                    }
                ],
            },
            followup_context={},
        )

        assessment = OfflineEvaluationService().evaluate(snapshot)

        self.assertAlmostEqual(assessment.score, 0.1)
        self.assertEqual(
            [finding.code for finding in assessment.findings],
            [
                "missing_report",
                "incomplete_tasks",
                "missing_summaries",
                "missing_compressed_context",
            ],
        )

    def test_missing_output_scores_zero(self) -> None:
        assessment = OfflineEvaluationService().evaluate(
            _make_snapshot(output={}, followup_context={"x": 1})
        )

        self.assertEqual(assessment.score, 0.0)
        self.assertEqual(
            [finding.code for finding in assessment.findings],
            ["missing_output"],
        )


class RecordingEvaluationService:
    """Test double proving compatibility calls the canonical evaluator."""

    def __init__(self) -> None:
        self.snapshots: list[RunSnapshot] = []

    def evaluate(self, snapshot: RunSnapshot) -> ResearchAssessment:
        self.snapshots.append(snapshot)
        return ResearchAssessment(
            run_id=snapshot.run_id,
            evaluated_at=datetime.now(timezone.utc),
            score=0.25,
            findings=(
                AssessmentFinding(
                    severity="warning",
                    code="adapter",
                    message="Delegated.",
                ),
            ),
        )


class TestEvaluatorCompatibility(unittest.TestCase):
    """Keep legacy evaluator APIs as projections over offline assessment."""

    def test_context_evaluation_delegates_to_offline_service(self) -> None:
        service = RecordingEvaluationService()
        evaluator = RuleBasedEvaluator(service=service)
        context = _make_context(
            report_markdown="# Report",
            compressed_context={"x": 1},
        )

        result = evaluator.evaluate(context)

        self.assertEqual(len(service.snapshots), 1)
        self.assertEqual(service.snapshots[0].run_id, context.run_id)
        self.assertEqual(result.score, 0.25)
        self.assertEqual([finding.code for finding in result.findings], ["adapter"])
        self.assertNotIn("_evaluate_payload", RuleBasedEvaluator.__dict__)

    def test_record_evaluation_delegates_to_offline_service(self) -> None:
        service = RecordingEvaluationService()
        evaluator = RuleBasedEvaluator(service=service)
        now = datetime.now(timezone.utc)
        record = HarnessRunRecord(
            run_id=uuid4().hex,
            topic="stored topic",
            started_at=now,
            completed_at=now,
            status="completed",
            config_snapshot={},
            metrics={},
            error=None,
            output={"report_markdown": "# Stored", "todo_items": []},
            compressed_context={"reasoning_memory": {}},
        )

        result = evaluator.evaluate_record(record)

        self.assertEqual(len(service.snapshots), 1)
        snapshot_wire = service.snapshots[0].as_dict()
        self.assertEqual(snapshot_wire["output"], record.output)
        self.assertEqual(
            snapshot_wire["followup_context"],
            record.compressed_context,
        )
        self.assertEqual(result.as_dict()["findings"][0]["code"], "adapter")


class TestLegacyScoringCompatibility(unittest.TestCase):
    """Retain the historical scores while the implementation moves offline."""

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
        result = self.evaluator.evaluate(
            _make_context(
                todo_items=tasks,
                report_markdown="# Report",
                compressed_context={"run_summary": {}},
            )
        )

        self.assertEqual(result.score, 1.0)
        self.assertEqual(result.findings, [])

    def test_no_output_scores_zero(self) -> None:
        result = self.evaluator.evaluate(_make_context(has_output=False))

        self.assertEqual(result.score, 0.0)
        self.assertIn("missing_output", [finding.code for finding in result.findings])

    def test_individual_deduction_codes_remain_available(self) -> None:
        tasks = [
            TodoItem(
                id=1,
                title="Incomplete",
                intent="Intent",
                query="q",
                status="in_progress",
                summary="",
            )
        ]
        result = self.evaluator.evaluate(
            _make_context(
                todo_items=tasks,
                report_markdown="",
                compressed_context={},
            )
        )

        codes = [finding.code for finding in result.findings]
        self.assertIn("missing_report", codes)
        self.assertIn("incomplete_tasks", codes)
        self.assertIn("missing_summaries", codes)
        self.assertIn("missing_compressed_context", codes)
        self.assertGreaterEqual(result.score, 0.0)

    def test_no_tasks_deduction_remains_available(self) -> None:
        result = self.evaluator.evaluate(
            _make_context(
                todo_items=[],
                report_markdown="# Report",
                compressed_context={"x": 1},
            )
        )

        self.assertIn("missing_tasks", [finding.code for finding in result.findings])


class TestHarnessCompatibilityBoundaries(unittest.TestCase):
    """Protect redaction, benchmark fixtures, and the reduced public surface."""

    def test_record_from_context_uses_only_safe_configuration_snapshot(self) -> None:
        secrets = {
            "llm_api_key": "llm-secret-sentinel",
            "llm_base_url": "https://secret-llm.invalid/v1",
            "github_token": "github-secret-sentinel",
            "github_api_base_url": "https://secret-github.invalid",
            "notes_workspace": "C:/secret/notes",
        }
        config = Configuration.from_env(secrets)
        context = _make_context(config=config)

        record = HarnessRunRecord.from_context(context)
        serialized = json.dumps(record.as_dict(), ensure_ascii=False)

        self.assertEqual(record.config_snapshot, config.safe_snapshot())
        for secret in secrets.values():
            self.assertNotIn(secret, serialized)

    def test_scenario_builds_canonical_command_as_offline_fixture(self) -> None:
        scenario = HarnessScenario(
            name="offline_quality_fixture",
            topic="deterministic topic",
            description="Offline benchmark fixture.",
        )

        command = scenario.build_request(Configuration.from_env())

        self.assertIs(type(command), ResearchCommand)
        self.assertEqual(command.metadata["scenario"], scenario.name)
        self.assertIn("offline", HarnessScenario.__doc__.lower())
        self.assertIn("benchmark", HarnessScenario.__doc__.lower())

    def test_obsolete_workflow_modules_are_not_public_exports(self) -> None:
        for name in ("ContextManager", "InMemoryEventBus"):
            with self.subTest(name=name):
                self.assertNotIn(name, harness.__all__)
                self.assertFalse(hasattr(harness, name))

    def test_legacy_evaluation_result_shape_is_unchanged(self) -> None:
        result = EvaluationResult(
            score=0.5,
            findings=[
                EvaluationFinding(
                    severity="warning",
                    code="legacy",
                    message="Legacy shape.",
                )
            ],
        )

        self.assertEqual(
            result.as_dict(),
            {
                "score": 0.5,
                "findings": [
                    {
                        "severity": "warning",
                        "message": "Legacy shape.",
                        "code": "legacy",
                    }
                ],
            },
        )


if __name__ == "__main__":
    unittest.main()
