"""Recorders used to persist harness run traces."""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

from .evaluator import EvaluationResult
from .models import HarnessRunRecord, RecorderConfig, RunContext


class JsonlRunRecorder:
    """Persist each harness run as a standalone JSON and append-only JSONL entry."""

    def __init__(self, config: RecorderConfig) -> None:
        self._config = config

    @property
    def base_path(self) -> Path:
        """Return the root directory used by the recorder."""
        return self._config.base_path

    def persist(
        self,
        context: RunContext,
        *,
        evaluation: EvaluationResult,
    ) -> HarnessRunRecord:
        """Serialize the run context to disk."""
        self.base_path.mkdir(parents=True, exist_ok=True)
        record = HarnessRunRecord.from_context(
            context,
            evaluation=evaluation.as_dict(),
        )

        run_path = self.base_path / f"{context.run_id}.json"
        index_path = self.base_path / "runs.jsonl"
        events_path = self.base_path / f"{context.run_id}.events.jsonl"

        run_path.write_text(
            json.dumps(record.as_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        with events_path.open("w", encoding="utf-8") as handle:
            for event in context.events:
                handle.write(json.dumps(event.as_dict(), ensure_ascii=False))
                handle.write("\n")
        index_entry = {
            "run_id": record.run_id,
            "topic": record.topic,
            "status": record.status,
            "started_at": record.started_at.isoformat() if hasattr(record.started_at, "isoformat") else str(record.started_at),
            "completed_at": record.completed_at.isoformat() if hasattr(record.completed_at, "isoformat") else str(record.completed_at),
            "score": evaluation.score,
        }
        with index_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(index_entry, ensure_ascii=False))
            handle.write("\n")

        logger.info(
            "Run persisted: run_id={} events={} score={:.2f}",
            context.run_id, len(context.events), evaluation.score,
        )
        return record

    def load(self, run_id: str) -> dict[str, object]:
        """Load one persisted run record from disk."""
        run_path = self.base_path / f"{run_id}.json"
        if not run_path.exists():
            raise FileNotFoundError(run_id)
        return json.loads(run_path.read_text(encoding="utf-8"))
