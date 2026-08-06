"""Contract tests for the installed hello-agents framework dependency."""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
from importlib import import_module
from importlib.metadata import distribution
from pathlib import Path
from typing import get_type_hints

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
TYPINGS_ROOT = BACKEND_ROOT / "typings"


def test_hello_agents_distribution_version_is_exactly_0_2_9() -> None:
    """Keep the framework contract pinned to the reviewed release."""
    assert distribution("hello-agents").version == "0.2.9"


def test_hello_agents_llm_message_contract_matches_reviewed_release() -> None:
    """Keep local typing aligned with the real 0.2.9 message-list boundary."""
    from hello_agents import HelloAgentsLLM

    for method_name in ("invoke", "stream_invoke"):
        method = getattr(HelloAgentsLLM, method_name)
        signature = inspect.signature(method)
        assert tuple(signature.parameters)[:2] == ("self", "messages")
        hints = get_type_hints(method)
        assert hints["messages"] == list[dict[str, str]]


def test_reviewed_hello_agents_stubs_match_installed_public_surface() -> None:
    """Keep the intentionally small local stubs faithful to real 0.2.9 APIs."""
    environment = os.environ.copy()
    environment["MYPYPATH"] = str(TYPINGS_ROOT)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy.stubtest",
            "hello_agents",
            "--concise",
            "--ignore-missing-stub",
        ],
        cwd=BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.parametrize(
    ("module_name", "class_name", "implementation_module", "methods"),
    [
        (
            "hello_agents",
            "HelloAgentsLLM",
            "hello_agents.core.llm",
            ("invoke", "stream_invoke"),
        ),
        (
            "hello_agents",
            "SimpleAgent",
            "hello_agents.agents.simple_agent",
            ("run", "stream_run"),
        ),
        (
            "hello_agents",
            "ToolAwareSimpleAgent",
            "hello_agents.agents.tool_aware_agent",
            ("run", "stream_run"),
        ),
        (
            "hello_agents.tools",
            "SearchTool",
            "hello_agents.tools.builtin.search_tool",
            ("run",),
        ),
        (
            "hello_agents.tools",
            "NoteTool",
            "hello_agents.tools.builtin.note_tool",
            ("run",),
        ),
    ],
)
def test_hello_agents_public_classes_come_from_installed_distribution(
    module_name: str,
    class_name: str,
    implementation_module: str,
    methods: tuple[str, ...],
) -> None:
    """Reject test-injected classes and verify the reviewed public methods."""
    module = import_module(module_name)
    framework_class = getattr(module, class_name, None)

    assert inspect.isclass(framework_class), (
        f"{module_name}.{class_name} must be exported by the installed framework"
    )
    assert framework_class.__module__ == implementation_module

    package_root = Path(
        distribution("hello-agents").locate_file("hello_agents")
    ).resolve()
    implementation_path = Path(inspect.getfile(framework_class)).resolve()
    assert implementation_path.is_relative_to(package_root)

    for method_name in methods:
        assert callable(getattr(framework_class, method_name, None)), (
            f"{class_name}.{method_name} must remain a public callable"
        )
