"""Coordinator tests for the single canonical research execution path."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor as RealThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import agent as agent_module
import services.search as search_service
from agent import DeepResearchAgent, _project_legacy_event
from config import Configuration
from harness.policy import HarnessPolicy, PolicyDecision
from models import ResearchState, TodoItem
from research.adapters import GovernedHelloAgentsLLM
from research.application import ResearchApplicationService
from research.contracts import EventKind, ResearchCommand, RunStatus
from research.operations import (
    GovernedOperations,
    OperationRejectedError,
    OperationScope,
)
from research.repository import SAFE_CONFIG_FIELDS, RunNotFoundError
from research.session import (
    CancellationRequestedError,
    CancellationToken,
    DeadlineExceededError,
    InvalidTransitionError,
    RunSession,
)
from services.github_research import GitHubRepositoryContext, GitHubRepositoryTarget
from services.planner import PlanningService
from services.reporter import ReportingService
from services.search import dispatch_search
from services.summarizer import SummarizationService, TaskSummaryInput


class FakePlanner:
    """Return detached tasks without touching canonical state."""

    def __init__(self, tasks: list[TodoItem] | None = None) -> None:
        self.tasks = tasks or [
            TodoItem(id=1, title="Task", intent="Intent", query="query")
        ]
        self.states: list[ResearchState] = []
        self.prior_contexts: list[dict[str, object] | None] = []
        self.operation_scopes: list[OperationScope | None] = []

    def plan_todo_list(
        self,
        state: ResearchState,
        prior_context: dict[str, object] | None = None,
        *,
        operation_scope: OperationScope | None = None,
    ) -> list[TodoItem]:
        self.states.append(state)
        self.prior_contexts.append(prior_context)
        self.operation_scopes.append(operation_scope)
        return [TodoItem(**task.to_dict()) for task in self.tasks]

    @staticmethod
    def create_fallback_task(state: ResearchState) -> TodoItem:
        return TodoItem(
            id=1,
            title="Fallback",
            intent="Fallback intent",
            query=state.research_topic or "fallback",
        )


class FakeSearchAdapter:
    """Return deterministic search data while recording worker concurrency."""

    def __init__(
        self,
        *,
        failing_queries: set[str] | None = None,
        synchronize_first: int = 0,
    ) -> None:
        self.failing_queries = failing_queries or set()
        self.synchronize_first = synchronize_first
        self.calls: list[tuple[str, int]] = []
        self.thread_ids: list[int] = []
        self.operation_scopes: list[OperationScope | None] = []
        self.max_active = 0
        self._active = 0
        self._lock = threading.Lock()
        self._release = threading.Event()

    def __call__(
        self,
        query: str,
        config: Configuration,
        loop_count: int,
        *,
        cancellation: CancellationToken | None = None,
        operation_scope: OperationScope | None = None,
    ) -> tuple[dict[str, Any], list[str], str | None, str]:
        del config, cancellation
        thread_id = threading.get_ident()
        with self._lock:
            self.calls.append((query, loop_count))
            self.thread_ids.append(thread_id)
            self.operation_scopes.append(operation_scope)
            self._active += 1
            self.max_active = max(self.max_active, self._active)
            call_number = len(self.calls)
            if self.synchronize_first and self._active == self.synchronize_first:
                self._release.set()

        try:
            if call_number <= self.synchronize_first:
                assert self._release.wait(timeout=2), "worker concurrency never reached bound"
            if query in self.failing_queries:
                raise RuntimeError("boom")
            return (
                {
                    "results": [
                        {
                            "title": f"Source for {query}",
                            "url": f"https://example.test/{query}",
                            "content": "RAW-WEB-BODY",
                        }
                    ]
                },
                ["safe notice"],
                None,
                "fake",
            )
        finally:
            with self._lock:
                self._active -= 1


def fake_context_preparer(
    search_result: dict[str, Any] | None,
    answer_text: str | None,
    config: Configuration,
) -> tuple[str, str]:
    """Return a safe source summary and a deliberately raw worker context."""
    del search_result, answer_text, config
    return "- Source https://example.test/source", "RAW-WEB-BODY"


class FakeSummarizer:
    """Stream deterministic text from an explicit immutable request."""

    def __init__(self) -> None:
        self.requests: list[object] = []
        self.operation_scopes: list[OperationScope | None] = []

    def stream_summary(
        self,
        request: object,
        *,
        operation_scope: OperationScope | None = None,
    ):
        self.requests.append(request)
        self.operation_scopes.append(operation_scope)
        assert not hasattr(request, "state")
        assert not hasattr(request, "task")
        with pytest.raises(FrozenInstanceError):
            request.topic = "mutated"  # type: ignore[attr-defined]

        chunks = ["### Summary\n", "- enough detail for the quality gate"]
        collected: list[str] = []

        def generate():
            for chunk in chunks:
                collected.append(chunk)
                yield chunk

        return generate(), lambda: "".join(collected)


class SequenceSummarizer:
    """Return one configured summary per worker attempt."""

    def __init__(self, summaries: list[str]) -> None:
        self._summaries = iter(summaries)

    def stream_summary(
        self,
        request: object,
        *,
        operation_scope: OperationScope | None = None,
    ):
        del request, operation_scope
        summary = next(self._summaries)
        collected: list[str] = []

        def generate():
            collected.append(summary)
            yield summary

        return generate(), lambda: "".join(collected)


class FakeReporter:
    """Return a report after observing canonical terminal task states."""

    def __init__(self) -> None:
        self.states: list[ResearchState] = []
        self.operation_scopes: list[OperationScope | None] = []

    def generate_report(
        self,
        state: ResearchState,
        notes_context: dict[str, Any] | None = None,
        *,
        operation_scope: OperationScope | None = None,
    ) -> str:
        del notes_context
        self.states.append(state)
        self.operation_scopes.append(operation_scope)
        statuses = ",".join(task.status for task in state.todo_items)
        task_sections = "\n".join(
            f"### Task {task.id}: {task.title}\n"
            f"Status: {task.status}. Summary: {task.summary or 'none'}."
            for task in state.todo_items
        )
        return (
            "# Final report\n\n"
            "## Task results\n"
            f"{task_sections or 'No planned tasks were required.'}\n\n"
            "## Findings\n"
            f"The run reached terminal task states ({statuses or 'none'}). "
            "The result is bounded, traceable, and ready for a follow-up. "
            "Source: https://example.test/reference."
        )


class FakeNoteAdapter:
    """Record note operations and their executing thread."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.thread_ids: list[int] = []
        self.operation_scopes: list[OperationScope | None] = []

    def _record(self, operation_scope: OperationScope | None = None) -> None:
        self.thread_ids.append(threading.get_ident())
        self.operation_scopes.append(operation_scope)
        if self.fail:
            raise RuntimeError("optional note failure")

    def create_task_note(
        self,
        *,
        task_id: int,
        title: str,
        content: str,
        operation_scope: OperationScope | None = None,
    ) -> str:
        del title, content
        self._record(operation_scope)
        return f"task-note-{task_id}"

    def note_path(self, note_id: str) -> str:
        self._record()
        return f"safe/{note_id}.md"

    def read_note(
        self,
        note_id: str,
        *,
        operation_scope: OperationScope | None = None,
    ) -> dict[str, str]:
        self._record(operation_scope)
        return {"note_id": note_id, "content": "safe note"}

    def read_all_task_notes(
        self,
        note_ids: list[str],
        *,
        operation_scope: OperationScope | None = None,
    ) -> dict[str, dict[str, str]]:
        self._record(operation_scope)
        return {note_id: {"content": "safe note"} for note_id in note_ids}

    def update_note(self, note_id: str, **kwargs: object) -> str:
        operation_scope = kwargs.pop("operation_scope", None)
        del kwargs
        self._record(
            operation_scope
            if isinstance(operation_scope, OperationScope)
            else None
        )
        return note_id

    def create_conclusion_note(
        self,
        *,
        title: str,
        content: str,
        operation_scope: OperationScope | None = None,
    ) -> str:
        del title, content
        self._record(operation_scope)
        return "report-note"


class UnusedGitHubAdapter:
    """Fail if an ordinary topic unexpectedly triggers GitHub collection."""

    def collect_repository_context(
        self,
        target: object,
        *,
        operation_scope: OperationScope,
        token: str | None = None,
        base_url: str = "https://api.github.com",
    ) -> object:
        del operation_scope, token, base_url
        raise AssertionError(f"unexpected GitHub target: {target}")


class RecordingRepository:
    """Minimal in-memory canonical repository for application integration."""

    def __init__(self) -> None:
        self.snapshots: dict[str, object] = {}

    def save(self, snapshot: object) -> None:
        self.snapshots[snapshot.run_id] = snapshot  # type: ignore[attr-defined]

    def load(self, run_id: str) -> object:
        try:
            return self.snapshots[run_id]
        except KeyError as exc:
            raise RunNotFoundError(run_id) from exc


class AllowingPolicy:
    """Allow deterministic application integration runs."""

    def evaluate(self, command: ResearchCommand) -> list[object]:
        del command
        return []

    def assert_executable(self, decisions: object) -> None:
        del decisions


def make_config(**overrides: object) -> Configuration:
    values: dict[str, object] = {
        "enable_notes": False,
        "enable_github_research": False,
        "enable_quality_gate": False,
        "max_concurrent_tasks": 2,
    }
    values.update(overrides)
    return Configuration.from_env(overrides=values)


