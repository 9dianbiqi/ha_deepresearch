"""Explicit, user-controlled preference and fact memory."""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

_LOGGER = logging.getLogger(__name__)
_SCOPE_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_MAX_TEXT_CHARS = 240
_MAX_LIST_LIMIT = 100
_MAX_CONTEXT_ITEMS = 20
_MAX_CONTEXT_CHARS = 220
_STATUS_PENDING = "pending"
_STATUS_CONFIRMED = "confirmed"
_KIND_PREFERENCE = "preference"
_KIND_FACT = "fact"


class MemoryValidationError(ValueError):
    """Raised when a memory payload is outside the safe bounded contract."""


class MemoryNotFoundError(LookupError):
    """Raised when a memory ID is not present in the requested scope."""


class MemoryStateError(ValueError):
    """Raised when a memory cannot transition to the requested state."""


class MemoryStoreUnavailableError(RuntimeError):
    """Raised when the local memory database cannot be opened."""


@dataclass(frozen=True, slots=True)
class UserMemory:
    """One bounded, user-controlled memory item."""

    memory_id: str
    scope: str
    kind: str
    text: str
    status: str
    source: str
    created_at: str
    updated_at: str
    confirmed_at: str | None

    def as_dict(self) -> dict[str, Any]:
        """Return the public JSON representation."""
        return {
            "memory_id": self.memory_id,
            "scope": self.scope,
            "kind": self.kind,
            "text": self.text,
            "status": self.status,
            "source": self.source,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "confirmed_at": self.confirmed_at,
        }


def normalize_memory_scope(value: str) -> str:
    """Normalize a path-safe local memory scope."""
    if not isinstance(value, str):
        raise MemoryValidationError("Memory scope must be text.")
    scope = unicodedata.normalize("NFKC", value).strip().casefold()
    if not _SCOPE_RE.fullmatch(scope):
        raise MemoryValidationError(
            "Memory scope must use 1-64 lowercase letters, digits, '.', '_' or '-'."
        )
    return scope


def normalize_memory_kind(value: str) -> str:
    """Normalize and validate the two supported memory kinds."""
    if not isinstance(value, str):
        raise MemoryValidationError("Memory kind must be text.")
    kind = value.strip().casefold()
    if kind not in {_KIND_PREFERENCE, _KIND_FACT}:
        raise MemoryValidationError("Memory kind must be preference or fact.")
    return kind


