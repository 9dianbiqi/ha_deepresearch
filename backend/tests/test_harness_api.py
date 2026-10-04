"""HTTP-level tests for the harness-managed research API."""

from __future__ import annotations

import importlib
import json
import sys
import traceback
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import patch

from fastapi.testclient import TestClient

TESTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TESTS_DIR.parent
SRC_DIR = BACKEND_DIR / "src"
for path in (BACKEND_DIR, SRC_DIR):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

main_module = importlib.import_module("main")
create_app = main_module.create_app
_models = importlib.import_module("models")
SummaryStateOutput = _models.SummaryStateOutput
TodoItem = _models.TodoItem
_artifacts = importlib.import_module("research.artifacts")
ArtifactPayload = _artifacts.ArtifactPayload
FileArtifactStore = _artifacts.FileArtifactStore
_repository = importlib.import_module("research.repository")
CorruptRunRecordError = _repository.CorruptRunRecordError
FileRunRepository = _repository.FileRunRepository
InvalidRunIdError = _repository.InvalidRunIdError
_contracts = importlib.import_module("research.contracts")
RunSnapshot = _contracts.RunSnapshot
RunStatus = _contracts.RunStatus


def test_configuration_log_presence_never_returns_secret_material() -> None:
    """Keep startup diagnostics informational without revealing credentials."""
    secret = "user:password@private.example/v1?token=raw-secret"
    rendered = main_module._configuration_presence(secret)
    assert rendered == "configured"
    assert not any(part in rendered for part in ("user", "password", "token"))


