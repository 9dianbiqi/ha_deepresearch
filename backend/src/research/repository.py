"""Atomic, redacted file persistence for canonical research snapshots."""

from __future__ import annotations

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
        if set(snapshot) != required_snapshot_fields:
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

        return RunSnapshot(
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
            schema_version=SCHEMA_VERSION,
        )

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
