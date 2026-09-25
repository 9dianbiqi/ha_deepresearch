"""JSON-only persistence and restoration for prepared research runtime state."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict
from enum import Enum
from typing import Any, cast

from .artifacts import ArtifactPayload, ArtifactStore
from .intelligence import (
    ArtifactDescriptorV2,
    EvidenceRecord,
    SourceReference,
    stable_evidence_id,
)
from .operations import OperationScope
from .profiles import RenderedResearchTask, ResearchMode, RetrievalBudget
from .sources import (
    CancellationCheckpoint,
    DetectionResult,
    ProviderContext,
    RetrievalBudgetTracker,
    SourceCollection,
    SourceRequestSpec,
    SourceTarget,
)
from .web_capture import WebCaptureResult, WebParagraph, WebSnapshotPayload

_SCHEMA_VERSION = 1
_ARTIFACT_PREFIX = "evidence_capture_"
_MAX_CAPTURE_ARTIFACT_BYTES = 20_000_000
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_BUDGET_FIELDS = ("requests", "results", "evidence", "enrich_passes")
_RECORD_FIELDS = frozenset(
    {
        "source_id",
        "canonical_url",
        "dimension",
        "evidence_type",
        "evidence_level",
        "title",
        "excerpt",
        "url",
        "locator_type",
        "locator",
        "section_path",
        "paragraph_id",
        "paragraph_index",
        "captured_at",
        "content_hash",
        "content_origin",
        "capture_notice_codes",
        "commit_sha",
        "file_path",
        "line_start",
        "line_end",
        "page_start",
        "page_end",
    }
)
_LOCATOR_FIELDS = frozenset(
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
_TARGET_METADATA_FIELDS = frozenset(
    {"owner", "repo", "topic", "title", "evidence_level", "content_origin"}
)


class EvidenceRecoveryError(RuntimeError):
    """Stable error raised when a prepared runtime snapshot cannot be restored."""

    def __init__(self, code: str, message: str) -> None:
        """Expose a stable failure code without including captured page bodies."""
        super().__init__(message)
        self.code = code


def _invalid(message: str) -> EvidenceRecoveryError:
    return EvidenceRecoveryError("evidence_recovery_invalid", message)


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _detach(value):
    """Thaw immutable checkpoint containers without changing JSON values."""
    if isinstance(value, Mapping):
        return {key: _detach(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_detach(item) for item in value]
    return value


def profile_fingerprint(profile) -> str:
    """Bind recovery to the complete registered policy, not only its name."""
    def normalize(value):
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, Mapping):
            return {key: normalize(item) for key, item in value.items()}
        if isinstance(value, (set, frozenset)):
            return sorted(normalize(item) for item in value)
        if isinstance(value, (tuple, list)):
            return [normalize(item) for item in value]
        return value
    return _sha256(_json_bytes(normalize(asdict(profile))))


def _text(value: object, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise _invalid(f"{name} must be text.")
    return value


def _positive_int(value: object, name: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise _invalid(f"{name} must be an integer greater than or equal to {minimum}.")
    return value


def _safe_metadata(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    metadata: dict[str, object] = {}
    for key in _TARGET_METADATA_FIELDS:
        item = value.get(key)
        if isinstance(item, str) and len(item) <= 2048:
            metadata[key] = item
    return metadata


def _target_to_dict(target: SourceTarget) -> dict[str, object]:
    return {
        "provider_id": target.provider_id,
        "source_kind": target.source_kind,
        "source_id": target.source_id,
        "canonical_url": target.canonical_url,
        "requested_ref": target.requested_ref,
        "metadata": _safe_metadata(target.metadata),
    }


def _target_from_dict(value: object) -> SourceTarget:
    if not isinstance(value, Mapping):
        raise _invalid("A source target must be an object.")
    requested_ref = value.get("requested_ref")
    if requested_ref is not None and not isinstance(requested_ref, str):
        raise _invalid("A source target requested_ref must be text or null.")
    return SourceTarget(
        provider_id=_text(value.get("provider_id"), "target provider_id"),
        source_kind=_text(value.get("source_kind"), "target source_kind"),
        source_id=_text(value.get("source_id"), "target source_id"),
        canonical_url=_text(value.get("canonical_url"), "target canonical_url", allow_empty=True),
        requested_ref=requested_ref,
        metadata=_safe_metadata(value.get("metadata")),
    )


def _record_to_dict(value: Mapping[str, Any]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key in _RECORD_FIELDS:
        item = value.get(key)
        if item is None:
            continue
        if key == "locator":
            if isinstance(item, Mapping):
                result[key] = {
                    locator_key: locator_value
                    for locator_key, locator_value in item.items()
                    if locator_key in _LOCATOR_FIELDS
                    and (
                        isinstance(locator_value, (str, int, float, bool))
                        or locator_value is None
                    )
                }
            continue
        if key in {"section_path", "capture_notice_codes"}:
            if isinstance(item, (tuple, list)) and all(
                isinstance(entry, str) for entry in item
            ):
                result[key] = list(item)
            continue
        if isinstance(item, (str, int, float, bool)):
            if key == "excerpt" and (not isinstance(item, str) or len(item) > 4096):
                raise _invalid("A provider record excerpt exceeds its recovery bound.")
            result[key] = item
    return result


def _record_list(value: object, name: str) -> list[dict[str, object]]:
    if not isinstance(value, (list, tuple)):
        raise _invalid(f"{name} must be a sequence.")
    if len(value) > 2000:
        raise _invalid(f"{name} exceeds its recovery bound.")
    result: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise _invalid(f"{name} entries must be objects.")
        result.append(_record_to_dict(item))
    return result


def _capture_to_dict(capture: WebCaptureResult) -> dict[str, object]:
    paragraphs = [
        {
            "paragraph_id": item.paragraph_id,
            "source_id": item.source_id,
            "canonical_url": item.canonical_url,
            "page_title": item.page_title,
            "section_path": list(item.section_path),
            "paragraph_index": item.paragraph_index,
            "exact_excerpt": item.exact_excerpt,
            "captured_at": item.captured_at,
            "content_hash": item.content_hash,
            "content_origin": item.content_origin,
            "evidence_level": item.evidence_level,
        }
        for item in capture.paragraphs
    ]
    snapshot: dict[str, object] | None = None
    if capture.snapshot is not None:
        item = capture.snapshot
        snapshot = {
            "source_id": item.source_id,
            "canonical_url": item.canonical_url,
            "page_title": item.page_title,
            "captured_at": item.captured_at,
            "content": item.content,
            "content_hash": item.content_hash,
            "suggested_filename": item.suggested_filename,
            "content_origin": item.content_origin,
            "mime_type": item.mime_type,
        }
    return {
        "status": capture.status,
        "source_id": capture.source_id,
        "canonical_url": capture.canonical_url,
        "page_title": capture.page_title,
        "captured_at": capture.captured_at,
        "paragraphs": paragraphs,
        "snapshot": snapshot,
        "metadata_excerpt": capture.metadata_excerpt,
        "notices": list(capture.notices),
        "notice_codes": list(capture.notice_codes),
        "content_origin": capture.content_origin,
    }


def _string_tuple(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise _invalid(f"{name} must be a sequence of text values.")
    return tuple(value)


def _capture_from_dict(value: object) -> WebCaptureResult:
    if not isinstance(value, Mapping):
        raise _invalid("A stored Web capture must be an object.")
    raw_paragraphs = value.get("paragraphs", ())
    if not isinstance(raw_paragraphs, (list, tuple)):
        raise _invalid("Web capture paragraphs must be a sequence.")
    paragraphs: list[WebParagraph] = []
    for item in raw_paragraphs:
        if not isinstance(item, Mapping):
            raise _invalid("A Web paragraph must be an object.")
        paragraphs.append(
            WebParagraph(
                paragraph_id=_text(item.get("paragraph_id"), "paragraph_id"),
                source_id=_text(item.get("source_id"), "paragraph source_id"),
                canonical_url=_text(item.get("canonical_url"), "paragraph canonical_url"),
                page_title=_text(item.get("page_title"), "paragraph page_title", allow_empty=True),
                section_path=_string_tuple(item.get("section_path", ()), "section_path"),
                paragraph_index=_positive_int(item.get("paragraph_index"), "paragraph_index", allow_zero=True),
                exact_excerpt=_text(item.get("exact_excerpt"), "paragraph excerpt", allow_empty=True),
                captured_at=_text(item.get("captured_at"), "paragraph captured_at"),
                content_hash=_text(item.get("content_hash"), "paragraph content_hash"),
                content_origin=_text(item.get("content_origin", "provided_content"), "paragraph content_origin"),
                evidence_level=_text(item.get("evidence_level", "full_text"), "paragraph evidence_level"),
            )
        )
    snapshot_value = value.get("snapshot")
    snapshot = None
    if snapshot_value is not None:
        if not isinstance(snapshot_value, Mapping):
            raise _invalid("Web snapshot must be an object or null.")
        snapshot = WebSnapshotPayload(
            source_id=_text(snapshot_value.get("source_id"), "snapshot source_id"),
            canonical_url=_text(snapshot_value.get("canonical_url"), "snapshot canonical_url"),
            page_title=_text(snapshot_value.get("page_title"), "snapshot page_title", allow_empty=True),
            captured_at=_text(snapshot_value.get("captured_at"), "snapshot captured_at"),
            content=_text(snapshot_value.get("content"), "snapshot content", allow_empty=True),
            content_hash=_text(snapshot_value.get("content_hash"), "snapshot content_hash"),
            suggested_filename=_text(snapshot_value.get("suggested_filename"), "snapshot suggested_filename"),
            content_origin=_text(snapshot_value.get("content_origin", "provided_content"), "snapshot content_origin"),
            mime_type=_text(snapshot_value.get("mime_type", "text/plain; charset=utf-8"), "snapshot mime_type"),
        )
    status = _text(value.get("status"), "capture status")
    if status not in {"complete", "partial", "failed", "metadata"}:
        raise _invalid("Web capture status is unsupported.")
    return WebCaptureResult(
        status=status,
        source_id=_text(value.get("source_id"), "capture source_id"),
        canonical_url=_text(value.get("canonical_url"), "capture canonical_url"),
        page_title=_text(value.get("page_title"), "capture page_title", allow_empty=True),
        captured_at=_text(value.get("captured_at"), "capture captured_at"),
        paragraphs=tuple(paragraphs),
        snapshot=snapshot,
        metadata_excerpt=_text(value.get("metadata_excerpt", ""), "metadata_excerpt", allow_empty=True),
        notices=_string_tuple(value.get("notices", ()), "capture notices"),
        notice_codes=_string_tuple(value.get("notice_codes", ()), "capture notice_codes"),
        content_origin=_text(value.get("content_origin", "search_metadata"), "capture content_origin"),
    )


def _collection_content_hash(collection: SourceCollection) -> str:
    payload = collection.provider_payload
    snapshot = getattr(payload, "snapshot", None)
    snapshot_hash = getattr(snapshot, "content_hash", None)
    if isinstance(snapshot_hash, str) and snapshot_hash.strip():
        return snapshot_hash.strip()
    payload_hash = getattr(payload, "content_hash", None)
    if isinstance(payload_hash, str) and payload_hash.strip():
        return payload_hash.strip()
    if isinstance(payload, Mapping):
        candidate = payload.get("content_hash") or payload.get("sha")
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    encoded = json.dumps(
        collection.records,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return _sha256(encoded)


def _collection_to_dict(
    collection: SourceCollection,
    *,
    capture_urls: Mapping[str, str],
) -> dict[str, object]:
    payload = collection.provider_payload
    capture_url = None
    if isinstance(payload, WebCaptureResult):
        capture_url = capture_urls.get(payload.canonical_url)
    return {
        "provider_id": collection.provider_id,
        "source_kind": collection.source_kind,
        "target": _target_to_dict(collection.target),
        "collection_status": collection.collection_status,
        "records": _record_list(collection.records, "collection records"),
        "resolved_version": collection.resolved_version,
        "captured_at": collection.captured_at,
        "notices": list(collection.notices),
        "notice_codes": list(collection.notice_codes),
        "content_hash": _collection_content_hash(collection),
        "capture_url": capture_url,
    }


def _collection_from_dict(
    value: object,
    *,
    captures: Mapping[str, WebCaptureResult],
    initial: bool = False,
) -> SourceCollection:
    if not isinstance(value, Mapping):
        raise _invalid("A source collection must be an object.")
    capture_url = value.get("capture_url")
    if capture_url is not None and not isinstance(capture_url, str):
        raise _invalid("A collection capture_url must be text or null.")
    payload: object | None = None
    if capture_url is not None:
        try:
            payload = captures[capture_url]
        except KeyError as exc:
            raise _invalid("A collection refers to an absent Web capture.") from exc
    else:
        content_hash = _text(value.get("content_hash"), "collection content_hash")
        payload = {"content_hash": content_hash} if content_hash else None
    version = value.get("resolved_version")
    if version is not None and not isinstance(version, str):
        raise _invalid("Collection resolved_version must be text or null.")
    return SourceCollection(
        provider_id=_text(value.get("provider_id"), "collection provider_id"),
        source_kind=_text(value.get("source_kind"), "collection source_kind"),
        target=_target_from_dict(value.get("target")),
        collection_status=_text(value.get("collection_status"), "collection status"),
        provider_payload=payload,
        records=tuple(_record_list(value.get("records", ()), "collection records")),
        resolved_version=version,
        captured_at=_text(value.get("captured_at"), "collection captured_at"),
        notices=_string_tuple(value.get("notices", ()), "collection notices"),
        notice_codes=_string_tuple(value.get("notice_codes", ()), "collection notice_codes"),
    )


def _budget_limits(budget: RetrievalBudget) -> dict[str, int]:
    return {
        "requests": budget.max_requests,
        "results": budget.max_results,
        "evidence": budget.max_evidence,
        "enrich_passes": budget.max_enrich_passes,
        "tasks": budget.max_tasks,
        "excerpt_chars": budget.max_excerpt_chars,
    }


def _task_to_dict(task: RenderedResearchTask) -> dict[str, object]:
    return {
        "id": task.id,
        "template_id": task.template_id,
        "dimension": task.dimension,
        "title": task.title,
        "intent": task.intent,
        "query": task.query,
        "source_strategy": task.source_strategy,
        "repository": task.repository,
    }


def _task_from_dict(value: object) -> RenderedResearchTask:
    if not isinstance(value, Mapping):
        raise _invalid("A rendered task must be an object.")
    repository = value.get("repository")
    if repository is not None and not isinstance(repository, str):
        raise _invalid("Task repository must be text or null.")
    return RenderedResearchTask(
        id=_positive_int(value.get("id"), "task id"),
        template_id=_text(value.get("template_id"), "task template_id"),
        dimension=_text(value.get("dimension"), "task dimension"),
        title=_text(value.get("title"), "task title"),
        intent=_text(value.get("intent"), "task intent", allow_empty=True),
        query=_text(value.get("query"), "task query", allow_empty=True),
        source_strategy=_text(value.get("source_strategy"), "task source_strategy"),
        repository=repository,
    )


def _safe_task_quality(value: object) -> object:
    """Retain only the known JSON report shape owned by the integration layer."""
    if not isinstance(value, Mapping):
        return None
    safe: dict[str, object] = {}
    for key in ("schema_version", "tasks", "summary", "attempts"):
        item = value.get(key)
        if key == "schema_version" and isinstance(item, int) and not isinstance(item, bool):
            safe[key] = item
        elif key == "summary" and isinstance(item, Mapping):
            safe[key] = {
                field_name: field_value
                for field_name, field_value in item.items()
                if field_name in {"accepted", "rejected", "pending", "failed"}
                and isinstance(field_value, int)
                and not isinstance(field_value, bool)
            }
        elif key in {"tasks", "attempts"} and isinstance(item, (list, tuple)):
            safe[key] = [
                dict(entry)
                for entry in item
                if isinstance(entry, Mapping)
                and all(
                    isinstance(k, str)
                    and (
                        isinstance(v, (str, int, float, bool))
                        or v is None
                    )
                    for k, v in entry.items()
                )
            ]
    return safe


def sanitize_evidence_recovery(payload: object) -> dict[str, object]:
    """Project a recovery object onto the explicit checkpoint schema."""
    payload = _detach(payload)
    if not isinstance(payload, Mapping):
        raise EvidenceRecoveryError("evidence_recovery_missing", "Evidence recovery state is missing.")
    fields = (
        "schema_version",
        "run_id",
        "profile_id",
        "profile_fingerprint",
        "version",
        "mode",
        "provider_id",
        "budget",
        "request",
        "detection",
        "targets",
        "rendered_tasks",
        "initial_collections",
        "admitted_records",
        "task_attempt_bindings",
        "accepted_attempts",
        "web_captures",
        "selected_web_collections",
        "web_record_keys",
        "enrichment_limitations",
    )
    safe = {key: payload[key] for key in fields if key in payload}
    def project(value, names):
        if not isinstance(value, Mapping):
            raise _invalid("A recovery object is malformed.")
        return {key: value[key] for key in names if key in value}

    safe["request"] = project(payload.get("request"), ("topic", "mode", "profile_id"))
    safe["budget"] = {
        "limits": project(payload.get("budget", {}).get("limits"), (*_BUDGET_FIELDS, "tasks", "excerpt_chars")),
        "used": project(payload.get("budget", {}).get("used"), _BUDGET_FIELDS),
    }
    safe["targets"] = [_target_to_dict(_target_from_dict(item)) for item in payload.get("targets", ())]
    detection = project(payload.get("detection"), ("provider_id", "matched", "confidence", "notices", "notice_codes"))
    for name in ("notices", "notice_codes"):
        detection[name] = list(_string_tuple(detection.get(name, ()), name))
    detection["targets"] = [_target_to_dict(_target_from_dict(item)) for item in payload.get("detection", {}).get("targets", ())]
    safe["detection"] = detection
    safe["rendered_tasks"] = [_task_to_dict(_task_from_dict(item)) for item in payload.get("rendered_tasks", ())]
    for name in ("initial_collections", "selected_web_collections"):
        safe[name] = []
        for raw in payload.get(name, ()):
            item = project(raw, ("provider_id", "source_kind", "collection_status", "resolved_version", "captured_at", "notices", "notice_codes", "content_hash", "capture_url"))
            item["target"] = _target_to_dict(_target_from_dict(raw.get("target")))
            item["records"] = _record_list(raw.get("records", ()), "collection records")
            for field in ("notices", "notice_codes"):
                item[field] = list(_string_tuple(item.get(field, ()), field))
            safe[name].append(item)
    safe["admitted_records"] = []
    for raw in payload.get("admitted_records", ()):
        item = project(raw, EvidenceRecord.__dataclass_fields__)
        item["source"] = project(raw.get("source"), SourceReference.__dataclass_fields__)
        item["locator"] = project(raw.get("locator"), _LOCATOR_FIELDS)
        attrs = raw.get("attributes", {})
        item["attributes"] = _record_to_dict(attrs)
        for name in ("notice_codes", "provider_collection_status"):
            if name in attrs:
                item["attributes"][name] = list(_string_tuple(attrs[name], name)) if name == "notice_codes" else _text(attrs[name], name)
        safe["admitted_records"].append(EvidenceRecord.from_dict(item).as_dict())
    safe["task_attempt_bindings"] = [project(item, ("task_id", "task_attempt", "dimension", "query", "evidence_ids")) for item in payload.get("task_attempt_bindings", ())]
    safe["web_captures"] = [project(item, ("url", "source_id", "canonical_url", "content_hash", "artifact_id", "artifact_type", "mime_type", "size_bytes", "checksum")) for item in payload.get("web_captures", ())]
    if "task_quality" in payload:
        quality = _safe_task_quality(payload.get("task_quality"))
        if quality is not None:
            safe["task_quality"] = quality
    highwater = payload.get("attempt_highwater")
    if isinstance(highwater, Mapping):
        safe_highwater: dict[str, int] = {}
        for key, value in highwater.items():
            try:
                task_id = str(_positive_int(int(key), "attempt highwater task id"))
            except (TypeError, ValueError, EvidenceRecoveryError):
                continue
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                safe_highwater[task_id] = value
        safe["attempt_highwater"] = safe_highwater
    return _detach(safe)


def _validate_ref(value: object) -> tuple[str, str]:
    if not isinstance(value, Mapping):
        raise _invalid("A Web capture reference must be an object.")
    content_hash = _text(value.get("content_hash"), "capture content_hash")
    artifact_id = _text(value.get("artifact_id"), "capture artifact_id")
    if _HASH_RE.fullmatch(content_hash) is None or artifact_id != f"{_ARTIFACT_PREFIX}{content_hash}":
        raise _invalid("A Web capture reference has an invalid content identity.")
    if value.get("artifact_type") != "evidence_recovery_web_capture" or value.get("mime_type") != "application/json":
        raise _invalid("A Web capture reference has an invalid artifact type.")
    size_bytes = _positive_int(value.get("size_bytes"), "capture size_bytes", allow_zero=True)
    if size_bytes > _MAX_CAPTURE_ARTIFACT_BYTES:
        raise _invalid("A Web capture artifact exceeds its size bound.")
    checksum = _text(value.get("checksum"), "capture checksum")
    if checksum != content_hash:
        raise _invalid("A Web capture checksum does not match its identity.")
    return _text(value.get("url"), "capture url", allow_empty=True), content_hash


def validate_evidence_recovery(
    payload: object,
    *,
    run_id: str,
    task_state: object | None = None,
) -> None:
    """Validate schema, run identity, budget, and every internal reference."""
    payload = _detach(payload)
    if payload is None:
        raise EvidenceRecoveryError("evidence_recovery_missing", "Evidence recovery state is missing.")
    if not isinstance(payload, Mapping):
        raise _invalid("Evidence recovery state must be an object.")
    if payload.get("schema_version") != _SCHEMA_VERSION or isinstance(payload.get("schema_version"), bool):
        raise _invalid("Evidence recovery schema version is unsupported.")
    if payload.get("run_id") != run_id:
        raise _invalid("Evidence recovery run identity does not match.")
    for key in ("profile_id", "provider_id"):
        _text(payload.get(key), key)
    _positive_int(payload.get("version"), "profile version")
    try:
        ResearchMode(payload.get("mode"))
    except (TypeError, ValueError) as exc:
        raise _invalid("Evidence recovery mode is unsupported.") from exc

    budget = payload.get("budget")
    if not isinstance(budget, Mapping) or not isinstance(budget.get("limits"), Mapping) or not isinstance(budget.get("used"), Mapping):
        raise _invalid("Evidence recovery budget is malformed.")
    for name in ("requests", "results", "evidence", "enrich_passes", "tasks", "excerpt_chars"):
        _positive_int(budget["limits"].get(name), f"budget limit {name}", allow_zero=name == "enrich_passes")
    for name in _BUDGET_FIELDS:
        used = _positive_int(budget["used"].get(name), f"budget used {name}", allow_zero=True)
        if used > budget["limits"][name]:
            raise _invalid(f"Budget use exceeds {name} limit.")

    request = payload.get("request")
    if not isinstance(request, Mapping):
        raise _invalid("Evidence recovery request is malformed.")
    _text(request.get("topic"), "request topic")
    _text(request.get("profile_id"), "request profile_id")
    if request.get("profile_id") != payload.get("profile_id") or request.get("mode") != payload.get("mode"):
        raise _invalid("Evidence recovery request identity is inconsistent.")

    targets = payload.get("targets")
    tasks = payload.get("rendered_tasks")
    if not isinstance(targets, (list, tuple)) or not isinstance(tasks, (list, tuple)):
        raise _invalid("Evidence recovery targets and tasks must be sequences.")
    target_ids = set()
    for raw in targets:
        target = _target_from_dict(raw)
        if target.source_id in target_ids:
            raise _invalid("Evidence recovery targets contain duplicate identities.")
        target_ids.add(target.source_id)
    task_ids = set()
    for raw in tasks:
        task = _task_from_dict(raw)
        if task.id in task_ids:
            raise _invalid("Evidence recovery tasks contain duplicate IDs.")
        task_ids.add(task.id)
    if len(tasks) > budget["limits"]["tasks"]:
        raise _invalid("Evidence recovery tasks exceed their budget.")

    capture_values = payload.get("web_captures")
    if not isinstance(capture_values, (list, tuple)):
        raise _invalid("Evidence recovery Web captures must be a sequence.")
    capture_urls: set[str] = set()
    capture_hashes: set[str] = set()
    for raw in capture_values:
        url, digest = _validate_ref(raw)
        if url in capture_urls:
            raise _invalid("Evidence recovery Web capture references must be unique.")
        capture_urls.add(url)
        capture_hashes.add(digest)

    initial_collections = payload.get("initial_collections")
    selected_collections = payload.get("selected_web_collections")
    if not isinstance(initial_collections, (list, tuple)) or not isinstance(selected_collections, (list, tuple)):
        raise _invalid("Evidence recovery collections must be sequences.")
    for collection in (*initial_collections, *selected_collections):
        if not isinstance(collection, Mapping):
            raise _invalid("A stored collection must be an object.")
        _target_from_dict(collection.get("target"))
        _text(collection.get("content_hash"), "collection content_hash")
        _record_list(collection.get("records", ()), "collection records")
        capture_url = collection.get("capture_url")
        if capture_url is not None and capture_url not in capture_urls:
            raise _invalid("A stored collection refers to a missing capture.")

    admitted_values = payload.get("admitted_records")
    if not isinstance(admitted_values, (list, tuple)):
        raise _invalid("Admitted evidence records must be a sequence.")
    admitted: dict[str, EvidenceRecord] = {}
    for raw in admitted_values:
        if not isinstance(raw, Mapping):
            raise _invalid("An admitted evidence record must be an object.")
        try:
            record = EvidenceRecord.from_dict(raw)
        except (TypeError, ValueError, KeyError) as exc:
            raise _invalid("An admitted evidence record is malformed.") from exc
        if record.evidence_id != stable_evidence_id(record.source, locator=record.locator, excerpt=record.excerpt):
            raise _invalid("An admitted evidence identity does not match its content.")
        if record.evidence_id in admitted:
            raise _invalid("Admitted evidence IDs must be unique.")
        admitted[record.evidence_id] = record

    bindings = payload.get("task_attempt_bindings")
    if not isinstance(bindings, (list, tuple)):
        raise _invalid("Task attempt bindings must be a sequence.")
    if task_state is not None:
        if not isinstance(task_state, (list, tuple)):
            raise _invalid("Checkpoint task state must be a sequence.")
        if any(not isinstance(item, Mapping) for item in task_state):
            raise _invalid("Checkpoint tasks must be objects.")
        task_ids = {_positive_int(item.get("id"), "checkpoint task id") for item in task_state}
        if len(task_ids) != len(task_state) or len(task_ids) > budget["limits"]["tasks"]:
            raise _invalid("Checkpoint task IDs are duplicate or exceed the task budget.")
    else:
        task_ids.update(_positive_int(b.get("task_id"), "binding task id") for b in bindings if isinstance(b, Mapping))
    if len(task_ids) > budget["limits"]["tasks"] or len(admitted) > budget["used"]["evidence"]:
        raise _invalid("Recovery evidence or tasks exceed their recorded budget.")
    binding_keys: set[tuple[int, int]] = set()
    for binding in bindings:
        if not isinstance(binding, Mapping):
            raise _invalid("A task attempt binding must be an object.")
        task_id = _positive_int(binding.get("task_id"), "binding task_id")
        attempt = _positive_int(binding.get("task_attempt"), "binding task_attempt")
        if task_id not in task_ids:
            raise _invalid("A task attempt binding refers to an unknown task.")
        binding_key = (task_id, attempt)
        if binding_key in binding_keys:
            raise _invalid("Task attempt binding identities must be unique.")
        binding_keys.add(binding_key)
        _text(binding.get("dimension"), "binding dimension")
        _text(binding.get("query"), "binding query")
        evidence_ids = binding.get("evidence_ids")
        if not isinstance(evidence_ids, (list, tuple)) or any(
            not isinstance(item, str) or item not in admitted for item in evidence_ids
        ):
            raise _invalid("A task attempt binding refers to unknown admitted evidence.")

    accepted = payload.get("accepted_attempts")
    if not isinstance(accepted, Mapping):
        raise _invalid("Accepted attempts must be an object.")
    for task_key, attempt_value in accepted.items():
        try:
            task_id = _positive_int(int(task_key), "accepted task ID")
        except (ValueError, TypeError) as exc:
            raise _invalid("Accepted attempt task IDs must be positive integers.") from exc
        if str(task_id) != str(task_key) or task_id not in task_ids:
            raise _invalid("Accepted attempts refer to an unknown task.")
        attempt = _positive_int(attempt_value, "accepted task attempt")
        if (task_id, attempt) not in binding_keys:
            raise _invalid("An accepted attempt has no matching task binding.")

    raw_keys = payload.get("web_record_keys")
    if not isinstance(raw_keys, (list, tuple)):
        raise _invalid("Web record keys must be a sequence.")
    for key in raw_keys:
        if not isinstance(key, (list, tuple)) or len(key) != 3 or any(not isinstance(item, str) for item in key):
            raise _invalid("A Web record key is malformed.")
    keys = {tuple(key) for key in raw_keys}
    expected_keys = {
        (collection["target"]["source_id"], str(record.get("paragraph_id", "metadata")), record["excerpt"])
        for collection in selected_collections for record in collection["records"]
    }
    if keys != expected_keys or len(keys) != len(raw_keys):
        raise _invalid("Web evidence deduplication keys are incomplete.")
    charged = len(keys) + sum(record.source.provider_id != "web" for record in admitted.values())
    if charged > budget["used"]["evidence"]:
        raise _invalid("Recorded budget does not cover the saved evidence.")
    highwater = payload.get("attempt_highwater", {})
    if not isinstance(highwater, Mapping):
        raise _invalid("Attempt counters must be an object.")
    for task, attempt in highwater.items():
        try:
            task_id = int(task)
        except (TypeError, ValueError) as exc:
            raise _invalid("Attempt task ID is invalid.") from exc
        if task_id < 1 or str(task_id) != str(task):
            raise _invalid("Attempt task ID is invalid.")
        _positive_int(attempt, "attempt highwater", allow_zero=True)
        if task_state is not None and task_id not in task_ids:
            raise _invalid("Attempt refers to an unknown task.")
    if any(highwater.get(str(task), 0) < attempt for task, attempt in binding_keys):
        raise _invalid("Attempt counter is behind its saved binding.")

    limitations = payload.get("enrichment_limitations")
    if not isinstance(limitations, (list, tuple)) or any(not isinstance(item, str) for item in limitations):
        raise _invalid("Enrichment limitations must be text values.")

    if task_state is not None:
        completed = _completed_task_ids(task_state)
        for task_id in completed:
            if accepted.get(str(task_id)) is None:
                raise _invalid("A completed task has no accepted evidence attempt.")


def _completed_task_ids(task_state: object) -> set[int]:
    if isinstance(task_state, Mapping):
        values = task_state.get("tasks", task_state.get("items", ()))
    else:
        values = task_state
    if not isinstance(values, (list, tuple)):
        return set()
    completed: set[int] = set()
    for item in values:
        if isinstance(item, Mapping):
            task_id, status = item.get("id", item.get("task_id")), item.get("status", item.get("state"))
        else:
            task_id = getattr(item, "id", getattr(item, "task_id", None))
            status = getattr(item, "status", getattr(item, "state", None))
        if isinstance(task_id, int) and not isinstance(task_id, bool) and str(status).casefold() in {
            "completed", "complete", "done", "success", "succeeded"
        }:
            completed.add(task_id)
    return completed


def _serialize_snapshot(snapshot: Mapping[str, Any]) -> dict[str, object]:
    capture_entries = list(snapshot.get("web_captures", ()))
    all_captures: dict[str, WebCaptureResult] = {}
    for url, capture in capture_entries:
        if isinstance(url, str) and isinstance(capture, WebCaptureResult):
            all_captures[url] = capture
    for collection in (*snapshot.get("initial_collections", ()), *snapshot.get("selected_web_collections", ())):
        if isinstance(collection, SourceCollection) and isinstance(collection.provider_payload, WebCaptureResult):
            capture = collection.provider_payload
            if not any(item is capture for item in all_captures.values()):
                all_captures.setdefault(capture.canonical_url, capture)

    encoded_captures: dict[str, tuple[str, bytes, WebCaptureResult]] = {}
    for url, capture in all_captures.items():
        body = _json_bytes(_capture_to_dict(capture))
        if len(body) > _MAX_CAPTURE_ARTIFACT_BYTES:
            raise EvidenceRecoveryError("evidence_recovery_artifact_invalid", "A Web capture exceeds its recovery artifact bound.")
        digest = _sha256(body)
        encoded_captures[url] = (digest, body, capture)

    capture_urls = {capture.canonical_url: url for url, (_, _, capture) in encoded_captures.items()}
    source_request = snapshot["request"]
    detection = snapshot["detection"]
    if not isinstance(source_request, SourceRequestSpec) or not isinstance(detection, DetectionResult):
        raise _invalid("Prepared research identity is malformed.")
    budget = snapshot["budget"]
    initial_collections = tuple(snapshot["initial_collections"])
    selected_collections = tuple(snapshot["selected_web_collections"])
    return {
        "schema_version": _SCHEMA_VERSION,
        "run_id": snapshot["run_id"],
        "profile_id": snapshot["profile_id"],
        "profile_fingerprint": snapshot["profile_fingerprint"],
        "version": snapshot["version"],
        "mode": snapshot["mode"],
        "provider_id": snapshot["provider_id"],
        "budget": {"limits": dict(snapshot["budget_limits"]), "used": dict(budget)},
        "request": {
            "topic": source_request.topic,
            "mode": source_request.mode.value,
            "profile_id": source_request.profile_id,
        },
        "detection": {
            "provider_id": detection.provider_id,
            "matched": detection.matched,
            "targets": [_target_to_dict(item) for item in detection.targets],
            "confidence": detection.confidence,
            "notices": list(detection.notices),
            "notice_codes": list(detection.notice_codes),
        },
        "targets": [_target_to_dict(item) for item in snapshot["targets"]],
        "rendered_tasks": [_task_to_dict(item) for item in snapshot["tasks"]],
        "initial_collections": [
            _collection_to_dict(item, capture_urls=capture_urls)
            for item in initial_collections
        ],
        "admitted_records": list(snapshot["admitted_records"]),
        "task_attempt_bindings": list(snapshot["task_attempt_bindings"]),
        "accepted_attempts": dict(snapshot["accepted_attempts"]),
        "attempt_highwater": dict(snapshot["attempt_highwater"]),
        "web_captures": [
            {
                "url": url,
                "source_id": capture.source_id,
                "canonical_url": capture.canonical_url,
                "content_hash": digest,
                "artifact_id": f"{_ARTIFACT_PREFIX}{digest}",
                "artifact_type": "evidence_recovery_web_capture",
                "mime_type": "application/json",
                "size_bytes": len(body),
                "checksum": digest,
            }
            for url, (digest, body, capture) in encoded_captures.items()
        ],
        "selected_web_collections": [
            _collection_to_dict(item, capture_urls=capture_urls)
            for item in selected_collections
        ],
        "web_record_keys": [list(item) for item in snapshot["web_record_keys"]],
        "enrichment_limitations": list(snapshot["enrichment_limitations"]),
        "_capture_bodies": {
            url: body for url, (_, body, _) in encoded_captures.items()
        },
    }


def persist_evidence_recovery(
    prepared: object,
    *,
    run_id: str,
    artifact_store: ArtifactStore,
) -> dict[str, object]:
    """Persist immutable prepared state and page captures as JSON artifacts."""
    snapshot_method = getattr(prepared, "export_recovery_state", None)
    if not callable(snapshot_method):
        raise _invalid("Prepared research does not expose recovery state.")
    try:
        snapshot = snapshot_method()
        payload = _serialize_snapshot(snapshot)
        if payload.get("run_id") != run_id:
            raise _invalid("Prepared research belongs to another run.")
        bodies = payload.pop("_capture_bodies")
        if not isinstance(bodies, Mapping):
            raise _invalid("Prepared Web capture artifacts are malformed.")
        for ref in cast(list[dict[str, Any]], payload["web_captures"]):
            url = ref["url"]
            body = bodies[url]
            descriptor = artifact_store.put(
                run_id,
                ArtifactPayload(
                    artifact_id=ref["artifact_id"],
                    artifact_type=ref["artifact_type"],
                    mime_type=ref["mime_type"],
                    title="Evidence recovery Web capture",
                    content=body,
                    description="Content-addressed Web capture for run recovery.",
                    source_ids=(ref["source_id"],),
                ),
            )
            if (
                not isinstance(descriptor, ArtifactDescriptorV2)
                or descriptor.artifact_id != ref["artifact_id"]
                or descriptor.size_bytes != ref["size_bytes"]
                or descriptor.checksum != ref["checksum"]
            ):
                raise EvidenceRecoveryError("evidence_recovery_artifact_invalid", "Artifact store returned an inconsistent Web capture descriptor.")
        safe = sanitize_evidence_recovery(payload)
        validate_evidence_recovery(safe, run_id=run_id)
        return safe
    except EvidenceRecoveryError:
        raise
    except Exception as exc:
        raise EvidenceRecoveryError("evidence_recovery_artifact_invalid", "Evidence recovery could not be persisted.") from exc


def _decode_capture_refs(
    payload: Mapping[str, Any],
    *,
    run_id: str,
    artifact_store: ArtifactStore,
) -> dict[str, WebCaptureResult]:
    captures: dict[str, WebCaptureResult] = {}
    for ref in payload["web_captures"]:
        url, digest = _validate_ref(ref)
        if url in captures:
            raise _invalid("Web capture URLs must be unique.")
        try:
            body = artifact_store.get(run_id, ref["artifact_id"])
        except Exception as exc:
            raise EvidenceRecoveryError("evidence_recovery_artifact_invalid", "A Web capture artifact is missing or unreadable.") from exc
        if not isinstance(body, (bytes, bytearray)):
            raise EvidenceRecoveryError("evidence_recovery_artifact_invalid", "A Web capture artifact did not return bytes.")
        encoded = bytes(body)
        if len(encoded) != ref["size_bytes"] or len(encoded) > _MAX_CAPTURE_ARTIFACT_BYTES or _sha256(encoded) != digest:
            raise EvidenceRecoveryError("evidence_recovery_artifact_invalid", "A Web capture artifact failed its size or checksum validation.")
        try:
            decoded = json.loads(encoded.decode("utf-8"))
            capture = _capture_from_dict(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, EvidenceRecoveryError) as exc:
            raise EvidenceRecoveryError("evidence_recovery_artifact_invalid", "A Web capture artifact is malformed.") from exc
        if capture.source_id != ref.get("source_id") or capture.canonical_url != ref.get("canonical_url"):
            raise EvidenceRecoveryError("evidence_recovery_artifact_invalid", "A Web capture artifact identity does not match its reference.")
        captures[url] = capture
    return captures


def restore_prepared_research(
    kernel: object,
    payload: object,
    *,
    run_id: str,
    cancellation: CancellationCheckpoint,
    operation_scope: OperationScope | None,
    artifact_store: ArtifactStore,
) -> object:
    """Restore a prepared kernel without invoking detection or collection."""
    payload = _detach(payload)
    validate_evidence_recovery(payload, run_id=run_id)
    assert isinstance(payload, Mapping)
    profile_registry = getattr(kernel, "profile_registry")
    provider_registry = getattr(kernel, "provider_registry")
    try:
        profile = profile_registry.get(payload["profile_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise EvidenceRecoveryError("evidence_recovery_profile_mismatch", "The checkpoint research profile is not registered.") from exc
    if (profile.version != payload["version"] or profile.mode.value != payload["mode"]
            or profile_fingerprint(profile) != payload.get("profile_fingerprint")):
        raise EvidenceRecoveryError("evidence_recovery_profile_mismatch", "The registered research profile has changed.")
    if _budget_limits(profile.retrieval_budget) != dict(payload["budget"]["limits"]):
        raise EvidenceRecoveryError("evidence_recovery_profile_mismatch", "The registered retrieval budget has changed.")
    try:
        provider = provider_registry.get(payload["provider_id"])
    except (KeyError, TypeError) as exc:
        raise EvidenceRecoveryError("evidence_recovery_profile_mismatch", "The checkpoint source provider is not registered.") from exc
    if ResearchMode(payload["mode"]) not in provider.supported_modes:
        raise EvidenceRecoveryError("evidence_recovery_profile_mismatch", "The registered source provider no longer supports this mode.")

    from .pipeline import PreparedResearch, TaskEvidenceBinding
    from .web_evidence import RunWebEvidence

    captures = _decode_capture_refs(
        payload,
        run_id=run_id,
        artifact_store=artifact_store,
    )
    tracker = RetrievalBudgetTracker.restore(profile.retrieval_budget, payload["budget"]["used"])
    request_value = payload["request"]
    request = SourceRequestSpec(
        topic=request_value["topic"],
        mode=request_value["mode"],
        profile_id=request_value["profile_id"],
    )
    detection_value = payload["detection"]
    if not isinstance(detection_value, Mapping):
        raise _invalid("Stored provider detection is malformed.")
    detection = DetectionResult(
        provider_id=_text(detection_value.get("provider_id"), "detection provider_id"),
        matched=cast(bool, detection_value.get("matched")),
        targets=tuple(_target_from_dict(item) for item in detection_value.get("targets", ())),
        confidence=detection_value.get("confidence", 0.0),
        notices=_string_tuple(detection_value.get("notices", ()), "detection notices"),
        notice_codes=_string_tuple(detection_value.get("notice_codes", ()), "detection notice_codes"),
    )
    targets = tuple(_target_from_dict(item) for item in payload["targets"])
    tasks = tuple(_task_from_dict(item) for item in payload["rendered_tasks"])
    collections = tuple(
        _collection_from_dict(item, captures=captures, initial=True)
        for item in payload["initial_collections"]
    )
    selected_collections = tuple(
        _collection_from_dict(item, captures=captures)
        for item in payload["selected_web_collections"]
    )
    context = ProviderContext(
        run_id=run_id,
        profile_id=profile.profile_id,
        mode=profile.mode,
        operation_scope=operation_scope,
        cancellation=cancellation,
        budget=tracker,
    )
    web_evidence = RunWebEvidence()
    web_evidence.restore_state(
        captures=captures,
        collections=selected_collections,
        record_keys=tuple(tuple(item) for item in payload["web_record_keys"]),
    )

    admitted: dict[str, EvidenceRecord] = {}
    for raw in payload["admitted_records"]:
        record = EvidenceRecord.from_dict(raw)
        admitted[record.evidence_id] = record
    bindings: dict[tuple[int, int], TaskEvidenceBinding] = {}
    for raw in payload["task_attempt_bindings"]:
        ids = tuple(raw["evidence_ids"])
        binding = TaskEvidenceBinding(
            task_id=raw["task_id"],
            task_attempt=raw["task_attempt"],
            dimension=raw["dimension"],
            query=raw["query"],
            evidence=tuple(admitted[item] for item in ids),
        )
        bindings[(binding.task_id, binding.task_attempt)] = binding
    accepted = {int(key): value for key, value in payload["accepted_attempts"].items()}
    prepared = PreparedResearch(
        request=request,
        profile=profile,
        provider=provider,
        detection=detection,
        targets=targets,
        collections=collections,
        tasks=tasks,
        provider_context=context,
        source_context={
            "provider_ids": [provider.provider_id],
            "targets": [_target_to_dict(item) for item in targets],
            "collection_count": len(collections),
        },
        web_evidence=web_evidence,
        _task_bindings=bindings,
        _accepted_attempts=accepted,
        _attempt_highwater={int(k): v for k, v in payload.get("attempt_highwater", {}).items()},
        _admitted_evidence=admitted,
        _enrichment_limitations=list(payload["enrichment_limitations"]),
    )
    return prepared


__all__ = [
    "EvidenceRecoveryError",
    "persist_evidence_recovery",
    "restore_prepared_research",
    "sanitize_evidence_recovery",
    "validate_evidence_recovery",
]
