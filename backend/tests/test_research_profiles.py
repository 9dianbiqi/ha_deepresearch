"""Contracts and built-in profiles for the first general research-core task."""

from __future__ import annotations

import pytest

from config import Configuration
from main import ResearchRequest, _normalize_harness_request
from models import ResearchState
from research.contracts import ResearchCommand
from research.profiles import (
    CoveragePolicy,
    ResearchDimension,
    ResearchMode,
    ResearchProfile,
    ResearchProfileRegistry,
    ResearchTaskTemplate,
    built_in_profile_registry,
)
from research.session import RunSession


def test_research_mode_values_and_builtin_profile_registry() -> None:
    """Expose stable modes and the three first versioned built-in profiles."""
    registry = built_in_profile_registry()

    assert [mode.value for mode in ResearchMode] == ["web", "github", "paper"]
    assert registry.get("web.default.v1").mode is ResearchMode.WEB
    assert registry.get("github.repository.v1").mode is ResearchMode.GITHUB
    assert registry.get("paper.abstract.v1").mode is ResearchMode.PAPER
    assert registry.resolve(mode=ResearchMode.GITHUB).profile_id == (
        "github.repository.v1"
    )


def test_profile_registry_rejects_duplicate_and_unknown_profiles() -> None:
    """A registry must not silently replace a profile or resolve an unknown ID."""
    registry = ResearchProfileRegistry()
    profile = ResearchProfile(
        profile_id="test.profile.v1",
        version=1,
        mode=ResearchMode.WEB,
        dimensions=(ResearchDimension(id="overview", title="Overview"),),
        task_templates=(),
        source_priority=("web",),
        coverage_policy=CoveragePolicy(required_dimensions=("overview",)),
    )
    registry.register(profile)

    with pytest.raises(ValueError, match="already registered"):
        registry.register(profile)
    with pytest.raises(KeyError):
        registry.get("missing.profile.v1")


def test_profile_validation_rejects_duplicate_dimensions_and_mode_mismatch() -> None:
    """Profile contracts fail early instead of creating ambiguous task plans."""
    with pytest.raises(ValueError, match="unique"):
        ResearchProfile(
            profile_id="test.duplicate.v1",
            version=1,
            mode=ResearchMode.WEB,
            dimensions=(
                ResearchDimension(id="overview", title="Overview"),
                ResearchDimension(id="overview", title="Duplicate"),
            ),
            task_templates=(),
            source_priority=("web",),
            coverage_policy=CoveragePolicy(required_dimensions=("overview",)),
        )

    with pytest.raises(ValueError, match="mode"):
        ResearchProfile(
            profile_id="test.mode.v1",
            version=1,
            mode=ResearchMode.GITHUB,
            dimensions=(ResearchDimension(id="overview", title="Overview"),),
            task_templates=(
                ResearchTaskTemplate(
                    template_id="overview",
                    dimension="overview",
                    title="Overview",
                    intent="Inspect overview",
                    query_template="{repository} overview",
                    source_strategy="web",
                    supported_modes=(ResearchMode.WEB,),
                ),
            ),
            source_priority=("github",),
            coverage_policy=CoveragePolicy(required_dimensions=("overview",)),
        )


def test_github_profile_renders_the_existing_four_tasks_and_comparison_task() -> None:
    """The fixed GitHub task content now comes from the versioned profile."""
    profile = built_in_profile_registry().get("github.repository.v1")
    tasks = profile.render_tasks(
        repository="owner/repo",
        comparison_repositories=("other/repo",),
    )

    assert len(tasks) == 5
    assert [task.title for task in tasks[:4]] == [
        "仓库概览与定位",
        "架构与代码结构",
        "演进时间线与路线图",
        "社区评价与替代方案",
    ]
    assert tasks[0].query == "owner/repo GitHub repository overview README features"
    assert tasks[-1].source_strategy == "github_api_compare"
    assert tasks[-1].repository == "owner/repo, other/repo"


