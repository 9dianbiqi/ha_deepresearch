"""High-level harness runner for controlled research execution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from agent import DeepResearchAgent
from models import SummaryStateOutput, TodoItem

from .compressor import ContextCompressor
from .context_manager import ContextManager
from .event_bus import InMemoryEventBus
from .evaluator import EvaluationResult, RuleBasedEvaluator
from .models import HarnessRunRequest, HarnessRunResult, RecorderConfig, RunContext, utc_now
from .policy import HarnessPolicy
from .recorder import JsonlRunRecorder


@dataclass(kw_only=True)
class HarnessRunner:
    """Coordinate orchestration, event capture, persistence, and evaluation."""

    event_bus: InMemoryEventBus
    recorder: JsonlRunRecorder
    evaluator: RuleBasedEvaluator
    policy: HarnessPolicy
    context_manager: ContextManager

    @classmethod
    def build_default(cls, *, base_path: str | Path = "./runs") -> "HarnessRunner":
        """Create a runner with local filesystem defaults."""
        event_bus = InMemoryEventBus()
        return cls(
            event_bus=event_bus,
            recorder=JsonlRunRecorder(RecorderConfig(base_path=Path(base_path))),
            evaluator=RuleBasedEvaluator(),
            policy=HarnessPolicy(),
            context_manager=ContextManager(ContextCompressor(), event_bus),
        )

    def run(self, request: HarnessRunRequest) -> HarnessRunResult:
        """Execute a research run under harness control."""
        context = RunContext(request=request, status="running")
        self.event_bus.emit(context, "run_started", topic=request.topic)
        evaluation = EvaluationResult(score=0.0)

        try:
            self._evaluate_policy(context)
            self._execute_agent(context)
            self._compress_context(context)
        except Exception as exc:
            context.status = "failed"
            context.error = str(exc)
            self.event_bus.emit(context, "run_failed", error=str(exc))
        finally:
            context.completed_at = utc_now()
            context.metrics["duration_seconds"] = context.duration_seconds
            context.metrics["event_count"] = len(context.events)

        evaluation = self._evaluate_run(context)
        context.metrics["evaluation_score"] = evaluation.score
        self._persist_run(context, evaluation)

        return HarnessRunResult(
            run_id=context.run_id,
            status=context.status,
            output=context.result,
            error=context.error,
            metrics=dict(context.metrics),
            findings=evaluation.findings,
            compressed_context=dict(context.compressed_context),
            policy_decisions=list(context.policy_decisions),
        )

    def stream(self, request: HarnessRunRequest):
        """Execute a research run while yielding agent stream events."""
        context = RunContext(request=request, status="running")
        self.event_bus.emit(context, "run_started", topic=request.topic)
        stream_state: dict[int, TodoItem] = {}
        report_markdown = ""

        try:
            self._evaluate_policy(context)
            agent = DeepResearchAgent(config=context.request.config)
            for event in agent.run_stream(context.request.topic):
                self.event_bus.emit(
                    context,
                    "research_event",
                    source_type=str(event.get("type", "unknown")),
                )
                self._ingest_stream_event(stream_state, event)
                if event.get("type") == "final_report":
                    report_markdown = str(event.get("report") or "")
                yield {"run_id": context.run_id, **event}

            context.result = SummaryStateOutput(
                running_summary=report_markdown,
                report_markdown=report_markdown,
                todo_items=[
                    stream_state[key]
                    for key in sorted(stream_state.keys())
                ],
            )
            context.status = "completed"
            self._compress_context(context)
        except Exception as exc:
            context.status = "failed"
            context.error = str(exc)
            self.event_bus.emit(context, "run_failed", error=str(exc))
            yield {
                "type": "error",
                "run_id": context.run_id,
                "detail": str(exc),
            }
        finally:
            context.completed_at = utc_now()
            context.metrics["duration_seconds"] = context.duration_seconds
            context.metrics["event_count"] = len(context.events)
            evaluation = self._evaluate_run(context)
            context.metrics["evaluation_score"] = evaluation.score
            self._persist_run(context, evaluation)

    def load_record(self, run_id: str) -> dict[str, object]:
        """Load one persisted harness run record."""
        return self.recorder.load(run_id)

    def _evaluate_policy(self, context: RunContext) -> None:
        decisions = self.policy.evaluate(context.request)
        context.policy_decisions = [item.as_dict() for item in decisions]
        self.event_bus.emit(
            context,
            "policy_checked",
            decisions=context.policy_decisions,
        )
        self.policy.assert_executable(decisions)

    def _execute_agent(self, context: RunContext) -> None:
        agent = DeepResearchAgent(config=context.request.config)
        context.result = agent.run(context.request.topic)
        context.status = "completed"
        self.event_bus.emit(
            context,
            "run_completed",
            todo_count=len(context.result.todo_items),
            has_report=bool((context.result.report_markdown or "").strip()),
        )

    def _compress_context(self, context: RunContext) -> None:
        self.context_manager.finalize(context)

    def _evaluate_run(self, context: RunContext) -> EvaluationResult:
        return self.evaluator.evaluate(context)

    def _persist_run(
        self,
        context: RunContext,
        evaluation: EvaluationResult,
    ) -> None:
        self.recorder.persist(context, evaluation=evaluation)

    def _ingest_stream_event(
        self,
        stream_state: dict[int, TodoItem],
        event: dict[str, Any],
    ) -> None:
        """Update a lightweight todo snapshot from stream events."""
        event_type = str(event.get("type") or "")
        if event_type == "todo_list":
            tasks = event.get("tasks")
            if isinstance(tasks, list):
                for item in tasks:
                    if not isinstance(item, dict):
                        continue
                    task_id = self._coerce_task_id(item.get("id"))
                    if task_id is None:
                        continue
                    stream_state[task_id] = TodoItem(
                        id=task_id,
                        title=str(item.get("title") or f"Task {task_id}"),
                        intent=str(item.get("intent") or ""),
                        query=str(item.get("query") or ""),
                        status=str(item.get("status") or "pending"),
                        summary=str(item.get("summary") or "") or None,
                        sources_summary=str(item.get("sources_summary") or "") or None,
                        note_id=self._optional_str(item.get("note_id")),
                        note_path=self._optional_str(item.get("note_path")),
                        stream_token=self._optional_str(item.get("stream_token")),
                    )
            return

        task_id = self._coerce_task_id(event.get("task_id"))
        if task_id is None:
            return
        task = stream_state.setdefault(
            task_id,
            TodoItem(
                id=task_id,
                title=f"Task {task_id}",
                intent="",
                query="",
            ),
        )

        if event_type == "task_status":
            task.status = str(event.get("status") or task.status)
            task.title = str(event.get("title") or task.title)
            task.intent = str(event.get("intent") or task.intent)
            task.summary = self._optional_str(event.get("summary")) or task.summary
            task.sources_summary = (
                self._optional_str(event.get("sources_summary")) or task.sources_summary
            )
            task.note_id = self._optional_str(event.get("note_id")) or task.note_id
            task.note_path = self._optional_str(event.get("note_path")) or task.note_path
            return

        if event_type == "sources":
            task.sources_summary = (
                self._optional_str(event.get("latest_sources"))
                or self._optional_str(event.get("sources_summary"))
                or task.sources_summary
            )
            task.note_id = self._optional_str(event.get("note_id")) or task.note_id
            task.note_path = self._optional_str(event.get("note_path")) or task.note_path
            return

        if event_type == "task_summary_chunk":
            chunk = self._optional_str(event.get("content")) or ""
            task.summary = (task.summary or "") + chunk
            return

        if event_type == "tool_call":
            task.note_id = self._optional_str(event.get("note_id")) or task.note_id
            task.note_path = self._optional_str(event.get("note_path")) or task.note_path

    @staticmethod
    def _coerce_task_id(value: object) -> int | None:
        try:
            numeric = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return numeric

    @staticmethod
    def _optional_str(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        return cleaned or None
