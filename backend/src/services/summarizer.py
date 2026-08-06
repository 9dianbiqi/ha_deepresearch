"""Task summarization utilities."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Tuple

from hello_agents import SimpleAgent

from config import Configuration
from models import SummaryState, TodoItem
from research.operations import OperationScope
from utils import strip_thinking_tokens


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskSummaryInput:
    """Detached read-only input consumed by one summarizer worker."""

    topic: str
    title: str
    intent: str
    query: str
    context: str
    note_id: str | None = None
    note_content: str = ""


class SummarizationService:
    """Handles synchronous and streaming task summarization."""

    def __init__(
        self,
        summarizer_factory: Callable[[], SimpleAgent],
        config: Configuration,
    ) -> None:
        """Store the per-task agent factory and research configuration."""
        self._agent_factory = summarizer_factory
        self._config = config

    def summarize_task(
        self,
        state: SummaryState,
        task: TodoItem,
        context: str,
        notes_context: dict[str, Any] | None = None,
        *,
        operation_scope: OperationScope | None = None,
    ) -> str:
        """Generate a summary through the historical state adapter."""
        return self.summarize(
            self._legacy_input(state, task, context, notes_context or {}),
            operation_scope=operation_scope,
        )

    def summarize(
        self,
        request: TaskSummaryInput,
        *,
        operation_scope: OperationScope | None = None,
    ) -> str:
        """Generate a task-specific summary from detached read-only input."""
        prompt = self._build_prompt(request)

        agent = self._agent_factory()
        try:
            if operation_scope is None:
                response = agent.run(prompt)
            else:
                response = agent.run(
                    prompt,
                    _research_operation_scope=operation_scope,
                )
        finally:
            agent.clear_history()

        summary_text = response.strip()
        if self._config.strip_thinking_tokens:
            summary_text = strip_thinking_tokens(summary_text)

        return summary_text or "暂无可用信息"

    def stream_task_summary(
        self,
        state: SummaryState,
        task: TodoItem,
        context: str,
        notes_context: dict[str, Any] | None = None,
        *,
        operation_scope: OperationScope | None = None,
    ) -> Tuple[Iterator[str], Callable[[], str]]:
        """Stream through the historical state adapter."""
        return self.stream_summary(
            self._legacy_input(state, task, context, notes_context or {}),
            operation_scope=operation_scope,
        )

    def stream_summary(
        self,
        request: TaskSummaryInput,
        *,
        operation_scope: OperationScope | None = None,
    ) -> Tuple[Iterator[str], Callable[[], str]]:
        """Stream summary text from detached read-only input."""
        prompt = self._build_prompt(request)
        remove_thinking = self._config.strip_thinking_tokens
        raw_buffer = ""
        visible_output = ""
        emit_index = 0
        agent = self._agent_factory()

        def flush_visible() -> Iterator[str]:
            nonlocal emit_index, raw_buffer
            while True:
                start = raw_buffer.find("<think>", emit_index)
                if start == -1:
                    if emit_index < len(raw_buffer):
                        segment = raw_buffer[emit_index:]
                        emit_index = len(raw_buffer)
                        if segment:
                            yield segment
                    break

                if start > emit_index:
                    segment = raw_buffer[emit_index:start]
                    emit_index = start
                    if segment:
                        yield segment

                end = raw_buffer.find("</think>", start)
                if end == -1:
                    break
                emit_index = end + len("</think>")

        def generator() -> Iterator[str]:
            nonlocal raw_buffer, visible_output, emit_index
            agent_stream: Iterator[str] | None = None
            try:
                agent_kwargs = (
                    {"_research_operation_scope": operation_scope}
                    if operation_scope is not None
                    else {}
                )
                agent_stream = iter(agent.stream_run(prompt, **agent_kwargs))
                for chunk in agent_stream:
                    raw_buffer += chunk
                    if remove_thinking:
                        for segment in flush_visible():
                            visible_output += segment
                            if segment:
                                yield segment
                    else:
                        visible_output += chunk
                        if chunk:
                            yield chunk
                if remove_thinking:
                    for segment in flush_visible():
                        visible_output += segment
                        if segment:
                            yield segment
            finally:
                close = getattr(agent_stream, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
                agent.clear_history()

        def get_summary() -> str:
            if remove_thinking:
                cleaned = strip_thinking_tokens(visible_output)
            else:
                cleaned = visible_output

            return cleaned.strip()

        return generator(), get_summary

    @staticmethod
    def _legacy_input(
        state: SummaryState,
        task: TodoItem,
        context: str,
        notes_context: dict[str, Any],
    ) -> TaskSummaryInput:
        """Project historical mutable arguments into a detached request."""
        note_content = ""
        if notes_context and task.note_id and task.note_id in notes_context:
            note_data = notes_context[task.note_id]
            if isinstance(note_data, dict):
                candidate = note_data.get("content", "")
                if isinstance(candidate, str):
                    note_content = candidate
        return TaskSummaryInput(
            topic=state.research_topic or "",
            title=task.title,
            intent=task.intent,
            query=task.query,
            context=context,
            note_id=task.note_id,
            note_content=note_content,
        )

    @staticmethod
    def _build_prompt(request: TaskSummaryInput) -> str:
        """Construct the prompt from one immutable worker request."""
        note_section = ""
        if request.note_id and request.note_content:
            note_section = (
                f"\n任务笔记（ID: {request.note_id}，已由系统自动同步）：\n"
                f"{request.note_content}\n"
                "请参考以上笔记内容，避免重复已有信息。\n"
            )

        return (
            f"任务主题：{request.topic}\n"
            f"任务名称：{request.title}\n"
            f"任务目标：{request.intent}\n"
            f"检索查询：{request.query}\n"
            f"任务上下文：\n{request.context}\n"
            f"{note_section}"
            "请返回一份面向用户的 Markdown 总结（遵循任务总结模板）。"
        )
