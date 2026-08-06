from pathlib import Path
from types import MappingProxyType
from uuid import UUID

import pytest
from pydantic import ValidationError

from config import Configuration
from research.contracts import ResearchCommand


def test_generated_run_id_is_uuid_hex() -> None:
    command = ResearchCommand(topic="topic", config=Configuration.from_env())
    assert UUID(command.run_id).hex == command.run_id


@pytest.mark.parametrize(
    "value",
    ["", "../escape", "..\\escape", "C:\\escape", "uuid.json"],
)
def test_invalid_explicit_run_id_is_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        ResearchCommand(topic="topic", config=Configuration.from_env(), run_id=value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("permission_mode", "STRICT"),
        ("permission_mode", "permissive"),
        ("caller_mode", "PUBLIC"),
        ("caller_mode", "admin"),
    ],
)
def test_command_rejects_unsupported_exact_modes(field: str, value: str) -> None:
    with pytest.raises(ValueError):
        ResearchCommand(
            topic="topic",
            config=Configuration(),
            **{field: value},
        )


def test_command_detaches_and_freezes_config_and_nested_metadata() -> None:
    original_config = Configuration(llm_model_id="original-model")
    original_metadata = {"nested": {"label": "original"}}
    command = ResearchCommand(
        topic="topic",
        config=original_config,
        metadata=original_metadata,
    )

    object.__setattr__(original_config, "llm_model_id", "mutated-model")
    original_metadata["nested"]["label"] = "mutated"  # type: ignore[index]

    assert command.config.llm_model_id == "original-model"
    assert isinstance(command.metadata, MappingProxyType)
    assert command.metadata["nested"]["label"] == "original"
    with pytest.raises(ValidationError):
        command.config.llm_model_id = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        command.metadata["added"] = True  # type: ignore[index]
    with pytest.raises(TypeError):
        command.metadata["nested"]["label"] = "changed"  # type: ignore[index]


@pytest.mark.parametrize("value", [0, -1, 86401, True])
def test_run_timeout_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValidationError):
        Configuration(run_timeout_seconds=value)


def test_run_timeout_is_optional_and_bounded() -> None:
    assert Configuration().run_timeout_seconds is None
    assert Configuration(run_timeout_seconds=0.1).run_timeout_seconds == 0.1
    assert Configuration(run_timeout_seconds=86400).run_timeout_seconds == 86400


def test_run_timeout_accepts_numeric_environment_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUN_TIMEOUT_SECONDS", "30.5")

    assert Configuration.from_env().run_timeout_seconds == 30.5


def test_benchmark_run_artifacts_are_ignored() -> None:
    patterns = (
        Path(__file__).resolve().parents[2] / ".gitignore"
    ).read_text(encoding="utf-8")
    assert "backend/benchmark_runs/" in patterns
    assert "backend/benchmark_results.json" in patterns
