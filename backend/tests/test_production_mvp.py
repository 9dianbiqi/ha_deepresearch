"""Focused HTTP tests for the production single-instance MVP boundary."""

from __future__ import annotations

import asyncio
import os
from threading import Event
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import patch

from fastapi.testclient import TestClient

from main import _iter_sse_events, create_app

AUTH_HEADERS = {"Authorization": "Bearer test-app-key"}


def _completed_result() -> SimpleNamespace:
    """Return the smallest successful result accepted by the public route."""
    return SimpleNamespace(
        status="completed",
        output=SimpleNamespace(
            report_markdown="report",
            running_summary="",
            todo_items=[],
            research_mode=None,
            research_profile_id=None,
            source_context={},
            research_intelligence={},
            github_intelligence={},
        ),
    )


class SlotRunner:
    """Deterministic runner whose failure and stream behavior can be changed."""

    def __init__(self) -> None:
        self.fail_sync = False
        self.stream_mode = "normal"

    def run(self, _request: Any) -> SimpleNamespace:
        if self.fail_sync:
            raise RuntimeError("runner failure")
        return _completed_result()

    def stream(self, _request: Any) -> Iterator[dict[str, Any]]:
        yield {"type": "status", "run_id": "run-1"}
        if self.stream_mode == "error":
            raise RuntimeError("stream failure")
        yield {"type": "done", "run_id": "run-1"}


def _client(runner: SlotRunner) -> TestClient:
    """Build an authenticated client with one process-local run slot."""
    with patch.dict(os.environ, {"APP_API_KEY": "test-app-key", "MAX_CONCURRENT_RUNS": "1"}, clear=False):
        app = create_app(harness_runner=runner)
    return TestClient(app, headers=AUTH_HEADERS)


def test_missing_or_unconfigured_key_is_always_unauthorized() -> None:
    """Protected routes fail closed while probes and CORS preflight remain public."""
    with patch.dict(os.environ, {"APP_API_KEY": ""}, clear=False):
        with TestClient(create_app(harness_runner=SlotRunner())) as client:
            missing = client.get("/harness/scenarios")
            health = client.get("/healthz")
            preflight = client.options(
                "/research",
                headers={
                    "Origin": "http://localhost:5173",
                    "Access-Control-Request-Method": "POST",
                },
            )

    assert missing.status_code == 401
    assert missing.json()["detail"]["code"] == "unauthorized"
    assert health.status_code == 200
    assert preflight.status_code == 200


def test_readiness_returns_503_when_durable_dependency_is_unavailable() -> None:
    """A runner without durable storage is not ready to receive traffic."""
    with TestClient(create_app(harness_runner=SlotRunner())) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "not_ready"


def test_sync_success_and_exception_release_the_run_slot() -> None:
    """Both normal and exceptional synchronous exits release admission."""
    runner = SlotRunner()
    with _client(runner) as client:
        assert client.post("/research", json={"topic": "first"}).status_code == 200

        runner.fail_sync = True
        assert client.post("/research", json={"topic": "failure"}).status_code == 500

        runner.fail_sync = False
        assert client.post("/research", json={"topic": "after failure"}).status_code == 200


def test_stream_success_and_exception_release_the_run_slot() -> None:
    """SSE terminal and error paths both release admission."""
    runner = SlotRunner()
    with _client(runner) as client:
        with client.stream("POST", "/research/stream", json={"topic": "stream"}) as response:
            assert response.status_code == 200
            assert '"type": "done"' in response.read().decode()

        runner.stream_mode = "error"
        with client.stream("POST", "/research/stream", json={"topic": "stream error"}) as response:
            assert response.status_code == 200
            assert '"type": "error"' in response.read().decode()

        assert client.post("/research", json={"topic": "after stream"}).status_code == 200


class CloseAwareIterator:
    """Iterator used to exercise the async SSE generator close boundary."""

    def __init__(self) -> None:
        self.first = True
        self.closed = Event()

    def __iter__(self) -> CloseAwareIterator:
        return self

    def __next__(self) -> dict[str, str]:
        if self.first:
            self.first = False
            return {"type": "status", "run_id": "run-1"}
        self.closed.wait(timeout=5)
        raise StopIteration

    def close(self) -> None:
        self.closed.set()


def test_sse_generator_close_releases_slot_after_client_disconnect() -> None:
    """Closing an active SSE generator invokes its release callback."""
    iterator = CloseAwareIterator()
    released = Event()

    async def exercise_close() -> None:
        stream = _iter_sse_events(
            None,
            None,
            stream_factory=lambda: iterator,
            run_id="run-1",
            on_close=released.set,
        )
        first = await stream.__anext__()
        assert '"type": "status"' in first
        await stream.aclose()

    asyncio.run(exercise_close())
    assert iterator.closed.is_set()
    assert released.is_set()
