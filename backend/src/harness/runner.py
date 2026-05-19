"""High-level harness runner for controlled research execution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent import DeepResearchAgent

from .compressor import ContextCompressor
from .context_manager import ContextManager
from .event_bus import InMemoryEventBus
from .evaluator import RuleBasedEvaluator
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

        try:
            decisions = self.policy.evaluate(request)
            context.policy_decisions = [item.as_dict() for item in decisions]
            self.event_bus.emit(
                context,
                "policy_checked",
                decisions=context.policy_decisions,
            )
            self.policy.assert_allowed(decisions)

            agent = DeepResearchAgent(config=request.config)
            context.result = agent.run(request.topic)
            context.status = "completed"
            self.event_bus.emit(
                context,
                "run_completed",
                todo_count=len(context.result.todo_items),
                has_report=bool((context.result.report_markdown or "").strip()),
            )
            self.context_manager.finalize(context)
        except Exception as exc:
            context.status = "failed"
            context.error = str(exc)
            self.event_bus.emit(context, "run_failed", error=str(exc))
        finally:
            context.completed_at = utc_now()
            context.metrics["duration_seconds"] = context.duration_seconds
            context.metrics["event_count"] = len(context.events)

        evaluation = self.evaluator.evaluate(context)
        context.metrics["evaluation_score"] = evaluation.score
        self.recorder.persist(context)

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
        """Placeholder for future event-driven streaming execution."""
        raise NotImplementedError("Streaming harness execution is not implemented yet.")

    def load_record(self, run_id: str) -> dict[str, object]:
        """Load one persisted harness run record."""
        return self.recorder.load(run_id)
