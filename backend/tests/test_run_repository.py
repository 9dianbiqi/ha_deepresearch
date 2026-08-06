"""Tests for durable, redacted research run persistence."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest

from config import Configuration
from harness.evaluator import EvaluationResult
from harness.models import RecorderConfig
from harness.recorder import JsonlRunRecorder
from models import ResearchState, TodoItem
from research.contracts import (
    EventKind,
    ResearchCommand,
    ResearchEvent,
    RunError,
    RunSnapshot,
    RunStatus,
)
from research.repository import (
    SAFE_CONFIG_FIELDS,
    CorruptRunRecordError,
    FileRunRepository,
    InvalidRunIdError,
    RunNotFoundError,
    RunRepositoryError,
    UnsupportedSchemaError,
)
from research.session import RunSession


@pytest.fixture
def completed_session() -> RunSession:
    """Build a completed session using fake sensitive configuration values."""
    config = Configuration(
        llm_provider="custom",
        llm_model_id="fake-model",
        llm_reporter_model_id="fake-reporter",
        llm_api_key="fake-secret-llm",
        llm_base_url="https://fake-sensitive-llm.invalid/v1",
        github_token="fake-secret-github",
        github_api_base_url="https://fake-sensitive-github.invalid",
        notes_workspace="fake-sensitive-workspace",
        run_timeout_seconds=123,
    )
    command = ResearchCommand(
        topic="repository topic",
        config=config,
        metadata={"prompt": "fake-sensitive-prompt"},
    )
    session = RunSession(
        command=command,
        state=ResearchState(research_topic=command.topic),
    )
    session.start()
    session.install_plan(
        [
            TodoItem(
                id=1,
                title="Completed task",
                intent="Test persistence",
                query="safe query",
                notices=["Cached source used"],
                retry_count=2,
                refined_queries=["refined safe query"],
            )
        ]
    )
    session.start_task(1)
    session.complete_task(
        1,
        summary="Canonical finding",
        sources_summary="Canonical source summary",
    )
    session.set_report("# Canonical report")
    session.followup_context = {
        "schema_version": 1,
        "source_run_id": session.run_id,
        "key_findings": ["Canonical finding"],
        "key_sources": ["Canonical source summary"],
        "open_questions": [],
    }
    session.metrics = {"duration_seconds": 1.25, "event_count": 5}
    session.policy_decisions = [
        {"capability": "research:run", "outcome": "allow", "reason": "safe"}
    ]
    prepared = session.prepare_terminal(
        RunStatus.COMPLETED,
        EventKind.RUN_COMPLETED,
    )
    session.confirm_terminal(prepared)
    return session


@pytest.fixture
def snapshot(completed_session: RunSession) -> RunSnapshot:
    """Return the completed session's canonical snapshot."""
    return completed_session.to_snapshot()


def test_snapshot_configuration_uses_exact_allowlist(
    tmp_path,
    completed_session: RunSession,
) -> None:
    repository = FileRunRepository(tmp_path)
    repository.save(completed_session.to_snapshot())

    run_path = tmp_path / "runs" / f"{completed_session.run_id}.json"
    raw = run_path.read_text(encoding="utf-8")
    envelope = json.loads(raw)
    loaded = repository.load(completed_session.run_id)

    assert set(envelope) == {"schema_version", "snapshot", "followup_context"}
    assert envelope["schema_version"] == 1
    assert set(loaded.config_snapshot) == set(SAFE_CONFIG_FIELDS)
    assert loaded.config_snapshot["run_timeout_seconds"] == 123
    for sentinel in (
        "fake-secret-llm",
        "fake-secret-github",
        "fake-sensitive-llm.invalid",
        "fake-sensitive-github.invalid",
        "fake-sensitive-workspace",
        "fake-sensitive-prompt",
        "llm_api_key",
        "github_token",
        "llm_base_url",
        "github_api_base_url",
        "notes_workspace",
    ):
        assert sentinel not in raw


