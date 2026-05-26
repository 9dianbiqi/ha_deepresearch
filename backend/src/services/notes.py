"""Helpers for note content formatting used by NoteSubAgent."""

from __future__ import annotations


def build_note_context(task_id: int, note_id: str, note_content: str) -> str:
    """Format note content for injection into agent prompts."""
    return (
        f"任务 {task_id} 笔记（ID: {note_id}）：\n"
        f"{note_content}\n"
    )


def build_note_tags(task_id: int) -> list[str]:
    """Return standard note tags for a task."""
    return ["deep_research", f"task_{task_id}"]
