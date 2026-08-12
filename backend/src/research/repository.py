"""Atomic, redacted file persistence for canonical research snapshots."""

from __future__ import annotations

import base64
import json
import os
import tempfile
from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Any

from config import SAFE_CONFIGURATION_FIELDS

from .contracts import (
    EventKind,
    ResearchEvent,
    RunError,
    RunSnapshot,
    RunStatus,
    normalize_run_id,
)
from .validation import CheckpointValidationError, validate_checkpoint_snapshot

SCHEMA_VERSION = 1
SAFE_CONFIG_FIELDS = SAFE_CONFIGURATION_FIELDS

_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "body",
        "content",
        "cookie",
        "cookies",
        "full_content",
        "full_page",
        "full_page_content",
        "headers",
        "http_headers",
        "metadata",
        "notes_workspace",
        "operation_result",
        "page_content",
        "password",
        "prompt",
        "prompts",
        "raw_body",
        "raw_context",
        "raw_result",
        "raw_results",
        "raw_source_body",
        "raw_source_bodies",
        "request_metadata",
        "secret",
        "token",
        "url",
        "workspace",
    }
)

_ROOT_LOCKS: dict[Path, RLock] = {}
_ROOT_LOCKS_GUARD = RLock()


class RunRepositoryError(RuntimeError):
    """Base error for durable run repository failures."""


class InvalidRunIdError(RunRepositoryError, ValueError):
    """Raised when a caller supplies a noncanonical or path-like run ID."""


class RunNotFoundError(RunRepositoryError, FileNotFoundError):
    """Raised when no durable record exists for a valid run ID."""


class CorruptRunRecordError(RunRepositoryError):
    """Raised when a stored record cannot reconstruct a canonical snapshot."""


class UnsupportedSchemaError(RunRepositoryError):
    """Raised when a record uses a schema version this repository cannot read."""


def _shared_root_lock(root: Path) -> RLock:
    """Return one process-wide lock for all repository instances at ``root``."""
    key = root.resolve(strict=False)
    with _ROOT_LOCKS_GUARD:
        lock = _ROOT_LOCKS.get(key)
        if lock is None:
            lock = RLock()
            _ROOT_LOCKS[key] = lock
        return lock


