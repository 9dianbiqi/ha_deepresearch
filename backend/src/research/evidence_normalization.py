"""Pure normalization from provider collections to canonical evidence records."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from .intelligence import (
    EvidenceLocator,
    EvidenceRecord,
    SourceReference,
    stable_evidence_id,
)
from .sources import SourceCollection


def _text(value: object, fallback: str) -> str:
    """Return one bounded provider text field with a deterministic fallback."""
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def _hash(value: object) -> str:
    """Hash JSON-compatible provider data independently of capture time."""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _safe_attributes(value: Mapping[str, Any]) -> dict[str, Any]:
    """Detach JSON-compatible attributes without adding run-local state."""
    try:
        detached = json.loads(json.dumps(dict(value), ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return {}
    return detached if isinstance(detached, dict) else {}


def _locator(record: Mapping[str, Any], *, url: str) -> EvidenceLocator:
    """Build an addressable locator from a provider record."""
    raw_locator = record.get("locator")
    if isinstance(raw_locator, Mapping):
        try:
            return EvidenceLocator.from_dict(raw_locator)
        except (TypeError, ValueError):
            pass
    file_path = record.get("file_path") if isinstance(record.get("file_path"), str) else None
    line_start = record.get("line_start") if isinstance(record.get("line_start"), int) else None
    line_end = record.get("line_end") if isinstance(record.get("line_end"), int) else None
    if file_path and line_start is not None and line_end is not None:
        try:
            return EvidenceLocator(
                locator_type="line",
                url=url,
                file_path=file_path,
                line_start=line_start,
                line_end=line_end,
            )
        except ValueError:
            pass
    page_start = record.get("page_start") if isinstance(record.get("page_start"), int) else None
    page_end = record.get("page_end") if isinstance(record.get("page_end"), int) else None
    if (page_start is None) != (page_end is None) or (
        page_start is not None and page_end is not None and page_end < page_start
    ):
        page_start = None
        page_end = None
    return EvidenceLocator(
        locator_type=_text(record.get("locator_type"), "record"),
        url=url,
        file_path=file_path,
        page_start=page_start,
        page_end=page_end,
    )


def _content_hash(collection: SourceCollection) -> str:
    """Return a stable collection hash, preferring a Web snapshot digest."""
    payload = collection.provider_payload
    snapshot = getattr(payload, "snapshot", None)
    snapshot_hash = getattr(snapshot, "content_hash", None)
    if isinstance(snapshot_hash, str) and snapshot_hash.strip():
        return snapshot_hash.strip()
    for payload_key in ("content_hash", "sha"):
        payload_hash = getattr(collection.provider_payload, payload_key, None)
        if isinstance(payload_hash, str) and payload_hash.strip():
            return payload_hash.strip()
    if isinstance(collection.provider_payload, Mapping):
        for key in ("content_hash", "sha"):
            value = collection.provider_payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    # Records are the bounded, provider-neutral representation.  This keeps a
    # complete GitHub collection stable across equivalent API response objects.
    return _hash(collection.records)


def normalize_collections(
    collections: Sequence[SourceCollection],
    max_excerpt_chars: int,
) -> tuple[EvidenceRecord, ...]:
    """Purely project collections into bounded canonical records.

    The function deliberately performs no searching, enrichment, quality-gate
    evaluation, or budget reservation.  It preserves source identity and
    capture provenance from each collection and de-duplicates only by the
    canonical evidence ID.
    """
    if (
        isinstance(max_excerpt_chars, bool)
        or not isinstance(max_excerpt_chars, int)
        or max_excerpt_chars <= 0
    ):
        raise ValueError("max_excerpt_chars must be a positive integer.")
    evidence: list[EvidenceRecord] = []
    seen: set[str] = set()
    for collection in collections:
        if not isinstance(collection, SourceCollection):
            continue
        target = collection.target
        source = SourceReference(
            provider_id=collection.provider_id,
            source_kind=collection.source_kind,
            source_id=target.source_id,
            canonical_url=target.canonical_url,
            requested_ref=target.requested_ref,
            resolved_version=collection.resolved_version,
            captured_at=collection.captured_at,
            content_hash=_content_hash(collection),
        )
        for index, record in enumerate(collection.records):
            excerpt = _text(
                record.get("excerpt") or record.get("summary") or record.get("text"),
                "Provider record",
            )[:max_excerpt_chars]
            url = _text(
                record.get("url") or record.get("source_url"),
                target.canonical_url,
            )
            locator = _locator(record, url=url)
            evidence_id = stable_evidence_id(source, locator=locator, excerpt=excerpt)
            if evidence_id in seen:
                continue
            seen.add(evidence_id)
            attributes = _safe_attributes(record)
            # Keep provider supplied attributes intact.  Dimension ownership is
            # represented by TaskEvidenceBinding, so this function never writes
            # a run/task dimension onto a shared record.
            attributes["provider_collection_status"] = collection.collection_status
            attributes["notice_codes"] = list(collection.notice_codes)
            evidence.append(
                EvidenceRecord(
                    evidence_id=evidence_id,
                    source=source,
                    evidence_type=_text(record.get("evidence_type"), "provider_record"),
                    evidence_level=_text(
                        record.get("evidence_level"),
                        "abstract" if collection.source_kind == "paper" else "metadata",
                    ),
                    title=_text(record.get("title"), f"{target.source_id} record {index + 1}"),
                    excerpt=excerpt,
                    locator=locator,
                    attributes=attributes,
                )
            )
    return tuple(evidence)


__all__ = ["normalize_collections"]
