"""Event bus implementations for harness instrumentation."""

from __future__ import annotations

from typing import Iterable

from .models import HarnessEvent, RunContext


class InMemoryEventBus:
    """Collects normalized events directly on the run context."""

    def emit(self, context: RunContext, event_type: str, **payload: object) -> HarnessEvent:
        """Append a new event to the current run context."""
        event = HarnessEvent(
            event_type=event_type,
            run_id=context.run_id,
            payload=dict(payload),
            sequence=len(context.events) + 1,
        )
        context.events.append(event)
        return event

    def emit_many(
        self,
        context: RunContext,
        event_type: str,
        payloads: Iterable[dict[str, object]],
    ) -> list[HarnessEvent]:
        """Append multiple events of the same type."""
        return [self.emit(context, event_type, **payload) for payload in payloads]