def _normalized_id(value: object, *, caller_supplied: bool) -> str:
    """Normalize a run ID and translate errors for its trust boundary."""
    try:
        return normalize_run_id(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        if caller_supplied:
            raise InvalidRunIdError("Run ID must be a canonical UUID string.") from exc
        raise CorruptRunRecordError("Stored run ID is invalid.") from exc


def _is_sensitive_key(key: str) -> bool:
    """Return whether a mapping key may expose unsafe request or source data."""
    normalized = key.strip().casefold().replace("-", "_")
    if normalized in _SENSITIVE_KEYS or normalized.startswith("raw_"):
        return True
    if normalized.endswith(("_api_key", "_password", "_secret", "_token", "_url")):
        return True
    if normalized.endswith(("_base_url", "_workspace", "_metadata", "_prompt")):
        return True
    if normalized.startswith("prompt_"):
        return True
    if normalized == "workspace_path" or normalized.endswith("_workspace_path"):
        return True
    if normalized in {"access_token", "auth_token", "refresh_token"}:
        return True
    return False


def _redact(value: Any) -> Any:
    """Copy JSON-like data while removing sensitive mapping fields."""
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            if key == "research_intelligence" and isinstance(item, Mapping):
                redacted[key] = _redact_v2_intelligence(item)
                continue
            if key.strip().casefold().replace("-", "_") == "stream_token":
                if item is None:
                    redacted[key] = None
                continue
            if not _is_sensitive_key(key):
                redacted[key] = _redact(item)
        return redacted
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    return value


_V2_TOP_FIELDS = frozenset(
    {
        "schema_version",
        "mode",
        "profile_id",
        "profile_version",
        "sources",
        "evidence",
        "claims",
        "coverage",
        "report_spec",
        "artifact_manifest",
        "evidence_frozen",
    }
)
_V2_SOURCE_FIELDS = frozenset(
    {
        "provider_id",
        "source_kind",
        "source_id",
        "canonical_url",
        "requested_ref",
        "resolved_version",
        "captured_at",
        "content_hash",
    }
)
_V2_EVIDENCE_FIELDS = frozenset(
    {
        "evidence_id",
        "source",
        "evidence_type",
        "evidence_level",
        "title",
        "excerpt",
        "locator",
        "attributes",
    }
)
_V2_LOCATOR_FIELDS = frozenset(
    {
        "locator_type",
        "url",
        "file_path",
        "line_start",
        "line_end",
        "page_start",
        "page_end",
        "section",
        "paragraph",
        "fragment",
    }
)
_V2_CLAIM_FIELDS = frozenset(
    {
        "claim_id",
        "dimension",
        "statement",
        "confidence",
        "evidence_ids",
        "conflicting_evidence_ids",
        "limitations",
        "reportable",
    }
)
_V2_ARTIFACT_FIELDS = frozenset(
    {
        "schema_version",
        "artifacts",
        "artifact_id",
        "artifact_type",
        "mime_type",
        "path",
        "title",
        "description",
        "source_ids",
        "size_bytes",
        "checksum",
        "created_at",
    }
)


def _redact_v2_intelligence(value: Mapping[str, Any]) -> dict[str, Any]:
    """Preserve safe v2 URLs/locators while never persisting artifact bodies."""
    child_sections = {
        "sources": "source_list",
        "evidence": "evidence_list",
        "claims": "claim_list",
        "coverage": "coverage",
        "report_spec": "report",
        "artifact_manifest": "artifact_list",
    }
    return {
        key: _redact_v2_value(
            item,
            section=child_sections.get(key, "scalar"),
        )
        for key, item in value.items()
        if isinstance(key, str) and key in _V2_TOP_FIELDS
    }


def _redact_v2_value(value: Any, *, section: str) -> Any:
    """Redact one allowlisted v2 section without treating source URLs as secrets."""
    if isinstance(value, Mapping):
        section = {
            "source_list": "source",
            "evidence_list": "evidence",
            "claim_list": "claim",
            "artifact_list": "artifact",
        }.get(section, section)
        allowed = {
            "top": _V2_TOP_FIELDS,
            "source": _V2_SOURCE_FIELDS,
            "evidence": _V2_EVIDENCE_FIELDS,
            "locator": _V2_LOCATOR_FIELDS,
            "claim": _V2_CLAIM_FIELDS,
            "artifact": _V2_ARTIFACT_FIELDS,
            "coverage": frozenset(
                {
                    "required_dimensions",
                    "covered_dimensions",
                    "missing_dimensions",
                    "weak_claims",
                    "conflicting_claims",
                    "coverage_score",
                    "allow_report",
                    "gap_queries",
                    "retry_count",
                    "blockers",
                    "warnings",
                    "dimension_results",
                }
            ),
            "report": frozenset(
                {
                    "title",
                    "executive_summary",
                    "sections",
                    "claim_ids",
                    "citation_ids",
                    "tables",
                    "charts",
                    "diagrams",
                    "limitations",
                }
            ),
            "attributes": frozenset(),
        }.get(section, frozenset())
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            if section != "attributes" and key not in allowed:
                continue
            if section == "attributes" and _is_sensitive_key(key):
                continue
            child_section = section
            if section == "top":
                child_section = {
                    "sources": "source_list",
                    "evidence": "evidence_list",
                    "claims": "claim_list",
                    "coverage": "coverage",
                    "report_spec": "report",
                    "artifact_manifest": "artifact_list",
                }.get(key, "scalar")
            elif section == "source_list":
                child_section = "source"
            elif section == "evidence_list":
                child_section = "evidence"
            elif section == "claim_list":
                child_section = "claim"
            elif section == "artifact_list":
                child_section = "artifact"
            elif section == "evidence" and key == "source":
                child_section = "source"
            elif section == "evidence" and key == "locator":
                child_section = "locator"
            elif section == "evidence" and key == "attributes":
                child_section = "attributes"
            result[key] = _redact_v2_value(item, section=child_section)
        return result
    if isinstance(value, (list, tuple)):
        item_section = {
            "source_list": "source",
            "evidence_list": "evidence",
            "claim_list": "claim",
            "artifact_list": "artifact",
        }.get(section, section)
        return [_redact_v2_value(item, section=item_section) for item in value]
    return value


def _schema_version(mapping: Mapping[str, Any], *, label: str) -> int:
    """Read and validate one schema version field."""
    value = mapping.get("schema_version")
    if not isinstance(value, int) or isinstance(value, bool):
        raise CorruptRunRecordError(f"{label} schema version is missing or invalid.")
    if value != SCHEMA_VERSION:
        raise UnsupportedSchemaError(
            f"Unsupported {label} schema version: {value}."
        )
    return value


def _detached_json_value(value: Any, *, label: str) -> Any:
    """Return JSON-like containers detached as plain dictionaries and lists."""
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise CorruptRunRecordError(f"{label} must use text object keys.")
        return {
            key: _detached_json_value(item, label=label)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_detached_json_value(item, label=label) for item in value]
    if isinstance(value, Enum):
        return value.value
    return value


def _mapping(value: Any, *, label: str) -> dict[str, Any]:
    """Require a string-keyed mapping and return a fully detached plain copy."""
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise CorruptRunRecordError(f"{label} must be an object.")
    return {
        key: _detached_json_value(item, label=label)
        for key, item in value.items()
    }


def _datetime(value: Any, *, label: str) -> datetime:
    """Parse one ISO timestamp."""
    if not isinstance(value, str):
        raise CorruptRunRecordError(f"{label} must be an ISO timestamp.")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise CorruptRunRecordError(f"{label} must be an ISO timestamp.") from exc


def _optional_datetime(value: Any, *, label: str) -> datetime | None:
    """Parse an optional ISO timestamp."""
    if value is None:
        return None
    return _datetime(value, label=label)


def _validate_followup_identity(
    followup_context: Mapping[str, Any],
    *,
    run_id: str,
) -> None:
    """Require an optional follow-up source ID to reference the same run."""
    if "source_run_id" not in followup_context:
        return
    source_run_id = _normalized_id(
        followup_context.get("source_run_id"),
        caller_supplied=False,
    )
    if source_run_id != run_id:
        raise CorruptRunRecordError(
            "Follow-up context source does not match the snapshot run ID."
        )


def _contains_sensitive_key(value: Any) -> bool:
    """Return whether stored JSON contains a key that must be redacted."""
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            if key == "research_intelligence" and isinstance(item, dict):
                # This subtree was already reduced by _redact_v2_intelligence.
                continue
            normalized = key.strip().casefold().replace("-", "_")
            if normalized == "stream_token" and item is None:
                continue
            if _is_sensitive_key(key) or _contains_sensitive_key(item):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_sensitive_key(item) for item in value)
    return False