def normalize_memory_text(value: str) -> str:
    """Normalize one explicit memory without accepting an empty or oversized value."""
    if not isinstance(value, str):
        raise MemoryValidationError("Memory text must be text.")
    text = unicodedata.normalize("NFKC", value).strip()
    if not text:
        raise MemoryValidationError("Memory text must not be empty.")
    if len(text) > _MAX_TEXT_CHARS:
        raise MemoryValidationError(
            f"Memory text must be at most {_MAX_TEXT_CHARS} characters."
        )
    return text


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class UserMemoryStore:
    """Persist only explicit candidates and user-confirmed local memory."""

    def __init__(self, root: str | Path) -> None:
        """Open the derived SQLite store and recover from a corrupt file."""
        self.root = Path(root)
        self._path = self.root / "user-memory.db"
        self._lock = RLock()
        self._ready = False
        self._recovery_attempted = False
        self._ensure_schema()

    @property
    def path(self) -> Path:
        """Return the local database path for diagnostics."""
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
        with self._lock:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                connection = self._connect()
                try:
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS user_memories (
                            memory_id TEXT PRIMARY KEY,
                            scope TEXT NOT NULL,
                            kind TEXT NOT NULL,
                            text TEXT NOT NULL,
                            status TEXT NOT NULL,
                            source TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            confirmed_at TEXT
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_user_memories_scope_status
                        ON user_memories(scope, status, updated_at DESC, memory_id DESC)
                        """
                    )
                    connection.commit()
                    self._ready = True
                finally:
                    connection.close()
            except (OSError, sqlite3.DatabaseError) as exc:
                self._ready = False
                if (
                    isinstance(exc, sqlite3.DatabaseError)
                    and not self._recovery_attempted
                    and self._quarantine_corrupt_file()
                ):
                    self._recovery_attempted = True
                    self._ensure_schema()
                    return
                _LOGGER.warning(
                    "User memory store is unavailable; continuing without memory."
                )

    def _quarantine_corrupt_file(self) -> bool:
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

    def _require_ready(self) -> None:
        if not self._ready:
            raise MemoryStoreUnavailableError("User memory store is unavailable.")

    @staticmethod
    def _from_row(row: sqlite3.Row) -> UserMemory:
        return UserMemory(
            memory_id=str(row["memory_id"]),
            scope=str(row["scope"]),
            kind=str(row["kind"]),
            text=str(row["text"]),
            status=str(row["status"]),
            source=str(row["source"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            confirmed_at=(
                str(row["confirmed_at"])
                if row["confirmed_at"] is not None
                else None
            ),
        )

    def create_candidate(
        self,
        *,
        text: str,
        kind: str = _KIND_PREFERENCE,
        scope: str = "default",
    ) -> UserMemory:
        """Create a pending candidate; it is never used by research yet."""
        self._require_ready()
        normalized_scope = normalize_memory_scope(scope)
        normalized_kind = normalize_memory_kind(kind)
        normalized_text = normalize_memory_text(text)
        memory_id = uuid4().hex
        now = _now()
        with self._lock:
            connection = self._connect()
            try:
                connection.execute(
                    """
                    INSERT INTO user_memories
                        (memory_id, scope, kind, text, status, source,
                         created_at, updated_at, confirmed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        memory_id,
                        normalized_scope,
                        normalized_kind,
                        normalized_text,
                        _STATUS_PENDING,
                        "explicit_user",
                        now,
                        now,
                    ),
                )
                connection.commit()
            finally:
                connection.close()
        return UserMemory(
            memory_id=memory_id,
            scope=normalized_scope,
            kind=normalized_kind,
            text=normalized_text,
            status=_STATUS_PENDING,
            source="explicit_user",
            created_at=now,
            updated_at=now,
            confirmed_at=None,
        )

    def get(self, memory_id: str, *, scope: str = "default") -> UserMemory:
        """Load one memory only inside its requested scope."""
        self._require_ready()
        normalized_scope = normalize_memory_scope(scope)
        if not isinstance(memory_id, str) or not re.fullmatch(r"[0-9a-f]{32}", memory_id):
            raise MemoryNotFoundError("Memory was not found.")
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT * FROM user_memories WHERE memory_id = ? AND scope = ?",
                    (memory_id, normalized_scope),
                ).fetchone()
            finally:
                connection.close()
        if row is None:
            raise MemoryNotFoundError("Memory was not found.")
        return self._from_row(row)

    def confirm(self, memory_id: str, *, scope: str = "default") -> UserMemory:
        """Promote one pending candidate after an explicit user action."""
        current = self.get(memory_id, scope=scope)
        if current.status == _STATUS_CONFIRMED:
            return current
        if current.status != _STATUS_PENDING:
            raise MemoryStateError("Memory is not awaiting confirmation.")
        now = _now()
        normalized_scope = normalize_memory_scope(scope)
        with self._lock:
            connection = self._connect()
            try:
                connection.execute(
                    """
                    UPDATE user_memories
                    SET status = ?, updated_at = ?, confirmed_at = ?
                    WHERE memory_id = ? AND scope = ? AND status = ?
                    """,
                    (
                        _STATUS_CONFIRMED,
                        now,
                        now,
                        memory_id,
                        normalized_scope,
                        _STATUS_PENDING,
                    ),
                )
                connection.commit()
            finally:
                connection.close()
        return self.get(memory_id, scope=normalized_scope)

    def delete(self, memory_id: str, *, scope: str = "default") -> bool:
        """Delete a pending or confirmed memory inside its scope."""
        self._require_ready()
        normalized_scope = normalize_memory_scope(scope)
        if not isinstance(memory_id, str) or not re.fullmatch(r"[0-9a-f]{32}", memory_id):
            return False
        with self._lock:
            connection = self._connect()
            try:
                cursor = connection.execute(
                    "DELETE FROM user_memories WHERE memory_id = ? AND scope = ?",
                    (memory_id, normalized_scope),
                )
                connection.commit()
                return cursor.rowcount > 0
            finally:
                connection.close()

    def list(
        self,
        *,
        scope: str = "default",
        include_pending: bool = True,
        limit: int = 50,
    ) -> tuple[UserMemory, ...]:
        """List bounded memory items, newest first."""
        self._require_ready()
        normalized_scope = normalize_memory_scope(scope)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= _MAX_LIST_LIMIT
        ):
            raise MemoryValidationError("Memory limit must be between 1 and 100.")
        status_clause = "" if include_pending else " AND status = ?"
        parameters: tuple[object, ...]
        if include_pending:
            parameters = (normalized_scope, limit)
        else:
            parameters = (normalized_scope, _STATUS_CONFIRMED, limit)
        with self._lock:
            connection = self._connect()
            try:
                rows = connection.execute(
                    """
                    SELECT * FROM user_memories
                    WHERE scope = ?
                    """
                    + status_clause
                    + " ORDER BY updated_at DESC, memory_id DESC LIMIT ?",
                    parameters,
                ).fetchall()
            finally:
                connection.close()
        return tuple(self._from_row(row) for row in rows)

    def confirmed_context(
        self,
        *,
        scope: str = "default",
        limit: int = _MAX_CONTEXT_ITEMS,
    ) -> tuple[dict[str, str], ...]:
        """Return only confirmed, bounded fields safe for planner context."""
        items = self.list(scope=scope, include_pending=False, limit=min(limit, _MAX_LIST_LIMIT))
        return tuple(
            {
                "memory_id": item.memory_id,
                "kind": item.kind,
                "text": item.text[:_MAX_CONTEXT_CHARS],
                "updated_at": item.updated_at,
            }
            for item in items[:_MAX_CONTEXT_ITEMS]
        )


__all__ = [
    "MemoryNotFoundError",
    "MemoryStateError",
    "MemoryStoreUnavailableError",
    "MemoryValidationError",
    "UserMemory",
    "UserMemoryStore",
    "normalize_memory_kind",
    "normalize_memory_scope",
    "normalize_memory_text",
]