def make_agent(
    *,
    tasks: list[TodoItem] | None = None,
    config: Configuration | None = None,
    planner: FakePlanner | None = None,
    search_adapter: FakeSearchAdapter | None = None,
    summarizer: FakeSummarizer | None = None,
    reporter: FakeReporter | None = None,
    note_adapter: FakeNoteAdapter | None = None,
) -> DeepResearchAgent:
    return DeepResearchAgent(
        config=config or make_config(),
        planner=planner or FakePlanner(tasks),
        search_adapter=search_adapter or FakeSearchAdapter(),
        context_preparer=fake_context_preparer,
        summarizer=summarizer or FakeSummarizer(),
        reporting=reporter or FakeReporter(),
        note_agent=note_adapter,
        github_adapter=UnusedGitHubAdapter(),
        legacy_event_queue_capacity=1,
    )


def make_started_session(config: Configuration) -> RunSession:
    session = RunSession(
        command=ResearchCommand(topic="topic", config=config),
        state=ResearchState(research_topic="topic"),
    )
    session.start()
    return session


@pytest.mark.parametrize("value", [1, 16])
def test_max_concurrent_tasks_accepts_documented_bounds(value: int) -> None:
    assert make_config(max_concurrent_tasks=value).max_concurrent_tasks == value


@pytest.mark.parametrize("value", [0, 17])
def test_max_concurrent_tasks_rejects_values_outside_bounds(value: int) -> None:
    with pytest.raises(ValidationError):
        make_config(max_concurrent_tasks=value)


def test_max_concurrent_tasks_is_in_safe_configuration_snapshot() -> None:
    assert "max_concurrent_tasks" in SAFE_CONFIG_FIELDS


def test_full_dependency_injection_skips_default_llm_initialization(monkeypatch) -> None:
    monkeypatch.setattr(
        DeepResearchAgent,
        "_init_llm",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("default LLM initialization must be skipped")
        ),
    )
    make_agent()


def test_default_role_agents_use_simple_agent_contract() -> None:
    from hello_agents import SimpleAgent

    coordinator = DeepResearchAgent(config=make_config())

    assert isinstance(coordinator.todo_agent, SimpleAgent)
    assert isinstance(coordinator.report_agent, SimpleAgent)
    assert coordinator._summarizer_factory is not None
    assert isinstance(coordinator._summarizer_factory(), SimpleAgent)


def test_production_role_agents_wrap_public_llm_with_governance() -> None:
    coordinator = DeepResearchAgent(
        config=make_config(),
        operation_authorizer=HarnessPolicy(),
    )

    planner_llm = coordinator.todo_agent.llm
    reporter_llm = coordinator.report_agent.llm
    summarizer = coordinator._summarizer_factory()
    summarizer_llm = summarizer.llm

    assert isinstance(planner_llm, GovernedHelloAgentsLLM)
    assert isinstance(reporter_llm, GovernedHelloAgentsLLM)
    assert isinstance(summarizer_llm, GovernedHelloAgentsLLM)
    assert planner_llm.role == "planner"
    assert reporter_llm.role == "reporter"
    assert summarizer_llm.role == "summarizer"


def test_execute_passes_one_run_bound_scope_to_all_injected_boundaries() -> None:
    config = make_config(enable_notes=True, max_concurrent_tasks=1)
    policy = HarnessPolicy()
    planner = FakePlanner()
    search = FakeSearchAdapter()
    summarizer = FakeSummarizer()
    reporter = FakeReporter()
    notes = FakeNoteAdapter()
    coordinator = DeepResearchAgent(
        config=config,
        planner=planner,
        search_adapter=search,
        context_preparer=fake_context_preparer,
        summarizer=summarizer,
        reporting=reporter,
        note_agent=notes,
        github_adapter=UnusedGitHubAdapter(),
        operation_authorizer=policy,
    )
    session = make_started_session(config)

    coordinator.execute(session, None)

    scopes = [
        *planner.operation_scopes,
        *search.operation_scopes,
        *summarizer.operation_scopes,
        *reporter.operation_scopes,
        *(scope for scope in notes.operation_scopes if scope is not None),
    ]
    assert scopes
    assert all(isinstance(scope, OperationScope) for scope in scopes)
    operations = {id(scope.operations): scope.operations for scope in scopes}
    assert len(operations) == 1
    governed = next(iter(operations.values()))
    assert isinstance(governed, GovernedOperations)
    assert governed.session is session
    assert planner.operation_scopes[0].task_id is None
    assert reporter.operation_scopes[0].task_id is None
    assert search.operation_scopes[0].task_id == 1
    assert summarizer.operation_scopes[0].task_id == 1
    assert search.operation_scopes[0].task_attempt == 1