@pytest.mark.parametrize(
    "value",
    ["../escape", "..\\escape", "C:\\escape", "bad.json"],
)
def test_repository_rejects_path_like_run_ids(tmp_path, value: str) -> None:
    with pytest.raises(InvalidRunIdError):
        FileRunRepository(tmp_path).load(value)


def test_failed_replace_keeps_previous_snapshot_readable(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    snapshot: RunSnapshot,
) -> None:
    repository = FileRunRepository(tmp_path)
    repository.save(snapshot)
    monkeypatch.setattr(os, "replace", Mock(side_effect=OSError("replace failed")))

    with pytest.raises(RunRepositoryError, match="replace failed"):
        repository.save(replace(snapshot, metrics={"new": True}))

    assert FileRunRepository(tmp_path).load(snapshot.run_id).metrics == snapshot.metrics
    assert list((tmp_path / "runs").iterdir()) == [
        tmp_path / "runs" / f"{snapshot.run_id}.json"
    ]


def test_concurrent_repository_instances_always_read_schema_v1(
    tmp_path,
    snapshot: RunSnapshot,
) -> None:
    FileRunRepository(tmp_path).save(snapshot)

    def save_and_load(worker: int) -> list[int]:
        observed: list[int] = []
        for iteration in range(8):
            repository = FileRunRepository(tmp_path)
            repository.save(
                replace(
                    snapshot,
                    metrics={"worker": worker, "iteration": iteration},
                )
            )
            observed.append(FileRunRepository(tmp_path).load(snapshot.run_id).schema_version)
        return observed

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(save_and_load, range(4)))

    assert results
    assert all(version == 1 for worker_result in results for version in worker_result)
    raw = json.loads(
        (tmp_path / "runs" / f"{snapshot.run_id}.json").read_text(encoding="utf-8")
    )
    assert raw["schema_version"] == 1


def test_load_reconstructs_typed_snapshot_and_preserves_terminal_data(
    tmp_path,
    snapshot: RunSnapshot,
) -> None:
    failed_snapshot = replace(
        snapshot,
        status=RunStatus.FAILED,
        error=RunError(code="fake_terminal_error", message="Fake terminal failure"),
    )
    repository = FileRunRepository(tmp_path)
    repository.save(failed_snapshot)

    loaded = repository.load(failed_snapshot.run_id)
    loaded_by_canonical_uuid = repository.load(str(UUID(failed_snapshot.run_id)))

    assert isinstance(loaded, RunSnapshot)
    assert loaded.status is RunStatus.FAILED
    assert loaded.error == failed_snapshot.error
    assert loaded.started_at == failed_snapshot.started_at
    assert loaded.completed_at == failed_snapshot.completed_at
    assert loaded.output == failed_snapshot.output
    assert loaded.followup_context == failed_snapshot.followup_context
    assert loaded.metrics == failed_snapshot.metrics
    assert loaded.policy_decisions == failed_snapshot.policy_decisions
    assert isinstance(loaded.events, tuple)
    assert loaded.events[0].kind is EventKind.RUN_STARTED
    assert loaded.events == failed_snapshot.events
    assert loaded_by_canonical_uuid == loaded