def test_default_app_sweeps_once_at_startup_but_never_during_creation(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Module-style app construction must not touch cache before ASGI startup."""
    assert hasattr(main_module, "sweep_search_cache"), (
        "default app startup cache sweep has not been composed"
    )
    runner = FakeRunner(base_path=tmp_path / "runs")
    config = main_module.Configuration(
        notes_workspace=str(tmp_path / "notes"),
        enable_notes=False,
    )
    swept: list[object] = []
    monkeypatch.setattr(
        main_module.HarnessRunner,
        "build_default",
        staticmethod(lambda *, base_path: runner),
    )
    monkeypatch.setattr(
        main_module.Configuration,
        "from_env",
        classmethod(lambda cls: config),
    )
    monkeypatch.setattr(
        main_module,
        "sweep_search_cache",
        lambda received: swept.append(received),
    )

    app = create_app()
    assert swept == []

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200

    assert swept == [config]


def test_custom_runner_startup_never_sweeps_search_cache(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Tests and embedded callers own cleanup when supplying a custom runner."""
    assert hasattr(main_module, "sweep_search_cache"), (
        "default app startup cache sweep has not been composed"
    )
    runner = FakeRunner(base_path=tmp_path / "runs")
    config = main_module.Configuration(
        notes_workspace=str(tmp_path / "notes"),
        enable_notes=False,
    )
    swept: list[object] = []
    monkeypatch.setattr(
        main_module.Configuration,
        "from_env",
        classmethod(lambda cls: config),
    )
    monkeypatch.setattr(
        main_module,
        "sweep_search_cache",
        lambda received: swept.append(received),
    )

    with TestClient(create_app(harness_runner=runner)) as client:
        assert client.get("/healthz").status_code == 200

    assert swept == []


def test_default_app_cache_sweep_failure_is_safe_and_does_not_block_startup(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Startup isolates sweep failures without logging exception or path details."""
    assert hasattr(main_module, "sweep_search_cache"), (
        "default app startup cache sweep has not been composed"
    )
    runner = FakeRunner(base_path=tmp_path / "runs")
    config = main_module.Configuration(
        notes_workspace=str(tmp_path / "notes-secret-sentinel"),
        enable_notes=False,
    )
    monkeypatch.setattr(
        main_module.HarnessRunner,
        "build_default",
        staticmethod(lambda *, base_path: runner),
    )
    monkeypatch.setattr(
        main_module.Configuration,
        "from_env",
        classmethod(lambda cls: config),
    )

    def fail_sweep(_config: object) -> None:
        raise RuntimeError("SWEEP_EXCEPTION_SECRET_SENTINEL")

    monkeypatch.setattr(main_module, "sweep_search_cache", fail_sweep)
    rendered: list[str] = []
    sink_id = main_module.logger.add(rendered.append, format="{message}")
    try:
        with TestClient(create_app()) as client:
            assert client.get("/healthz").status_code == 200
    finally:
        main_module.logger.remove(sink_id)

    log_text = "".join(rendered)
    assert "Search cache startup sweep failed." in log_text
    assert "SWEEP_EXCEPTION_SECRET_SENTINEL" not in log_text
    assert "notes-secret-sentinel" not in log_text


def test_direct_server_defaults_to_loopback_and_validates_port() -> None:
    """Keep the unauthenticated development server local unless explicitly exposed."""
    with patch.dict("os.environ", {}, clear=True):
        assert main_module._server_bind_config() == ("127.0.0.1", 8000)
    with patch.dict("os.environ", {"HOST": "0.0.0.0", "PORT": "9000"}, clear=True):
        assert main_module._server_bind_config() == ("0.0.0.0", 9000)
    with patch.dict("os.environ", {"PORT": "70000"}, clear=True):
        with unittest.TestCase().assertRaisesRegex(ValueError, "PORT"):
            main_module._server_bind_config()


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
        if "github.com" in request.topic:
            yield {
                "type": "github_repository",
                "run_id": "run-sync-001",
                "repository": {
                    "full_name": "bytedance/deer-flow",
                    "url": "https://github.com/bytedance/deer-flow",
                    "stars": 100,
                },
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
        if run_id == "invalid-id":
            raise InvalidRunIdError("must not leak invalid path")
        if run_id == "corrupt-id":
            raise CorruptRunRecordError("Authorization: Bearer record-secret")
        if run_id == "unexpected-id":
            raise RuntimeError("Authorization: Bearer repository-secret")
        if run_id not in self._records:
            raise FileNotFoundError(run_id)
        return self._records[run_id]


class HarnessApiTests(unittest.TestCase):
    """Covers the public and internal harness-managed HTTP routes."""

    def setUp(self) -> None:
        self.tmpdir = TemporaryDirectory()
        self.runner = FakeRunner(base_path=Path(self.tmpdir.name))
        self.client = TestClient(
            create_app(harness_runner=self.runner),
            headers={"Authorization": "Bearer test-app-key"},
        )

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_cors_allows_only_configured_origins(self) -> None:
        """Reject browser preflight from an origin outside the local allowlist."""
        with patch.dict(
            "os.environ",
            {"CORS_ORIGINS": "http://localhost:5174"},
            clear=False,
        ):
            client = TestClient(create_app(harness_runner=self.runner))

        allowed = client.options(
            "/research",
            headers={
                "Origin": "http://localhost:5174",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        rejected = client.options(
            "/research",
            headers={
                "Origin": "https://attacker.example",
                "Access-Control-Request-Method": "POST",
            },
        )

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(
            allowed.headers.get("access-control-allow-origin"),
            "http://localhost:5174",
        )
        self.assertNotIn("access-control-allow-origin", rejected.headers)
        self.assertGreaterEqual(rejected.status_code, 400)

    def test_cors_wildcard_configuration_is_rejected(self) -> None:
        """Never turn the unauthenticated localhost API into a wildcard endpoint."""
        with patch.dict("os.environ", {"CORS_ORIGINS": "*"}, clear=False):
            with self.assertRaisesRegex(ValueError, "explicit origins"):
                create_app(harness_runner=self.runner)

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
        self.assertTrue(events[-1]["stream_telemetry"]["stream_completed"])
        self.assertGreaterEqual(events[-1]["stream_telemetry"]["event_count"], 4)
        self.assertGreater(events[-1]["stream_telemetry"]["bytes_sent"], 0)
        self.assertEqual(self.runner.last_stream_request.topic, "stream topic")
        self.assertEqual(self.runner.last_stream_request.caller_mode, "public")

    def test_research_stream_preserves_github_repository_events(self) -> None:
        with self.client.stream(
            "POST",
            "/research/stream",
            json={"topic": "https://github.com/bytedance/deer-flow"},
        ) as response:
            self.assertEqual(response.status_code, 200)
            lines = [
                line
                for line in response.iter_lines()
                if line and line.startswith("data:")
            ]

        events = [json.loads(line[5:].strip()) for line in lines]
        github_events = [
            item for item in events if item.get("type") == "github_repository"
        ]
        self.assertEqual(len(github_events), 1)
        self.assertEqual(
            github_events[0]["repository"]["full_name"],
            "bytedance/deer-flow",
        )
        self.assertEqual(events[-1]["type"], "done")

    def test_run_record_lookup_returns_persisted_payload(self) -> None:
        self.client.post("/harness/run", json={"topic": "record topic"})

        response = self.client.get("/harness/runs/run-sync-001")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["run_id"], "run-sync-001")
        self.assertEqual(payload["evaluation"]["score"], 1.0)

    def test_artifact_download_survives_new_app_instance_and_checks_ownership(self) -> None:
        """Artifact bodies remain downloadable after rebuilding the service."""
        root = Path(self.tmpdir.name) / "durable-data"
        run_id = "12345678-1234-5678-1234-567812345678"

        class PersistentRunner:
            def __init__(self) -> None:
                self.repository = FileRunRepository(root)
                self.artifact_store = FileArtifactStore(self.repository)

            def load_record(self, requested_run_id: str) -> dict[str, Any]:
                return self.repository.load(requested_run_id).as_dict()

        runner = PersistentRunner()
        descriptor = runner.artifact_store.put(
            run_id,
            ArtifactPayload(
                artifact_id="artifact_report_markdown",
                artifact_type="report_markdown",
                mime_type="text/markdown",
                title="Report",
                content="# Durable report\n",
            ),
        )
        now = datetime.now(timezone.utc)
        runner.repository.save(
            RunSnapshot(
                run_id=run_id,
                topic="durable artifact",
                status=RunStatus.COMPLETED,
                started_at=now,
                completed_at=now,
                parent_run_id=None,
                output={
                    "report_markdown": "# Durable report\n",
                    "research_intelligence": {
                        "schema_version": 2,
                        "mode": "web",
                        "profile_id": "web.general",
                        "profile_version": 1,
                        "sources": [],
                        "evidence": [],
                        "claims": [],
                        "coverage": {},
                        "report_spec": {"title": "Report"},
                        "artifact_manifest": {
                            "schema_version": 2,
                            "artifacts": [descriptor.as_dict()],
                        },
                        "evidence_frozen": True,
                    },
                },
                followup_context={},
                metrics={},
                policy_decisions=(),
                config_snapshot={},
                events=(),
            )
        )

        with TestClient(
            create_app(harness_runner=runner),
            headers={"Authorization": "Bearer test-app-key"},
        ) as client:
            first = client.get(f"/runs/{run_id}/artifacts/{descriptor.artifact_id}")
            missing = client.get(f"/runs/{run_id}/artifacts/not-owned")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.content, b"# Durable report\n")
        self.assertTrue(first.headers["content-type"].startswith("text/markdown"))
        self.assertIn("filename=\"report.md\"", first.headers["content-disposition"])
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["detail"]["code"], "artifact_not_found")

        with TestClient(
            create_app(harness_runner=PersistentRunner()),
            headers={"Authorization": "Bearer test-app-key"},
        ) as client:
            restarted = client.get(
                f"/runs/{run_id}/artifacts/{descriptor.artifact_id}"
            )

        self.assertEqual(restarted.status_code, 200)
        self.assertEqual(restarted.content, b"# Durable report\n")

    def test_readiness_probe_checks_durable_and_artifact_directories(self) -> None:
        """The readiness probe verifies the same root used by the store."""
        repository = FileRunRepository(Path(self.tmpdir.name) / "ready-data")
        runner = SimpleNamespace(
            repository=repository,
            artifact_store=FileArtifactStore(repository),
        )
        with TestClient(create_app(harness_runner=runner)) as client:
            self.assertEqual(client.get("/healthz").status_code, 200)
            ready = client.get("/readyz")

        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.json(), {"status": "ready"})
        self.assertTrue(repository.root.is_dir())
        self.assertTrue(repository.runs_dir.is_dir())
        self.assertTrue(runner.artifact_store.artifact_root.is_dir())

    def test_configured_bearer_key_protects_routes_but_not_probes(self) -> None:
        """Health probes stay public while application routes require the key."""
        secret = "test-app-key-not-for-logs"
        with patch.dict("os.environ", {"APP_API_KEY": secret}, clear=False):
            with TestClient(create_app(harness_runner=self.runner)) as client:
                health = client.get("/healthz")
                readiness = client.get("/readyz")
                missing = client.get("/harness/scenarios")
                wrong = client.get(
                    "/harness/scenarios",
                    headers={"Authorization": "Bearer wrong-key"},
                )
                valid = client.get(
                    "/harness/scenarios",
                    headers={"Authorization": f"Bearer {secret}"},
                )

        self.assertEqual(health.status_code, 200)
        self.assertNotEqual(readiness.status_code, 401)
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(valid.status_code, 200)
        self.assertNotIn(secret, missing.text)
        self.assertNotIn(secret, wrong.text)

    def test_run_capacity_returns_429_without_waiting(self) -> None:
        """A second top-level run is rejected while the configured slot is active."""
        started = Event()
        release = Event()

        class BlockingRunner(FakeRunner):
            def run(self, request: Any) -> Any:
                started.set()
                release.wait(timeout=5)
                return super().run(request)

        runner = BlockingRunner(base_path=Path(self.tmpdir.name))
        with patch.dict("os.environ", {"MAX_CONCURRENT_RUNS": "1"}, clear=False):
            with TestClient(
                create_app(harness_runner=runner),
                headers={"Authorization": "Bearer test-app-key"},
            ) as client:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    first_future = executor.submit(
                        client.post,
                        "/research",
                        json={"topic": "first"},
                    )
                    self.assertTrue(started.wait(timeout=5))
                    second = client.post("/research", json={"topic": "second"})
                    release.set()
                    first = first_future.result(timeout=5)

        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.json()["detail"]["code"], "capacity_exceeded")
        self.assertEqual(first.status_code, 200)

    def test_request_limits_return_stable_errors_without_echoing_payloads(self) -> None:
        """Topic and metadata limits do not reflect user text in validation errors."""
        long_topic = "TOPIC_SENTINEL " + "x" * main_module._MAX_TOPIC_LENGTH
        long_metadata = {"prompt": "METADATA_SENTINEL " + "x" * 20_000}

        topic_response = self.client.post("/research", json={"topic": long_topic})
        metadata_response = self.client.post(
            "/harness/run",
            json={"topic": "bounded", "metadata": long_metadata},
        )

        self.assertEqual(topic_response.status_code, 422)
        self.assertEqual(topic_response.json()["detail"]["code"], "invalid_request")
        self.assertEqual(metadata_response.status_code, 422)
        self.assertEqual(metadata_response.json()["detail"]["code"], "invalid_request")
        self.assertNotIn("TOPIC_SENTINEL", topic_response.text)
        self.assertNotIn("METADATA_SENTINEL", metadata_response.text)

    def test_canonical_run_lookup_and_deprecated_alias_share_implementation(self) -> None:
        self.client.post("/harness/run", json={"topic": "record topic"})

        canonical = self.client.get("/runs/run-sync-001")
        legacy = self.client.get("/harness/runs/run-sync-001")

        self.assertEqual(canonical.status_code, 200)
        self.assertEqual(canonical.json(), legacy.json())
        schema = self.client.get("/openapi.json").json()
        self.assertTrue(
            schema["paths"]["/harness/runs/{run_id}"]["get"]["deprecated"]
        )

    def test_run_lookup_maps_missing_invalid_and_corrupt_records_safely(self) -> None:
        cases = [
            ("missing-id", 404, "run_not_found"),
            ("invalid-id", 400, "invalid_run_id"),
            ("corrupt-id", 500, "corrupt_run_record"),
            ("unexpected-id", 500, "repository_error"),
        ]

        for run_id, status_code, code in cases:
            with self.subTest(run_id=run_id):
                response = self.client.get(f"/runs/{run_id}")
                self.assertEqual(response.status_code, status_code)
                self.assertEqual(response.json()["detail"]["code"], code)
                self.assertNotIn("record-secret", response.text)

    def test_sync_routes_map_typed_result_errors_without_raw_detail(self) -> None:
        cases = [
            ("invalid_command", "failed", 400),
            ("policy_rejected", "rejected", 403),
            ("operation_rejected", "rejected", 403),
            ("parent_not_found", "failed", 404),
            ("parent_not_resumable", "failed", 409),
            ("parent_pending", "failed", 409),
            ("deadline_exceeded", "cancelled", 408),
            ("persistence_failed", "failed", 500),
            ("report_incomplete", "report_incomplete", 500),
        ]

        for error_code, status, expected_status in cases:
            with self.subTest(error_code=error_code):
                self.runner.run = lambda request, code=error_code, state=status: SimpleNamespace(
                    run_id=request.run_id,
                    status=state,
                    output=None,
                    error="Authorization: Bearer result-secret",
                    error_code=code,
                    metrics={},
                    findings=[],
                    compressed_context={},
                    policy_decisions=[],
                )
                public = self.client.post("/research", json={"topic": "topic"})
                internal = self.client.post("/harness/run", json={"topic": "topic"})
                self.assertEqual(public.status_code, expected_status)
                self.assertEqual(internal.status_code, expected_status)
                self.assertNotIn("result-secret", public.text)
                self.assertNotIn("result-secret", internal.text)

    def test_stream_routes_share_one_safe_error_boundary_and_close_iterator(self) -> None:
        sentinel = "Authorization: Bearer stream-secret RAW_BODY"
        created: list[Any] = []
        logged: list[str] = []

        class RecordingLogger:
            def exception(self, message: str, *args: Any, **kwargs: Any) -> None:
                logged.append(f"{message}\n{traceback.format_exc()}")

            def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
                logged.append(message)

        class ExplodingIterator:
            def __init__(self, run_id: str) -> None:
                self.run_id = run_id
                self.index = 0
                self.closed = False

            def __iter__(self):
                return self

            def __next__(self) -> dict[str, Any]:
                if self.index == 0:
                    self.index += 1
                    return {
                        "type": "status",
                        "run_id": self.run_id,
                        "schema_version": 1,
                        "sequence": 1,
                    }
                raise RuntimeError(sentinel)

            def close(self) -> None:
                self.closed = True

        def stream(request: Any) -> Any:
            self.runner.last_stream_request = request
            iterator = ExplodingIterator(request.run_id)
            created.append(iterator)
            return iterator

        self.runner.stream = stream
        observed: list[dict[str, Any]] = []
        with patch.object(main_module, "logger", RecordingLogger()):
            for endpoint, payload in [
                ("/research/stream", {"topic": "topic"}),
                (
                    "/research/continue/stream",
                    {"topic": "topic", "parent_run_id": "0" * 32},
                ),
            ]:
                with self.client.stream("POST", endpoint, json=payload) as response:
                    events = [
                        json.loads(line[5:].strip())
                        for line in response.iter_lines()
                        if line and line.startswith("data:")
                    ]
                observed.append(events[-1])
                self.assertEqual(events[-1]["type"], "error")
                self.assertEqual(events[-1]["code"], "stream_failed")
                self.assertEqual(events[-1]["schema_version"], 1)
                self.assertIn("run_id", events[-1])
                self.assertFalse(any(item["type"] == "done" for item in events))
                self.assertFalse(events[-1]["stream_telemetry"]["stream_completed"])
                self.assertEqual(events[-1]["stream_telemetry"]["terminal_type"], "error")
                self.assertNotIn("stream-secret", json.dumps(events))

        self.assertTrue(all(iterator.closed for iterator in created))
        self.assertEqual(observed[0]["detail"], observed[1]["detail"])
        self.assertNotIn("stream-secret", "\n".join(logged))

    def test_stream_stops_at_runner_terminal_without_appending_fallback_done(self) -> None:
        sentinel = "must not iterate after terminal"

        class DoneThenExplode:
            def __init__(self, run_id: str) -> None:
                self.run_id = run_id
                self.index = 0
                self.closed = False

            def __iter__(self):
                return self

            def __next__(self) -> dict[str, Any]:
                if self.index == 0:
                    self.index += 1
                    return {
                        "type": "done",
                        "run_id": self.run_id,
                        "schema_version": 1,
                        "sequence": 1,
                    }
                raise RuntimeError(sentinel)

            def close(self) -> None:
                self.closed = True

        created: list[DoneThenExplode] = []

        def stream(request: Any) -> DoneThenExplode:
            iterator = DoneThenExplode(request.run_id)
            created.append(iterator)
            return iterator

        self.runner.stream = stream
        with self.client.stream(
            "POST",
            "/research/stream",
            json={"topic": "topic"},
        ) as response:
            events = [
                json.loads(line[5:].strip())
                for line in response.iter_lines()
                if line and line.startswith("data:")
            ]

        self.assertEqual([item["type"] for item in events], ["done"])
        self.assertTrue(created[0].closed)

    def test_stream_close_failure_does_not_replace_terminal_event(self) -> None:
        sentinel = "Authorization: Bearer close-secret"
        logged: list[str] = []

        class RecordingLogger:
            def exception(self, message: str, *args: Any, **kwargs: Any) -> None:
                logged.append(f"{message}\n{traceback.format_exc()}")

            def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
                logged.append(message)

        class TerminalThenCloseFailure:
            def __init__(self, run_id: str, terminal_type: str) -> None:
                self.run_id = run_id
                self.terminal_type = terminal_type
                self.emitted = False
                self.closed = False

            def __iter__(self):
                return self

            def __next__(self) -> dict[str, Any]:
                if not self.emitted:
                    self.emitted = True
                    return {
                        "type": self.terminal_type,
                        "run_id": self.run_id,
                        "schema_version": 1,
                        "sequence": 1,
                    }
                raise StopIteration

            def close(self) -> None:
                self.closed = True
                raise RuntimeError(sentinel)

        for terminal_type in ("done", "error"):
            with self.subTest(terminal_type=terminal_type):
                created: list[TerminalThenCloseFailure] = []

                def stream(request: Any) -> TerminalThenCloseFailure:
                    iterator = TerminalThenCloseFailure(request.run_id, terminal_type)
                    created.append(iterator)
                    return iterator

                self.runner.stream = stream
                with patch.object(main_module, "logger", RecordingLogger()):
                    with self.client.stream(
                        "POST",
                        "/research/stream",
                        json={"topic": "topic"},
                    ) as response:
                        events = [
                            json.loads(line[5:].strip())
                            for line in response.iter_lines()
                            if line and line.startswith("data:")
                        ]

                self.assertEqual([item["type"] for item in events], [terminal_type])
                self.assertTrue(created[0].closed)

        self.assertNotIn("close-secret", "\n".join(logged))

    def test_scenarios_endpoint_returns_seed_scenarios(self) -> None:
        response = self.client.get("/harness/scenarios")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertGreaterEqual(len(payload), 1)
        self.assertIn("name", payload[0])
        self.assertIn("topic", payload[0])


if __name__ == "__main__":
    unittest.main()
