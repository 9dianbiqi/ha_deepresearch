"""Context lifecycle management for harness runs."""

from __future__ import annotations

from .compressor import ContextCompressor
from .event_bus import InMemoryEventBus
from .models import RunContext


class ContextManager:
    """Populate and compress the long-lived context for a run."""

    def __init__(
        self,
        compressor: ContextCompressor,
        event_bus: InMemoryEventBus,
    ) -> None:
        self._compressor = compressor
        self._event_bus = event_bus

    def finalize(self, context: RunContext) -> dict[str, object]:
        """Compute compressed context once the run has finished."""
        compressed = self._compressor.compress_output(context.result)
        context.compressed_context = dict(compressed)
        self._event_bus.emit(
            context,
            "context_compressed",
            task_brief_count=len(compressed.get("task_briefs", [])),
            open_question_count=len(compressed.get("open_questions", [])),
        )
        return compressed
