"""Local, rebuildable research history index and bounded related-run recall."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import sqlite3
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from .contracts import RunSnapshot, RunStatus
from .repository import FileRunRepository

_LOGGER = logging.getLogger(__name__)
_INDEX_SCHEMA_VERSION = 1
_MAX_TOPIC_CHARS = 240
_MAX_FINDINGS = 5
_MAX_SOURCES = 3
_MAX_OPEN_QUESTIONS = 10
_MAX_ITEM_CHARS = 180
_MAX_RECALL_MATCHES = 3
_MAX_FTS_CANDIDATES = 50
_MIN_RECALL_SCORE = 0.35
_MAX_RECALL_TOPIC_CHARS = 100
_MAX_RECALL_ITEM_CHARS = 120
_CURSOR_VERSION = 1
_ASCII_TOKEN_RE = re.compile(r"[a-z0-9]+")
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")


class HistoryCursorError(ValueError):
    """Raised when a history pagination cursor is malformed."""


@dataclass(frozen=True, slots=True)
class HistoryPage:
    """One stable page of completed research summaries."""

    items: tuple[dict[str, Any], ...]
    next_cursor: str | None

    def as_dict(self) -> dict[str, Any]:
        """Return the public JSON representation."""
        return {"items": [dict(item) for item in self.items], "next_cursor": self.next_cursor}


def _clean_text(value: object, limit: int) -> str:
    """Return bounded, stripped text or an empty string."""
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _text_list(value: object, *, limit: int, item_limit: int) -> list[str]:
    """Return a bounded list of safe text items."""
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value:
        cleaned = _clean_text(item, item_limit)
        if not cleaned:
            continue
        result.append(cleaned)
        if len(result) >= limit:
            break
    return result


def _snapshot_context(snapshot: RunSnapshot) -> tuple[list[str], list[str], list[str]]:
    """Extract the already-redacted bounded follow-up context."""
    context = snapshot.followup_context
    return (
        _text_list(context.get("key_findings"), limit=_MAX_FINDINGS, item_limit=_MAX_ITEM_CHARS),
        _text_list(context.get("key_sources"), limit=_MAX_SOURCES, item_limit=_MAX_ITEM_CHARS),
        _text_list(context.get("open_questions"), limit=_MAX_OPEN_QUESTIONS, item_limit=_MAX_ITEM_CHARS),
    )


def _normalize_text(value: str) -> str:
    """Normalize text for deterministic Chinese/English tokenization."""
    return unicodedata.normalize("NFKC", value).casefold()


def _tokens(value: str) -> tuple[str, ...]:
    """Build ASCII keyword and overlapping CJK bigram tokens."""
    normalized = _normalize_text(value)
    tokens: list[str] = []
    tokens.extend(_ASCII_TOKEN_RE.findall(normalized))
    for run in _CJK_RUN_RE.findall(normalized):
        tokens.extend(run)
        tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    return tuple(dict.fromkeys(token for token in tokens if token))


def _searchable_text(topic: str, findings: Sequence[str], sources: Sequence[str], questions: Sequence[str]) -> str:
    """Create a compact tokenized document for FTS5 and fallback scans."""
    return " ".join(
        _tokens(" ".join([topic, *findings, *sources, *questions]))
    )


def _encode_cursor(completed_at: str, run_id: str) -> str:
    """Encode an opaque, URL-safe history cursor."""
    payload = json.dumps(
        {"v": _CURSOR_VERSION, "completed_at": completed_at, "run_id": run_id},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(value: str | None) -> tuple[str, str] | None:
    """Decode and validate an opaque history cursor."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise HistoryCursorError("History cursor is invalid.")
    try:
        padded = value + "=" * (-len(value) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise HistoryCursorError("History cursor is invalid.") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("v") != _CURSOR_VERSION
        or not isinstance(payload.get("completed_at"), str)
        or not isinstance(payload.get("run_id"), str)
    ):
        raise HistoryCursorError("History cursor is invalid.")
    return payload["completed_at"], payload["run_id"]


