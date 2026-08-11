"""Independent note sub-agent that wraps NoteTool with a clean method interface.

All note operations go through this sub-agent — the main research agents
never see ``[TOOL_CALL:...]`` markers in their text output.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from threading import Lock, RLock
from typing import Any, Callable

from hello_agents.tools import NoteTool

from research.operations import OperationRejectedError, OperationScope
from research.session import CancellationRequestedError, DeadlineExceededError

logger = logging.getLogger(__name__)


class _WorkspaceToolState:
    """Share one index-bearing NoteTool behind a workspace lock."""

    def __init__(self, tool_factory: Callable[..., object]) -> None:
        self.lock = RLock()
        self.tool: object | None = None
        self.tool_factory = tool_factory


_WORKSPACE_STATES_LOCK = Lock()
_WORKSPACE_STATES: dict[tuple[str, int], _WorkspaceToolState] = {}
_SAFE_NOTE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def _workspace_state(
    workspace: Path,
    tool_factory: Callable[..., object],
) -> _WorkspaceToolState:
    key = (str(workspace.resolve(strict=False)), id(tool_factory))
    with _WORKSPACE_STATES_LOCK:
        state = _WORKSPACE_STATES.get(key)
        if state is None:
            state = _WorkspaceToolState(tool_factory)
            _WORKSPACE_STATES[key] = state
        return state


class NoteToolAdapter:
    """Encapsulates all NoteTool operations behind typed methods.

    Design principle: note CRUD happens outside the main agent text stream.
    Callers receive structured data (dicts, ids), never magic-string markers.
    """

    def __init__(
        self,
        workspace: str | Path | None = None,
        *,
        tool_factory: Callable[..., object] = NoteTool,
    ) -> None:
        """Bind the service to a shared NoteTool for the selected workspace."""
        self._workspace = Path(workspace) if workspace is not None else Path("./notes")
        self._expose_path = workspace is not None
        self._state = _workspace_state(self._workspace, tool_factory)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_task_note(
        self,
        *,
        task_id: int,
        title: str,
        content: str = "",
        operation_scope: OperationScope | None = None,
    ) -> str | None:
        """Create a ``task_state`` note for one research task.

        Returns the ``note_id`` on success, ``None`` on failure.
        """
        tags = ["deep_research", f"task_{task_id}"]
        payload = {
            "action": "create",
            "task_id": task_id,
            "title": f"任务 {task_id}: {title}",
            "note_type": "task_state",
            "tags": tags,
            "content": content or f"任务概览：{title}",
        }
        response = self._run(
            payload,
            operation_scope=operation_scope,
            operation_name="notes.create",
            capabilities=("notes:write",),
            resource={"action": "create", "note_kind": "task_state"},
        )
        note_id = self._parse_note_id(response)
        if note_id:
            logger.info("Created task note: task_id=%d note_id=%s", task_id, note_id)
        else:
            logger.warning("Failed to create task note: task_id=%d", task_id)
        return note_id

    def read_note(
        self,
        note_id: str,
        *,
        operation_scope: OperationScope | None = None,
    ) -> dict[str, Any]:
        """Read a note and return structured content."""
        response = self._run(
            {"action": "read", "note_id": note_id},
            operation_scope=operation_scope,
            operation_name="notes.read",
            capabilities=("notes:read", "notes:write"),
            resource={"action": "read", "note_id": note_id},
        )
        return self._parse_note_response(response, note_id)

    def update_note(
        self,
        note_id: str,
        *,
        task_id: int | None = None,
        title: str | None = None,
        content: str,
        note_type: str = "task_state",
        operation_scope: OperationScope | None = None,
    ) -> str | None:
        """Update an existing note. Returns note_id on success."""
        payload: dict[str, Any] = {
            "action": "update",
            "note_id": note_id,
            "content": content,
            "note_type": note_type,
        }
        if task_id is not None:
            payload["task_id"] = task_id
            payload["tags"] = ["deep_research", f"task_{task_id}"]
        if title:
            payload["title"] = title

        response = self._run(
            payload,
            operation_scope=operation_scope,
            operation_name="notes.update",
            capabilities=("notes:read", "notes:write"),
            resource={
                "action": "update",
                "note_kind": note_type,
                "note_id": note_id,
            },
        )
        if response.startswith("❌"):
            logger.warning("Note update failed: note_id=%s", note_id)
            return None
        return note_id

    def create_conclusion_note(
        self,
        *,
        title: str,
        content: str,
        operation_scope: OperationScope | None = None,
    ) -> str | None:
        """Create a ``conclusion`` note for the final report."""
        payload = {
            "action": "create",
            "title": title,
            "note_type": "conclusion",
            "tags": ["deep_research", "report"],
            "content": content,
        }
        response = self._run(
            payload,
            operation_scope=operation_scope,
            operation_name="notes.create",
            capabilities=("notes:write",),
            resource={"action": "create", "note_kind": "conclusion"},
        )
        note_id = self._parse_note_id(response)
        if note_id:
            logger.info("Created conclusion note: note_id=%s", note_id)
        return note_id

    def read_all_task_notes(
        self,
        note_ids: list[str],
        *,
        operation_scope: OperationScope | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Batch-read task notes and return {note_id: parsed_content}."""
        result: dict[str, dict[str, Any]] = {}
        for note_id in note_ids:
            if note_id:
                try:
                    result[note_id] = self.read_note(
                        note_id,
                        operation_scope=operation_scope,
                    )
                except (
                    OperationRejectedError,
                    CancellationRequestedError,
                    DeadlineExceededError,
                ):
                    raise
                except Exception:
                    logger.error("Failed to read note: note_id=%s", note_id)
                    result[note_id] = {"error": "读取失败"}
        return result

    def note_path(self, note_id: str) -> str | None:
        """Return a safe workspace-relative display path for one note."""
        if self._expose_path and _SAFE_NOTE_ID_RE.fullmatch(note_id):
            return f"{note_id}.md"
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run(
        self,
        payload: dict[str, Any],
        *,
        operation_scope: OperationScope | None,
        operation_name: str,
        capabilities: tuple[str, ...],
        resource: dict[str, object],
    ) -> str:
        """Construct and call NoteTool only inside the governed callback."""

        def invoke() -> str:
            with self._state.lock:
                tool = self._state.tool
                if tool is None:
                    tool = self._state.tool_factory(workspace=str(self._workspace))
                    self._state.tool = tool
                run = getattr(tool, "run")
                return str(run(dict(payload)))

        if operation_scope is None:
            return invoke()
        spec = operation_scope.spec(
            operation_name=operation_name,
            capabilities=capabilities,
            resource=resource,
        )
        return operation_scope.operations.call(spec, invoke)

    @staticmethod
    def _parse_note_id(response: str) -> str | None:
        """Extract note ID from a create/update response."""
        if not response:
            return None
        match = re.search(r"ID:\s*([^\n]+)", response)
        if match:
            return match.group(1).strip()
        return None

    @staticmethod
    def _parse_note_response(response: str, note_id: str) -> dict[str, Any]:
        """Parse a note read response into structured data."""
        if not response:
            return {"note_id": note_id, "content": ""}
        if response.startswith("❌"):
            return {"note_id": note_id, "error": response}
        return {"note_id": note_id, "content": response}


NoteSubAgent = NoteToolAdapter


__all__ = ["NoteSubAgent", "NoteToolAdapter"]
