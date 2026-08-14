"""Pure compatibility projection from typed research events to legacy SSE."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .contracts import EventKind, ResearchEvent

_TASK_TEXT_FIELDS = (
    "title",
    "intent",
    "query",
    "status",
    "summary",
    "sources_summary",
    "note_id",
    "note_path",
    "stream_token",
    "source_strategy",
)
_REPOSITORY_TEXT_FIELDS = (
    "owner",
    "repo",
    "full_name",
    "url",
    "default_branch",
    "language",
)
_REPOSITORY_COUNT_FIELDS = ("stars", "forks", "open_issues")
_NOTICE_MESSAGES = {
    "search_backend_notice": "Search backend returned a notice.",
    "search_backend_unavailable": "搜索服务暂时不可用，请稍后重试。",
    "github_api_notice": "GitHub API returned a notice.",
    "github_api_context_failed": "GitHub API context collection failed.",
}
_SEARCH_BACKENDS = frozenset(
    {"advanced", "duckduckgo", "none", "perplexity", "searxng", "tavily"}
)
_TERMINAL_CODES = frozenset(
    {
        "cancelled",
        "checkpoint_persistence_failed",
        "checkpoint_corrupt",
        "checkpoint_not_found",
        "checkpoint_version_unsupported",
        "run_not_resumable",
        "recovery_unsupported",
        "deadline_exceeded",
        "context_projection_failed",
        "coordinator_failed",
        "invalid_command",
        "invalid_run_id",
        "operation_rejected",
        "parent_corrupt",
        "parent_not_found",
        "parent_not_resumable",
        "parent_pending",
        "persistence_failed",
        "policy_error",
        "policy_rejected",
        "repository_error",
        "report_incomplete",
        "run_already_active",
        "run_failed",
        "run_rejected",
        "terminal_validation_failed",
    }
)


def _optional_text(value: object) -> str | None:
    """Return text values only, rejecting nested JSON containers."""
    return value if isinstance(value, str) else None


def _text(value: object) -> str:
    """Return text values or the safe empty-string compatibility default."""
    return value if isinstance(value, str) else ""


def _optional_int(value: object) -> int | None:
    """Return integer metadata while rejecting booleans and containers."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text_list(value: object) -> list[str]:
    """Return a detached list containing text elements only."""
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, str)]


def _tasks(value: object) -> list[dict[str, Any]]:
    """Return allowlisted task dictionaries only."""
    if not isinstance(value, (list, tuple)):
        return []
    tasks: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        task: dict[str, Any] = {}
        if "id" in item:
            task["id"] = _optional_int(item.get("id"))
        if "retry_count" in item:
            task["retry_count"] = _optional_int(item.get("retry_count"))
        for field in _TASK_TEXT_FIELDS:
            if field in item:
                task[field] = _optional_text(item.get(field))
        if "refined_queries" in item:
            task["refined_queries"] = _text_list(item.get("refined_queries"))
        notices, notice_codes = _notice_fields(item)
        if "notices" in item or "notice_codes" in item:
            task["notices"] = notices
            task["notice_codes"] = notice_codes
        if "repository" in item:
            task["repository"] = _repository_reference(item.get("repository"))
        tasks.append(task)
    return tasks


def _repository(value: object) -> dict[str, Any]:
    """Return allowlisted repository metadata only."""
    if not isinstance(value, Mapping):
        return {}
    repository: dict[str, Any] = {}
    for field in _REPOSITORY_TEXT_FIELDS:
        field_value = value.get(field)
        if isinstance(field_value, str):
            repository[field] = field_value
    for field in _REPOSITORY_COUNT_FIELDS:
        field_value = _optional_int(value.get(field))
        if field_value is not None:
            repository[field] = field_value
    return repository


def _repository_reference(value: object) -> str | None:
    """Return task/source repository slugs only, never nested metadata."""
    return value if isinstance(value, str) else None