def test_worker_passes_deadline_aware_session_to_search_backoff() -> None:
    class DeadlineAwareSearch(FakeSearchAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.cancellation_dependencies: list[object] = []

        def __call__(
            self,
            query: str,
            config: Configuration,
            loop_count: int,
            *,
            cancellation: object | None = None,
            operation_scope: OperationScope | None = None,
        ) -> tuple[dict[str, Any], list[str], str | None, str]:
            self.cancellation_dependencies.append(cancellation)
            return super().__call__(
                query,
                config,
                loop_count,
                cancellation=None,
                operation_scope=operation_scope,
            )

    config = make_config(max_concurrent_tasks=1, run_timeout_seconds=30)
    search = DeadlineAwareSearch()
    coordinator = make_agent(config=config, search_adapter=search)
    session = make_started_session(config)

    coordinator.execute(session, None)

    assert search.cancellation_dependencies == [session]


@pytest.mark.parametrize(
    "control_error",
    [
        OperationRejectedError(),
        DeadlineExceededError("deadline"),
    ],
)
def test_worker_control_error_stops_admission_and_is_rethrown(
    control_error: BaseException,
) -> None:
    config = make_config(max_concurrent_tasks=1)

    class ControlFailingSearch(FakeSearchAdapter):
        def __call__(self, query: str, config: Configuration, loop_count: int, **kwargs):
            del config, loop_count, kwargs
            self.calls.append((query, 0))
            raise control_error

    search = ControlFailingSearch()
    coordinator = DeepResearchAgent(
        config=config,
        planner=FakePlanner(
            [
                TodoItem(id=1, title="Denied", intent="I", query="deny"),
                TodoItem(id=2, title="Never", intent="I", query="never"),
            ]
        ),
        search_adapter=search,
        context_preparer=fake_context_preparer,
        summarizer=FakeSummarizer(),
        reporting=FakeReporter(),
        note_agent=None,
        github_adapter=UnusedGitHubAdapter(),
        operation_authorizer=HarnessPolicy(),
    )
    session = make_started_session(config)

    with pytest.raises(type(control_error)) as caught:
        coordinator.execute(session, None)

    assert caught.value is control_error
    assert session.cancellation.is_cancelled
    assert [query for query, _ in search.calls] == ["deny"]
    assert session.state.structured_report is None


def test_note_rejection_cancels_inflight_worker_before_summary_operation() -> None:
    task_two_search_started = threading.Event()
    rejection_committed = threading.Event()

    class RejectNoteWritesPolicy(HarnessPolicy):
        def evaluate_capability(
            self,
            capability: str,
            request: ResearchCommand,
        ) -> PolicyDecision:
            if capability == "notes:write":
                return PolicyDecision(
                    capability=capability,
                    outcome="deny",
                    reason="Denied for cancellation-race coverage.",
                )
            return super().evaluate_capability(capability, request)

    class CoordinatedSearch(FakeSearchAdapter):
        def __call__(self, query: str, *args: object, **kwargs: object):
            if query == "task-two":
                task_two_search_started.set()
                assert rejection_committed.wait(timeout=2)
            else:
                assert task_two_search_started.wait(timeout=2)
            return super().__call__(query, *args, **kwargs)

    class GovernedRaceSummarizer:
        def __init__(self) -> None:
            self.requested_tasks: list[int] = []
            self.delegate_tasks: list[int] = []

        def stream_summary(
            self,
            request: object,
            *,
            operation_scope: OperationScope | None = None,
        ):
            del request
            assert operation_scope is not None
            assert operation_scope.task_id is not None
            task_id = operation_scope.task_id
            self.requested_tasks.append(task_id)
            collected: list[str] = []
            spec = operation_scope.spec(
                operation_name="summarizer.stream",
                capabilities=("llm:invoke",),
                resource={"role": "summarizer"},
            )

            def delegate():
                self.delegate_tasks.append(task_id)
                chunk = f"### Task {task_id}\n- governed summary"
                collected.append(chunk)
                yield chunk

            return (
                operation_scope.operations.stream(spec, delegate),
                lambda: "".join(collected),
            )

    class DynamicallyRejectingNotes(FakeNoteAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.rejection: OperationRejectedError | None = None
            self.callback_calls = 0

        def update_note(self, note_id: str, **kwargs: object) -> str:
            operation_scope = kwargs.pop("operation_scope", None)
            del kwargs
            assert isinstance(operation_scope, OperationScope)
            spec = operation_scope.spec(
                operation_name="notes.update",
                capabilities=("notes:write",),
                resource={
                    "action": "update",
                    "note_kind": "task",
                    "note_id": note_id,
                },
            )

            def callback() -> str:
                self.callback_calls += 1
                return note_id

            try:
                return operation_scope.operations.call(spec, callback)
            except OperationRejectedError as exc:
                self.rejection = exc
                raise

    config = make_config(
        enable_notes=True,
        max_concurrent_tasks=2,
    )
    summarizer = GovernedRaceSummarizer()
    notes = DynamicallyRejectingNotes()
    coordinator = DeepResearchAgent(
        config=config,
        planner=FakePlanner(
            [
                TodoItem(id=1, title="One", intent="I", query="task-one"),
                TodoItem(id=2, title="Two", intent="I", query="task-two"),
            ]
        ),
        search_adapter=CoordinatedSearch(),
        context_preparer=fake_context_preparer,
        summarizer=summarizer,
        reporting=FakeReporter(),
        note_agent=notes,
        github_adapter=UnusedGitHubAdapter(),
        operation_authorizer=RejectNoteWritesPolicy(),
    )
    session = make_started_session(config)
    operation_events: list[tuple[EventKind, str | None]] = []

    def observe(event: Any) -> None:
        kind = event.kind
        if kind in {
            EventKind.OPERATION_STARTED,
            EventKind.OPERATION_COMPLETED,
            EventKind.OPERATION_FAILED,
            EventKind.OPERATION_REJECTED,
        }:
            operation_events.append((kind, event.operation_id))
        if kind is EventKind.OPERATION_REJECTED:
            rejection_committed.set()
            # Deliberately widen the visibility window: the rejected event is
            # public before the coordinator regains control and requests
            # cancellation.
            time.sleep(0.05)

    session.add_observer(observe)

    with pytest.raises(OperationRejectedError) as caught:
        coordinator.execute(session, None)

    assert caught.value is notes.rejection
    assert notes.callback_calls == 0
    assert session.cancellation.is_cancelled
    assert summarizer.delegate_tasks == [1]
    rejected_index = next(
        index
        for index, (kind, _operation_id) in enumerate(operation_events)
        if kind is EventKind.OPERATION_REJECTED
    )
    started_before_rejection = {
        operation_id
        for kind, operation_id in operation_events[:rejected_index]
        if kind is EventKind.OPERATION_STARTED
    }
    for kind, operation_id in operation_events[rejected_index + 1 :]:
        assert kind is not EventKind.OPERATION_STARTED
        if kind in {EventKind.OPERATION_COMPLETED, EventKind.OPERATION_FAILED}:
            assert operation_id in started_before_rejection


def test_worker_rejection_precedes_raw_observer_cancellation() -> None:
    class DenySearchPolicy(HarnessPolicy):
        def evaluate_capability(
            self,
            capability: str,
            request: ResearchCommand,
        ) -> PolicyDecision:
            if capability == "search:web":
                return PolicyDecision(
                    capability=capability,
                    outcome="deny",
                    reason="Denied for rejection-precedence coverage.",
                )
            return super().evaluate_capability(capability, request)

    class GovernedRejectingSearch:
        def __init__(self) -> None:
            self.callback_calls = 0

        def __call__(
            self,
            query: str,
            config: Configuration,
            loop_count: int,
            *,
            cancellation: object | None = None,
            operation_scope: OperationScope | None = None,
        ):
            del config, loop_count, cancellation
            assert operation_scope is not None
            spec = operation_scope.spec(
                operation_name="search.execute",
                capabilities=("search:web",),
                resource={
                    "query_hash": "f" * 64,
                    "backend": "fake",
                },
            )

            def callback():
                self.callback_calls += 1
                return ({"results": []}, [], None, "fake")

            return operation_scope.operations.call(spec, callback)

    config = make_config(max_concurrent_tasks=1)
    search = GovernedRejectingSearch()
    coordinator = DeepResearchAgent(
        config=config,
        planner=FakePlanner(),
        search_adapter=search,
        context_preparer=fake_context_preparer,
        summarizer=FakeSummarizer(),
        reporting=FakeReporter(),
        note_agent=None,
        github_adapter=UnusedGitHubAdapter(),
        operation_authorizer=DenySearchPolicy(),
    )
    session = make_started_session(config)
    rejected_operation_ids: list[str] = []

    def cancel_and_block(event: Any) -> None:
        if event.kind is EventKind.OPERATION_REJECTED:
            assert event.operation_id is not None
            rejected_operation_ids.append(event.operation_id)
            session.cancellation.cancel()
            time.sleep(0.1)

    session.add_observer(cancel_and_block)

    with pytest.raises(OperationRejectedError) as rejected:
        coordinator.execute(session, None)

    assert search.callback_calls == 0
    assert len(rejected_operation_ids) == 1
    assert rejected.value.operation_id == rejected_operation_ids[0]
    assert session.cancellation.is_cancelled


def test_concurrent_worker_rejections_propagate_first_committed_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_workers = threading.Event()
    first_rejection_visible = threading.Event()
    second_error_ready = threading.Event()

    class ReleasingResultQueue(queue.Queue):
        def get(self, *args: object, **kwargs: object):
            release_workers.set()
            return super().get(*args, **kwargs)

    class DenySearchPolicy(HarnessPolicy):
        def evaluate_capability(
            self,
            capability: str,
            request: ResearchCommand,
        ) -> PolicyDecision:
            if capability == "search:web":
                return PolicyDecision(
                    capability=capability,
                    outcome="deny",
                    reason="Denied for first-rejection coverage.",
                )
            return super().evaluate_capability(capability, request)

    class OrderedRejectingSearch:
        def __init__(self) -> None:
            self.errors: dict[str, OperationRejectedError] = {}

        def __call__(
            self,
            query: str,
            config: Configuration,
            loop_count: int,
            *,
            cancellation: object | None = None,
            operation_scope: OperationScope | None = None,
        ):
            del config, loop_count, cancellation
            assert operation_scope is not None
            assert release_workers.wait(timeout=2)
            if query == "task-one":
                assert first_rejection_visible.wait(timeout=2)
            spec = operation_scope.spec(
                operation_name="search.execute",
                capabilities=("search:web",),
                resource={
                    "query_hash": ("1" if query == "task-one" else "2") * 64,
                    "backend": "fake",
                },
            )
            try:
                return operation_scope.operations.call(
                    spec,
                    lambda: ({"results": []}, [], None, "fake"),
                )
            except OperationRejectedError as exc:
                self.errors[query] = exc
                if query == "task-one":
                    second_error_ready.set()
                raise

    monkeypatch.setattr(agent_module, "Queue", ReleasingResultQueue)
    config = make_config(max_concurrent_tasks=2)
    search = OrderedRejectingSearch()
    coordinator = DeepResearchAgent(
        config=config,
        planner=FakePlanner(
            [
                TodoItem(id=1, title="One", intent="I", query="task-one"),
                TodoItem(id=2, title="Two", intent="I", query="task-two"),
            ]
        ),
        search_adapter=search,
        context_preparer=fake_context_preparer,
        summarizer=FakeSummarizer(),
        reporting=FakeReporter(),
        note_agent=None,
        github_adapter=UnusedGitHubAdapter(),
        operation_authorizer=DenySearchPolicy(),
    )
    session = make_started_session(config)
    rejection_events: list[tuple[int | None, str | None]] = []

    def order_rejections(event: Any) -> None:
        if event.kind is not EventKind.OPERATION_REJECTED:
            return
        rejection_events.append((event.task_id, event.operation_id))
        if event.task_id == 2:
            first_rejection_visible.set()
            assert second_error_ready.wait(timeout=2)

    session.add_observer(order_rejections)

    with pytest.raises(OperationRejectedError) as propagated:
        coordinator.execute(session, None)

    assert [task_id for task_id, _operation_id in rejection_events] == [2, 1]
    first_operation_id = rejection_events[0][1]
    second_operation_id = rejection_events[1][1]
    assert first_operation_id is not None
    assert second_operation_id is not None
    assert first_operation_id != second_operation_id
    assert search.errors["task-one"].operation_id == second_operation_id
    assert propagated.value.operation_id == first_operation_id


def test_github_control_error_is_not_downgraded_to_optional_notice() -> None:
    rejected = OperationRejectedError()

    class RejectingGitHub:
        def collect_repository_context(self, target: object, **kwargs: object) -> object:
            del target, kwargs
            raise rejected

    config = make_config(enable_github_research=True)
    coordinator = DeepResearchAgent(
        config=config,
        planner=FakePlanner(),
        search_adapter=FakeSearchAdapter(),
        context_preparer=fake_context_preparer,
        summarizer=FakeSummarizer(),
        reporting=FakeReporter(),
        note_agent=None,
        github_adapter=RejectingGitHub(),
        operation_authorizer=HarnessPolicy(),
    )
    session = RunSession(
        command=ResearchCommand(
            topic="https://github.com/owner/repository",
            config=config,
        ),
        state=ResearchState(
            research_topic="https://github.com/owner/repository"
        ),
    )
    session.start()

    with pytest.raises(OperationRejectedError) as caught:
        coordinator.execute(session, None)

    assert caught.value is rejected


def test_note_control_error_is_not_downgraded_to_optional_failure() -> None:
    rejected = OperationRejectedError()

    class RejectingNotes(FakeNoteAdapter):
        def create_task_note(self, **kwargs: object) -> str:
            del kwargs
            raise rejected

    config = make_config(enable_notes=True, max_concurrent_tasks=1)
    coordinator = DeepResearchAgent(
        config=config,
        planner=FakePlanner(),
        search_adapter=FakeSearchAdapter(),
        context_preparer=fake_context_preparer,
        summarizer=FakeSummarizer(),
        reporting=FakeReporter(),
        note_agent=RejectingNotes(),
        github_adapter=UnusedGitHubAdapter(),
        operation_authorizer=HarnessPolicy(),
    )
    session = make_started_session(config)

    with pytest.raises(OperationRejectedError) as caught:
        coordinator.execute(session, None)

    assert caught.value is rejected


def test_real_venv_role_agent_does_not_enter_tool_iteration() -> None:
    backend_dir = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(backend_dir / "src")
    environment["PYTHONIOENCODING"] = "utf-8"
    script = r'''
import json
from agent import DeepResearchAgent
from config import Configuration
from harness.policy import HarnessPolicy
from models import ResearchState
from research.contracts import ResearchCommand
from research.operations import GovernedOperations, OperationScope
from research.session import RunSession

class StubLLM:
    def __init__(self):
        self.stream_calls = 0
        self.invoke_calls = 0

    def stream_invoke(self, messages, **kwargs):
        del messages, kwargs
        self.stream_calls += 1
        yield '[TOOL_CALL:note:{"action":"read","note_id":"secret"}]'

    def invoke(self, messages, **kwargs):
        del messages, kwargs
        self.invoke_calls += 1
        return "unexpected fallback"

llm = StubLLM()
coordinator = object.__new__(DeepResearchAgent)
coordinator.llm = llm
coordinator.config = Configuration(enable_notes=False)
role = coordinator._create_role_agent(
    name="role",
    system_prompt="system",
    llm=llm,
)
session = RunSession(
    command=ResearchCommand(topic="topic", config=coordinator.config),
    state=ResearchState(research_topic="topic"),
)
session.start()
operation_scope = OperationScope(
    operations=GovernedOperations(session, HarnessPolicy()),
)
output = "".join(
    role.stream_run(
        "prompt",
        _research_operation_scope=operation_scope,
    )
)
assert type(role).__name__ == "SimpleAgent", type(role).__name__
assert llm.stream_calls == 1, llm.stream_calls
assert llm.invoke_calls == 0, llm.invoke_calls
assert "[TOOL_CALL:note:" in output, output
print(json.dumps({"stream_calls": llm.stream_calls, "output": output}))
'''

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=backend_dir,
        env=environment,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert ".venv" in sys.executable
    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["stream_calls"] == 1


def test_legacy_run_and_stream_use_identical_canonical_output() -> None:
    sync_agent = make_agent()
    stream_agent = make_agent()

    sync_output = sync_agent.run("topic")
    events = list(stream_agent.run_stream("topic"))

    assert sync_agent.last_session is not None
    assert stream_agent.last_session is not None
    assert sync_agent.last_session.to_legacy_output() == sync_output
    assert stream_agent.last_session.to_legacy_output() == sync_output
    assert [event["type"] for event in events].count("done") == 1
    assert events[-1]["type"] == "done"
    assert events[-2]["type"] == "final_report"
    assert "raw_context" not in json.dumps(events)
    assert "RAW-WEB-BODY" not in json.dumps(events)


def test_legacy_projection_uses_immutable_event_not_later_task_state() -> None:
    session = make_started_session(make_config())
    session.install_plan(
        [
            TodoItem(
                id=1,
                title="Original title",
                intent="Original intent",
                query="q",
                note_id="original-note",
                stream_token="task_1",
            )
        ]
    )
    started = session.start_task(1)
    session.complete_task(1, summary="later summary", sources_summary="later source")

    projected = _project_legacy_event(started)

    assert projected is not None
    assert projected["status"] == "in_progress"
    assert projected["title"] == "Original title"
    assert projected["note_id"] == "original-note"
    assert projected.get("summary") is None


def test_worker_exception_marks_task_failed_and_other_tasks_still_report() -> None:
    tasks = [
        TodoItem(id=1, title="Fails", intent="I", query="fail"),
        TodoItem(id=2, title="Works", intent="I", query="pass"),
    ]
    reporter = FakeReporter()
    agent = make_agent(
        tasks=tasks,
        search_adapter=FakeSearchAdapter(failing_queries={"fail"}),
        reporter=reporter,
    )
    session = make_started_session(agent.config)

    agent.execute(session, None)

    assert [task.status for task in session.state.todo_items] == ["failed", "completed"]
    assert sum(event.kind is EventKind.TASK_FAILED for event in session.events) == 1
    assert session.state.structured_report
    assert reporter.states[-1] is session.state
    assert [event.sequence for event in session.events] == list(
        range(1, len(session.events) + 1)
    )


def test_planner_receives_the_canonical_session_state() -> None:
    """Do not create a second mutable ResearchState for planning."""
    planner = FakePlanner()
    agent = make_agent(planner=planner)
    session = make_started_session(agent.config)

    agent.execute(session, None)

    assert len(planner.states) == 1
    assert planner.states[0] is session.state


def test_github_collection_does_not_mutate_state_before_session_transition() -> None:
    """Keep repository context writes behind RunSession.record_repository()."""
    config = make_config(enable_github_research=True)

    class GitHubAdapter:
        def collect_repository_context(
            self,
            target: GitHubRepositoryTarget,
        ) -> GitHubRepositoryContext:
            return GitHubRepositoryContext(target=target, repository={"stars": 1})

    agent = DeepResearchAgent(
        config=config,
        planner=FakePlanner(),
        search_adapter=FakeSearchAdapter(),
        context_preparer=fake_context_preparer,
        summarizer=FakeSummarizer(),
        reporting=FakeReporter(),
        note_agent=None,
        github_adapter=GitHubAdapter(),
    )
    session = RunSession(
        command=ResearchCommand(
            topic="https://github.com/owner/repository",
            config=config,
        ),
        state=ResearchState(
            research_topic="https://github.com/owner/repository",
        ),
    )
    session.start()
    scope = OperationScope(
        operations=GovernedOperations(session, HarnessPolicy()),
    )

    context = agent._prepare_github_context(
        session.state,
        config=config,
        operation_scope=scope,
    )

    assert context is not None
    assert session.state.github_context == {}


def test_no_results_retry_preserves_payload_history_and_original_query() -> None:
    class EmptyThenSuccessSearch(FakeSearchAdapter):
        def __call__(self, query: str, *args: object, **kwargs: object):
            if query == "original":
                return ({"results": []}, [], None, "fake")
            return super().__call__(query, *args, **kwargs)

    task = TodoItem(
        id=1,
        title="Retry",
        intent="broader, intent",
        query="original",
    )
    agent = make_agent(tasks=[task], search_adapter=EmptyThenSuccessSearch())
    session = make_started_session(agent.config)

    agent.execute(session, None)

    canonical = session.state.todo_items[0]
    retries = [
        event for event in session.events
        if event.kind is EventKind.TASK_RETRY_SCHEDULED
    ]
    assert canonical.status == "completed"
    assert canonical.query == "original"
    assert canonical.retry_count == 1
    assert canonical.refined_queries == ["broader intent"]
    assert {
        field: retries[0].payload[field]
        for field in ("previous_query", "refined_query", "attempt", "reason")
    } == {
        "previous_query": "original",
        "refined_query": "broader intent",
        "attempt": 1,
        "reason": "no_search_results",
    }


def test_quality_gate_retry_preserves_reason_and_original_query() -> None:
    task = TodoItem(
        id=1,
        title="Quality",
        intent="broader, intent",
        query="original",
    )
    config = make_config(enable_quality_gate=True)
    agent = make_agent(
        tasks=[task],
        config=config,
        summarizer=SequenceSummarizer(
            ["short", "### Good\n- sufficiently detailed structured summary"]
        ),  # type: ignore[arg-type]
    )
    session = make_started_session(config)

    agent.execute(session, None)

    canonical = session.state.todo_items[0]
    retry = next(
        event for event in session.events
        if event.kind is EventKind.TASK_RETRY_SCHEDULED
    )
    assert canonical.status == "completed"
    assert canonical.query == "original"
    assert canonical.refined_queries == ["broader intent"]
    assert retry.payload["attempt"] == 1
    assert retry.payload["reason"] == "too_short,no_structure"


def test_task_workers_never_exceed_configured_concurrency() -> None:
    tasks = [
        TodoItem(id=index, title=f"T{index}", intent="I", query=f"q{index}")
        for index in range(1, 6)
    ]
    search = FakeSearchAdapter(synchronize_first=2)
    agent = make_agent(tasks=tasks, search_adapter=search)
    session = make_started_session(agent.config)

    agent.execute(session, None)

    assert search.max_active == 2
    assert all(task.status == "completed" for task in session.state.todo_items)


def test_executor_never_has_more_than_bound_submitted_futures(monkeypatch) -> None:
    recorders: list[object] = []

    class RecordingExecutor:
        def __init__(self, *, max_workers: int, thread_name_prefix: str) -> None:
            self._inner = RealThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix=thread_name_prefix,
            )
            self._lock = threading.Lock()
            self.outstanding = 0
            self.max_outstanding = 0
            recorders.append(self)

        def submit(self, function, *args, **kwargs):
            with self._lock:
                self.outstanding += 1
                self.max_outstanding = max(self.max_outstanding, self.outstanding)
            future = self._inner.submit(function, *args, **kwargs)

            def completed(_future) -> None:
                with self._lock:
                    self.outstanding -= 1

            future.add_done_callback(completed)
            return future

        def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
            self._inner.shutdown(wait=wait, cancel_futures=cancel_futures)

    monkeypatch.setattr("agent.ThreadPoolExecutor", RecordingExecutor)
    tasks = [
        TodoItem(id=index, title=f"T{index}", intent="I", query=f"q{index}")
        for index in range(1, 9)
    ]
    agent = make_agent(
        tasks=tasks,
        search_adapter=FakeSearchAdapter(synchronize_first=2),
    )
    session = make_started_session(agent.config)

    agent.execute(session, None)

    assert recorders
    assert recorders[0].max_outstanding <= 2  # type: ignore[attr-defined]


def test_capacity_one_legacy_stream_close_requests_cancellation_quickly(monkeypatch) -> None:
    class BlockingCancellationSearch(FakeSearchAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.exited = threading.Event()
            self.queries: list[str] = []
            self.cancellation_dependencies: list[RunSession] = []

        def __call__(
            self,
            query: str,
            *args: object,
            cancellation: RunSession | None = None,
            **kwargs: object,
        ):
            del args, kwargs
            assert cancellation is not None
            self.queries.append(query)
            self.cancellation_dependencies.append(cancellation)
            self.entered.set()
            try:
                while not cancellation.wait(0.01):
                    pass
                cancellation.raise_if_cancelled()
            finally:
                self.exited.set()

    search = BlockingCancellationSearch()
    config = make_config(max_concurrent_tasks=1)
    tasks = [
        TodoItem(id=1, title="One", intent="I", query="q1"),
        TodoItem(id=2, title="Two", intent="I", query="q2"),
    ]
    agent = make_agent(tasks=tasks, config=config, search_adapter=search)
    captured: list[RunSession] = []
    original_new_session = agent._new_legacy_session

    def capture_session(*args: object, **kwargs: object) -> RunSession:
        session = original_new_session(*args, **kwargs)
        captured.append(session)
        return session

    monkeypatch.setattr(agent, "_new_legacy_session", capture_session)
    stream = agent.run_stream("topic")
    assert next(stream)["type"] == "status"
    assert next(stream)["type"] == "todo_list"
    assert search.entered.wait(timeout=1)

    started = time.monotonic()
    stream.close()
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert search.exited.wait(timeout=1)
    assert search.queries == ["q1"]
    assert search.cancellation_dependencies == [captured[0]]
    assert search.cancellation_dependencies[0].cancellation.is_cancelled
    assert captured[0].cancellation.is_cancelled
    assert captured[0].state.todo_items[1].status == "pending"
    assert agent.last_session is None


def test_all_execute_transitions_run_on_the_coordinator_thread(monkeypatch) -> None:
    coordinator_thread = threading.get_ident()
    agent = make_agent()
    session = make_started_session(agent.config)
    transition_threads: list[int] = []
    transition_names = (
        "record_repository",
        "install_plan",
        "start_task",
        "record_sources",
        "append_task_summary",
        "record_retry",
        "complete_task",
        "skip_task",
        "fail_task",
        "record_report_note",
        "set_report",
    )
    for name in transition_names:
        original = getattr(session, name)

        def wrapped(*args: object, _original=original, **kwargs: object):
            transition_threads.append(threading.get_ident())
            return _original(*args, **kwargs)

        monkeypatch.setattr(session, name, wrapped)

    agent.execute(session, None)

    assert transition_threads
    assert set(transition_threads) == {coordinator_thread}


def test_note_io_stays_on_coordinator_thread_and_metadata_is_projected() -> None:
    coordinator_thread = threading.get_ident()
    notes = FakeNoteAdapter()
    search = FakeSearchAdapter()
    config = make_config(enable_notes=True)
    agent = make_agent(config=config, search_adapter=search, note_adapter=notes)
    session = make_started_session(config)

    agent.execute(session, None)

    assert notes.thread_ids
    assert set(notes.thread_ids) == {coordinator_thread}
    assert search.thread_ids and set(search.thread_ids) != {coordinator_thread}
    task = session.state.todo_items[0]
    assert task.note_id == "task-note-1"
    assert task.note_path == "safe/task-note-1.md"
    assert session.state.report_note_id == "report-note"
    assert session.state.report_note_path == "safe/report-note.md"
    note_events = [
        event for event in session.events
        if event.kind is EventKind.REPORT_NOTE_CREATED
    ]
    assert note_events[0].payload["note_id"] == "report-note"


def test_optional_note_failure_does_not_fail_completed_task() -> None:
    config = make_config(enable_notes=True)
    agent = make_agent(
        config=config,
        note_adapter=FakeNoteAdapter(fail=True),
    )
    session = make_started_session(config)

    agent.execute(session, None)

    assert session.state.todo_items[0].status == "completed"
    assert session.state.structured_report


def test_worker_exception_secrets_never_enter_typed_legacy_or_logs(caplog) -> None:
    sentinel = "ghp_secret-key RAW-PRIVATE-BODY"

    class SecretFailingSearch(FakeSearchAdapter):
        def __call__(self, *args: object, **kwargs: object):
            raise RuntimeError(sentinel)

    agent = make_agent(search_adapter=SecretFailingSearch())
    with caplog.at_level("ERROR"):
        events = list(agent.run_stream("topic"))
    assert agent.last_session is not None
    typed = json.dumps(
        [event.as_dict() for event in agent.last_session.events],
        ensure_ascii=False,
    )

    assert sentinel not in typed
    assert sentinel not in json.dumps(events, ensure_ascii=False)
    assert sentinel not in caplog.text
    failed = next(
        event for event in agent.last_session.events
        if event.kind is EventKind.TASK_FAILED
    )
    assert failed.payload["message"] == "Task execution failed."
    assert events[-2]["type"] == "final_report"
    assert events[-1]["type"] == "done"


def test_github_exception_secrets_never_enter_events() -> None:
    sentinel = "github_token_secret RAW-GITHUB-BODY"

    class SecretFailingGitHub:
        def collect_repository_context(self, target: object) -> object:
            del target
            raise RuntimeError(sentinel)

    config = make_config(enable_github_research=True)
    agent = DeepResearchAgent(
        config=config,
        planner=FakePlanner(),
        search_adapter=FakeSearchAdapter(),
        context_preparer=fake_context_preparer,
        summarizer=FakeSummarizer(),
        reporting=FakeReporter(),
        note_agent=None,
        github_adapter=SecretFailingGitHub(),
    )

    events = list(agent.run_stream("https://github.com/owner/repository"))

    assert agent.last_session is not None
    serialized = json.dumps(
        [event.as_dict() for event in agent.last_session.events],
        ensure_ascii=False,
    )
    assert sentinel not in serialized
    assert sentinel not in json.dumps(events, ensure_ascii=False)
    repository = next(
        event for event in events if event["type"] == "github_repository"
    )
    assert repository["notices"] == ["GitHub API context collection failed."]


def test_cancellation_stops_worker_progress_without_false_completion() -> None:
    token = CancellationToken()

    class CancellingSearch(FakeSearchAdapter):
        def __call__(self, *args: object, **kwargs: object):
            token.cancel()
            return super().__call__(*args, **kwargs)

    config = make_config()
    agent = make_agent(config=config, search_adapter=CancellingSearch())
    session = RunSession(
        command=ResearchCommand(topic="topic", config=config),
        state=ResearchState(research_topic="topic"),
        cancellation_token=token,
    )
    session.start()

    with pytest.raises(CancellationRequestedError):
        agent.execute(session, None)

    assert not any(event.kind is EventKind.TASK_COMPLETED for event in session.events)
    assert session.state.structured_report is None


def test_cancellation_after_completed_queue_delivery_prevents_merge(
    monkeypatch,
) -> None:
    cancellation = CancellationToken()

    class CancellingDeliveryQueue:
        def __init__(self, maxsize: int = 0) -> None:
            self._queue: queue.Queue[object] = queue.Queue(maxsize=maxsize)

        def put(self, item: object, timeout: float | None = None) -> None:
            self._queue.put(item, timeout=timeout)

        def _cancel_if_completed(self, item: object) -> object:
            kind = getattr(item, "kind", None)
            if getattr(kind, "value", None) == "completed":
                cancellation.cancel()
            return item

        def get(self, timeout: float | None = None) -> object:
            return self._cancel_if_completed(self._queue.get(timeout=timeout))

        def get_nowait(self) -> object:
            return self._cancel_if_completed(self._queue.get_nowait())

    monkeypatch.setattr(agent_module, "Queue", CancellingDeliveryQueue)
    config = make_config(max_concurrent_tasks=1)
    agent = make_agent(config=config)
    session = RunSession(
        command=ResearchCommand(topic="topic", config=config),
        state=ResearchState(research_topic="topic"),
        cancellation_token=cancellation,
    )
    session.start()

    with pytest.raises(CancellationRequestedError):
        agent.execute(session, None)

    assert session.state.todo_items[0].status != "completed"
    assert not any(
        event.kind is EventKind.TASK_COMPLETED for event in session.events
    )


def test_cancellation_during_terminal_transition_prevents_note_update() -> None:
    cancellation = CancellationToken()

    class TrackingNotes(FakeNoteAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.update_calls = 0

        def update_note(self, note_id: str, **kwargs: object) -> str:
            self.update_calls += 1
            return super().update_note(note_id, **kwargs)

    notes = TrackingNotes()
    config = make_config(enable_notes=True, max_concurrent_tasks=1)
    agent = make_agent(config=config, note_adapter=notes)
    session = RunSession(
        command=ResearchCommand(topic="topic", config=config),
        state=ResearchState(research_topic="topic"),
        cancellation_token=cancellation,
    )
    session.add_observer(
        lambda event: cancellation.cancel()
        if event.kind is EventKind.TASK_COMPLETED
        else None
    )
    session.start()

    with pytest.raises(CancellationRequestedError):
        agent.execute(session, None)

    assert notes.update_calls == 0


def test_cancellation_on_failed_future_prevents_failure_transition_and_note(
    monkeypatch,
) -> None:
    cancellation = CancellationToken()

    class TrackingNotes(FakeNoteAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.update_calls = 0

        def update_note(self, note_id: str, **kwargs: object) -> str:
            self.update_calls += 1
            return super().update_note(note_id, **kwargs)

    class CancellingFuture:
        def __init__(self, inner) -> None:
            self._inner = inner

        def done(self) -> bool:
            completed = self._inner.done()
            if completed:
                cancellation.cancel()
            return completed

        def result(self):
            return self._inner.result()

    class CancellingExecutor:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._inner = RealThreadPoolExecutor(*args, **kwargs)

        def submit(self, function, *args: object, **kwargs: object):
            return CancellingFuture(self._inner.submit(function, *args, **kwargs))

        def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
            self._inner.shutdown(wait=wait, cancel_futures=cancel_futures)

    monkeypatch.setattr(agent_module, "ThreadPoolExecutor", CancellingExecutor)
    notes = TrackingNotes()
    config = make_config(enable_notes=True, max_concurrent_tasks=1)
    agent = make_agent(
        config=config,
        note_adapter=notes,
    )

    def explode_worker(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("worker failure")

    monkeypatch.setattr(agent, "_run_task_worker", explode_worker)
    session = RunSession(
        command=ResearchCommand(topic="topic", config=config),
        state=ResearchState(research_topic="topic"),
        cancellation_token=cancellation,
    )
    session.start()

    with pytest.raises(CancellationRequestedError):
        agent.execute(session, None)

    assert not any(event.kind is EventKind.TASK_FAILED for event in session.events)
    assert notes.update_calls == 0


def test_cancellation_returning_from_planner_stops_before_notes_or_plan() -> None:
    token = CancellationToken()
    notes = FakeNoteAdapter()

    class CancellingPlanner(FakePlanner):
        def plan_todo_list(self, state: ResearchState, prior_context=None):
            token.cancel()
            return super().plan_todo_list(state, prior_context)

    config = make_config(enable_notes=True)
    agent = DeepResearchAgent(
        config=config,
        planner=CancellingPlanner(),
        search_adapter=FakeSearchAdapter(),
        context_preparer=fake_context_preparer,
        summarizer=FakeSummarizer(),
        reporting=FakeReporter(),
        note_agent=notes,
        github_adapter=UnusedGitHubAdapter(),
    )
    session = RunSession(
        command=ResearchCommand(topic="topic", config=config),
        state=ResearchState(research_topic="topic"),
        cancellation_token=token,
    )
    session.start()

    with pytest.raises(CancellationRequestedError):
        agent.execute(session, None)

    assert notes.thread_ids == []
    assert not any(event.kind is EventKind.PLAN_CREATED for event in session.events)


def test_cancellation_returning_from_github_stops_before_repository_or_plan() -> None:
    token = CancellationToken()

    class CancellingGitHub:
        def collect_repository_context(
            self,
            target: GitHubRepositoryTarget,
        ) -> GitHubRepositoryContext:
            token.cancel()
            return GitHubRepositoryContext(target=target, repository={"stars": 1})

    config = make_config(enable_github_research=True)
    agent = DeepResearchAgent(
        config=config,
        planner=FakePlanner(),
        search_adapter=FakeSearchAdapter(),
        context_preparer=fake_context_preparer,
        summarizer=FakeSummarizer(),
        reporting=FakeReporter(),
        note_agent=None,
        github_adapter=CancellingGitHub(),
    )
    session = RunSession(
        command=ResearchCommand(
            topic="https://github.com/owner/repository",
            config=config,
        ),
        state=ResearchState(research_topic="https://github.com/owner/repository"),
        cancellation_token=token,
    )
    session.start()

    with pytest.raises(CancellationRequestedError):
        agent.execute(session, None)

    assert not any(
        event.kind in {EventKind.REPOSITORY_DETECTED, EventKind.PLAN_CREATED}
        for event in session.events
    )


def test_cancellation_returning_from_reporter_stops_before_note_and_report() -> None:
    token = CancellationToken()
    notes = FakeNoteAdapter()

    class CancellingReporter(FakeReporter):
        def generate_report(self, state: ResearchState, notes_context=None) -> str:
            del state, notes_context
            token.cancel()
            return "# cancelled report"

    config = make_config(enable_notes=True)
    agent = make_agent(
        config=config,
        reporter=CancellingReporter(),
        note_adapter=notes,
    )
    session = RunSession(
        command=ResearchCommand(topic="topic", config=config),
        state=ResearchState(research_topic="topic"),
        cancellation_token=token,
    )
    session.start()

    with pytest.raises(CancellationRequestedError):
        agent.execute(session, None)

    assert "report-note" not in {
        task.note_id for task in session.state.todo_items
    }
    assert session.state.report_note_id is None
    assert session.state.structured_report is None
    assert not any(
        event.kind in {EventKind.REPORT_NOTE_CREATED, EventKind.REPORT_GENERATED}
        for event in session.events
    )


def test_notices_remain_per_task_and_are_projected_with_source_status() -> None:
    class NoticeSearch(FakeSearchAdapter):
        def __call__(self, query: str, *args: object, **kwargs: object):
            result, _notices, answer, backend = super().__call__(
                query,
                *args,
                **kwargs,
            )
            return result, [f"notice-{query}"], answer, backend

    tasks = [
        TodoItem(id=1, title="One", intent="I", query="one"),
        TodoItem(id=2, title="Two", intent="I", query="two"),
    ]
    agent = make_agent(tasks=tasks, search_adapter=NoticeSearch())
    events = list(agent.run_stream("topic"))

    assert agent.last_session is not None
    by_id = {task.id: task for task in agent.last_session.state.todo_items}
    assert by_id[1].notices == ["Search backend returned a notice."]
    assert by_id[2].notices == ["Search backend returned a notice."]
    assert by_id[1].notice_codes == ["search_backend_notice"]
    assert by_id[2].notice_codes == ["search_backend_notice"]
    sources = [event for event in events if event["type"] == "sources"]
    assert {event["task_id"]: event["notices"] for event in sources} == {
        1: ["Search backend returned a notice."],
        2: ["Search backend returned a notice."],
    }
    assert {event["task_id"]: event["notice_codes"] for event in sources} == {
        1: ["search_backend_notice"],
        2: ["search_backend_notice"],
    }
    assert all(event["status"] == "in_progress" for event in sources)


def test_search_secrets_never_reach_cache_snapshot_or_note(tmp_path: Path) -> None:
    """The provider boundary must protect every durable downstream projection."""

    class SecretSearchRunner:
        def run(self, parameters: dict[str, Any]) -> dict[str, Any]:
            del parameters
            return {
                "results": [
                    {
                        "title": "Safe source",
                        "url": (
                            "https://source-user:source-password@example.test/reference"
                            "?token=TOKEN_SENTINEL#SIGNATURE_SENTINEL"
                        ),
                        "content": (
                            "Useful content at "
                            "https://content-user:content-password@cdn.example.test/body"
                            "?signature=SIGNATURE_SENTINEL"
                        ),
                        "raw_content": (
                            "Full page at https://raw-user:raw-password@raw.example.test/page"
                            "?token=RAW_CONTENT_SENTINEL"
                        ),
                        "headers": {
                            "Authorization": "ARBITRARY_PAYLOAD_SENTINEL"
                        },
                    }
                ],
                "backend": "duckduckgo",
                "answer": (
                    "Answer at https://answer-user:answer-password@example.test/answer"
                    "?token=TOKEN_SENTINEL"
                ),
                "extra": "ARBITRARY_PAYLOAD_SENTINEL",
            }

    runner = SecretSearchRunner()

    def secure_dispatch(
        query: str,
        config: Configuration,
        loop_count: int,
        cancellation: object | None = None,
        *,
        operation_scope: OperationScope | None = None,
    ):
        return dispatch_search(
            query,
            config,
            loop_count,
            use_cache=True,
            cancellation=cancellation,  # type: ignore[arg-type]
            operation_scope=operation_scope,
            search_adapter=runner,
        )

    class EchoContextSummarizer:
        def stream_summary(
            self,
            request: object,
            *,
            operation_scope: OperationScope | None = None,
        ):
            del operation_scope
            summary = str(getattr(request, "context"))
            collected: list[str] = []

            def generate():
                collected.append(summary)
                yield summary

            return generate(), lambda: "".join(collected)

    class RecordingSearchNote(FakeNoteAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.persisted_content: list[str] = []

        def update_note(self, note_id: str, **kwargs: object) -> str:
            content = kwargs.get("content")
            if isinstance(content, str):
                self.persisted_content.append(content)
            return super().update_note(note_id, **kwargs)

        def create_conclusion_note(
            self,
            *,
            title: str,
            content: str,
            operation_scope: OperationScope | None = None,
        ) -> str:
            self.persisted_content.extend((title, content))
            return super().create_conclusion_note(
                title=title,
                content=content,
                operation_scope=operation_scope,
            )

    config = make_config(
        enable_notes=True,
        notes_workspace=str(tmp_path / "notes"),
        fetch_full_page=True,
        max_concurrent_tasks=1,
    )
    note = RecordingSearchNote()
    coordinator = DeepResearchAgent(
        config=config,
        planner=FakePlanner(
            [TodoItem(id=1, title="Security", intent="Inspect", query="safe query")]
        ),
        search_adapter=secure_dispatch,
        context_preparer=search_service.prepare_research_context,
        summarizer=EchoContextSummarizer(),
        reporting=FakeReporter(),
        note_agent=note,
        github_adapter=UnusedGitHubAdapter(),
    )
    repository = RecordingRepository()
    service = ResearchApplicationService(
        coordinator=coordinator,
        repository=repository,  # type: ignore[arg-type]
        policy=AllowingPolicy(),
    )

    result = service.execute(ResearchCommand(topic="topic", config=config))
    snapshot = repository.snapshots[result.run_id]
    cache_files = list((tmp_path / "cache" / "search").glob("*.json"))
    assert len(cache_files) == 1
    cache_wire = json.loads(cache_files[0].read_text(encoding="utf-8"))
    serialized = json.dumps(
        {
            "result": result.output.todo_items[0].to_dict() if result.output else None,
            "snapshot": snapshot.as_dict(),  # type: ignore[attr-defined]
            "notes": note.persisted_content,
            "cache": cache_wire,
        },
        ensure_ascii=False,
    )

    for sentinel in (
        "source-user",
        "source-password",
        "content-user",
        "content-password",
        "raw-user",
        "raw-password",
        "answer-user",
        "answer-password",
        "TOKEN_SENTINEL",
        "SIGNATURE_SENTINEL",
        "RAW_CONTENT_SENTINEL",
        "ARBITRARY_PAYLOAD_SENTINEL",
    ):
        assert sentinel not in serialized
    assert "https://example.test/reference" in serialized
    assert "https://cdn.example.test/body" in serialized
    assert "https://raw.example.test/page" in serialized
    assert "raw_content" not in cache_wire["results"][0]
    assert "answer" not in cache_wire


def test_untrusted_search_notice_is_replaced_by_stable_code_and_message() -> None:
    sentinel = "Authorization: Bearer search-secret RAW_SEARCH_BODY"

    class UntrustedNoticeSearch(FakeSearchAdapter):
        def __call__(self, query: str, *args: object, **kwargs: object):
            result, _notices, answer, _backend = super().__call__(
                query,
                *args,
                **kwargs,
            )
            return result, [sentinel], answer, "private-backend"

    agent = make_agent(search_adapter=UntrustedNoticeSearch())
    legacy_events = list(agent.run_stream("topic"))

    assert agent.last_session is not None
    session = agent.last_session
    serialized = json.dumps(
        {
            "typed": [event.as_dict() for event in session.events],
            "legacy": legacy_events,
            "canonical": [task.to_dict() for task in session.state.todo_items],
            "snapshot": session.to_snapshot().as_dict(),
            "report": session.state.structured_report,
        },
        ensure_ascii=False,
    )
    assert sentinel not in serialized
    assert "Bearer search-secret" not in serialized
    assert "RAW_SEARCH_BODY" not in serialized

    source_event = next(
        event for event in session.events if event.kind is EventKind.SOURCES_COLLECTED
    )
    source_payload = source_event.as_dict()["payload"]
    assert source_payload["backend"] == "private-backend"
    assert source_payload["notices"] == ["Search backend returned a notice."]
    assert source_payload["notice_codes"] == ["search_backend_notice"]
    task = session.state.todo_items[0]
    assert task.notices == ["Search backend returned a notice."]
    assert task.notice_codes == ["search_backend_notice"]
    legacy_source = next(event for event in legacy_events if event["type"] == "sources")
    assert legacy_source["backend"] is None
    assert legacy_source["notice_codes"] == ["search_backend_notice"]


def test_untrusted_github_notice_is_redacted_from_all_run_surfaces() -> None:
    sentinel = "Authorization: Bearer github-secret RAW_GITHUB_BODY"

    class UntrustedNoticeGitHub:
        def collect_repository_context(
            self,
            target: GitHubRepositoryTarget,
        ) -> GitHubRepositoryContext:
            return GitHubRepositoryContext(
                target=target,
                repository={"stars": 7},
                notices=[sentinel],
            )

    config = make_config(enable_github_research=True)
    agent = DeepResearchAgent(
        config=config,
        planner=FakePlanner(),
        search_adapter=FakeSearchAdapter(),
        context_preparer=fake_context_preparer,
        summarizer=FakeSummarizer(),
        reporting=FakeReporter(),
        note_agent=None,
        github_adapter=UntrustedNoticeGitHub(),
    )
    legacy_events = list(agent.run_stream("https://github.com/owner/repository"))

    assert agent.last_session is not None
    session = agent.last_session
    serialized = json.dumps(
        {
            "typed": [event.as_dict() for event in session.events],
            "legacy": legacy_events,
            "github_context": session.state.github_context,
            "canonical": [task.to_dict() for task in session.state.todo_items],
            "snapshot": session.to_snapshot().as_dict(),
            "report": session.state.structured_report,
        },
        ensure_ascii=False,
    )
    assert sentinel not in serialized
    assert "Bearer github-secret" not in serialized
    assert "RAW_GITHUB_BODY" not in serialized

    repository_event = next(
        event for event in session.events if event.kind is EventKind.REPOSITORY_DETECTED
    )
    repository_payload = repository_event.as_dict()["payload"]
    assert repository_payload["notices"] == ["GitHub API returned a notice."]
    assert repository_payload["notice_codes"] == ["github_api_notice"]
    legacy_repository = next(
        event for event in legacy_events if event["type"] == "github_repository"
    )
    assert legacy_repository["notices"] == ["GitHub API returned a notice."]
    assert legacy_repository["notice_codes"] == ["github_api_notice"]


def test_reporter_exception_propagates_without_final_report_or_done() -> None:
    class RaisingReporter(FakeReporter):
        def generate_report(self, state: ResearchState, notes_context=None) -> str:
            del state, notes_context
            raise RuntimeError("reporter exploded")

    agent = make_agent(reporter=RaisingReporter())
    observed: list[dict[str, Any]] = []

    with pytest.raises(RuntimeError, match="reporter exploded"):
        for event in agent.run_stream("topic"):
            observed.append(event)

    assert not any(event["type"] == "final_report" for event in observed)
    assert not any(event["type"] == "done" for event in observed)
    assert agent.last_session is None


def test_reporting_service_exception_is_redacted_everywhere(caplog) -> None:
    sentinel = "REPORTER_SECRET Authorization: Bearer private-token"

    class ExplodingReportAgent:
        def run(self, prompt: str) -> str:
            del prompt
            raise RuntimeError(sentinel)

        def clear_history(self) -> None:
            return None

    config = make_config()
    reporter = ReportingService(ExplodingReportAgent(), config)  # type: ignore[arg-type]
    coordinator = make_agent(config=config, reporter=reporter)  # type: ignore[arg-type]
    repository = RecordingRepository()
    typed_events = []
    service = ResearchApplicationService(
        coordinator=coordinator,
        repository=repository,  # type: ignore[arg-type]
        policy=AllowingPolicy(),
    )

    with caplog.at_level("ERROR"):
        result = service.execute(
            ResearchCommand(topic="topic", config=config),
            observer=typed_events.append,
        )

    assert result.status is RunStatus.REPORT_INCOMPLETE
    assert result.error is not None
    assert result.error.code == "report_incomplete"
    assert result.output is not None
    assert sentinel not in (result.output.report_markdown or "")
    assert sentinel not in json.dumps(
        [event.as_dict() for event in typed_events],
        ensure_ascii=False,
    )
    snapshot = repository.snapshots[result.run_id]
    assert sentinel not in json.dumps(snapshot.as_dict(), ensure_ascii=False)  # type: ignore[attr-defined]
    assert sentinel not in caplog.text

    legacy_agent = make_agent(
        config=config,
        reporter=ReportingService(ExplodingReportAgent(), config),  # type: ignore[arg-type]
    )
    legacy_events = list(legacy_agent.run_stream("topic"))
    assert sentinel not in json.dumps(legacy_events, ensure_ascii=False)


def test_execute_requires_and_preserves_running_lifecycle() -> None:
    agent = make_agent()
    pending = RunSession(
        command=ResearchCommand(topic="topic", config=agent.config),
        state=ResearchState(research_topic="topic"),
    )
    with pytest.raises(InvalidTransitionError):
        agent.execute(pending, None)

    running = make_started_session(agent.config)
    agent.execute(running, None)

    assert running.status is RunStatus.RUNNING
    assert not any(
        event.kind in {
            EventKind.RUN_COMPLETED,
            EventKind.RUN_FAILED,
            EventKind.RUN_CANCELLED,
            EventKind.RUN_REJECTED,
        }
        for event in running.events
    )


def test_direct_legacy_adapters_call_execute_exactly_once(monkeypatch) -> None:
    sync_agent = make_agent()
    stream_agent = make_agent()
    sync_calls = 0
    stream_calls = 0
    sync_execute = sync_agent.execute
    stream_execute = stream_agent.execute

    def count_sync(*args: object, **kwargs: object) -> None:
        nonlocal sync_calls
        sync_calls += 1
        sync_execute(*args, **kwargs)

    def count_stream(*args: object, **kwargs: object) -> None:
        nonlocal stream_calls
        stream_calls += 1
        stream_execute(*args, **kwargs)

    monkeypatch.setattr(sync_agent, "execute", count_sync)
    monkeypatch.setattr(stream_agent, "execute", count_stream)

    sync_agent.run("topic")
    stream_events = list(stream_agent.run_stream("topic"))

    assert sync_calls == 1
    assert stream_calls == 1
    assert stream_agent.last_session is not None
    completion = stream_events[-1]
    assert completion == {
        "type": "done",
        "run_id": stream_agent.last_session.run_id,
        "schema_version": 1,
        "sequence": len(stream_agent.last_session.events) + 1,
    }
    assert not any(
        event.kind in {
            EventKind.RUN_COMPLETED,
            EventKind.RUN_FAILED,
            EventKind.RUN_CANCELLED,
            EventKind.RUN_REJECTED,
        }
        for event in stream_agent.last_session.events
    )


def test_three_empty_search_attempts_skip_task_and_still_generate_report() -> None:
    class EmptySearch(FakeSearchAdapter):
        def __call__(self, query: str, *args: object, **kwargs: object):
            del query, args, kwargs
            return ({"results": []}, ["empty"], None, "fake")

    task = TodoItem(id=1, title="Empty", intent="I", query="original")
    agent = make_agent(tasks=[task], search_adapter=EmptySearch())
    session = make_started_session(agent.config)

    agent.execute(session, None)

    canonical = session.state.todo_items[0]
    assert canonical.status == "skipped"
    assert canonical.query == "original"
    assert canonical.retry_count == 2
    assert len(canonical.refined_queries) == 2
    assert session.state.structured_report is not None
    assert "# Final report" in session.state.structured_report
    assert "states (skipped)" in session.state.structured_report


def test_search_retry_backoff_waits_on_cancellation(monkeypatch) -> None:
    class CancellingWait:
        def wait(self, delay: float) -> bool:
            assert delay > 0
            return True

        def raise_if_cancelled(self) -> None:
            raise CancellationRequestedError("cancelled")

    monkeypatch.setattr(
        "services.search._try_single_search",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("transient")),
    )
    monkeypatch.setattr(
        "services.search.time.sleep",
        lambda delay: (_ for _ in ()).throw(
            AssertionError(f"unconditional sleep used: {delay}")
        ),
    )

    with pytest.raises(CancellationRequestedError):
        dispatch_search(
            "query",
            make_config(),
            0,
            use_cache=False,
            cancellation=CancellingWait(),
        )


def test_real_venv_search_import_and_lazy_initialization_are_cp936_safe() -> None:
    backend_dir = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(backend_dir / "src")
    environment["PYTHONIOENCODING"] = "cp936"
    environment["PYTHONUTF8"] = "0"
    script = r'''
import json
from research.adapters import HelloAgentsSearchAdapter

adapter = HelloAgentsSearchAdapter()
assert not hasattr(adapter._local, "tool")
adapter.run({"input": ""})
tool = adapter._local.tool
print(json.dumps({"tool_type": type(tool).__name__}))
'''

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=backend_dir,
        env=environment,
        capture_output=True,
        timeout=20,
        check=False,
    )
    stdout = completed.stdout.decode("cp936", errors="replace")
    stderr = completed.stderr.decode("cp936", errors="replace")

    assert ".venv" in sys.executable
    assert completed.returncode == 0, stderr or stdout
    output_lines = [line for line in stdout.splitlines() if line.strip()]
    assert json.loads(output_lines[-1]) == {"tool_type": "SearchTool"}


def test_search_tool_is_lazy_and_isolated_per_worker_thread() -> None:
    from research.adapters import HelloAgentsSearchAdapter

    created: list[object] = []
    creation_guard = threading.Lock()

    class FakeSearchTool:
        def __init__(self, *, backend: str) -> None:
            assert backend == "hybrid"
            with creation_guard:
                created.append(self)

        def run(self, payload: dict[str, Any]) -> int:
            del payload
            return id(self)

    adapter = HelloAgentsSearchAdapter(tool_factory=FakeSearchTool)
    start = threading.Barrier(2)

    def resolve_twice() -> tuple[int, int]:
        start.wait(timeout=1)
        first = adapter.run({})
        second = adapter.run({})
        return first, second

    with RealThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(resolve_twice) for _ in range(2)]
        resolved = [future.result(timeout=2) for future in futures]

    assert all(first == second for first, second in resolved)
    assert resolved[0][0] != resolved[1][0]
    assert len(created) == 2


def test_search_cache_writes_use_atomic_replace_for_same_key(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config = make_config(notes_workspace=str(tmp_path / "notes"))
    replacements: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def recording_replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        replacements.append((Path(source), Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(search_service.os, "replace", recording_replace)
    start = threading.Barrier(4)
    payloads = [
        {
            "results": [
                {
                    "title": f"result-{index}",
                    "url": "https://example.test/reference?token=not-persisted",
                    "content": f"content-{index}",
                }
            ],
            "backend": "duckduckgo",
        }
        for index in range(4)
    ]

    def save(payload: dict[str, Any]) -> None:
        start.wait(timeout=1)
        search_service._save_to_cache("same-query", config, payload)

    with RealThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(save, payload) for payload in payloads]
        for future in futures:
            future.result(timeout=2)

    cache_file = (
        search_service._cache_dir(config)
        / f"{search_service._cache_key('same-query', config)}.json"
    )
    persisted = json.loads(cache_file.read_text(encoding="utf-8"))
    assert persisted["schema_version"] == 1
    assert persisted["backend"] == "duckduckgo"
    assert persisted["results"][0]["url"] == "https://example.test/reference"
    assert persisted["results"][0]["title"] in {
        f"result-{index}" for index in range(4)
    }
    assert persisted["results"][0]["content"] in {
        f"content-{index}" for index in range(4)
    }
    assert "not-persisted" not in json.dumps(persisted)
    assert len(replacements) == 4
    assert all(source != target == cache_file for source, target in replacements)
    assert not list(cache_file.parent.glob("*.tmp"))


def test_summary_stream_close_does_not_yield_during_generator_exit() -> None:
    class StreamingAgent:
        def __init__(self) -> None:
            self.cleared = False

        def stream_run(self, prompt: str):
            del prompt
            yield "visible<think>hidden</think>tail"

        def clear_history(self) -> None:
            self.cleared = True

    role_agent = StreamingAgent()
    service = SummarizationService(
        lambda: role_agent,  # type: ignore[arg-type]
        make_config(strip_thinking_tokens=True),
    )
    stream, _get_summary = service.stream_summary(
        TaskSummaryInput(
            topic="topic",
            title="title",
            intent="intent",
            query="query",
            context="context",
        )
    )

    assert next(stream) == "visible"
    stream.close()
    assert role_agent.cleared


def test_planner_returns_tasks_without_mutating_input_state() -> None:
    class PlannerAgent:
        def run(self, prompt: str) -> str:
            del prompt
            return json.dumps(
                {"tasks": [{"title": "T", "intent": "I", "query": "q"}]}
            )

        def clear_history(self) -> None:
            return None

    original = TodoItem(id=99, title="Existing", intent="I", query="old")
    state = ResearchState(research_topic="topic", todo_items=[original])
    planner = PlanningService(PlannerAgent(), make_config())  # type: ignore[arg-type]

    planned = planner.plan_todo_list(state)

    assert [task.id for task in planned] == [1]
    assert state.todo_items == [original]


def test_planner_serializes_agent_history_across_concurrent_runs() -> None:
    class HistorySensitivePlannerAgent:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0
            self.history: list[str] = []
            self.clear_calls = 0
            self.guard = threading.Lock()

        def run(self, prompt: str) -> str:
            topic = "PLANNER_ALPHA" if "PLANNER_ALPHA" in prompt else "PLANNER_BETA"
            with self.guard:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                self.history.append(topic)
            time.sleep(0.05)
            with self.guard:
                observed = "|".join(self.history)
                self.active -= 1
            return json.dumps(
                {"tasks": [{"title": topic, "intent": "I", "query": observed}]}
            )

        def clear_history(self) -> None:
            with self.guard:
                self.history.clear()
                self.clear_calls += 1

    role_agent = HistorySensitivePlannerAgent()
    service = PlanningService(role_agent, make_config())  # type: ignore[arg-type]
    start = threading.Barrier(2)

    def plan(topic: str) -> str:
        start.wait(timeout=1)
        return service.plan_todo_list(ResearchState(research_topic=topic))[0].query

    with RealThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(plan, "PLANNER_ALPHA"),
            executor.submit(plan, "PLANNER_BETA"),
        ]
        queries = {future.result(timeout=2) for future in futures}

    assert queries == {"PLANNER_ALPHA", "PLANNER_BETA"}
    assert role_agent.max_active == 1
    assert role_agent.clear_calls == 2


def test_planner_clears_history_when_agent_run_raises() -> None:
    class ExplodingPlannerAgent:
        def __init__(self) -> None:
            self.cleared = False

        def run(self, prompt: str) -> str:
            del prompt
            raise RuntimeError("planner failure")

        def clear_history(self) -> None:
            self.cleared = True

    role_agent = ExplodingPlannerAgent()
    service = PlanningService(role_agent, make_config())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="planner failure"):
        service.plan_todo_list(ResearchState(research_topic="topic"))

    assert role_agent.cleared


def test_reporter_serializes_agent_history_across_concurrent_runs() -> None:
    class HistorySensitiveReportAgent:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0
            self.history: list[str] = []
            self.clear_calls = 0
            self.guard = threading.Lock()

        def run(self, prompt: str) -> str:
            topic = "REPORT_ALPHA" if "REPORT_ALPHA" in prompt else "REPORT_BETA"
            with self.guard:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                self.history.append(topic)
            time.sleep(0.05)
            with self.guard:
                observed = "|".join(self.history)
                self.active -= 1
            return observed

        def clear_history(self) -> None:
            with self.guard:
                self.history.clear()
                self.clear_calls += 1

    role_agent = HistorySensitiveReportAgent()
    service = ReportingService(role_agent, make_config())  # type: ignore[arg-type]
    start = threading.Barrier(2)

    def report(topic: str) -> str:
        start.wait(timeout=1)
        return service.generate_report(ResearchState(research_topic=topic))

    with RealThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(report, "REPORT_ALPHA"),
            executor.submit(report, "REPORT_BETA"),
        ]
        reports = {future.result(timeout=2) for future in futures}

    assert reports == {"REPORT_ALPHA", "REPORT_BETA"}
    assert role_agent.max_active == 1
    assert role_agent.clear_calls == 2


def test_application_service_completes_with_real_coordinator_and_fakes() -> None:
    config = make_config()
    coordinator = make_agent(config=config)
    repository = RecordingRepository()
    service = ResearchApplicationService(
        coordinator=coordinator,
        repository=repository,  # type: ignore[arg-type]
        policy=AllowingPolicy(),
    )

    result = service.execute(ResearchCommand(topic="topic", config=config))

    assert result.status is RunStatus.COMPLETED
    assert result.output is not None
    assert result.output.report_markdown is not None
    assert "# Final report" in result.output.report_markdown
    assert "states (completed)" in result.output.report_markdown
    assert result.run_id in repository.snapshots
