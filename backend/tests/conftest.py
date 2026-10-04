"""Shared test fixtures and path setup for the backend test suite."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

TESTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TESTS_DIR.parent
SRC_DIR = BACKEND_DIR / "src"

# HTTP tests exercise the production fail-closed authentication boundary.
os.environ["APP_API_KEY"] = "test-app-key"

for path in (BACKEND_DIR, SRC_DIR):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

# Fall back only when the dependency is genuinely unavailable. Installed
# distributions must be imported normally so tests exercise their real contract.
try:
    __import__("loguru")
except ModuleNotFoundError as exc:
    if exc.name != "loguru":
        raise
    fake_loguru = ModuleType("loguru")

    class _FakeLogger:
        def __init__(self) -> None:
            self._sinks: dict[int, Any] = {}
            self._next_sink_id = 0

        def add(self, *args: Any, **kwargs: Any) -> int:
            sink = args[0] if args else None
            sink_id = self._next_sink_id
            self._next_sink_id += 1
            self._sinks[sink_id] = sink
            return sink_id

        def remove(self, *args: Any, **kwargs: Any) -> None:
            if args:
                self._sinks.pop(args[0], None)

        def _emit(self, message: str) -> None:
            for sink in list(self._sinks.values()):
                if callable(sink):
                    sink(message)
                elif hasattr(sink, "write"):
                    sink.write(message + "\n")

        def info(self, *args: Any, **kwargs: Any) -> None:
            self._emit(str(args[0]) if args else "")

        def warning(self, *args: Any, **kwargs: Any) -> None:
            self._emit(str(args[0]) if args else "")

        def exception(self, *args: Any, **kwargs: Any) -> None:
            self._emit(str(args[0]) if args else "")

        def debug(self, *args: Any, **kwargs: Any) -> None:
            self._emit(str(args[0]) if args else "")

    fake_loguru.logger = _FakeLogger()  # type: ignore[attr-defined]
    sys.modules["loguru"] = fake_loguru

try:
    __import__("huggingface_hub")
except ModuleNotFoundError as exc:
    if exc.name != "huggingface_hub":
        raise
    fake_huggingface_hub = ModuleType("huggingface_hub")

    def _snapshot_download(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("huggingface_hub is not available in backend unit tests.")

    fake_huggingface_hub.snapshot_download = _snapshot_download  # type: ignore[attr-defined]
    sys.modules["huggingface_hub"] = fake_huggingface_hub

try:
    __import__("hello_agents")
except ModuleNotFoundError as exc:
    if exc.name != "hello_agents":
        raise
    fake_hello_agents = ModuleType("hello_agents")
    fake_tools = ModuleType("hello_agents.tools")
    fake_builtin = ModuleType("hello_agents.tools.builtin")
    fake_note_tool = ModuleType("hello_agents.tools.builtin.note_tool")

    class _FakeHelloAgentsLLM:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.args = args
            self.kwargs = kwargs

        def invoke(self, prompt: str) -> str:
            return ""

        def stream_invoke(self, prompt: str):
            return iter(())

    class _FakeSimpleAgent:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.args = args
            self.kwargs = kwargs

        def run(self, prompt: str) -> str:
            return ""

        def stream_run(self, prompt: str):
            return iter(())

        def clear_history(self) -> None:
            return None

    class _FakeToolAwareSimpleAgent(_FakeSimpleAgent):
        pass

    class _FakeSearchTool:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.args = args
            self.kwargs = kwargs

        def run(self, payload: dict[str, Any]) -> dict[str, Any]:
            return {"results": [], "backend": payload.get("backend"), "answer": None}

    class _FakeNoteTool:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.args = args
            self.kwargs = kwargs

        def run(self, payload: dict[str, Any]) -> str:
            action = payload.get("action")
            if action == "create":
                return "Created note. ID: fake-note"
            if action == "read":
                return "fake note content"
            if action == "update":
                return "Updated note. ID: fake-note"
            return ""

    fake_hello_agents.HelloAgentsLLM = _FakeHelloAgentsLLM  # type: ignore[attr-defined]
    fake_hello_agents.SimpleAgent = _FakeSimpleAgent  # type: ignore[attr-defined]
    fake_hello_agents.ToolAwareSimpleAgent = _FakeToolAwareSimpleAgent  # type: ignore[attr-defined]
    fake_tools.SearchTool = _FakeSearchTool  # type: ignore[attr-defined]
    fake_tools.NoteTool = _FakeNoteTool  # type: ignore[attr-defined]
    fake_note_tool.NoteTool = _FakeNoteTool  # type: ignore[attr-defined]
    sys.modules["hello_agents"] = fake_hello_agents
    sys.modules["hello_agents.tools"] = fake_tools
    sys.modules["hello_agents.tools.builtin"] = fake_builtin
    sys.modules["hello_agents.tools.builtin.note_tool"] = fake_note_tool
