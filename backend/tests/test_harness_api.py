"""HTTP-level tests for the harness-managed research API."""

from __future__ import annotations

import json
import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Iterator
from types import ModuleType, SimpleNamespace

from fastapi.testclient import TestClient

TESTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TESTS_DIR.parent
SRC_DIR = BACKEND_DIR / "src"
for path in (BACKEND_DIR, SRC_DIR):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

if "loguru" not in sys.modules:
    fake_loguru = ModuleType("loguru")

    class _FakeLogger:
        def add(self, *args: Any, **kwargs: Any) -> None:
            return None

        def info(self, *args: Any, **kwargs: Any) -> None:
            return None

        def exception(self, *args: Any, **kwargs: Any) -> None:
            return None

    fake_loguru.logger = _FakeLogger()
    sys.modules["loguru"] = fake_loguru

if "harness" not in sys.modules:
    fake_harness = ModuleType("harness")

    @dataclass(kw_only=True)
    class _HarnessRunRequest:
        topic: str
        config: Any
        metadata: dict[str, Any] = field(default_factory=dict)
        permission_mode: str = "default"
        caller_mode: str = "public"
        run_id: str = "test-run"

    class _HarnessRunner:
        @classmethod
        def build_default(cls, *, base_path: str = "./runs") -> "_HarnessRunner":
            return cls()

        def run(self, request: Any) -> Any:
            raise RuntimeError("Tests should inject a harness runner explicitly.")

        def stream(self, request: Any) -> Iterator[dict[str, Any]]:
            raise RuntimeError("Tests should inject a harness runner explicitly.")

        def load_record(self, run_id: str) -> dict[str, Any]:
            raise FileNotFoundError(run_id)

    @dataclass
    class _Scenario:
        name: str
        topic: str
        description: str
        search_api: Any = None
        metadata: dict[str, Any] = field(default_factory=dict)

    def _build_default_scenarios() -> list[_Scenario]:
        return [
            _Scenario(
                name="smoke_single_topic",
                topic="local llm deep research workflow design",
                description="Basic smoke test covering the happy path.",
            )
        ]

    fake_harness.HarnessRunRequest = _HarnessRunRequest
    fake_harness.HarnessRunner = _HarnessRunner
    fake_harness.build_default_scenarios = _build_default_scenarios
    sys.modules["harness"] = fake_harness

from main import create_app
from models import SummaryStateOutput, TodoItem


@dataclass
class EvaluationFinding:
    """Minimal finding shape for HTTP response tests."""

    severity: str
    message: str
    code: str | None = None


@dataclass
class FakeRunner:
    """Small in-memory runner used by interface tests."""

    base_path: Path
    last_run_request: Any | None = None
    last_stream_request: Any | None = None
    _records: dict[str, dict[str, Any]] = field(default_factory=dict)

    def run(self, request: Any) -> Any:
        self.last_run_request = request
        todo_item = TodoItem(
            id=1,
            title="Test task",
            intent="Validate harness routing",
            query=request.topic,
            status="completed",
            summary="Task summary",
            sources_summary="* Source A : https://example.com",
        )
        result = SimpleNamespace(
            run_id="run-sync-001",
            status="completed",
            output=SummaryStateOutput(
                running_summary="Final report",
                report_markdown="Final report",
                todo_items=[todo_item],
            ),
            metrics={"duration_seconds": 0.1, "evaluation_score": 1.0},
            findings=[
                EvaluationFinding(
                    severity="warning",
                    code="example",
                    message="Example finding",
                )
            ],
            compressed_context={
                "run_summary": {
                    "completed_tasks": [{"task_id": 1, "title": "Test task"}],
                    "incomplete_tasks": [],
                    "report_excerpt": "Final report",
                },
                "reasoning_memory": {
                    "key_findings": ["Task summary"],
                    "key_sources": ["* Source A : https://example.com"],
                    "open_questions": [],
                },
            },
            policy_decisions=[
                {
                    "capability": "research:run",
                    "outcome": "allow",
                    "reason": "ok",
                }
            ],
        )
        self._records[result.run_id] = {
            "run_id": result.run_id,
            "status": result.status,
            "metrics": result.metrics,
            "compressed_context": result.compressed_context,
            "policy_decisions": result.policy_decisions,
            "output": {
                "report_markdown": result.output.report_markdown,
                "todo_items": [
                    {
                        "id": todo_item.id,
                        "title": todo_item.title,
                        "status": todo_item.status,
                    }
                ],
            },
            "evaluation": {
                "score": 1.0,
                "findings": [
                    {
                        "severity": "warning",
                        "code": "example",
                        "message": "Example finding",
                    }
                ],
            },
        }
        return result

    def stream(self, request: Any) -> Iterator[dict[str, Any]]:
        self.last_stream_request = request
        yield {
            "type": "status",
            "message": "starting",
            "run_id": "run-sync-001",
        }
        yield {
            "type": "todo_list",
            "run_id": "run-sync-001",
            "tasks": [
                {
                    "id": 1,
                    "title": "Stream task",
                    "intent": "Stream intent",
                    "query": request.topic,
                    "status": "pending",
                }
            ],
        }
        yield {
            "type": "final_report",
            "run_id": "run-sync-001",
            "report": "stream report",
        }
        yield {"type": "done", "run_id": "run-sync-001"}

    def load_record(self, run_id: str) -> dict[str, Any]:
        if run_id not in self._records:
            raise FileNotFoundError(run_id)
        return self._records[run_id]


