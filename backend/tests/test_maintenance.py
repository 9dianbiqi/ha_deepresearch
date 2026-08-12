"""Tests for bounded, durable-data maintenance."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from maintenance import cleanup_expired_data
from research.artifacts import ArtifactPayload, FileArtifactStore
from research.contracts import RunSnapshot, RunStatus
from research.history import ResearchHistoryStore
from research.repository import FileRunRepository


def _snapshot(run_id: str, timestamp: datetime) -> RunSnapshot:
    """Build a minimal terminal snapshot accepted by the file repository."""
    return RunSnapshot(
        run_id=run_id,
        topic=f"topic-{run_id}",
        status=RunStatus.COMPLETED,
        started_at=timestamp,
        completed_at=timestamp,
        parent_run_id=None,
        output={"report_markdown": "report"},
        followup_context={},
        metrics={},
        policy_decisions=(),
        config_snapshot={},
        events=(),
    )


def test_cleanup_removes_only_expired_terminal_runs_and_rebuilds_history(
    tmp_path: Path,
) -> None:
    """Expired JSON, matching artifacts, and derived history are cleaned together."""
    root = tmp_path / "data"
    repository = FileRunRepository(root)
    artifact_store = FileArtifactStore(repository)
    old_id = str(uuid4())
    current_id = str(uuid4())
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    old_time = now - timedelta(days=31)

    repository.save(_snapshot(old_id, old_time))
    repository.save(_snapshot(current_id, now))
    artifact_store.put(
        old_id,
        ArtifactPayload(
            artifact_id="artifact_report_markdown",
            artifact_type="report_markdown",
            mime_type="text/markdown",
            title="Old report",
            content="old",
        ),
    )
    (root / "artifacts").mkdir(parents=True, exist_ok=True)
    ResearchHistoryStore(repository)

    preview = cleanup_expired_data(root, 30, now=now, dry_run=True)
    assert preview.deleted_runs == 1
    assert repository.load(old_id).run_id == old_id.replace("-", "")

    result = cleanup_expired_data(root, 30, now=now)
    assert result.deleted_runs == 1
    assert result.deleted_artifact_directories == 1
    with pytest.raises(FileNotFoundError):
        repository.load(old_id)
    assert repository.load(current_id).run_id == current_id.replace("-", "")
    assert not (root / "artifacts" / old_id.replace("-", "")).exists()

    history = ResearchHistoryStore(repository)
    page = history.list_runs()
    assert [item["run_id"] for item in page.items] == [current_id.replace("-", "")]


def test_cleanup_rejects_workspace_or_incomplete_data_root(tmp_path: Path) -> None:
    """A maintenance command must fail closed before it can delete broad paths."""
    with pytest.raises(ValueError, match="workspace"):
        cleanup_expired_data(Path.cwd(), 30)

    incomplete = tmp_path / "not-the-data-root"
    with pytest.raises(ValueError, match="runs and artifacts"):
        cleanup_expired_data(incomplete, 30)
