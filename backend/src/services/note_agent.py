"""Independent note sub-agent that wraps NoteTool with a clean method interface.

All note operations go through this sub-agent — the main research agents
never see ``[TOOL_CALL:...]`` markers in their text output.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

from hello_agents.tools.builtin.note_tool import NoteTool

logger = logging.getLogger(__name__)


class NoteSubAgent:
    """Encapsulates all NoteTool operations behind typed methods.

    Design principle: note CRUD happens outside the main agent text stream.
    Callers receive structured data (dicts, ids), never magic-string markers.
    """

    def __init__(self, workspace: str | Path | None = None) -> None:
        self._tool = NoteTool(workspace=str(workspace)) if workspace else NoteTool()
        self._workspace = workspace

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_task_note(
        self,
        *,
        task_id: int,
        title: str,
        content: str = "",
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
        response = self._tool.run(payload)
        note_id = self._parse_note_id(response)
        if note_id:
            logger.info("Created task note: task_id=%d note_id=%s", task_id, note_id)
        else:
            logger.warning("Failed to create note for task_id=%d: %s", task_id, response)
        return note_id

    def read_note(self, note_id: str) -> dict[str, Any]:
        """Read a note and return structured content."""
        response = self._tool.run({"action": "read", "note_id": note_id})
        return self._parse_note_response(response, note_id)

    def update_note(
        self,
        note_id: str,
        *,
        task_id: int | None = None,
        title: str | None = None,
        content: str,
        note_type: str = "task_state",
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

        response = self._tool.run(payload)
        if response.startswith("❌"):
            logger.warning("Note update failed: note_id=%s response=%s", note_id, response)
            return None
        return note_id

    def create_conclusion_note(
        self,
        *,
        title: str,
        content: str,
    ) -> str | None:
        """Create a ``conclusion`` note for the final report."""
        payload = {
            "action": "create",
            "title": title,
            "note_type": "conclusion",
            "tags": ["deep_research", "report"],
            "content": content,
        }
        response = self._tool.run(payload)
        note_id = self._parse_note_id(response)
        if note_id:
            logger.info("Created conclusion note: note_id=%s", note_id)
        return note_id

    def read_all_task_notes(self, note_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Batch-read task notes and return {note_id: parsed_content}."""
        result: dict[str, dict[str, Any]] = {}
        for note_id in note_ids:
            if note_id:
                try:
                    result[note_id] = self.read_note(note_id)
                except Exception:
                    logger.exception("Failed to read note %s", note_id)
                    result[note_id] = {"error": "读取失败"}
        return result

    def note_path(self, note_id: str) -> str | None:
        """Return the filesystem path for a note if workspace is configured."""
        if self._workspace:
            return str(Path(self._workspace) / f"{note_id}.md")
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_note_id(response: str) -> Optional[str]:
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