class HarnessApiTests(unittest.TestCase):
    """Covers the public and internal harness-managed HTTP routes."""

    def setUp(self) -> None:
        self.tmpdir = TemporaryDirectory()
        self.runner = FakeRunner(base_path=Path(self.tmpdir.name))
        self.client = TestClient(create_app(harness_runner=self.runner))

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_research_endpoint_uses_harness_runner(self) -> None:
        response = self.client.post(
            "/research",
            json={"topic": "test topic"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["report_markdown"], "Final report")
        self.assertEqual(payload["todo_items"][0]["title"], "Test task")
        self.assertEqual(self.runner.last_run_request.topic, "test topic")
        self.assertEqual(self.runner.last_run_request.caller_mode, "public")

    def test_internal_harness_endpoint_returns_governance_fields(self) -> None:
        response = self.client.post(
            "/harness/run",
            json={
                "topic": "internal topic",
                "permission_mode": "strict",
                "metadata": {"suite": "api"},
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["run_id"], "run-sync-001")
        self.assertEqual(payload["mode"], "internal")
        self.assertIn("compressed_context", payload)
        self.assertIn("policy_decisions", payload)
        self.assertEqual(self.runner.last_run_request.permission_mode, "strict")
        self.assertEqual(self.runner.last_run_request.metadata["suite"], "api")

    def test_research_stream_returns_sse_events_with_run_id(self) -> None:
        with self.client.stream(
            "POST",
            "/research/stream",
            json={"topic": "stream topic"},
        ) as response:
            self.assertEqual(response.status_code, 200)
            lines = [
                line
                for line in response.iter_lines()
                if line and line.startswith("data:")
            ]

        events = [json.loads(line[5:].strip()) for line in lines]
        self.assertEqual(events[0]["type"], "status")
        self.assertEqual(events[0]["run_id"], "run-sync-001")
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(self.runner.last_stream_request.topic, "stream topic")
        self.assertEqual(self.runner.last_stream_request.caller_mode, "public")

    def test_run_record_lookup_returns_persisted_payload(self) -> None:
        self.client.post("/harness/run", json={"topic": "record topic"})

        response = self.client.get("/harness/runs/run-sync-001")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["run_id"], "run-sync-001")
        self.assertEqual(payload["evaluation"]["score"], 1.0)

    def test_scenarios_endpoint_returns_seed_scenarios(self) -> None:
        response = self.client.get("/harness/scenarios")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertGreaterEqual(len(payload), 1)
        self.assertIn("name", payload[0])
        self.assertIn("topic", payload[0])


if __name__ == "__main__":
    unittest.main()
