"""Compression helpers for long research context."""

from __future__ import annotations

from models import SummaryStateOutput
from research.context import project_legacy_compressed_context


class ContextCompressor:
    """Produce compact summaries from research output."""

    def compress_output(self, output: SummaryStateOutput | None) -> dict[str, object]:
        """Return the deprecated bounded compatibility context package."""
        return project_legacy_compressed_context(output)
