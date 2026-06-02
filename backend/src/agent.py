"""Orchestrator coordinating the deep research workflow."""

from __future__ import annotations

import logging
from queue import Empty, Queue
from threading import Lock, Thread
from typing import Any, Callable, Iterator

from hello_agents import HelloAgentsLLM, ToolAwareSimpleAgent

from config import Configuration
from prompts import (
    report_writer_instructions,
    task_summarizer_instructions,
    todo_planner_system_prompt,
)
from models import SummaryState, SummaryStateOutput, TodoItem
from services.note_agent import NoteSubAgent
from services.planner import PlanningService
from services.reporter import ReportingService
from services.search import dispatch_search, prepare_research_context
from services.summarizer import SummarizationService

logger = logging.getLogger(__name__)


class DeepResearchAgent:
    """Coordinator orchestrating TODO-based research workflow using HelloAgents."""

    def __init__(self, config: Configuration | None = None) -> None:
        """Initialise the coordinator with configuration and shared tools."""
        self.config = config or Configuration.from_env()
        self.llm = self._init_llm()

        # Reporter gets a faster model when configured (DeerFlow pattern)
        if self.config.llm_reporter_model_id:
            self._reporter_llm = self._init_llm(
                model_id=self.config.llm_reporter_model_id,
                max_tokens=3000,
            )
        else:
            self._reporter_llm = self.llm

        self.note_agent = (
            NoteSubAgent(workspace=self.config.notes_workspace)
            if self.config.enable_notes
            else None
        )

        self._state_lock = Lock()

        self.todo_agent = self._create_tool_aware_agent(
            name="研究规划专家",
            system_prompt=todo_planner_system_prompt.strip(),
        )
        self.report_agent = self._create_tool_aware_agent(
            name="报告撰写专家",
            system_prompt=report_writer_instructions.strip(),
            llm=self._reporter_llm,
        )

        self._summarizer_factory: Callable[[], ToolAwareSimpleAgent] = lambda: self._create_tool_aware_agent(  # noqa: E501
            name="任务总结专家",
            system_prompt=task_summarizer_instructions.strip(),
        )

        self.planner = PlanningService(self.todo_agent, self.config)
        self.summarizer = SummarizationService(self._summarizer_factory, self.config)
        self.reporting = ReportingService(self.report_agent, self.config)
        self._last_search_notices: list[str] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def _init_llm(
        self,
        *,
        model_id: str | None = None,
        max_tokens: int | None = None,
    ) -> HelloAgentsLLM:
        """Instantiate HelloAgentsLLM following configuration preferences.

        Args:
            model_id: Override the default model (e.g. for Reporter).
            max_tokens: Override the default max_tokens limit.
        """
        llm_kwargs: dict[str, Any] = {
            "temperature": 0.0,
            "timeout": self.config.llm_timeout,
            "max_tokens": max_tokens or self.config.llm_max_tokens,
        }

        resolved_model = model_id or self.config.llm_model_id or self.config.local_llm
        if resolved_model:
            llm_kwargs["model"] = resolved_model

        provider = (self.config.llm_provider or "").strip()
        if provider:
            llm_kwargs["provider"] = provider

        if provider == "ollama":
            llm_kwargs["base_url"] = self.config.sanitized_ollama_url()
            if self.config.llm_api_key:
                llm_kwargs["api_key"] = self.config.llm_api_key
            else:
                llm_kwargs["api_key"] = "ollama"
        elif provider == "lmstudio":
            llm_kwargs["base_url"] = self.config.lmstudio_base_url
            if self.config.llm_api_key:
                llm_kwargs["api_key"] = self.config.llm_api_key
        else:
            if self.config.llm_base_url:
                llm_kwargs["base_url"] = self.config.llm_base_url
            if self.config.llm_api_key:
                llm_kwargs["api_key"] = self.config.llm_api_key

        return HelloAgentsLLM(**llm_kwargs)

    def _create_tool_aware_agent(
        self,
        *,
        name: str,
        system_prompt: str,
        llm: HelloAgentsLLM | None = None,
    ) -> ToolAwareSimpleAgent:
        """Instantiate a ToolAwareSimpleAgent without tool registry.

        Note operations are handled by NoteSubAgent separately — agents
        produce clean text output only, never ``[TOOL_CALL:...]`` markers.
        """
        return ToolAwareSimpleAgent(
            name=name,
            llm=llm or self.llm,
            system_prompt=system_prompt,
            enable_tool_calling=False,
            tool_registry=None,
        )

    def run(
        self,
        topic: str,
        prior_context: dict[str, Any] | None = None,
    ) -> SummaryStateOutput:
        """Execute the research workflow and return the final report."""
        state = SummaryState(research_topic=topic)
        state.todo_items = self.planner.plan_todo_list(
            state, prior_context=prior_context,
        )

        if not state.todo_items:
            logger.info("No TODO items generated; falling back to single task")
            state.todo_items = [self.planner.create_fallback_task(state)]

        self._create_task_notes(state)

        for task in state.todo_items:
            self._execute_task(state, task, emit_stream=False)

        notes_context = self._read_all_task_notes(state)
        report = self.reporting.generate_report(state, notes_context)
        state.structured_report = report
        state.running_summary = report
        self._persist_conclusion_note(state, report)

        return SummaryStateOutput(
            running_summary=report,
            report_markdown=report,
            todo_items=state.todo_items,
        )

    def run_stream(
        self,
        topic: str,
        prior_context: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Execute the workflow yielding incremental progress events."""
        state = SummaryState(research_topic=topic)
        logger.debug("Starting streaming research: topic=%s", topic)
        yield {"type": "status", "message": "初始化研究流程"}

        state.todo_items = self.planner.plan_todo_list(
            state, prior_context=prior_context,
        )
        if not state.todo_items:
            state.todo_items = [self.planner.create_fallback_task(state)]

        self._create_task_notes(state)

        channel_map: dict[int, dict[str, Any]] = {}
        for index, task in enumerate(state.todo_items, start=1):
            token = f"task_{task.id}"
            task.stream_token = token
            channel_map[task.id] = {"step": index, "token": token}

        yield {
            "type": "todo_list",
            "tasks": [self._serialize_task(t) for t in state.todo_items],
            "step": 0,
        }

        event_queue: Queue[dict[str, Any]] = Queue()

        def enqueue(
            event: dict[str, Any],
            *,
            task: TodoItem | None = None,
            step_override: int | None = None,
        ) -> None:
            payload = dict(event)
            target_task_id = payload.get("task_id")
            if task is not None:
                target_task_id = task.id
                payload["task_id"] = task.id

            channel = channel_map.get(target_task_id) if target_task_id is not None else None
            if channel:
                payload.setdefault("step", channel["step"])
                payload["stream_token"] = channel["token"]
            if step_override is not None:
                payload["step"] = step_override
            event_queue.put(payload)

        threads: list[Thread] = []

        def worker(task: TodoItem, step: int) -> None:
            try:
                enqueue(
                    {
                        "type": "task_status",
                        "task_id": task.id,
                        "status": "in_progress",
                        "title": task.title,
                        "intent": task.intent,
                        "note_id": task.note_id,
                        "note_path": task.note_path,
                    },
                    task=task,
                )

                for event in self._execute_task(state, task, emit_stream=True, step=step):
                    enqueue(event, task=task)
            except Exception as exc:  # pragma: no cover - defensive guardrail
                logger.exception("Task execution failed", exc_info=exc)
                enqueue(
                    {
                        "type": "task_status",
                        "task_id": task.id,
                        "status": "failed",
                        "detail": str(exc),
                        "title": task.title,
                        "intent": task.intent,
                        "note_id": task.note_id,
                        "note_path": task.note_path,
                    },
                    task=task,
                )
            finally:
                enqueue({"type": "__task_done__", "task_id": task.id})

        for task in state.todo_items:
            step = channel_map.get(task.id, {}).get("step", 0)
            thread = Thread(target=worker, args=(task, step), daemon=True)
            threads.append(thread)
            thread.start()

        active_workers = len(state.todo_items)
        finished_workers = 0

        try:
            while finished_workers < active_workers:
                event = event_queue.get()
                if event.get("type") == "__task_done__":
                    finished_workers += 1
                    continue
                yield event

            while True:
                try:
                    event = event_queue.get_nowait()
                except Empty:
                    break
                if event.get("type") != "__task_done__":
                    yield event
        finally:
            for thread in threads:
                thread.join(timeout=120)
                if thread.is_alive():
                    logger.warning("Task thread %s did not finish within timeout", thread.name)

        notes_context = self._read_all_task_notes(state)
        report = self.reporting.generate_report(state, notes_context)
        state.structured_report = report
        state.running_summary = report

        note_event = self._persist_conclusion_note(state, report)
        if note_event:
            yield note_event

        yield {
            "type": "final_report",
            "report": report,
            "note_id": state.report_note_id,
            "note_path": state.report_note_path,
        }
        yield {"type": "done"}

    # ------------------------------------------------------------------
    # Execution helpers
    # ------------------------------------------------------------------
    def _execute_task(
        self,
        state: SummaryState,
        task: TodoItem,
        *,
        emit_stream: bool,
        step: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Run search + summarization for a single task with retry on poor results."""
        task.status = "in_progress"
        max_retries = 3
        original_query = task.query

        for attempt in range(max_retries):
            with self._state_lock:
                loop_count = state.research_loop_count
                state.research_loop_count += 1

            search_result, notices, answer_text, backend = dispatch_search(
                task.query,
                self.config,
                loop_count,
            )
            self._last_search_notices = notices
            task.notices = notices

            if notices and emit_stream:
                for notice in notices:
                    if notice:
                        yield {
                            "type": "status",
                            "message": notice,
                            "task_id": task.id,
                            "step": step,
                        }

            # No search results — refine query and retry
            if not search_result or not search_result.get("results"):
                if attempt < max_retries - 1:
                    old_query = task.query
                    task.query = self._refine_query(task, attempt)
                    task.retry_count += 1
                    task.refined_queries.append(old_query)
                    logger.info(
                        "Task %d attempt %d: no results for query=%r → refined to %r",
                        task.id, attempt + 1, old_query, task.query,
                    )
                    if emit_stream:
                        yield {
                            "type": "task_retry",
                            "task_id": task.id,
                            "previous_query": old_query,
                            "refined_query": task.query,
                            "attempt": attempt + 1,
                            "reason": "no_search_results",
                            "step": step,
                        }
                    continue
                task.status = "skipped"
                task.query = original_query  # restore for record
                if emit_stream:
                    yield {
                        "type": "task_status",
                        "task_id": task.id,
                        "status": "skipped",
                        "title": task.title,
                        "intent": task.intent,
                        "note_id": task.note_id,
                        "note_path": task.note_path,
                        "step": step,
                    }
                return

            sources_summary, context = prepare_research_context(
                search_result,
                answer_text,
                self.config,
            )

            task.sources_summary = sources_summary

            with self._state_lock:
                state.web_research_results.append(context)
                state.sources_gathered.append(sources_summary)

            notes_context = self._read_task_note(task)

            summary_text: str | None = None

            if emit_stream:
                yield {
                    "type": "sources",
                    "task_id": task.id,
                    "latest_sources": sources_summary,
                    "raw_context": context,
                    "step": step,
                    "backend": backend,
                    "note_id": task.note_id,
                    "note_path": task.note_path,
                }

                summary_stream, summary_getter = self.summarizer.stream_task_summary(
                    state, task, context, notes_context,
                )
                try:
                    for chunk in summary_stream:
                        if chunk:
                            yield {
                                "type": "task_summary_chunk",
                                "task_id": task.id,
                                "content": chunk,
                                "note_id": task.note_id,
                                "step": step,
                            }
                finally:
                    summary_text = summary_getter()
            else:
                summary_text = self.summarizer.summarize_task(
                    state, task, context, notes_context,
                )

            task.summary = summary_text.strip() if summary_text else "暂无可用信息"

            # Quality check — refine and retry if insufficient
            quality = self._check_summary_quality(task.summary)
            if quality["passed"]:
                break

            if attempt < max_retries - 1:
                old_query = task.query
                task.query = self._refine_query(task, attempt)
                task.retry_count += 1
                task.refined_queries.append(old_query)
                logger.info(
                    "Task %d attempt %d: summary insufficient (reasons=%s) "
                    "→ refined query to %r",
                    task.id, attempt + 1, quality["reasons"], task.query,
                )
                if emit_stream:
                    yield {
                        "type": "task_retry",
                        "task_id": task.id,
                        "previous_query": old_query,
                        "refined_query": task.query,
                        "attempt": attempt + 1,
                        "reason": ",".join(quality["reasons"]),
                        "step": step,
                    }

        task.status = "completed"
        task.query = original_query  # restore for record

        self._update_task_note(task)

        if emit_stream:
            yield {
                "type": "task_status",
                "task_id": task.id,
                "status": "completed",
                "summary": task.summary,
                "sources_summary": task.sources_summary,
                "note_id": task.note_id,
                "note_path": task.note_path,
                "step": step,
            }

    def _serialize_task(self, task: TodoItem) -> dict[str, Any]:
        """Convert task dataclass to serializable dict for frontend."""
        return task.to_dict()

    # ------------------------------------------------------------------
    # Summary quality & query refinement
    # ------------------------------------------------------------------

    @staticmethod
    def _check_summary_quality(summary: str) -> dict[str, Any]:
        """Rule-based quality check for a task summary.

        Returns a dict with ``passed`` (bool) and ``reasons`` (list[str]).
        """
        passed = True
        reasons: list[str] = []

        if not summary or summary.strip() == "暂无可用信息":
            passed = False
            reasons.append("empty_or_fallback")

        if len(summary.strip()) < 30:
            passed = False
            reasons.append("too_short")

        has_structure = any(
            marker in summary for marker in ("###", "- ", "* ", "1. ", "2. ")
        )
        if not has_structure:
            passed = False
            reasons.append("no_structure")

        return {"passed": passed, "reasons": reasons}

    @staticmethod
    def _refine_query(task: TodoItem, attempt: int) -> str:
        """Generate a broader or alternative search query after a failed attempt.

        * attempt 0 — extract keywords from ``intent``
        * attempt 1 — use the task title plus English fallback keywords
        """
        if attempt == 0:
            keywords = (
                task.intent.replace("，", ",")
                .replace("、", ",")
                .replace("；", ",")
                .split(",")
            )
            refined = " ".join(k.strip() for k in keywords if k.strip())
            return refined or f"{task.title} 深入分析"

        # attempt >= 1: broader English-oriented query
        return f"{task.title} overview latest research"

    # ------------------------------------------------------------------
    # Note sub-agent helpers
    # ------------------------------------------------------------------

    def _create_task_notes(self, state: SummaryState) -> None:
        """Create note entries for each planned task via NoteSubAgent."""
        if not self.note_agent:
            return
        for task in state.todo_items:
            note_id = self.note_agent.create_task_note(
                task_id=task.id,
                title=task.title,
                content=f"任务概览：{task.intent}\n检索查询：{task.query}",
            )
            if note_id:
                task.note_id = note_id
                task.note_path = self.note_agent.note_path(note_id)

    def _read_task_note(self, task: TodoItem) -> dict[str, Any]:
        """Read a single task's note content."""
        if not self.note_agent or not task.note_id:
            return {}
        return {task.note_id: self.note_agent.read_note(task.note_id)}

    def _read_all_task_notes(self, state: SummaryState) -> dict[str, Any]:
        """Read all task notes for report generation."""
        if not self.note_agent:
            return {}
        note_ids = [t.note_id for t in state.todo_items if t.note_id]
        return self.note_agent.read_all_task_notes(note_ids)

    def _update_task_note(self, task: TodoItem) -> None:
        """Update a task's note with the latest summary."""
        if not self.note_agent or not task.note_id:
            return
        content_parts = [f"任务状态：{task.status}"]
        if task.summary:
            content_parts.append(f"\n任务总结：\n{task.summary}")
        if task.sources_summary:
            content_parts.append(f"\n来源概览：\n{task.sources_summary}")
        self.note_agent.update_note(
            task.note_id,
            task_id=task.id,
            title=f"任务 {task.id}: {task.title}",
            content="\n".join(content_parts),
        )

    def _persist_conclusion_note(self, state: SummaryState, report: str) -> dict[str, Any] | None:
        """Save the final report as a conclusion note via NoteSubAgent."""
        if not self.note_agent or not report or not report.strip():
            return None

        note_title = f"研究报告：{state.research_topic}".strip() or "研究报告"
        note_id = self.note_agent.create_conclusion_note(
            title=note_title,
            content=report.strip(),
        )

        if not note_id:
            return None

        state.report_note_id = note_id
        state.report_note_path = self.note_agent.note_path(note_id)

        payload = {
            "type": "report_note",
            "note_id": note_id,
            "title": note_title,
            "content": report,
        }
        if state.report_note_path:
            payload["note_path"] = state.report_note_path

        return payload


