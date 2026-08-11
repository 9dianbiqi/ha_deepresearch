"""Packaging and frozen-dependency contract tests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = BACKEND_ROOT / "pyproject.toml"
LOCK_PATH = BACKEND_ROOT / "uv.lock"


def _toml(path: Path) -> dict[str, object]:
    with path.open("rb") as stream:
        return tomllib.load(stream)


def test_setuptools_discovers_runtime_modules_and_packages() -> None:
    pyproject = _toml(PYPROJECT_PATH)
    tool = pyproject["tool"]
    assert isinstance(tool, dict)
    setuptools = tool["setuptools"]
    assert isinstance(setuptools, dict)

    assert set(setuptools["py-modules"]) == {
        "agent",
        "config",
        "main",
        "models",
        "prompts",
        "utils",
    }
    packages = setuptools["packages"]
    assert isinstance(packages, dict)
    discovery = packages["find"]
    assert discovery == {
        "where": ["src"],
        "include": ["research*", "harness*", "services*"],
    }
    assert not (BACKEND_ROOT / "src" / "__init__.py").exists()


def test_documented_dev_group_installs_test_and_quality_tools() -> None:
    pyproject = _toml(PYPROJECT_PATH)
    groups = pyproject["dependency-groups"]
    assert isinstance(groups, dict)
    dev = groups["dev"]
    assert isinstance(dev, list)
    requirements = [item for item in dev if isinstance(item, str)]
    assert any(item.startswith("ruff") for item in requirements)
    assert any(item.startswith("mypy") for item in requirements)
    assert any(item.startswith("pytest") for item in requirements)
    assert any(
        item.startswith("tomli") and "python_version < '3.11'" in item
        for item in requirements
    )
    assert any(item.startswith("types-requests") for item in requirements)


def test_mypy_resolves_src_layout_as_local_code() -> None:
    """Keep project imports on the local typed source tree during analysis."""
    pyproject = _toml(PYPROJECT_PATH)
    tool = pyproject["tool"]
    assert isinstance(tool, dict)
    assert tool["mypy"] == {
        "mypy_path": ["src", "typings"],
        "explicit_package_bases": True,
    }


def test_string_readme_is_backend_relative_and_exists() -> None:
    pyproject = _toml(PYPROJECT_PATH)
    project = pyproject["project"]
    assert isinstance(project, dict)
    readme = project.get("readme")
    if isinstance(readme, str):
        assert (BACKEND_ROOT / readme).is_file()


def test_lock_freezes_exact_hello_agents_029_requirement() -> None:
    pyproject = _toml(PYPROJECT_PATH)
    project = pyproject["project"]
    assert isinstance(project, dict)
    dependencies = project["dependencies"]
    assert "hello-agents==0.2.9" in dependencies

    lock = _toml(LOCK_PATH)
    raw_packages = lock["package"]
    assert isinstance(raw_packages, list)
    packages = {
        item["name"]: item
        for item in raw_packages
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    assert packages["hello-agents"]["version"] == "0.2.9"

    root = packages["helloagents-deep-researcher"]
    metadata = root["metadata"]
    assert isinstance(metadata, dict)
    requirements = metadata["requires-dist"]
    assert isinstance(requirements, list)
    hello_agents = next(
        item
        for item in requirements
        if isinstance(item, dict) and item.get("name") == "hello-agents"
    )
    assert hello_agents["specifier"] == "==0.2.9"


def test_runtime_declares_hello_agents_import_dependency() -> None:
    """Keep the installed production entrypoint importable without test fallbacks."""
    pyproject = _toml(PYPROJECT_PATH)
    project = pyproject["project"]
    assert isinstance(project, dict)
    dependencies = project["dependencies"]
    assert isinstance(dependencies, list)
    assert any(
        isinstance(item, str) and item.startswith("huggingface-hub")
        for item in dependencies
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from importlib.metadata import version; "
                "import agent, main; "
                "assert version('hello-agents') == '0.2.9'"
            ),
        ],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