class FileRunRepository:
    """Persist one schema-v1 JSON envelope per normalized run ID."""

    def __init__(self, root: str | Path) -> None:
        """Initialize the repository root and its process-shared lock."""
        self._root = Path(root).resolve(strict=False)
        self._runs_dir = self._root / "runs"
        self._lock = _shared_root_lock(self._root)

    @property
    def root(self) -> Path:
        """Return the repository root."""
        return self._root

    @property
    def runs_dir(self) -> Path:
        """Return the directory containing schema-v1 run envelopes."""
        return self._runs_dir

    def save(self, snapshot: RunSnapshot) -> None:
        """Atomically persist a redacted canonical snapshot."""
        normalized_run_id = _normalized_id(snapshot.run_id, caller_supplied=True)
        target_path = self._runs_dir / f"{normalized_run_id}.json"
        temporary_path: Path | None = None

        with self._lock:
            try:
                envelope = self._serialize_envelope(snapshot, normalized_run_id)
                self._deserialize_envelope(envelope, normalized_run_id)
                self._runs_dir.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self._runs_dir,
                    prefix=f".{normalized_run_id}.",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    temporary_path = Path(handle.name)
                    if not self._is_valid_temporary_path(
                        temporary_path,
                        normalized_run_id,
                    ):
                        raise RunRepositoryError(
                            "Repository created an invalid temporary path."
                        )
                    json.dump(envelope, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_path, target_path)
                temporary_path = None
            except RunRepositoryError:
                self._remove_validated_temporary(temporary_path, normalized_run_id)
                raise
            except Exception as exc:
                self._remove_validated_temporary(temporary_path, normalized_run_id)
                raise RunRepositoryError(
                    f"Could not save run {normalized_run_id}: {exc}"
                ) from exc

    def save_checkpoint(self, snapshot: RunSnapshot) -> None:
        """Persist one validated checkpoint through the same atomic writer."""
        self.save(snapshot)

    def load(self, run_id: str) -> RunSnapshot:
        """Load and reconstruct one typed schema-v1 snapshot."""
        normalized_run_id = _normalized_id(run_id, caller_supplied=True)
        target_path = self._runs_dir / f"{normalized_run_id}.json"

        with self._lock:
            if not target_path.is_file():
                raise RunNotFoundError(normalized_run_id)
            try:
                raw = target_path.read_text(encoding="utf-8")
            except FileNotFoundError as exc:
                raise RunNotFoundError(normalized_run_id) from exc
            except UnicodeError as exc:
                raise CorruptRunRecordError(
                    f"Run {normalized_run_id} is not valid UTF-8 JSON."
                ) from exc
            except OSError as exc:
                raise RunRepositoryError(
                    f"Could not read run {normalized_run_id}: {exc}"
                ) from exc
            try:
                envelope = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise CorruptRunRecordError(
                    f"Run {normalized_run_id} is not valid UTF-8 JSON."
                ) from exc
            return self._deserialize_envelope(envelope, normalized_run_id)

    def iter_snapshots(
        self,
        *,
        status: RunStatus | None = None,
    ) -> tuple[RunSnapshot, ...]:
        """Return validated snapshots, skipping missing or corrupt files."""
        with self._lock:
            if not self._runs_dir.is_dir():
                return ()
            paths = tuple(self._runs_dir.glob("*.json"))
        snapshots: list[RunSnapshot] = []
        for path in paths:
            try:
                snapshot = self.load(path.stem)
            except (RunRepositoryError, OSError, ValueError):
                continue
            if status is not None and snapshot.status is not status:
                continue
            snapshots.append(snapshot)
        snapshots.sort(
            key=lambda item: (
                item.completed_at or item.started_at,
                item.run_id,
            ),
            reverse=True,
        )
        return tuple(snapshots)

    def list_summaries(
        self,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> Any:
        """Return bounded completed-run summaries for history browsing."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("History limit must be between 1 and 100.")
        decoded = self._decode_history_cursor(cursor)
        snapshots = list(self.iter_snapshots(status=RunStatus.COMPLETED))
        if decoded is not None:
            cursor_completed_at, cursor_run_id = decoded
            snapshots = [
                item
                for item in snapshots
                if (item.completed_at or item.started_at).isoformat() < cursor_completed_at
                or (
                    (item.completed_at or item.started_at).isoformat() == cursor_completed_at
                    and item.run_id < cursor_run_id
                )
            ]
        visible = snapshots[:limit]
        items: list[dict[str, Any]] = []
        for snapshot in visible:
            output = snapshot.output
            raw_tasks = output.get("todo_items")
            task_count = len(raw_tasks) if isinstance(raw_tasks, (list, tuple)) else 0
            report = output.get("report_markdown") or output.get("running_summary") or ""
            report_excerpt = report.strip()[:280] if isinstance(report, str) else ""
            items.append(
                {
                    "run_id": snapshot.run_id,
                    "topic": snapshot.topic[:240],
                    "status": snapshot.status.value,
                    "started_at": snapshot.started_at.isoformat(),
                    "completed_at": snapshot.completed_at.isoformat() if snapshot.completed_at else None,
                    "parent_run_id": snapshot.parent_run_id,
                    "task_count": task_count,
                    "report_excerpt": report_excerpt,
                    "resumable": snapshot.resumable,
                    "recovery_resumable": snapshot.recovery_resumable,
                    "last_resumable_parent": snapshot.last_resumable_parent,
                }
            )
        next_cursor = None
        if len(snapshots) > limit and visible:
            last = visible[-1]
            timestamp = (last.completed_at or last.started_at).isoformat()
            next_cursor = self._encode_history_cursor(timestamp, last.run_id)
        try:
            from .history import HistoryPage

            return HistoryPage(items=tuple(items), next_cursor=next_cursor)
        except ImportError:  # pragma: no cover - package import guard
            return {"items": items, "next_cursor": next_cursor}

    @staticmethod
    def _encode_history_cursor(completed_at: str, run_id: str) -> str:
        payload = json.dumps(
            {"v": 1, "completed_at": completed_at, "run_id": run_id},
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_history_cursor(value: str | None) -> tuple[str, str] | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("History cursor is invalid.")
        try:
            padded = value + "=" * (-len(value) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("History cursor is invalid.") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("v") != 1
            or not isinstance(payload.get("completed_at"), str)
            or not isinstance(payload.get("run_id"), str)
        ):
            raise ValueError("History cursor is invalid.")
        return payload["completed_at"], payload["run_id"]

    def _serialize_envelope(
        self,
        snapshot: RunSnapshot,
        normalized_run_id: str,
    ) -> dict[str, Any]:
        if snapshot.schema_version != SCHEMA_VERSION:
            raise UnsupportedSchemaError(
                f"Unsupported snapshot schema version: {snapshot.schema_version}."
            )

        parent_run_id = None
        if snapshot.parent_run_id is not None:
            parent_run_id = _normalized_id(
                snapshot.parent_run_id,
                caller_supplied=False,
            )

        followup_context = _mapping(
            _redact(snapshot.followup_context),
            label="Follow-up context",
        )
        _validate_followup_identity(
            followup_context,
            run_id=normalized_run_id,
        )

        events: list[dict[str, Any]] = []
        for event in snapshot.events:
            if event.schema_version != SCHEMA_VERSION:
                raise UnsupportedSchemaError(
                    f"Unsupported event schema version: {event.schema_version}."
                )
            event_run_id = _normalized_id(event.run_id, caller_supplied=False)
            if event_run_id != normalized_run_id:
                raise CorruptRunRecordError(
                    "Event run ID does not match the snapshot run ID."
                )
            event_payload = event.as_dict()
            event_payload["run_id"] = normalized_run_id
            event_payload["payload"] = _redact(event.payload)
            events.append(event_payload)

        config_snapshot = _mapping(snapshot.config_snapshot, label="Configuration")
        safe_config = {
            field: _redact(config_snapshot[field])
            for field in SAFE_CONFIG_FIELDS
            if field in config_snapshot
        }
        output = _mapping(_redact(snapshot.output), label="Snapshot output")
        metrics = _mapping(_redact(snapshot.metrics), label="Snapshot metrics")
        policy_decisions = _redact(list(snapshot.policy_decisions))
        if not isinstance(policy_decisions, list) or not all(
            isinstance(item, dict) for item in policy_decisions
        ):
            raise CorruptRunRecordError("Policy decisions must be a list of objects.")

        snapshot_payload = {
            "schema_version": SCHEMA_VERSION,
            "run_id": normalized_run_id,
            "topic": snapshot.topic,
            "status": snapshot.status.value,
            "started_at": snapshot.started_at.isoformat(),
            "completed_at": (
                snapshot.completed_at.isoformat() if snapshot.completed_at else None
            ),
            "parent_run_id": parent_run_id,
            "output": output,
            "followup_context": followup_context,
            "metrics": metrics,
            "policy_decisions": policy_decisions,
            "config_snapshot": safe_config,
            "events": events,
            "error": (
                {"code": snapshot.error.code, "message": snapshot.error.message}
                if snapshot.error is not None
                else None
            ),
            "failure_reason": snapshot.failure_reason,
            "checkpoint": snapshot.checkpoint,
            "checkpoint_state": (
                _redact(snapshot.checkpoint_state)
                if snapshot.checkpoint_state is not None
                else None
            ),
            "resumable": snapshot.resumable,
            "recovery_resumable": snapshot.recovery_resumable,
            "last_resumable_parent": snapshot.last_resumable_parent,
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "snapshot": snapshot_payload,
            "followup_context": followup_context,
        }

    def _deserialize_envelope(
        self,
        raw_envelope: Any,
        requested_run_id: str,
    ) -> RunSnapshot:
        envelope = _mapping(raw_envelope, label="Run envelope")
        _schema_version(envelope, label="Envelope")
        if set(envelope) != {"schema_version", "snapshot", "followup_context"}:
            raise CorruptRunRecordError("Run envelope has an invalid shape.")

        snapshot = _mapping(envelope.get("snapshot"), label="Snapshot")
        _schema_version(snapshot, label="Snapshot")
        required_snapshot_fields = {
            "schema_version",
            "run_id",
            "topic",
            "status",
            "started_at",
            "completed_at",
            "parent_run_id",
            "output",
            "followup_context",
            "metrics",
            "policy_decisions",
            "config_snapshot",
            "events",
            "error",
        }
        optional_snapshot_fields = {
            "failure_reason",
            "checkpoint",
            "checkpoint_state",
            "resumable",
            "recovery_resumable",
            "last_resumable_parent",
        }
        if not required_snapshot_fields.issubset(snapshot) or not set(snapshot).issubset(
            required_snapshot_fields | optional_snapshot_fields
        ):
            raise CorruptRunRecordError("Snapshot has an invalid shape.")

        stored_run_id = _normalized_id(
            snapshot.get("run_id"),
            caller_supplied=False,
        )
        if stored_run_id != requested_run_id:
            raise CorruptRunRecordError(
                "Requested run ID does not match the stored snapshot."
            )

        envelope_followup = _mapping(
            envelope.get("followup_context"),
            label="Envelope follow-up context",
        )
        snapshot_followup = _mapping(
            snapshot.get("followup_context"),
            label="Snapshot follow-up context",
        )
        if envelope_followup != snapshot_followup:
            raise CorruptRunRecordError(
                "Envelope and snapshot follow-up contexts do not match."
            )
        _validate_followup_identity(snapshot_followup, run_id=stored_run_id)

        config_snapshot = _mapping(
            snapshot.get("config_snapshot"),
            label="Configuration snapshot",
        )
        if not set(config_snapshot).issubset(SAFE_CONFIG_FIELDS):
            raise CorruptRunRecordError(
                "Configuration snapshot contains fields outside the allowlist."
            )

        output = _mapping(snapshot.get("output"), label="Snapshot output")
        metrics = _mapping(snapshot.get("metrics"), label="Snapshot metrics")
        policy_raw = snapshot.get("policy_decisions")
        if not isinstance(policy_raw, list) or not all(
            isinstance(item, dict) for item in policy_raw
        ):
            raise CorruptRunRecordError("Policy decisions must be a list of objects.")
        if any(
            _contains_sensitive_key(value)
            for value in (output, snapshot_followup, metrics, policy_raw)
        ):
            raise CorruptRunRecordError("Snapshot contains an unsafe persisted field.")

        events_raw = snapshot.get("events")
        if not isinstance(events_raw, list):
            raise CorruptRunRecordError("Snapshot events must be a list.")
        events = tuple(
            self._deserialize_event(item, run_id=stored_run_id)
            for item in events_raw
        )
        previous_sequence = 0
        for event in events:
            if event.sequence <= previous_sequence:
                raise CorruptRunRecordError(
                    "Research event sequence must be strictly increasing."
                )
            previous_sequence = event.sequence

        status_raw = snapshot.get("status")
        try:
            status = RunStatus(status_raw)
        except (TypeError, ValueError) as exc:
            raise CorruptRunRecordError("Snapshot status is invalid.") from exc

        topic = snapshot.get("topic")
        if not isinstance(topic, str):
            raise CorruptRunRecordError("Snapshot topic must be text.")

        parent_run_id_raw = snapshot.get("parent_run_id")
        parent_run_id = None
        if parent_run_id_raw is not None:
            parent_run_id = _normalized_id(
                parent_run_id_raw,
                caller_supplied=False,
            )

        error_raw = snapshot.get("error")
        error = None
        if error_raw is not None:
            error_mapping = _mapping(error_raw, label="Run error")
            if set(error_mapping) != {"code", "message"} or not all(
                isinstance(error_mapping.get(field), str)
                for field in ("code", "message")
            ):
                raise CorruptRunRecordError("Run error has an invalid shape.")
            error = RunError(
                code=error_mapping["code"],
                message=error_mapping["message"],
            )

        failure_reason = snapshot.get("failure_reason")
        if failure_reason is not None and not isinstance(failure_reason, str):
            raise CorruptRunRecordError("Failure reason must be text or null.")
        checkpoint = snapshot.get("checkpoint")
        if checkpoint is not None and not isinstance(checkpoint, str):
            raise CorruptRunRecordError("Checkpoint must be text or null.")
        checkpoint_state_raw = snapshot.get("checkpoint_state")
        checkpoint_state = None
        if checkpoint_state_raw is not None:
            checkpoint_state = _mapping(
                checkpoint_state_raw,
                label="Checkpoint state",
            )
        resumable_raw = snapshot.get("resumable")
        if resumable_raw is not None and not isinstance(resumable_raw, bool):
            raise CorruptRunRecordError("Resumable must be boolean or null.")
        recovery_resumable_raw = snapshot.get("recovery_resumable")
        if recovery_resumable_raw is not None and not isinstance(
            recovery_resumable_raw,
            bool,
        ):
            raise CorruptRunRecordError(
                "Recovery resumable must be boolean or null."
            )
        last_resumable_parent = snapshot.get("last_resumable_parent")
        if last_resumable_parent is not None:
            last_resumable_parent = _normalized_id(
                last_resumable_parent,
                caller_supplied=False,
            )

        loaded_snapshot = RunSnapshot(
            run_id=stored_run_id,
            topic=topic,
            status=status,
            started_at=_datetime(snapshot.get("started_at"), label="started_at"),
            completed_at=_optional_datetime(
                snapshot.get("completed_at"),
                label="completed_at",
            ),
            parent_run_id=parent_run_id,
            output=output,
            followup_context=snapshot_followup,
            metrics=metrics,
            policy_decisions=tuple(dict(item) for item in policy_raw),
            config_snapshot=config_snapshot,
            events=events,
            error=error,
            failure_reason=failure_reason or (error.code if error else None),
            checkpoint=checkpoint,
            checkpoint_state=checkpoint_state,
            resumable=(
                resumable_raw
                if resumable_raw is not None
                else status is RunStatus.COMPLETED
            ),
            recovery_resumable=(
                recovery_resumable_raw
                if recovery_resumable_raw is not None
                else None
            ),
            last_resumable_parent=last_resumable_parent,
            schema_version=SCHEMA_VERSION,
        )
        try:
            validate_checkpoint_snapshot(loaded_snapshot)
        except CheckpointValidationError as exc:
            raise CorruptRunRecordError(str(exc)) from exc
        return loaded_snapshot

    def _deserialize_event(
        self,
        raw_event: Any,
        *,
        run_id: str,
    ) -> ResearchEvent:
        event = _mapping(raw_event, label="Research event")
        _schema_version(event, label="Event")
        required_event_fields = {
            "schema_version",
            "type",
            "run_id",
            "task_id",
            "operation_id",
            "sequence",
            "occurred_at",
            "payload",
        }
        if set(event) != required_event_fields:
            raise CorruptRunRecordError("Research event has an invalid shape.")

        event_run_id = _normalized_id(event.get("run_id"), caller_supplied=False)
        if event_run_id != run_id:
            raise CorruptRunRecordError(
                "Event run ID does not match the snapshot run ID."
            )
        try:
            kind = EventKind(event.get("type"))
        except (TypeError, ValueError) as exc:
            raise CorruptRunRecordError("Research event kind is invalid.") from exc

        sequence = event.get("sequence")
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence < 1
        ):
            raise CorruptRunRecordError("Research event sequence is invalid.")
        task_id = event.get("task_id")
        if task_id is not None and (
            not isinstance(task_id, int) or isinstance(task_id, bool)
        ):
            raise CorruptRunRecordError("Research event task ID is invalid.")
        operation_id = event.get("operation_id")
        if operation_id is not None and not isinstance(operation_id, str):
            raise CorruptRunRecordError("Research event operation ID is invalid.")
        payload = _mapping(event.get("payload"), label="Research event payload")
        if _contains_sensitive_key(payload):
            raise CorruptRunRecordError("Research event contains an unsafe field.")

        return ResearchEvent(
            kind=kind,
            run_id=event_run_id,
            sequence=sequence,
            occurred_at=_datetime(event.get("occurred_at"), label="occurred_at"),
            payload=payload,
            task_id=task_id,
            operation_id=operation_id,
            schema_version=SCHEMA_VERSION,
        )

    def _is_valid_temporary_path(self, path: Path, run_id: str) -> bool:
        try:
            resolved = path.resolve(strict=False)
            runs_dir = self._runs_dir.resolve(strict=False)
        except OSError:
            return False
        return (
            resolved.parent == runs_dir
            and resolved.name.startswith(f".{run_id}.")
            and resolved.suffix == ".tmp"
        )

    def _remove_validated_temporary(
        self,
        path: Path | None,
        run_id: str,
    ) -> None:
        if path is None or not self._is_valid_temporary_path(path, run_id):
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return
