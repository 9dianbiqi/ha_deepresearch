"""Safe maintenance commands for the single-instance durable data root."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from research.contracts import RunStatus, normalize_run_id
from research.history import ResearchHistoryStore
from research.repository import FileRunRepository, RunRepositoryError

_TERMINAL_STATUSES = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.REPORT_INCOMPLETE,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.REJECTED,
    }
)


@dataclass(frozen=True, slots=True)
class CleanupSummary:
    """Describe one bounded cleanup pass without exposing file contents."""

    data_dir: str
    retention_days: int
    cutoff: str
    deleted_runs: int
    deleted_artifact_directories: int
    dry_run: bool

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-safe command result."""
        return {
            "data_dir": self.data_dir,
            "retention_days": self.retention_days,
            "cutoff": self.cutoff,
            "deleted_runs": self.deleted_runs,
            "deleted_artifact_directories": self.deleted_artifact_directories,
            "dry_run": self.dry_run,
        }


def _resolved_data_root(data_dir: str | Path) -> Path:
    """Resolve and reject paths that could target the workspace or filesystem root."""
    root = Path(data_dir).expanduser().resolve(strict=False)
    cwd = Path.cwd().resolve(strict=False)
    if root == root.parent or root == cwd or cwd.is_relative_to(root):
        raise ValueError("DATA_DIR must be a dedicated child directory, not the workspace.")
    if root == Path.home().resolve(strict=False):
        raise ValueError("DATA_DIR must not be the user home directory.")
    if (root / ".git").exists():
        raise ValueError("DATA_DIR must not contain a Git worktree.")
    return root


def _validated_child(path: Path, parent: Path) -> Path:
    """Resolve one cleanup target and require it stays beneath its known parent."""
    resolved_parent = parent.resolve(strict=False)
    resolved = path.resolve(strict=False)
    if resolved.parent != resolved_parent:
        raise ValueError("Cleanup target escaped its application directory.")
    return resolved


def _run_timestamp(snapshot: object) -> datetime | None:
    """Read a snapshot timestamp without trusting malformed records."""
    completed_at = getattr(snapshot, "completed_at", None)
    started_at = getattr(snapshot, "started_at", None)
    candidate = completed_at or started_at
    return candidate if isinstance(candidate, datetime) else None


def cleanup_expired_data(
    data_dir: str | Path,
    retention_days: int,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> CleanupSummary:
    """Delete only old terminal runs and their matching artifacts safely."""
    if isinstance(retention_days, bool) or not 1 <= retention_days <= 3650:
        raise ValueError("retention_days must be between 1 and 3650.")
    root = _resolved_data_root(data_dir)
    runs_dir = _validated_child(root / "runs", root)
    artifacts_dir = _validated_child(root / "artifacts", root)
    if not root.is_dir() or not runs_dir.is_dir() or not artifacts_dir.is_dir():
        raise ValueError("DATA_DIR must contain dedicated runs and artifacts directories.")

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    cutoff = current - timedelta(days=retention_days)
    repository = FileRunRepository(root)
    expired_run_ids: set[str] = set()

    for path in sorted(runs_dir.glob("*.json")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            run_id = normalize_run_id(path.stem)
            snapshot = repository.load(run_id)
        except (RunRepositoryError, OSError, ValueError):
            continue
        timestamp = _run_timestamp(snapshot)
        if (
            timestamp is None
            or timestamp >= cutoff
            or snapshot.status not in _TERMINAL_STATUSES
        ):
            continue
        target = _validated_child(runs_dir / f"{run_id}.json", runs_dir)
        expired_run_ids.add(run_id)
        if not dry_run:
            target.unlink(missing_ok=True)

    deleted_artifact_directories = 0
    for run_id in sorted(expired_run_ids):
        raw_artifact_dir = artifacts_dir / run_id
        if raw_artifact_dir.is_symlink():
            continue
        artifact_dir = _validated_child(raw_artifact_dir, artifacts_dir)
        if artifact_dir.is_dir():
            deleted_artifact_directories += 1
            if not dry_run:
                shutil.rmtree(artifact_dir)

    if not dry_run and expired_run_ids:
        # The SQLite index is derived state; canonical JSON snapshots remain the
        # source of truth and are scanned again after deletion.
        ResearchHistoryStore(repository).rebuild()

    return CleanupSummary(
        data_dir=str(root),
        retention_days=retention_days,
        cutoff=cutoff.isoformat(),
        deleted_runs=len(expired_run_ids),
        deleted_artifact_directories=deleted_artifact_directories,
        dry_run=dry_run,
    )


def _parser() -> argparse.ArgumentParser:
    """Build the maintenance command parser."""
    parser = argparse.ArgumentParser(description="HelloAgents data maintenance")
    subparsers = parser.add_subparsers(dest="command", required=True)
    cleanup = subparsers.add_parser("cleanup", help="remove expired terminal runs")
    cleanup.add_argument(
        "--data-dir",
        default=os.getenv("DATA_DIR", "./data"),
        help="dedicated DATA_DIR containing runs/ and artifacts/",
    )
    cleanup.add_argument(
        "--retention-days",
        type=int,
        default=int(os.getenv("RETENTION_DAYS", "30")),
    )
    cleanup.add_argument(
        "--apply",
        action="store_true",
        help="perform deletion; without this flag only report the targets",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one safe maintenance command and print only bounded metadata."""
    args = _parser().parse_args(argv)
    if args.command != "cleanup":
        return 2
    summary = cleanup_expired_data(
        args.data_dir,
        args.retention_days,
        dry_run=not args.apply,
    )
    sys.stdout.write(json.dumps(summary.as_dict(), ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["CleanupSummary", "cleanup_expired_data", "main"]