def _notice_fields(payload: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """Regenerate trusted notice messages from a small code allowlist."""
    raw_codes = payload.get("notice_codes")
    codes: list[str] = []
    if isinstance(raw_codes, (list, tuple)):
        codes = [
            code
            for code in raw_codes
            if isinstance(code, str) and code in _NOTICE_MESSAGES
        ]
    if not codes:
        raw_messages = payload.get("notices")
        if isinstance(raw_messages, (list, tuple)):
            for code, message in _NOTICE_MESSAGES.items():
                if message in raw_messages:
                    codes.append(code)
    codes = list(dict.fromkeys(codes))
    return [_NOTICE_MESSAGES[code] for code in codes], codes


def _safe_code(value: object, fallback: str) -> str:
    """Return one stable machine code without copying arbitrary provider text."""
    if isinstance(value, str) and value in _TERMINAL_CODES:
        return value
    return fallback


def _safe_backend(value: object) -> str | None:
    """Return only one configured public search backend identifier."""
    return value if isinstance(value, str) and value in _SEARCH_BACKENDS else None


def _generic_research(payload: Mapping[str, Any]) -> bool:
    """Return whether an event belongs to a non-GitHub research mode."""
    mode = payload.get("research_mode")
    return isinstance(mode, str) and mode != "github"


def _generic_metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Project only safe provider-neutral metadata into an additive event."""
    projected: dict[str, Any] = {}
    mode = payload.get("research_mode")
    profile_id = payload.get("profile_id")
    if isinstance(mode, str):
        projected["research_mode"] = mode
    if isinstance(profile_id, str):
        projected["profile_id"] = profile_id
    provider_ids = payload.get("provider_ids")
    if isinstance(provider_ids, (list, tuple)):
        projected["provider_ids"] = [
            item for item in provider_ids if isinstance(item, str)
        ]
    for field in ("source_count", "bundle_schema_version"):
        value = _optional_int(payload.get(field))
        if value is not None:
            projected[field] = value
    return projected


class LegacySseProjector:
    """Project immutable typed events into the historical flat SSE envelope."""

    def project(self, event: ResearchEvent) -> dict[str, Any] | None:
        """Return one safe legacy event, or ``None`` for internal event kinds."""
        payload = event.as_dict()["payload"]
        projected: dict[str, Any] = {
            "run_id": event.run_id,
            "schema_version": event.schema_version,
            "sequence": event.sequence,
        }

        if event.kind is EventKind.RUN_STARTED:
            projected.update({"type": "status", "message": "初始化研究流程"})
        elif event.kind is EventKind.RUN_RECOVERY_STARTED:
            projected.update({"type": "status", "message": "从可信检查点恢复研究"})
        elif event.kind is EventKind.REPOSITORY_DETECTED:
            if _generic_research(payload):
                projected.update(
                    {
                        "type": "research_source",
                        **_generic_metadata(payload),
                    }
                )
                return projected
            repository = _repository(payload.get("repository"))
            notices, notice_codes = _notice_fields(payload)
            projected.update(
                {
                    "type": "github_repository",
                    "message": (
                        f"已识别 GitHub 仓库：{repository.get('full_name', '')}"
                    ),
                    "repository": repository,
                    "notices": notices,
                    "notice_codes": notice_codes,
                }
            )
        elif event.kind is EventKind.EVIDENCE_COLLECTED:
            projected.update(
                {
                    "type": (
                        "research_evidence"
                        if _generic_research(payload)
                        else "github_evidence"
                    ),
                    "snapshot_count": _optional_int(payload.get("snapshot_count")) or 0,
                    "evidence_count": _optional_int(payload.get("evidence_count")) or 0,
                    "claim_count": _optional_int(payload.get("claim_count")) or 0,
                    "artifact_count": _optional_int(payload.get("artifact_count")) or 0,
                    "bundle_schema_version": _optional_int(
                        payload.get("bundle_schema_version")
                    )
                    or 1,
                }
            )
            if _generic_research(payload):
                projected.update(_generic_metadata(payload))
        elif event.kind is EventKind.COVERAGE_UPDATED:
            score = payload.get("coverage_score")
            projected.update(
                {
                    "type": "coverage_update",
                    "coverage_score": (
                        float(score) if isinstance(score, (int, float)) else 0.0
                    ),
                    "covered_dimensions": _text_list(payload.get("covered_dimensions")),
                    "missing_dimensions": _text_list(payload.get("missing_dimensions")),
                    "gap_queries": _text_list(payload.get("gap_queries")),
                    "allow_report": bool(payload.get("allow_report", False)),
                }
            )
            if _generic_research(payload):
                projected.update(_generic_metadata(payload))
        elif event.kind is EventKind.ARTIFACT_READY:
            projected.update(
                {
                    "type": "artifact_ready",
                    "artifact_id": _optional_text(payload.get("artifact_id")),
                    "artifact_type": _optional_text(payload.get("artifact_type")),
                    "mime_type": _optional_text(payload.get("mime_type")),
                    "path": _optional_text(payload.get("path")),
                    "title": _optional_text(payload.get("title")),
                    "checksum": _optional_text(payload.get("checksum")),
                }
            )
        elif event.kind is EventKind.PLAN_CREATED:
            projected.update(
                {
                    "type": "todo_list",
                    "tasks": _tasks(payload.get("tasks")),
                    "step": 0,
                }
            )
        elif event.kind is EventKind.HISTORY_RECALLED:
            projected.update(
                {
                    "type": "history_recalled",
                    "match_count": _optional_int(payload.get("match_count")) or 0,
                }
            )
        elif event.kind in {
            EventKind.TASK_STARTED,
            EventKind.TASK_COMPLETED,
            EventKind.TASK_SKIPPED,
            EventKind.TASK_FAILED,
        }:
            status_by_kind = {
                EventKind.TASK_STARTED: "in_progress",
                EventKind.TASK_COMPLETED: "completed",
                EventKind.TASK_SKIPPED: "skipped",
                EventKind.TASK_FAILED: "failed",
            }
            projected.update(
                {
                    "type": "task_status",
                    "task_id": _optional_int(event.task_id),
                    "status": status_by_kind[event.kind],
                    "step": _optional_int(payload.get("step")),
                }
            )
            if event.kind is EventKind.TASK_FAILED:
                projected["detail"] = "Task execution failed."
            for field in (
                "title",
                "intent",
                "summary",
                "sources_summary",
                "note_id",
                "note_path",
                "source_strategy",
                "repository",
                "stream_token",
            ):
                value = payload.get(field)
                projected[field] = (
                    _repository_reference(value)
                    if field == "repository"
                    else _optional_text(value)
                )
        elif event.kind is EventKind.SOURCES_COLLECTED:
            notices, notice_codes = _notice_fields(payload)
            latest_sources = _optional_text(payload.get("latest_sources"))
            if latest_sources is None:
                latest_sources = _optional_text(payload.get("sources_summary"))
            projected.update(
                {
                    "type": "sources",
                    "task_id": _optional_int(event.task_id),
                    "latest_sources": latest_sources,
                    "backend": _safe_backend(payload.get("backend")),
                    "step": _optional_int(payload.get("step")),
                    "notices": notices,
                    "notice_codes": notice_codes,
                }
            )
            for field in (
                "note_id",
                "note_path",
                "source_strategy",
                "repository",
                "stream_token",
                "status",
            ):
                value = payload.get(field)
                projected[field] = (
                    _repository_reference(value)
                    if field == "repository"
                    else _optional_text(value)
                )
        elif event.kind is EventKind.SUMMARY_DELTA:
            projected.update(
                {
                    "type": "task_summary_chunk",
                    "task_id": _optional_int(event.task_id),
                    "content": _text(payload.get("chunk")),
                    "step": _optional_int(payload.get("step")),
                    "note_id": _optional_text(payload.get("note_id")),
                    "stream_token": _optional_text(payload.get("stream_token")),
                }
            )
        elif event.kind is EventKind.SUMMARY_QUALITY_UPDATE:
            raw_score = payload.get("overall_score")
            projected.update(
                {
                    "type": "summary_quality_update",
                    "overall_score": (
                        float(raw_score)
                        if isinstance(raw_score, (int, float))
                        and not isinstance(raw_score, bool)
                        else 0.0
                    ),
                    "passed": bool(payload.get("passed", False)),
                    "paragraph_count": _optional_int(
                        payload.get("paragraph_count")
                    )
                    or 0,
                    "claim_count": _optional_int(payload.get("claim_count")) or 0,
                    "blocked_paragraph_count": _optional_int(
                        payload.get("blocked_paragraph_count")
                    )
                    or 0,
                    "blocker_codes": _text_list(payload.get("blocker_codes")),
                }
            )
        elif event.kind is EventKind.TASK_RETRY_SCHEDULED:
            projected.update(
                {
                    "type": "task_retry",
                    "task_id": _optional_int(event.task_id),
                    "step": _optional_int(payload.get("step")),
                }
            )
            for field in (
                "previous_query",
                "refined_query",
                "attempt",
                "reason",
                "stream_token",
            ):
                value = payload.get(field)
                projected[field] = (
                    _optional_int(value)
                    if field == "attempt"
                    else _optional_text(value)
                )
        elif event.kind is EventKind.REPORT_NOTE_CREATED:
            projected.update(
                {
                    "type": "report_note",
                    "note_id": _optional_text(payload.get("note_id")),
                    "note_path": _optional_text(payload.get("note_path")),
                    "title": _optional_text(payload.get("title")),
                }
            )
        elif event.kind is EventKind.REPORT_GENERATED:
            projected.update(
                {
                    "type": "final_report",
                    "report": _text(payload.get("report")),
                    "note_id": _optional_text(payload.get("note_id")),
                    "note_path": _optional_text(payload.get("note_path")),
                }
            )
        elif event.kind is EventKind.RUN_COMPLETED:
            projected["type"] = "done"
            if "resumable" in payload:
                projected["resumable"] = bool(payload.get("resumable"))
            if "recovery_resumable" in payload:
                projected["recovery_resumable"] = bool(
                    payload.get("recovery_resumable")
                )
            if "last_resumable_parent" in payload:
                projected["last_resumable_parent"] = _optional_text(
                    payload.get("last_resumable_parent")
                )
        elif event.kind in {
            EventKind.RUN_FAILED,
            EventKind.RUN_REJECTED,
            EventKind.RUN_CANCELLED,
        }:
            defaults = {
                EventKind.RUN_FAILED: (
                    "run_failed",
                    "Research run failed.",
                ),
                EventKind.RUN_REJECTED: (
                    "run_rejected",
                    "Research run was rejected.",
                ),
                EventKind.RUN_CANCELLED: (
                    "cancelled",
                    "Research run was cancelled.",
                ),
            }
            fallback_code, detail = defaults[event.kind]
            projected.update(
                {
                    "type": "error",
                    "code": _safe_code(payload.get("code"), fallback_code),
                    "detail": detail,
                }
            )
            if "resumable" in payload:
                projected["resumable"] = bool(payload.get("resumable"))
            if "recovery_resumable" in payload:
                projected["recovery_resumable"] = bool(
                    payload.get("recovery_resumable")
                )
            if "last_resumable_parent" in payload:
                projected["last_resumable_parent"] = _optional_text(
                    payload.get("last_resumable_parent")
                )
            if "checkpoint" in payload:
                projected["checkpoint"] = _optional_text(payload.get("checkpoint"))
        else:
            return None

        return projected


_DEFAULT_PROJECTOR = LegacySseProjector()


def project_legacy_event(event: ResearchEvent) -> dict[str, Any] | None:
    """Project one event through the sole stateless compatibility mapping."""
    return _DEFAULT_PROJECTOR.project(event)


__all__ = ["LegacySseProjector", "project_legacy_event"]
