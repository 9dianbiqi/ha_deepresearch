"""Shared test fixtures and path setup for the backend test suite."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from typing import Any

TESTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TESTS_DIR.parent
SRC_DIR = BACKEND_DIR / "src"

for path in (BACKEND_DIR, SRC_DIR):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

# Provide a fake loguru module so tests don't require the real dependency.
if "loguru" not in sys.modules:
    fake_loguru = ModuleType("loguru")

    class _FakeLogger:
        def add(self, *args: Any, **kwargs: Any) -> None:
            return None

        def info(self, *args: Any, **kwargs: Any) -> None:
            return None

        def warning(self, *args: Any, **kwargs: Any) -> None:
            return None

        def exception(self, *args: Any, **kwargs: Any) -> None:
            return None

        def debug(self, *args: Any, **kwargs: Any) -> None:
            return None

    fake_loguru.logger = _FakeLogger()  # type: ignore[attr-defined]
    sys.modules["loguru"] = fake_loguru
