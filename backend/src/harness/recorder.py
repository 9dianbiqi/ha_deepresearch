"""Compatibility recorder backed by canonical schema-v1 run snapshots."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from models import SummaryStateOutput, TodoItem
from research.context import FollowupContext, project_legacy_compressed_context
from research.contracts import RunSnapshot
from research.repository import FileRunRepository, RunNotFoundError

from .evaluator import EvaluationResult
from .models import HarnessRunRecord, RecorderConfig, RunContext


def _optional_text(value: object) -> str | None:
    """Return an optional text field from a stored legacy task."""
    return value if isinstance(value, str) else None


def _wire_mapping(wire: dict[str, Any], field_name: str) -> dict[str, Any]:
    """Return one required detached object from the snapshot wire view."""
    value = wire.get(field_name)
    if not isinstance(value, dict):
        raise TypeError(f"Snapshot wire field {field_name!r} must be an object.")
    return value


def _wire_mapping_list(
    wire: dict[str, Any],
    field_name: str,
) -> list[dict[str, Any]]:
    """Return one required list of detached objects from the wire view."""
    value = wire.get(field_name)
    if not isinstance(value, list):
        raise TypeError(f"Snapshot wire field {field_name!r} must be a list.")
    items: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise TypeError(
                f"Snapshot wire field {field_name!r} must contain objects."
            )
        items.append(item)
    return items


def _stored_output(raw_output: dict[str, Any]) -> SummaryStateOutput:
    """Reconstruct the legacy output view used by compatibility projection."""
    items: list[TodoItem] = []
    raw_items = raw_output.get("todo_items")
    if isinstance(raw_items, list):
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            task_id = raw_item.get("id")
            if not isinstance(task_id, int) or isinstance(task_id, bool):
                continue
            notices = raw_item.get("notices")
            refined_queries = raw_item.get("refined_queries")
            retry_count = raw_item.get("retry_count")
            items.append(
                TodoItem(
                    id=task_id,
                    title=str(raw_item.get("title") or f"Task {task_id}"),
                    intent=str(raw_item.get("intent") or ""),
                    query=str(raw_item.get("query") or ""),
                    status=str(raw_item.get("status") or "pending"),
                    summary=_optional_text(raw_item.get("summary")),
                    sources_summary=_optional_text(raw_item.get("sources_summary")),
                    notices=(
                        [item for item in notices if isinstance(item, str)]
                        if isinstance(notices, list)
                        else []
                    ),
                    note_id=_optional_text(raw_item.get("note_id")),
                    note_path=_optional_text(raw_item.get("note_path")),
                    stream_token=_optional_text(raw_item.get("stream_token")),
                    retry_count=(
                        retry_count
                        if isinstance(retry_count, int) and not isinstance(retry_count, bool)
                        else 0
                    ),
                    refined_queries=(
                        [item for item in refined_queries if isinstance(item, str)]
                        if isinstance(refined_queries, list)
                        else []
                    ),
                    source_strategy=_optional_text(raw_item.get("source_strategy")),
                    repository=_optional_text(raw_item.get("repository")),
                )
            )
    raw_github_intelligence = raw_output.get("github_intelligence")
    return SummaryStateOutput(
        running_summary=_optional_text(raw_output.get("running_summary")),
        report_markdown=_optional_text(raw_output.get("report_markdown")),
        todo_items=items,
        github_intelligence=(
            dict(raw_github_intelligence)
            if isinstance(raw_github_intelligence, dict)
            else {}
        ),
    )


def _string_tuple(value: object) -> tuple[str, ...]:
    """Return only string entries from a stored sequence."""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _stored_followup(
    run_id: str,
    raw: dict[str, Any],
) -> FollowupContext:
    """Normalize flat or historical nested follow-up memory."""
    nested = raw.get("reasoning_memory")
    memory = nested if isinstance(nested, dict) else raw
    return FollowupContext(
        source_run_id=run_id,
        key_findings=_string_tuple(memory.get("key_findings")),
        key_sources=_string_tuple(memory.get("key_sources")),
        open_questions=_string_tuple(memory.get("open_questions")),
    )


def _legacy_record(
    snapshot: RunSnapshot,
    *,
    evaluation: dict[str, Any] | None = None,
) -> HarnessRunRecord:
    """Derive the historical record type from one loaded v1 snapshot."""
    wire = snapshot.as_dict()
    raw_output = _wire_mapping(wire, "output")
    raw_followup = _wire_mapping(wire, "followup_context")
    output = _stored_output(raw_output)
    compressed_context = project_legacy_compressed_context(
        output,
        followup_context=_stored_followup(snapshot.run_id, raw_followup),
    )
    return HarnessRunRecord(
        run_id=snapshot.run_id,
        topic=snapshot.topic,
        started_at=snapshot.started_at,
        completed_at=snapshot.completed_at,
        status=snapshot.status.value,
        config_snapshot=_wire_mapping(wire, "config_snapshot"),
        metrics=_wire_mapping(wire, "metrics"),
        error=snapshot.error.message if snapshot.error else None,
        events=_wire_mapping_list(wire, "events"),
        output=raw_output,
        compressed_context=compressed_context,
        policy_decisions=_wire_mapping_list(wire, "policy_decisions"),
        evaluation=evaluation or {},
    )


class JsonlRunRecorder:
    """Retain the legacy API while storing only canonical snapshot envelopes."""

    def __init__(self, config: RecorderConfig) -> None:
        """Initialize the recorder at the configured repository root."""
        self._config = config
        self._repository = FileRunRepository(config.base_path)

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
        """Persist through ``FileRunRepository`` without secondary event logs."""
        self._repository.save(context.to_snapshot())
        snapshot = self._repository.load(context.run_id)
        record = _legacy_record(snapshot, evaluation=evaluation.as_dict())

        logger.info(
            "Run persisted: run_id={} events={} score={:.2f}",
            context.run_id, len(context.events), evaluation.score,
        )
        return record

    def load(self, run_id: str) -> dict[str, object]:
        """Load a v1 snapshot and expose its derived legacy dictionary."""
        try:
            snapshot = self._repository.load(run_id)
        except RunNotFoundError as exc:
            raise FileNotFoundError(run_id) from exc
        return _legacy_record(snapshot, evaluation={}).as_dict()