def test_repository_redacts_dangerous_event_and_raw_source_fields(
    tmp_path,
    snapshot: RunSnapshot,
) -> None:
    unsafe_event = ResearchEvent(
        kind=EventKind.SOURCES_COLLECTED,
        run_id=snapshot.run_id,
        sequence=snapshot.events[-1].sequence + 1,
        occurred_at=datetime.now(timezone.utc),
        payload={
            "latest_sources": "Safe source synopsis",
            "note_path": "safe-note-path",
            "raw_context": "fake-sensitive-event-context",
            "raw_source_body": "fake-sensitive-source-body",
            "headers": {"Authorization": "fake-sensitive-header"},
            "token": "fake-sensitive-event-token",
            "github_token": "fake-sensitive-suffixed-token",
            "source_url": "https://fake-sensitive-source.invalid/body",
            "prompt_text": "fake-sensitive-prompt-text",
            "workspace_path": "fake-sensitive-workspace-path",
        },
    )
    unsafe_output = {
        **snapshot.output,
        "raw_context": "fake-sensitive-output-context",
        "todo_items": [
                {
                    **snapshot.output["todo_items"][0],
                    "raw_source_body": "fake-sensitive-output-source",
                    "stream_token": "fake-sensitive-stream-token",
                }
        ],
    }
    repository = FileRunRepository(tmp_path)
    repository.save(
        replace(
            snapshot,
            output=unsafe_output,
            events=snapshot.events + (unsafe_event,),
        )
    )

    run_path = tmp_path / "runs" / f"{snapshot.run_id}.json"
    raw = run_path.read_text(encoding="utf-8")
    loaded = repository.load(snapshot.run_id)

    for sentinel in (
        "fake-sensitive-event-context",
        "fake-sensitive-source-body",
        "fake-sensitive-header",
        "fake-sensitive-event-token",
        "fake-sensitive-suffixed-token",
        "fake-sensitive-source.invalid",
        "fake-sensitive-prompt-text",
        "fake-sensitive-workspace-path",
        "fake-sensitive-output-context",
        "fake-sensitive-output-source",
        "fake-sensitive-stream-token",
        "raw_context",
        "raw_source_body",
        '"headers"',
        '"token"',
    ):
        assert sentinel not in raw
    assert loaded.events[-1].payload == {
        "latest_sources": "Safe source synopsis",
        "note_path": "safe-note-path",
    }
    assert "raw_context" not in loaded.output
    assert "raw_source_body" not in loaded.output["todo_items"][0]
    assert "stream_token" not in loaded.output["todo_items"][0]


@pytest.mark.parametrize(
    "mutation",
    ["snapshot_run_id", "event_run_id", "followup_copies", "followup_source_run_id"],
)
def test_load_rejects_inconsistent_run_and_followup_identifiers(
    tmp_path,
    snapshot: RunSnapshot,
    mutation: str,
) -> None:
    repository = FileRunRepository(tmp_path)
    repository.save(snapshot)
    run_path = tmp_path / "runs" / f"{snapshot.run_id}.json"
    envelope = json.loads(run_path.read_text(encoding="utf-8"))
    tampered = deepcopy(envelope)
    other_run_id = uuid4().hex

    if mutation == "snapshot_run_id":
        tampered["snapshot"]["run_id"] = other_run_id
    elif mutation == "event_run_id":
        tampered["snapshot"]["events"][0]["run_id"] = other_run_id
    elif mutation == "followup_copies":
        tampered["followup_context"]["key_findings"] = ["Different finding"]
    else:
        tampered["followup_context"]["source_run_id"] = other_run_id
        tampered["snapshot"]["followup_context"]["source_run_id"] = other_run_id

    run_path.write_text(json.dumps(tampered), encoding="utf-8")

    with pytest.raises(CorruptRunRecordError):
        repository.load(snapshot.run_id)


def test_invalid_runtime_snapshot_cannot_replace_previous_good_record(
    tmp_path,
    snapshot: RunSnapshot,
) -> None:
    repository = FileRunRepository(tmp_path)
    repository.save(snapshot)
    invalid_snapshot = replace(snapshot, topic=None)  # type: ignore[arg-type]

    with pytest.raises(CorruptRunRecordError, match="topic"):
        repository.save(invalid_snapshot)

    assert repository.load(snapshot.run_id).topic == snapshot.topic


def test_nonincreasing_event_sequence_cannot_replace_previous_good_record(
    tmp_path,
    snapshot: RunSnapshot,
) -> None:
    repository = FileRunRepository(tmp_path)
    repository.save(snapshot)
    invalid_snapshot = replace(
        snapshot,
        events=(snapshot.events[1], snapshot.events[0], *snapshot.events[2:]),
    )

    with pytest.raises(CorruptRunRecordError, match="sequence"):
        repository.save(invalid_snapshot)

    assert repository.load(snapshot.run_id).events == snapshot.events


