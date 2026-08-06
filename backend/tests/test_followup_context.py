"""Tests for bounded follow-up context and compressor compatibility."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from config import Configuration
from harness.compressor import ContextCompressor
from models import ResearchState, SummaryStateOutput, TodoItem
from research.context import (
    FollowupContext,
    FollowupContextProjector,
    ResearchContextAssembler,
)
from research.contracts import ResearchCommand
from research.session import RunSession


def make_session(tasks: list[TodoItem]) -> RunSession:
    """Create a running session with the supplied canonical task state."""
    command = ResearchCommand(topic="follow-up topic", config=Configuration())
    session = RunSession(
        command=command,
        state=ResearchState(
            research_topic=command.topic,
            web_research_results=["fake-raw-web-body"],
            sources_gathered=["fake-raw-source-body"],
        ),
    )
    session.start()
    session.install_plan(tasks)
    return session


def make_many_tasks() -> list[TodoItem]:
    """Return enough completed and incomplete tasks to exceed every budget."""
    tasks = [
        TodoItem(
            id=index,
            title=f"Completed {index}",
            intent="intent",
            query="query",
            status="completed",
            summary=f"Finding {index} " + ("x" * 240),
            sources_summary=f"Source {index} " + ("y" * 240) + "\nignored line",
        )
        for index in range(1, 8)
    ]
    nonterminal_statuses = ["failed", "skipped", "pending", "in_progress", "cancelled"]
    for offset in range(12):
        task_id = 100 + offset
        tasks.append(
            TodoItem(
                id=task_id,
                title=f"Open question {offset}",
                intent="intent",
                query="query",
                status=nonterminal_statuses[offset % len(nonterminal_statuses)],
                summary="fake-noncompleted-finding",
                sources_summary=None,
            )
        )
    return tasks


def test_followup_context_is_frozen_slotted_keyword_only_schema_v1() -> None:
    context = FollowupContext(
        source_run_id="fake-source",
        key_findings=("finding",),
        key_sources=("source",),
        open_questions=("question",),
    )

    assert context.schema_version == 1
    assert hasattr(FollowupContext, "__slots__")
    assert context.as_dict() == {
        "schema_version": 1,
        "source_run_id": "fake-source",
        "key_findings": ["finding"],
        "key_sources": ["source"],
        "open_questions": ["question"],
    }
    with pytest.raises(FrozenInstanceError):
        context.source_run_id = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        FollowupContext("source", (), (), ())  # type: ignore[misc]


def test_followup_projection_is_versioned_bounded_and_canonical_only() -> None:
    session = make_session(make_many_tasks())

    context = FollowupContextProjector().project(session)
    serialized = json.dumps(context.as_dict())

    assert context.schema_version == 1
    assert context.source_run_id == session.run_id
    assert len(context.key_findings) == 5
    assert len(context.key_sources) == 3
    assert len(context.open_questions) == 10
    assert all(len(item) <= 180 for item in context.key_findings)
    assert all(len(item) <= 180 for item in context.key_sources)
    assert all("\n" not in item for item in context.key_sources)
    assert "fake-noncompleted-finding" not in serialized
    assert "fake-raw-web-body" not in serialized
    assert "fake-raw-source-body" not in serialized


def test_failed_skipped_and_other_noncompleted_tasks_become_open_questions() -> None:
    tasks = [
        TodoItem(
            id=1,
            title="Failed task",
            intent="intent",
            query="query",
            status="failed",
        ),
        TodoItem(
            id=2,
            title="Skipped task",
            intent="intent",
            query="query",
            status="skipped",
        ),
        TodoItem(
            id=3,
            title="Cancelled task",
            intent="intent",
            query="query",
            status="cancelled",
        ),
    ]

    context = FollowupContextProjector().project(make_session(tasks))

    assert context.open_questions == (
        "Failed task",
        "Skipped task",
        "Cancelled task",
    )


def test_projection_filters_blank_canonical_values_before_budgeting() -> None:
    tasks = [
        TodoItem(
            id=1,
            title="Completed blank",
            intent="intent",
            query="query",
            status="completed",
            summary="   ",
            sources_summary="\n\t",
        ),
        TodoItem(
            id=2,
            title="Completed useful",
            intent="intent",
            query="query",
            status="completed",
            summary="  Useful finding  ",
            sources_summary="  Useful source  \nIgnored line",
        ),
        TodoItem(
            id=3,
            title="   ",
            intent="intent",
            query="query",
            status="failed",
        ),
    ]

    context = FollowupContextProjector().project(make_session(tasks))

    assert context.key_findings == ("Useful finding",)
    assert context.key_sources == ("Useful source",)
    assert context.open_questions == ()


def test_context_assembler_returns_only_budgeted_typed_memory() -> None:
    followup_context = FollowupContext(
        source_run_id="fake-source",
        key_findings=tuple("f" * 200 for _ in range(7)),
        key_sources=tuple("s" * 200 for _ in range(5)),
        open_questions=tuple(f"Question {index}" for index in range(12)),
    )

    assembled = ResearchContextAssembler().assemble(followup_context)

    assert assembled is not None
    assert set(assembled) == {"key_findings", "key_sources", "open_questions"}
    assert len(assembled["key_findings"]) == 5
    assert len(assembled["key_sources"]) == 3
    assert len(assembled["open_questions"]) == 10
    assert all(len(item) <= 180 for item in assembled["key_findings"])
    assert all(len(item) <= 180 for item in assembled["key_sources"])
    assert "raw_context" not in json.dumps(assembled)
    assert ResearchContextAssembler().assemble(None) is None


def test_context_assembler_filters_blanks_and_retruncates_external_values() -> None:
    followup_context = FollowupContext(
        source_run_id="fake-source",
        key_findings=("  ", "  " + ("f" * 200) + "  "),
        key_sources=("\n", "  " + ("s" * 200) + "  "),
        open_questions=("\t", "  Useful question  "),
    )

    assembled = ResearchContextAssembler().assemble(followup_context)

    assert assembled == {
        "key_findings": ["f" * 180],
        "key_sources": ["s" * 180],
        "open_questions": ["Useful question"],
    }


def test_followup_context_from_legacy_is_bounded_and_ignores_raw_fields() -> None:
    context = FollowupContext.from_legacy(
        {
            "source_run_id": "legacy-parent",
            "key_findings": [" f " * 100] * 7,
            "key_sources": [" source " * 50] * 5,
            "open_questions": [f" Question {index} " for index in range(12)],
            "raw_context": "must-not-survive",
        }
    )

    assert context is not None
    assert context.source_run_id == "legacy-parent"
    assert len(context.key_findings) == 5
    assert len(context.key_sources) == 3
    assert len(context.open_questions) == 10
    assert all(len(item) <= 180 for item in context.key_findings)
    assert all(len(item) <= 180 for item in context.key_sources)
    assert "must-not-survive" not in json.dumps(context.as_dict())
    assert FollowupContext.from_legacy(None) is None


def test_context_compressor_keeps_bounded_legacy_shape() -> None:
    output = SummaryStateOutput(
        running_summary="Legacy report",
        report_markdown="# Legacy report",
        todo_items=make_many_tasks(),
    )

    compressed = ContextCompressor().compress_output(output)

    assert set(compressed) == {"run_summary", "reasoning_memory"}
    assert set(compressed["reasoning_memory"]) == {
        "key_findings",
        "key_sources",
        "open_questions",
    }
    assert len(compressed["reasoning_memory"]["key_findings"]) == 5
    assert len(compressed["reasoning_memory"]["key_sources"]) == 3
    assert len(compressed["reasoning_memory"]["open_questions"]) == 10
    assert "fake-noncompleted-finding" not in json.dumps(
        compressed["reasoning_memory"]
    )
    assert "deprecated" in (ContextCompressor.compress_output.__doc__ or "").lower()
