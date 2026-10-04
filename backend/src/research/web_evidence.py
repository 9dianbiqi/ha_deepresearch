"""Share run-local page captures and select task-relevant paragraph evidence."""

from __future__ import annotations

import re
from _thread import LockType
from collections.abc import Mapping, Sequence
from dataclasses import replace
from threading import Lock, RLock
from typing import Any

from .evidence_normalization import normalize_collections
from .providers.web import WebSourceProvider
from .sources import (
    BudgetExceededError,
    ProviderContext,
    SourceCollection,
    SourceTarget,
)
from .task_quality import TaskEvidence
from .web_capture import WebCaptureResult, canonicalize_web_url


def _terms(text: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9][a-z0-9_.+-]+", text.casefold()))
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        words.update(run[index : index + 2] for index in range(len(run) - 1))
    return words - {"the", "and", "for", "with", "what", "how", "this", "that"}


def select_records(
    records: Sequence[Mapping[str, Any]],
    intent: str,
    *,
    limit: int = 3,
) -> tuple[Mapping[str, Any], ...]:
    """Select relevant passages and adjacent context without asserting support."""
    if limit <= 0:
        return ()
    expected = _terms(intent)

    def score(index: int) -> tuple[float, int]:
        record = records[index]
        observed = _terms(str(record.get("excerpt", "")))
        headings = _terms(" ".join(record.get("section_path", ())))
        relevance = len(expected & observed) + 0.5 * len(expected & headings)
        return relevance, -index

    ranked = sorted(range(len(records)), key=score, reverse=True)
    selected = ranked[: max(1, limit - 1)]
    # Reserve one slot for the neighboring qualification or explanation.
    if selected and len(selected) < limit:
        best = selected[0]
        for neighbor in (best + 1, best - 1):
            if 0 <= neighbor < len(records) and neighbor not in selected:
                selected.append(neighbor)
                break
    for index in ranked:
        if len(selected) >= limit:
            break
        if index not in selected:
            selected.append(index)
    return tuple(records[index] for index in sorted(selected))


def _excerpt(text: str, intent: str, limit: int) -> str:
    """Retain an exact relevant window when a paragraph exceeds the budget."""
    if len(text) <= limit:
        return text.strip()
    terms = _terms(intent)
    starts = list(range(0, len(text) - limit + 1, max(1, limit // 2)))
    starts.append(len(text) - limit)
    start = max(
        starts,
        key=lambda offset: (
            len(terms & _terms(text[offset : offset + limit])),
            -offset,
        ),
    )
    return text[start : start + limit].strip()


class RunWebEvidence:
    """Own one run's shared captures and selected evidence ledger."""

    def __init__(self) -> None:
        """Initialize run-local caches; never share page bodies across runs."""
        self._lock = RLock()
        self._page_locks: dict[str, LockType] = {}
        self._captures: dict[str, WebCaptureResult] = {}
        self._collections: dict[tuple[str, str], SourceCollection] = {}
        self._record_keys: set[tuple[str, str, str]] = set()

    def read(
        self,
        provider: WebSourceProvider,
        results: Sequence[Mapping[str, Any]],
        context: ProviderContext,
        *,
        intent: str,
        dimension: str,
        max_excerpt_chars: int,
        limit: int = 12,
    ) -> tuple[SourceCollection, ...]:
        """Capture each URL once and reserve only selected paragraph records."""
        collections: list[SourceCollection] = []
        seen: set[str] = set()
        remaining = limit
        for result in results:
            context.cancellation.raise_if_cancelled()
            if remaining <= 0:
                break
            url = canonicalize_web_url(str(result.get("url", "")))
            if url in seen:
                continue
            seen.add(url)
            with self._lock:
                page_lock = self._page_locks.setdefault(url, Lock())
            while not page_lock.acquire(timeout=0.1):
                context.cancellation.raise_if_cancelled()
            try:
                context.cancellation.raise_if_cancelled()
                with self._lock:
                    capture = self._captures.get(url)
                if capture is None:
                    capture = provider.capture_search_result(result, context)
                    with self._lock:
                        self._captures[url] = capture
            finally:
                page_lock.release()
            records = select_records(
                capture.as_records(dimension=dimension), intent, limit=min(3, remaining)
            )
            selected: list[Mapping[str, Any]] = []
            with self._lock:
                for record in records:
                    excerpt = _excerpt(
                        str(record["excerpt"]), intent, max_excerpt_chars
                    )
                    locator = dict(record["locator"])
                    if "fragment" in locator:
                        locator["fragment"] = excerpt[:1024]
                    bounded = {**record, "excerpt": excerpt, "locator": locator}
                    key = (
                        capture.source_id,
                        str(record.get("paragraph_id", "metadata")),
                        str(bounded["excerpt"]),
                    )
                    if key not in self._record_keys:
                        if context.budget.remaining()["evidence"] <= 0:
                            continue
                        try:
                            context.budget.reserve(evidence=1)
                        except BudgetExceededError:
                            # A repository worker may reserve the last unit
                            # after remaining() was read. Commit earlier selected
                            # passages rather than leaving orphaned ledger keys.
                            continue
                        self._record_keys.add(key)
                    selected.append(bounded)
                collection = SourceCollection(
                    provider_id="web",
                    source_kind="web_page",
                    target=SourceTarget(
                        provider_id="web",
                        source_kind="web_page",
                        source_id=capture.source_id,
                        canonical_url=capture.canonical_url,
                    ),
                    collection_status=capture.status,
                    provider_payload=capture,
                    records=tuple(selected),
                    captured_at=capture.captured_at,
                    notices=capture.notices,
                    notice_codes=capture.notice_codes,
                )
                ledger_key = (capture.source_id, dimension)
                prior = self._collections.get(ledger_key)
                merged = list(prior.records) if prior else []
                for record in selected:
                    if record not in merged:
                        merged.append(record)
                self._collections[ledger_key] = replace(
                    collection, records=tuple(merged)
                )
            collections.append(collection)
            remaining -= len(selected)
        return tuple(collections)

    def collections(self) -> tuple[SourceCollection, ...]:
        """Return selected evidence and original snapshots for report finalization."""
        with self._lock:
            return tuple(self._collections.values())

    def restore_state(self, *, captures, collections, record_keys) -> None:
        """Restore a detached ledger into fresh locks without any network reads."""
        with self._lock:
            self._captures = dict(captures)
            self._page_locks = {}
            self._record_keys = set(record_keys)
            self._collections = {}
            for collection in collections:
                dimension = str(collection.records[0].get("dimension", "overview")) if collection.records else "overview"
                self._collections[(collection.target.source_id, dimension)] = collection


def task_evidence_from_collections(
    collections: Sequence[SourceCollection],
    *,
    query: str,
) -> tuple[TaskEvidence, ...]:
    """Use the canonical report evidence identity and exact selected excerpt."""
    records = normalize_collections(collections, max_excerpt_chars=2_000)
    return tuple(TaskEvidence.from_record(record, query) for record in records)