def test_command_accepts_explicit_mode_and_profile_id() -> None:
    """Run commands carry explicit mode/profile selection without changing defaults."""
    command = ResearchCommand(
        topic="paper topic",
        config=Configuration(),
        research_mode=ResearchMode.PAPER,
        research_profile_id="paper.abstract.v1",
    )

    assert command.research_mode is ResearchMode.PAPER
    assert command.research_profile_id == "paper.abstract.v1"
    assert ResearchCommand(topic="topic", config=Configuration()).research_mode is None


@pytest.mark.parametrize("value", ["", "../escape", "Paper Profile", "UPPER"])
def test_command_rejects_invalid_profile_ids(value: str) -> None:
    """Profile IDs are safe registry keys, not arbitrary paths or labels."""
    with pytest.raises(ValueError):
        ResearchCommand(
            topic="topic",
            config=Configuration(),
            research_profile_id=value,
        )


def test_api_profile_fields_map_to_the_unified_command() -> None:
    """Public requests map the external profile name to the command ID field."""
    payload = ResearchRequest(
        topic="paper topic",
        research_mode=ResearchMode.PAPER,
        research_profile="paper.abstract.v1",
    )
    command = _normalize_harness_request(payload, caller_mode="public")

    assert command.research_mode is ResearchMode.PAPER
    assert command.research_profile_id == "paper.abstract.v1"


def test_checkpoint_round_trip_preserves_explicit_mode_and_profile() -> None:
    """Recovery reuses the selected profile instead of detecting a new one."""
    command = ResearchCommand(
        topic="paper topic",
        config=Configuration(enable_notes=False),
        research_mode=ResearchMode.PAPER,
        research_profile_id="paper.abstract.v1",
    )
    session = RunSession(
        command=command,
        state=ResearchState(research_topic=command.topic),
    )
    session.start()
    snapshot = session.persist_checkpoint("planning_completed")

    checkpoint = snapshot.checkpoint_state
    assert checkpoint is not None
    assert checkpoint["state"]["research_mode"] == "paper"
    assert checkpoint["state"]["research_profile_id"] == "paper.abstract.v1"

    restored = RunSession.restore_from_snapshot(snapshot)
    assert restored.command.research_mode is ResearchMode.PAPER
    assert restored.command.research_profile_id == "paper.abstract.v1"


def test_legacy_checkpoint_without_mode_or_profile_defaults_to_auto_detection() -> None:
    """Old checkpoints remain readable with both new command fields unset."""
    command = ResearchCommand(
        topic="legacy topic",
        config=Configuration(enable_notes=False),
    )
    session = RunSession(
        command=command,
        state=ResearchState(research_topic=command.topic),
    )
    session.start()
    snapshot = session.persist_checkpoint("planning_completed")
    state = dict(snapshot.checkpoint_state or {})
    continuation = dict(state["state"])
    continuation.pop("research_mode", None)
    continuation.pop("research_profile_id", None)
    state["state"] = continuation

    legacy_snapshot = snapshot.__class__(
        run_id=snapshot.run_id,
        topic=snapshot.topic,
        status=snapshot.status,
        started_at=snapshot.started_at,
        completed_at=snapshot.completed_at,
        parent_run_id=snapshot.parent_run_id,
        output=snapshot.output,
        followup_context=snapshot.followup_context,
        metrics=snapshot.metrics,
        policy_decisions=snapshot.policy_decisions,
        config_snapshot=snapshot.config_snapshot,
        events=snapshot.events,
        error=snapshot.error,
        failure_reason=snapshot.failure_reason,
        checkpoint=snapshot.checkpoint,
        checkpoint_state=state,
        resumable=snapshot.resumable,
        recovery_resumable=snapshot.recovery_resumable,
        last_resumable_parent=snapshot.last_resumable_parent,
    )

    restored = RunSession.restore_from_snapshot(legacy_snapshot)
    assert restored.command.research_mode is None
    assert restored.command.research_profile_id is None