def test_load_uses_explicit_absent_corrupt_and_unsupported_errors(tmp_path) -> None:
    repository = FileRunRepository(tmp_path)
    missing_id = uuid4().hex
    with pytest.raises(RunNotFoundError):
        repository.load(missing_id)

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True)
    corrupt_id = uuid4().hex
    (runs_dir / f"{corrupt_id}.json").write_text("{not-json", encoding="utf-8")
    with pytest.raises(CorruptRunRecordError):
        repository.load(corrupt_id)

    unsupported_id = uuid4().hex
    (runs_dir / f"{unsupported_id}.json").write_text(
        json.dumps({"schema_version": 2, "snapshot": {}, "followup_context": {}}),
        encoding="utf-8",
    )
    with pytest.raises(UnsupportedSchemaError):
        repository.load(unsupported_id)

    inner_unsupported_id = uuid4().hex
    inner_unsupported = {
        "schema_version": 1,
        "snapshot": {"schema_version": 2},
        "followup_context": {},
    }
    (runs_dir / f"{inner_unsupported_id}.json").write_text(
        json.dumps(inner_unsupported),
        encoding="utf-8",
    )
    with pytest.raises(UnsupportedSchemaError):
        repository.load(inner_unsupported_id)


def test_load_maps_invalid_utf8_to_corrupt_record_error(tmp_path) -> None:
    repository = FileRunRepository(tmp_path)
    run_id = uuid4().hex
    repository.runs_dir.mkdir(parents=True)
    (repository.runs_dir / f"{run_id}.json").write_bytes(b"\xff\xfe\xfa")

    with pytest.raises(CorruptRunRecordError):
        repository.load(run_id)


def test_legacy_recorder_delegates_without_secondary_event_logs(
    tmp_path,
    completed_session: RunSession,
) -> None:
    recorder = JsonlRunRecorder(RecorderConfig(base_path=tmp_path))
    record = recorder.persist(
        completed_session,
        evaluation=EvaluationResult(score=1.0),
    )

    loaded = recorder.load(completed_session.run_id)

    assert record.run_id == completed_session.run_id
    assert loaded["run_id"] == completed_session.run_id
    assert loaded["status"] == "completed"
    assert loaded["output"]["report_markdown"] == "# Canonical report"
    for payload in (record.as_dict(), loaded):
        task = payload["output"]["todo_items"][0]
        assert task["notices"] == ["Cached source used"]
        assert task["retry_count"] == 2
        assert task["refined_queries"] == ["refined safe query"]
        json.dumps(payload, ensure_ascii=False)
    assert set(loaded["compressed_context"]) == {"run_summary", "reasoning_memory"}
    assert loaded["compressed_context"]["run_summary"]["completed_tasks"] == [
        {
            "task_id": 1,
            "title": "Completed task",
            "summary_excerpt": "Canonical finding",
            "sources_excerpt": "Canonical source summary",
        }
    ]
    assert loaded["compressed_context"]["reasoning_memory"] == {
        "key_findings": ["Canonical finding"],
        "key_sources": ["Canonical source summary"],
        "open_questions": [],
    }
    assert loaded["compressed_context"]["run_summary"]["report_excerpt"] == (
        "# Canonical report"
    )
    assert loaded["evaluation"] == {}
    assert (tmp_path / "runs" / f"{completed_session.run_id}.json").is_file()
    assert not (tmp_path / f"{completed_session.run_id}.json").exists()
    assert not (tmp_path / f"{completed_session.run_id}.events.jsonl").exists()
    assert not (tmp_path / "runs.jsonl").exists()
    with pytest.raises(FileNotFoundError):
        recorder.load(uuid4().hex)
