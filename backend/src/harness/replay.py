"""Replay helpers for previously recorded harness runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_run_record(path: str | Path) -> dict[str, Any]:
    """Load a persisted harness run record from disk."""
    record_path = Path(path)
    return json.loads(record_path.read_text(encoding="utf-8"))