class ResearchHistoryStore:
    """Maintain a derived SQLite index while keeping snapshots canonical."""

    def __init__(self, repository: FileRunRepository) -> None:
        """Open the local index and best-effort rebuild it from snapshots."""
        self._repository = repository
        self._path = repository.root / "research-history.db"
        self._lock = RLock()
        self._fts_available = False
        self._ready = False
        self._index_dirty = False
        self._recovery_attempted = False
        self._ensure_schema()
        self.rebuild()

    @property
    def path(self) -> Path:
        """Return the derived index path for diagnostics and tests."""
        return self._path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._path,
            timeout=5,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        """Create the normal table and use FTS5 when the runtime supports it."""
        with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                connection = self._connect()
                try:
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS history_runs (
                            run_id TEXT PRIMARY KEY,
                            topic TEXT NOT NULL,
                            started_at TEXT NOT NULL,
                            completed_at TEXT NOT NULL,
                            parent_run_id TEXT,
                            task_count INTEGER NOT NULL DEFAULT 0,
                            report_excerpt TEXT NOT NULL DEFAULT '',
                            findings_json TEXT NOT NULL,
                            sources_json TEXT NOT NULL,
                            questions_json TEXT NOT NULL,
                            searchable_text TEXT NOT NULL
                        )
                        """
                    )
                    columns = {
                        row[1]
                        for row in connection.execute("PRAGMA table_info(history_runs)")
                    }
                    if "started_at" not in columns:
                        connection.execute(
                            "ALTER TABLE history_runs ADD COLUMN started_at TEXT NOT NULL DEFAULT ''"
                        )
                    if "task_count" not in columns:
                        connection.execute(
                            "ALTER TABLE history_runs ADD COLUMN task_count INTEGER NOT NULL DEFAULT 0"
                        )
                    if "report_excerpt" not in columns:
                        connection.execute(
                            "ALTER TABLE history_runs ADD COLUMN report_excerpt TEXT NOT NULL DEFAULT ''"
                        )
                    try:
                        connection.execute(
                            """
                            CREATE VIRTUAL TABLE IF NOT EXISTS history_runs_fts
                            USING fts5(run_id UNINDEXED, topic, searchable_text)
                            """
                        )
                        self._fts_available = True
                    except sqlite3.OperationalError:
                        self._fts_available = False
                    connection.execute(f"PRAGMA user_version = {_INDEX_SCHEMA_VERSION}")
                    connection.commit()
                    self._ready = True
                finally:
                    connection.close()
            except (OSError, sqlite3.DatabaseError) as exc:
                self._ready = False
                self._index_dirty = True
                if (
                    isinstance(exc, sqlite3.DatabaseError)
                    and not self._recovery_attempted
                    and self._quarantine_corrupt_index()
                ):
                    self._recovery_attempted = True
                    self._ensure_schema()
                    if self._ready:
                        return
                _LOGGER.warning("Research history index is unavailable; continuing without memory.")

    def _quarantine_corrupt_index(self) -> bool:
        """Move an unreadable derived index aside so it can be rebuilt safely."""
        if not self._path.is_file():
            return False
        quarantine = self._path.with_name(
            f"{self._path.name}.corrupt-{uuid4().hex[:12]}"
        )
        try:
            os.replace(self._path, quarantine)
        except OSError:
            return False
        return True

    def rebuild(self) -> None:
        """Rebuild the derived index, skipping corrupt canonical records."""
        if not self._ready:
            return
        snapshots = self._repository.iter_snapshots(status=RunStatus.COMPLETED)
        with self._lock:
            try:
                connection = self._connect()
                try:
                    connection.execute("DELETE FROM history_runs")
                    if self._fts_available:
                        connection.execute("DELETE FROM history_runs_fts")
                    for snapshot in snapshots:
                        self._upsert_locked(connection, snapshot)
                    connection.commit()
                    self._index_dirty = False
                finally:
                    connection.close()
            except (OSError, sqlite3.DatabaseError):
                self._index_dirty = True
                _LOGGER.warning("Research history index rebuild failed; continuing without memory.")

    def index_snapshot(self, snapshot: RunSnapshot) -> None:
        """Index one completed snapshot after canonical persistence succeeds."""
        if not self._ready or snapshot.status is not RunStatus.COMPLETED:
            return
        with self._lock:
            try:
                connection = self._connect()
                try:
                    self._upsert_locked(connection, snapshot)
                    connection.commit()
                finally:
                    connection.close()
            except (OSError, sqlite3.DatabaseError):
                self._index_dirty = True
                _LOGGER.warning("Research history index update failed; run remains durable.")

    def _upsert_locked(self, connection: sqlite3.Connection, snapshot: RunSnapshot) -> None:
        completed_at = snapshot.completed_at or snapshot.started_at
        findings, sources, questions = _snapshot_context(snapshot)
        topic = _clean_text(snapshot.topic, _MAX_TOPIC_CHARS)
        searchable = _searchable_text(topic, findings, sources, questions)
        output = snapshot.output
        raw_tasks = output.get("todo_items")
        task_count = len(raw_tasks) if isinstance(raw_tasks, (list, tuple)) else 0
        report = output.get("report_markdown") or output.get("running_summary") or ""
        report_excerpt = _clean_text(report, 280)
        connection.execute(
            """
            INSERT INTO history_runs
                (run_id, topic, started_at, completed_at, parent_run_id, task_count,
                 report_excerpt, findings_json, sources_json, questions_json, searchable_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                topic=excluded.topic,
                started_at=excluded.started_at,
                completed_at=excluded.completed_at,
                parent_run_id=excluded.parent_run_id,
                task_count=excluded.task_count,
                report_excerpt=excluded.report_excerpt,
                findings_json=excluded.findings_json,
                sources_json=excluded.sources_json,
                questions_json=excluded.questions_json,
                searchable_text=excluded.searchable_text
            """,
            (
                snapshot.run_id,
                topic,
                snapshot.started_at.isoformat(),
                completed_at.isoformat(),
                snapshot.parent_run_id,
                task_count,
                report_excerpt,
                json.dumps(findings, ensure_ascii=False),
                json.dumps(sources, ensure_ascii=False),
                json.dumps(questions, ensure_ascii=False),
                searchable,
            ),
        )
        if self._fts_available:
            connection.execute(
                "DELETE FROM history_runs_fts WHERE run_id = ?",
                (snapshot.run_id,),
            )
            connection.execute(
                "INSERT INTO history_runs_fts (run_id, topic, searchable_text) VALUES (?, ?, ?)",
                (snapshot.run_id, topic, searchable),
            )

    def list_runs(self, *, limit: int = 20, cursor: str | None = None) -> HistoryPage:
        """Return completed history summaries in stable newest-first order."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("History limit must be between 1 and 100.")
        decoded = _decode_cursor(cursor)
        if self._ready and not self._index_dirty:
            try:
                with self._lock:
                    connection = self._connect()
                    try:
                        rows = self._list_rows(connection, limit, decoded)
                    finally:
                        connection.close()
                return self._page_from_rows(rows, limit)
            except (OSError, sqlite3.DatabaseError):
                _LOGGER.warning("Research history list index failed; using canonical scan.")
        return self._repository.list_summaries(limit=limit, cursor=cursor)

    @staticmethod
    def _list_rows(
        connection: sqlite3.Connection,
        limit: int,
        cursor: tuple[str, str] | None,
    ) -> list[sqlite3.Row]:
        if cursor is None:
            return list(
                connection.execute(
                    "SELECT * FROM history_runs ORDER BY completed_at DESC, run_id DESC LIMIT ?",
                    (limit + 1,),
                )
            )
        completed_at, run_id = cursor
        return list(
            connection.execute(
                """
                SELECT * FROM history_runs
                WHERE completed_at < ? OR (completed_at = ? AND run_id < ?)
                ORDER BY completed_at DESC, run_id DESC
                LIMIT ?
                """,
                (completed_at, completed_at, run_id, limit + 1),
            )
        )

    @staticmethod
    def _page_from_rows(rows: list[sqlite3.Row], limit: int) -> HistoryPage:
        has_more = len(rows) > limit
        visible = rows[:limit]
        items: list[dict[str, Any]] = []
        for row in visible:
            items.append(
                {
                    "run_id": row["run_id"],
                    "topic": row["topic"],
                    "status": RunStatus.COMPLETED.value,
                    "started_at": row["started_at"] or row["completed_at"],
                    "completed_at": row["completed_at"],
                    "parent_run_id": row["parent_run_id"],
                    "task_count": int(row["task_count"] or 0),
                    "report_excerpt": row["report_excerpt"] or "",
                    "resumable": True,
                    "last_resumable_parent": None,
                }
            )
        next_cursor = None
        if has_more and visible:
            last = visible[-1]
            next_cursor = _encode_cursor(last["completed_at"], last["run_id"])
        return HistoryPage(items=tuple(items), next_cursor=next_cursor)

    def recall(
        self,
        topic: str,
        *,
        exclude_run_ids: Iterable[str] = (),
    ) -> tuple[dict[str, Any], ...]:
        """Return at most three bounded, high-relevance related runs."""
        query_tokens = set(_tokens(topic))
        if not query_tokens or not self._ready or self._index_dirty:
            return ()
        excluded = set(exclude_run_ids)
        try:
            with self._lock:
                connection = self._connect()
                try:
                    rows = self._recall_rows(connection, query_tokens)
                finally:
                    connection.close()
        except (OSError, sqlite3.DatabaseError):
            _LOGGER.warning("Research history recall failed; continuing without memory.")
            return ()

        matches: list[dict[str, Any]] = []
        for row in rows:
            run_id = row["run_id"]
            if run_id in excluded:
                continue
            document_tokens = set(row["searchable_text"].split())
            overlap = query_tokens.intersection(document_tokens)
            if not overlap:
                continue
            coverage = len(overlap) / max(len(query_tokens), 1)
            specificity = len(overlap) / max(len(document_tokens), 1)
            score = min(1.0, 0.7 * coverage + 0.3 * specificity)
            normalized_topic = _normalize_text(row["topic"])
            normalized_query = _normalize_text(topic).strip()
            if normalized_query and normalized_query in normalized_topic:
                score = max(score, 0.9)
            if score < _MIN_RECALL_SCORE:
                continue
            try:
                findings = json.loads(row["findings_json"])
                sources = json.loads(row["sources_json"])
                questions = json.loads(row["questions_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not all(
                isinstance(values, list)
                for values in (findings, sources, questions)
            ):
                continue
            matches.append(
                {
                    "run_id": run_id,
                    "topic": row["topic"][:_MAX_RECALL_TOPIC_CHARS],
                    "completed_at": row["completed_at"],
                    "score": round(score, 4),
                    "key_findings": [
                        item[:_MAX_RECALL_ITEM_CHARS]
                        for item in findings
                        if isinstance(item, str)
                    ][:2],
                    "key_sources": [
                        item[:_MAX_RECALL_ITEM_CHARS]
                        for item in sources
                        if isinstance(item, str)
                    ][:1],
                    "open_questions": [
                        item[:_MAX_RECALL_ITEM_CHARS]
                        for item in questions
                        if isinstance(item, str)
                    ][:2],
                }
            )
        matches.sort(
            key=lambda item: (item["completed_at"], item["run_id"]),
            reverse=True,
        )
        matches.sort(key=lambda item: item["score"], reverse=True)
        return tuple(matches[:_MAX_RECALL_MATCHES])

    def _recall_rows(
        self,
        connection: sqlite3.Connection,
        query_tokens: set[str],
    ) -> list[sqlite3.Row]:
        if self._fts_available:
            query = " OR ".join(
                '"' + token.replace('"', '""') + '"'
                for token in sorted(query_tokens)
            )
            rows = list(
                connection.execute(
                    """
                    SELECT r.* FROM history_runs r
                    JOIN history_runs_fts f ON f.run_id = r.run_id
                    WHERE history_runs_fts MATCH ?
                    ORDER BY bm25(history_runs_fts) ASC
                    LIMIT ?
                    """,
                    (query, _MAX_FTS_CANDIDATES),
                )
            )
            if rows:
                return rows
        return list(
            connection.execute(
                "SELECT * FROM history_runs ORDER BY completed_at DESC, run_id DESC LIMIT ?",
                (_MAX_FTS_CANDIDATES,),
            )
        )


__all__ = ["HistoryCursorError", "HistoryPage", "ResearchHistoryStore"]
