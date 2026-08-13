"""Contracts for framework adapters at governed side-effect boundaries."""

from __future__ import annotations

import importlib
import json
import logging
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock
from typing import Any

import pytest

from config import Configuration, SearchAPI
from harness.policy import PolicyDecision
from models import ResearchState
from research.contracts import EventKind, ResearchCommand
from research.session import RunSession


class AllowingPolicy:
    """Allow every requested capability while retaining real policy decisions."""

    def evaluate_capability(
        self,
        capability: str,
        command: ResearchCommand,
    ) -> PolicyDecision:
        del command
        return PolicyDecision(
            capability=capability,
            outcome="allow",
            reason="Allowed by adapter contract test.",
        )


class OutcomePolicy(AllowingPolicy):
    """Override selected capability outcomes for denial-path contracts."""

    def __init__(self, outcomes: dict[str, str]) -> None:
        self.outcomes = outcomes

    def evaluate_capability(
        self,
        capability: str,
        command: ResearchCommand,
    ) -> PolicyDecision:
        del command
        return PolicyDecision(
            capability=capability,
            outcome=self.outcomes.get(capability, "allow"),
            reason="Static adapter-test decision.",
        )


def _load_adapters() -> Any:
    """Import the production adapter module after a useful RED assertion."""
    module_path = Path(__file__).parents[1] / "src" / "research" / "adapters.py"
    assert module_path.exists(), "research/adapters.py has not been implemented"
    return importlib.import_module("research.adapters")


def _started_session() -> RunSession:
    command = ResearchCommand(
        topic="adapter contract",
        config=Configuration(enable_notes=False),
    )
    session = RunSession(
        command=command,
        state=ResearchState(research_topic=command.topic),
    )
    session.start()
    return session


def _operation_scope(*, task_id: int | None = None, task_attempt: int = 1):
    from research.operations import GovernedOperations, OperationScope

    return OperationScope(
        operations=GovernedOperations(_started_session(), AllowingPolicy()),
        task_id=task_id,
        task_attempt=task_attempt,
        fallback_index=1,
    )


def test_governed_llm_consumes_private_scope_before_public_delegate() -> None:
    """The project-only scope must never become an OpenAI request keyword."""
    adapters = _load_adapters()
    from research.operations import GovernedOperations, OperationScope

    class RecordingLLM:
        model = "safe-model"

        def __init__(self) -> None:
            self.kwargs: dict[str, Any] | None = None

        def invoke(
            self,
            messages: list[dict[str, str]],
            **kwargs: Any,
        ) -> str:
            assert messages[-1]["content"] == "private prompt"
            self.kwargs = dict(kwargs)
            return "delegate result"

    session = _started_session()
    operations = GovernedOperations(session, AllowingPolicy())
    scope = OperationScope(
        operations=operations,
        task_attempt=1,
        fallback_index=1,
    )
    delegate = RecordingLLM()
    llm = adapters.GovernedHelloAgentsLLM(
        delegate,
        role="planner",
        model_id="safe-model",
    )

    result = llm.invoke(
        [{"role": "user", "content": "private prompt"}],
        temperature=0.25,
        _research_operation_scope=scope,
    )

    assert result == "delegate result"
    assert delegate.kwargs == {"temperature": 0.25}
    operation_events = [
        event
        for event in session.events
        if event.kind
        in {EventKind.OPERATION_STARTED, EventKind.OPERATION_COMPLETED}
    ]
    assert [event.kind for event in operation_events] == [
        EventKind.OPERATION_STARTED,
        EventKind.OPERATION_COMPLETED,
    ]
    serialized = str([event.as_dict() for event in operation_events])
    assert "private prompt" not in serialized


def test_governed_llm_rejects_missing_scope_before_delegate() -> None:
    """A production wrapper cannot silently bypass run-bound governance."""
    adapters = _load_adapters()

    class ForbiddenLLM:
        model = "safe-model"

        def invoke(self, messages: object, **kwargs: object) -> str:
            raise AssertionError(f"delegate was called: {messages!r} {kwargs!r}")

    llm = adapters.GovernedHelloAgentsLLM(
        ForbiddenLLM(),
        role="planner",
        model_id="safe-model",
    )

    with pytest.raises(adapters.MissingOperationScopeError):
        llm.invoke([{"role": "user", "content": "prompt"}])


def test_governed_llm_exposes_only_reviewed_non_callable_data() -> None:
    """Expose the provider required by Agent.__str__, not arbitrary delegate data."""
    adapters = _load_adapters()

    class Delegate:
        model = "safe-model"
        provider = "custom"
        public_value = "available"
        _private_value = "hidden"

    wrapper = adapters.GovernedHelloAgentsLLM(Delegate(), role="planner")

    assert wrapper.provider == "custom"
    with pytest.raises(AttributeError):
        getattr(wrapper, "public_value")
    with pytest.raises(AttributeError):
        getattr(wrapper, "_private_value")


def test_governed_llm_rejects_unknown_public_callable_without_invoking_it() -> None:
    """Unknown framework methods cannot bypass the governed invoke methods."""
    adapters = _load_adapters()

    class Delegate:
        model = "safe-model"
        provider = "custom"

        def __init__(self) -> None:
            self.think_calls = 0

        def think(self, messages: object) -> str:
            self.think_calls += 1
            return f"unsafe:{messages!r}"

    delegate = Delegate()
    wrapper = adapters.GovernedHelloAgentsLLM(delegate, role="planner")

    with pytest.raises(AttributeError):
        wrapper.think([{"role": "user", "content": "private prompt"}])
    assert delegate.think_calls == 0


def test_governed_llm_stream_is_lazy_and_strips_scope() -> None:
    """Streaming authorization and delegate construction start at first pull."""
    adapters = _load_adapters()
    from research.operations import GovernedOperations, OperationScope

    class RecordingLLM:
        model = "safe-model"

        def __init__(self) -> None:
            self.calls = 0
            self.kwargs: dict[str, Any] | None = None

        def stream_invoke(
            self,
            messages: list[dict[str, str]],
            **kwargs: Any,
        ):
            assert messages[-1]["content"] == "stream prompt"
            self.calls += 1
            self.kwargs = dict(kwargs)
            yield "one"
            yield "two"

    session = _started_session()
    operations = GovernedOperations(session, AllowingPolicy())
    scope = OperationScope(
        operations=operations,
        task_id=7,
        task_attempt=2,
        fallback_index=1,
    )
    delegate = RecordingLLM()
    llm = adapters.GovernedHelloAgentsLLM(
        delegate,
        role="summarizer",
        model_id="safe-model",
    )

    stream = llm.stream_invoke(
        [{"role": "user", "content": "stream prompt"}],
        temperature=0.1,
        _research_operation_scope=scope,
    )
    assert delegate.calls == 0
    assert next(stream) == "one"
    assert delegate.calls == 1
    assert delegate.kwargs == {"temperature": 0.1}
    assert list(stream) == ["two"]

    operation_events = [
        event
        for event in session.events
        if event.kind
        in {EventKind.OPERATION_STARTED, EventKind.OPERATION_COMPLETED}
    ]
    assert [event.kind for event in operation_events] == [
        EventKind.OPERATION_STARTED,
        EventKind.OPERATION_COMPLETED,
    ]
    assert all(event.task_id == 7 for event in operation_events)
    serialized = str([event.as_dict() for event in operation_events])
    assert "stream prompt" not in serialized


def test_planner_forwards_explicit_scope_through_simple_agent_run() -> None:
    """Planning must bind its LLM call to the current run without shared state."""
    from services.planner import PlanningService

    class PlannerAgent:
        def __init__(self) -> None:
            self.kwargs: dict[str, Any] | None = None

        def run(self, prompt: str, **kwargs: Any) -> str:
            assert prompt
            self.kwargs = dict(kwargs)
            return '{"tasks":[{"title":"T","intent":"I","query":"Q"}]}'

        def clear_history(self) -> None:
            return None

    scope = _operation_scope()
    agent = PlannerAgent()
    service = PlanningService(agent, Configuration(enable_notes=False))  # type: ignore[arg-type]

    tasks = service.plan_todo_list(
        ResearchState(research_topic="topic"),
        operation_scope=scope,
    )

    assert len(tasks) == 1
    assert agent.kwargs == {"_research_operation_scope": scope}


def test_planner_logs_do_not_leak_raw_output_or_generated_titles(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Planner diagnostics must contain counts, not LLM-generated content."""
    from services.planner import PlanningService

    raw_sentinel = "PLANNER_RAW_OUTPUT_SECRET"
    title_sentinel = "PLANNER_GENERATED_TITLE_SECRET"

    class Agent:
        def run(self, prompt: str, **kwargs: Any) -> str:
            del prompt, kwargs
            return json.dumps(
                {
                    "private_response": raw_sentinel,
                    "tasks": [
                        {
                            "title": title_sentinel,
                            "intent": "intent",
                            "query": "query",
                        }
                    ],
                }
            )

        def clear_history(self) -> None:
            return None

    service = PlanningService(  # type: ignore[arg-type]
        Agent(),
        Configuration(enable_notes=False),
    )
    caplog.set_level(logging.INFO, logger="services.planner")

    tasks = service.plan_todo_list(ResearchState(research_topic="topic"))
    assert tasks[0].title == title_sentinel
    assert "Planner produced 1 task" in caplog.text
    assert raw_sentinel not in caplog.text
    assert title_sentinel not in caplog.text


def test_summarizer_forwards_scope_to_fresh_stream_agent() -> None:
    """Each task attempt must pass its own immutable scope to its fresh agent."""
    from services.summarizer import SummarizationService, TaskSummaryInput

    class SummaryAgent:
        def __init__(self) -> None:
            self.kwargs: dict[str, Any] | None = None

        def stream_run(self, prompt: str, **kwargs: Any):
            assert prompt
            self.kwargs = dict(kwargs)
            yield "summary"

        def clear_history(self) -> None:
            return None

    scope = _operation_scope(task_id=4, task_attempt=2)
    agent = SummaryAgent()
    service = SummarizationService(
        lambda: agent,  # type: ignore[arg-type]
        Configuration(enable_notes=False, strip_thinking_tokens=False),
    )
    stream, get_summary = service.stream_summary(
        TaskSummaryInput(
            topic="topic",
            title="title",
            intent="intent",
            query="query",
            context="context",
        ),
        operation_scope=scope,
    )

    assert list(stream) == ["summary"]
    assert get_summary() == "summary"
    assert agent.kwargs == {"_research_operation_scope": scope}


def test_summarizer_legacy_methods_forward_scope() -> None:
    """Historical service entry points must retain the governed call context."""
    from models import TodoItem
    from services.summarizer import SummarizationService

    class SummaryAgent:
        def __init__(self) -> None:
            self.run_kwargs: dict[str, Any] | None = None
            self.stream_kwargs: dict[str, Any] | None = None

        def run(self, prompt: str, **kwargs: Any) -> str:
            assert prompt
            self.run_kwargs = dict(kwargs)
            return "complete summary"

        def stream_run(self, prompt: str, **kwargs: Any):
            assert prompt
            self.stream_kwargs = dict(kwargs)
            yield "stream summary"

        def clear_history(self) -> None:
            return None

    scope = _operation_scope(task_id=5)
    agents: list[SummaryAgent] = []

    def factory() -> SummaryAgent:
        agent = SummaryAgent()
        agents.append(agent)
        return agent

    service = SummarizationService(
        factory,  # type: ignore[arg-type]
        Configuration(enable_notes=False, strip_thinking_tokens=False),
    )
    state = ResearchState(research_topic="topic")
    task = TodoItem(id=5, title="title", intent="intent", query="query")

    assert service.summarize_task(
        state,
        task,
        "context",
        operation_scope=scope,
    ) == "complete summary"
    stream, get_summary = service.stream_task_summary(
        state,
        task,
        "context",
        operation_scope=scope,
    )
    assert list(stream) == ["stream summary"]
    assert get_summary() == "stream summary"
    assert agents[0].run_kwargs == {"_research_operation_scope": scope}
    assert agents[1].stream_kwargs == {"_research_operation_scope": scope}


def test_summarizer_close_closes_underlying_agent_stream() -> None:
    """Closing SSE consumption must terminalize the governed LLM iterator."""
    from services.summarizer import SummarizationService, TaskSummaryInput

    class ClosingStream:
        def __init__(self) -> None:
            self.closed = False
            self.values = iter(("first", "second"))

        def __iter__(self):
            return self

        def __next__(self) -> str:
            return next(self.values)

        def close(self) -> None:
            self.closed = True

    class Agent:
        def __init__(self, stream: ClosingStream) -> None:
            self.stream = stream
            self.cleared = False

        def stream_run(self, prompt: str, **kwargs: Any) -> ClosingStream:
            del prompt, kwargs
            return self.stream

        def clear_history(self) -> None:
            self.cleared = True

    delegate = ClosingStream()
    agent = Agent(delegate)
    service = SummarizationService(
        lambda: agent,  # type: ignore[arg-type]
        Configuration(enable_notes=False, strip_thinking_tokens=False),
    )
    stream, _summary = service.stream_summary(
        TaskSummaryInput(
            topic="topic",
            title="title",
            intent="intent",
            query="query",
            context="context",
        )
    )

    assert next(stream) == "first"
    stream.close()

    assert delegate.closed
    assert agent.cleared


def test_reporter_forwards_scope_and_rethrows_operation_rejection() -> None:
    """Optional report fallback must not convert policy rejection into success."""
    from research.operations import OperationRejectedError
    from services.reporter import ReportingService

    class RejectingAgent:
        def __init__(self) -> None:
            self.kwargs: dict[str, Any] | None = None
            self.cleared = False

        def run(self, prompt: str, **kwargs: Any) -> str:
            assert prompt
            self.kwargs = dict(kwargs)
            raise OperationRejectedError("a" * 32)

        def clear_history(self) -> None:
            self.cleared = True

    scope = _operation_scope()
    agent = RejectingAgent()
    service = ReportingService(agent, Configuration(enable_notes=False))  # type: ignore[arg-type]

    with pytest.raises(OperationRejectedError):
        service.generate_report(
            ResearchState(research_topic="topic"),
            operation_scope=scope,
        )

    assert agent.kwargs == {"_research_operation_scope": scope}
    assert agent.cleared


def test_search_tool_is_lazy_and_constructed_inside_governed_attempt() -> None:
    """SearchTool construction must occur only after authorization and start audit."""
    adapters = _load_adapters()
    from research.operations import GovernedOperations, OperationScope
    from services.search import dispatch_search

    session = _started_session()
    operations = GovernedOperations(session, AllowingPolicy())
    scope = OperationScope(
        operations=operations,
        task_id=2,
        task_attempt=1,
        fallback_index=1,
    )
    created_after_started: list[bool] = []

    class SearchTool:
        def __init__(self, *, backend: str) -> None:
            assert backend == "hybrid"
            created_after_started.append(
                any(event.kind is EventKind.OPERATION_STARTED for event in session.events)
            )

        def run(self, parameters: dict[str, Any]) -> dict[str, Any]:
            return {
                "results": [{"title": "result", "url": "https://example.test"}],
                "backend": parameters["backend"],
                "answer": None,
            }

    search_adapter = adapters.HelloAgentsSearchAdapter(tool_factory=SearchTool)
    result, _notices, _answer, backend = dispatch_search(
        "private search query",
        Configuration(enable_notes=False, search_api=SearchAPI.TAVILY),
        0,
        use_cache=False,
        operation_scope=scope,
        search_adapter=search_adapter,
    )

    assert result and result["results"]
    assert backend == "tavily"
    assert created_after_started == [True]
    audit = [
        event
        for event in session.events
        if event.kind
        in {EventKind.OPERATION_STARTED, EventKind.OPERATION_COMPLETED}
    ]
    assert [event.kind for event in audit] == [
        EventKind.OPERATION_STARTED,
        EventKind.OPERATION_COMPLETED,
    ]
    assert audit[0].payload["operation_name"] == "search.execute"
    assert audit[0].payload["fallback_index"] == 1
    assert audit[0].payload["operation_attempt"] == 1
    serialized = str([event.as_dict() for event in audit])
    assert "private search query" not in serialized
    assert "query_hash" in serialized


def test_duckduckgo_compatibility_path_uses_auto_router() -> None:
    """The DuckDuckGo fallback avoids ddgs' empty explicit backend route."""
    adapters = _load_adapters()
    calls: list[dict[str, Any]] = []

    class FakeDDGS:
        def __init__(self, *, timeout: int) -> None:
            assert timeout == 10

        def __enter__(self) -> "FakeDDGS":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def text(self, query: str, **kwargs: Any) -> list[dict[str, str]]:
            calls.append({"query": query, **kwargs})
            return [
                {
                    "title": "Result",
                    "href": "https://example.test/result",
                    "body": "Evidence",
                }
            ]

    adapter = adapters.HelloAgentsSearchAdapter(duckduckgo_factory=FakeDDGS)
    result = adapter.run(
        {
            "input": "compatibility query",
            "backend": "duckduckgo",
            "max_results": 3,
        }
    )

    assert result["backend"] == "duckduckgo"
    assert result["results"][0]["url"] == "https://example.test/result"
    assert calls == [
        {"query": "compatibility query", "max_results": 3, "backend": "auto"}
    ]


def test_search_tool_construction_never_replaces_or_swallows_process_stdout(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """A worker constructing SearchTool must not capture another worker's output."""
    adapters = _load_adapters()
    constructor_entered = Event()
    concurrent_printed = Event()
    stdout_before = sys.stdout
    sentinel = "CONCURRENT_LLM_STDOUT_SENTINEL"

    class BlockingSearchTool:
        def __init__(self, *, backend: str) -> None:
            assert backend == "hybrid"
            constructor_entered.set()
            assert concurrent_printed.wait(timeout=2)

        def run(self, parameters: dict[str, Any]) -> dict[str, Any]:
            del parameters
            return {"results": []}

    adapter = adapters.HelloAgentsSearchAdapter(tool_factory=BlockingSearchTool)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(adapter.run, {})
        assert constructor_entered.wait(timeout=2)
        stdout_during_construction = sys.stdout
        sys.stdout.write(f"{sentinel}\n")
        sys.stdout.flush()
        concurrent_printed.set()
        future.result(timeout=2)

    captured = capfd.readouterr()
    assert stdout_during_construction is stdout_before
    assert sentinel in captured.out


def test_perplexity_rejection_prevents_cache_tool_and_fallback(
    tmp_path: Path,
) -> None:
    """Premium denial is control flow and cannot downgrade to DuckDuckGo."""
    adapters = _load_adapters()
    from research.operations import (
        GovernedOperations,
        OperationRejectedError,
        OperationScope,
    )
    from services.search import dispatch_search

    command = ResearchCommand(
        topic="premium search",
        config=Configuration(
            enable_notes=False,
            notes_workspace=str(tmp_path / "notes"),
            search_api=SearchAPI.PERPLEXITY,
        ),
    )
    session = RunSession(
        command=command,
        state=ResearchState(research_topic=command.topic),
    )
    session.start()
    scope = OperationScope(
        operations=GovernedOperations(
            session,
            OutcomePolicy({"search:premium": "ask"}),
        ),
        task_id=1,
        task_attempt=1,
        fallback_index=1,
    )

    def forbidden_tool_factory(*args: Any, **kwargs: Any) -> object:
        raise AssertionError(f"SearchTool constructed: {args!r} {kwargs!r}")

    with pytest.raises(OperationRejectedError):
        dispatch_search(
            "private premium query",
            command.config,
            0,
            use_cache=True,
            operation_scope=scope,
            search_adapter=adapters.HelloAgentsSearchAdapter(
                tool_factory=forbidden_tool_factory
            ),
        )

    assert not (tmp_path / "cache").exists()
    rejected = [
        event for event in session.events if event.kind is EventKind.OPERATION_REJECTED
    ]
    assert len(rejected) == 1
    assert rejected[0].payload["operation_name"] == "search.cache_read"


def test_search_retries_reuse_operation_id_with_one_based_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each physical retry is governed without losing logical correlation."""
    from research.operations import GovernedOperations, OperationScope
    from services import search as search_service

    session = _started_session()
    scope = OperationScope(
        operations=GovernedOperations(session, AllowingPolicy()),
        task_id=3,
        task_attempt=2,
        fallback_index=1,
    )

    class RetryingSearch:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, parameters: dict[str, Any]) -> dict[str, Any]:
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError("transient provider detail")
            return {
                "results": [{"title": "ok", "url": "https://example.test"}],
                "backend": parameters["backend"],
                "answer": None,
            }

    adapter = RetryingSearch()
    monkeypatch.setattr(search_service.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(search_service.random, "uniform", lambda _a, _b: 0.0)

    result, *_rest = search_service.dispatch_search(
        "retry query",
        Configuration(
            enable_notes=False,
            search_api=SearchAPI.DUCKDUCKGO,
        ),
        0,
        use_cache=False,
        operation_scope=scope,
        search_adapter=adapter,
    )

    assert result and result["results"]
    assert adapter.calls == 3
    audit = [
        event
        for event in session.events
        if event.kind
        in {
            EventKind.OPERATION_STARTED,
            EventKind.OPERATION_COMPLETED,
            EventKind.OPERATION_FAILED,
        }
    ]
    assert len({event.operation_id for event in audit}) == 1
    assert [event.payload["operation_attempt"] for event in audit] == [
        1,
        1,
        2,
        2,
        3,
        3,
    ]
    assert [event.kind for event in audit] == [
        EventKind.OPERATION_STARTED,
        EventKind.OPERATION_FAILED,
        EventKind.OPERATION_STARTED,
        EventKind.OPERATION_FAILED,
        EventKind.OPERATION_STARTED,
        EventKind.OPERATION_COMPLETED,
    ]
    serialized = str([event.as_dict() for event in audit])
    assert "retry query" not in serialized
    assert "transient provider detail" not in serialized


def test_search_cache_write_is_governed_and_hit_skips_tool(tmp_path: Path) -> None:
    """Cache persistence is audited and a governed hit never constructs a tool."""
    from research.operations import GovernedOperations, OperationScope
    from services.search import dispatch_search

    config = Configuration(
        enable_notes=False,
        notes_workspace=str(tmp_path / "notes"),
        search_api=SearchAPI.DUCKDUCKGO,
    )

    class SuccessfulSearch:
        def run(self, parameters: dict[str, Any]) -> dict[str, Any]:
            return {
                "results": [{"title": "cached", "url": "https://example.test"}],
                "backend": parameters["backend"],
                "answer": None,
            }

    first_session = _started_session()
    first_scope = OperationScope(
        operations=GovernedOperations(first_session, AllowingPolicy()),
        task_id=1,
        task_attempt=1,
        fallback_index=1,
    )
    dispatch_search(
        "cache query",
        config,
        0,
        use_cache=True,
        operation_scope=first_scope,
        search_adapter=SuccessfulSearch(),
    )
    first_starts = [
        event.payload["operation_name"]
        for event in first_session.events
        if event.kind is EventKind.OPERATION_STARTED
    ]
    assert first_starts == [
        "search.cache_read",
        "search.execute",
        "search.cache_write",
    ]

    class ForbiddenSearch:
        def run(self, parameters: dict[str, Any]) -> dict[str, Any]:
            raise AssertionError(f"cache hit invoked search: {parameters!r}")

    second_session = _started_session()
    second_scope = OperationScope(
        operations=GovernedOperations(second_session, AllowingPolicy()),
        task_id=1,
        task_attempt=1,
        fallback_index=1,
    )
    cached, *_rest = dispatch_search(
        "cache query",
        config,
        0,
        use_cache=True,
        operation_scope=second_scope,
        search_adapter=ForbiddenSearch(),
    )

    assert cached and cached["results"][0]["title"] == "cached"
    second_starts = [
        event.payload["operation_name"]
        for event in second_session.events
        if event.kind is EventKind.OPERATION_STARTED
    ]
    assert second_starts == ["search.cache_read"]


def test_search_cache_hit_log_uses_query_hash(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cache-hit diagnostics must identify the lookup without query text."""
    from services import search

    config = Configuration(
        enable_notes=False,
        notes_workspace=str(tmp_path / "notes"),
        search_api=SearchAPI.DUCKDUCKGO,
    )
    query_sentinel = "CACHE_HIT_PRIVATE_QUERY_SENTINEL"
    cache_file = (
        search._cache_dir(config)
        / f"{search._cache_key(query_sentinel, config)}.json"
    )
    cache_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "results": [],
                "backend": "none",
                "notices": [],
                "notice_codes": [],
            }
        ),
        encoding="utf-8",
    )
    expected_hash = search.hashlib.sha256(query_sentinel.encode("utf-8")).hexdigest()
    caplog.set_level(logging.INFO, logger="services.search")

    cached = search._load_from_cache(query_sentinel, config)
    assert cached == {
        "schema_version": 1,
        "results": [],
        "backend": "none",
        "notices": [],
        "notice_codes": [],
    }
    assert expected_hash in caplog.text
    assert query_sentinel not in caplog.text


def test_search_cache_read_error_log_does_not_leak_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cache read warnings must not persist exception paths or messages."""
    from services import search

    config = Configuration(
        enable_notes=False,
        notes_workspace=str(tmp_path / "notes"),
        search_api=SearchAPI.DUCKDUCKGO,
    )
    cache_file = (
        search._cache_dir(config)
        / f"{search._cache_key('redacted read query', config)}.json"
    )
    cache_file.write_text("{}", encoding="utf-8")
    sentinel = "CACHE_READ_SECRET_WORKSPACE_PATH"

    def fail_open(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise OSError(sentinel)

    monkeypatch.setattr("builtins.open", fail_open)
    caplog.set_level(logging.WARNING, logger="services.search")

    assert search._load_from_cache("redacted read query", config) is None
    assert "Search cache read failed" in caplog.text
    assert sentinel not in caplog.text


def test_search_cache_write_error_log_does_not_leak_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cache write warnings must not persist exception paths or messages."""
    from services import search

    config = Configuration(
        enable_notes=False,
        notes_workspace=str(tmp_path / "notes"),
        search_api=SearchAPI.DUCKDUCKGO,
    )
    sentinel = "CACHE_WRITE_SECRET_WORKSPACE_PATH"

    def fail_named_temporary_file(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise OSError(sentinel)

    monkeypatch.setattr(
        search.tempfile,
        "NamedTemporaryFile",
        fail_named_temporary_file,
    )
    caplog.set_level(logging.WARNING, logger="services.search")

    search._save_to_cache("redacted write query", config, {"results": []})
    assert "Search cache write failed" in caplog.text
    assert sentinel not in caplog.text


def test_github_denial_prevents_client_construction() -> None:
    """Aggregate GitHub authorization must precede Session/client construction."""
    adapters = _load_adapters()
    from research.operations import (
        GovernedOperations,
        OperationRejectedError,
        OperationScope,
    )
    from services.github_research import GitHubRepositoryTarget

    session = _started_session()
    scope = OperationScope(
        operations=GovernedOperations(
            session,
            OutcomePolicy({"github:read": "deny"}),
        ),
        task_attempt=1,
        fallback_index=1,
    )

    def forbidden_factory(*args: Any, **kwargs: Any) -> object:
        raise AssertionError(f"GitHub client constructed: {args!r} {kwargs!r}")

    github = adapters.GovernedGitHubAdapter(client_factory=forbidden_factory)
    with pytest.raises(OperationRejectedError):
        github.collect_repository_context(
            GitHubRepositoryTarget(owner="owner", repo="repo"),
            operation_scope=scope,
            token="must-not-be-recorded",
            base_url="https://api.github.test",
        )

    rejected = [
        event for event in session.events if event.kind is EventKind.OPERATION_REJECTED
    ]
    assert len(rejected) == 1
    serialized = str(rejected[0].as_dict())
    assert "must-not-be-recorded" not in serialized
    assert "api.github.test" not in serialized


def test_github_client_is_constructed_after_started_event() -> None:
    """An allowed aggregate operation wraps construction and all client I/O."""
    adapters = _load_adapters()
    from research.operations import GovernedOperations, OperationScope
    from services.github_research import (
        GitHubRepositoryContext,
        GitHubRepositoryTarget,
    )

    session = _started_session()
    scope = OperationScope(
        operations=GovernedOperations(session, AllowingPolicy()),
        task_attempt=1,
        fallback_index=1,
    )
    target = GitHubRepositoryTarget(owner="owner", repo="repo")
    construction_checks: list[bool] = []

    class Client:
        def collect_repository_context(
            self,
            received: GitHubRepositoryTarget,
        ) -> GitHubRepositoryContext:
            assert received == target
            return GitHubRepositoryContext(
                target=received,
                repository={"name": received.full_name},
            )

    def client_factory(*, token: str | None, base_url: str) -> Client:
        assert token == "token"
        assert base_url == "https://api.github.test"
        construction_checks.append(
            any(event.kind is EventKind.OPERATION_STARTED for event in session.events)
        )
        return Client()

    context = adapters.GovernedGitHubAdapter(
        client_factory=client_factory
    ).collect_repository_context(
        target,
        operation_scope=scope,
        token="token",
        base_url="https://api.github.test",
    )

    assert context.repository["name"] == "owner/repo"
    assert construction_checks == [True]
    audit = [
        event
        for event in session.events
        if event.kind
        in {EventKind.OPERATION_STARTED, EventKind.OPERATION_COMPLETED}
    ]
    assert [event.kind for event in audit] == [
        EventKind.OPERATION_STARTED,
        EventKind.OPERATION_COMPLETED,
    ]
    assert audit[0].payload["operation_name"] == "github.collect"
    assert dict(audit[0].payload["resource"]) == {
        "owner": "owner",
        "repo": "repo",
        "resource_kind": "repository_context",
    }


def test_default_github_adapter_passes_public_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production client checks cancellation between aggregate GETs."""
    adapters = _load_adapters()
    from research.operations import GovernedOperations, OperationScope
    from services import github_research

    session = _started_session()
    scope = OperationScope(
        operations=GovernedOperations(session, AllowingPolicy()),
        task_attempt=1,
        fallback_index=1,
    )
    target = github_research.GitHubRepositoryTarget(owner="owner", repo="repo")
    checkpoints: list[Any] = []

    class Client:
        def collect_repository_context(self, received: Any) -> Any:
            return github_research.GitHubRepositoryContext(target=received)

    def factory(
        *,
        token: str | None,
        base_url: str,
        checkpoint: Any,
    ) -> Client:
        del token, base_url
        checkpoints.append(checkpoint)
        return Client()

    monkeypatch.setattr(github_research, "GitHubResearchClient", factory)

    adapters.GovernedGitHubAdapter().collect_repository_context(
        target,
        operation_scope=scope,
    )

    assert len(checkpoints) == 1
    checkpoints[0]()


def test_note_adapter_constructor_is_filesystem_lazy(tmp_path: Path) -> None:
    """Creating the lightweight adapter cannot construct NoteTool or workspace."""
    from services.note_agent import NoteToolAdapter

    constructed = 0

    def forbidden_factory(*args: Any, **kwargs: Any) -> object:
        nonlocal constructed
        constructed += 1
        raise AssertionError(f"NoteTool constructed: {args!r} {kwargs!r}")

    NoteToolAdapter(
        workspace=tmp_path / "notes",
        tool_factory=forbidden_factory,
    )

    assert constructed == 0
    assert not (tmp_path / "notes").exists()


def test_note_path_is_workspace_relative_and_rejects_unsafe_ids(
    tmp_path: Path,
) -> None:
    """Never project an absolute host path or traversal through note metadata."""
    from services.note_agent import NoteToolAdapter

    workspace = (tmp_path / "private" / "notes").resolve()
    adapter = NoteToolAdapter(workspace=workspace, tool_factory=object)

    safe_path = adapter.note_path("note_20260719_0")
    assert safe_path == "note_20260719_0.md"
    assert str(workspace) not in safe_path
    assert adapter.note_path("../outside") is None
    assert adapter.note_path(r"..\outside") is None


def test_note_write_denial_prevents_tool_construction_and_redacts_content(
    tmp_path: Path,
) -> None:
    """A denied note write has no constructor or filesystem side effect."""
    from research.operations import (
        GovernedOperations,
        OperationRejectedError,
        OperationScope,
    )
    from services.note_agent import NoteToolAdapter

    session = _started_session()
    scope = OperationScope(
        operations=GovernedOperations(
            session,
            OutcomePolicy({"notes:write": "deny"}),
        ),
        task_id=1,
        task_attempt=1,
        fallback_index=1,
    )

    def forbidden_factory(*args: Any, **kwargs: Any) -> object:
        raise AssertionError(f"NoteTool constructed: {args!r} {kwargs!r}")

    adapter = NoteToolAdapter(
        workspace=tmp_path / "notes",
        tool_factory=forbidden_factory,
    )
    with pytest.raises(OperationRejectedError):
        adapter.create_task_note(
            task_id=1,
            title="private title",
            content="private content",
            operation_scope=scope,
        )

    assert not (tmp_path / "notes").exists()
    rejected = [
        event for event in session.events if event.kind is EventKind.OPERATION_REJECTED
    ]
    assert len(rejected) == 1
    serialized = str(rejected[0].as_dict())
    assert "private title" not in serialized
    assert "private content" not in serialized
    assert str(tmp_path) not in serialized


def test_note_tool_constructs_after_started_and_is_shared_per_workspace(
    tmp_path: Path,
) -> None:
    """One workspace-locked tool is lazily shared by compatible adapters."""
    from research.operations import GovernedOperations, OperationScope
    from services.note_agent import NoteSubAgent, NoteToolAdapter

    session = _started_session()
    scope = OperationScope(
        operations=GovernedOperations(session, AllowingPolicy()),
        task_id=1,
        task_attempt=1,
        fallback_index=1,
    )
    constructed_after_started: list[bool] = []
    payloads: list[dict[str, Any]] = []

    class Tool:
        def __init__(self, *, workspace: str) -> None:
            assert workspace == str(tmp_path / "notes")
            constructed_after_started.append(
                any(event.kind is EventKind.OPERATION_STARTED for event in session.events)
            )

        def run(self, payload: dict[str, Any]) -> str:
            payloads.append(dict(payload))
            return "Created note. ID: note-1"

    first = NoteToolAdapter(workspace=tmp_path / "notes", tool_factory=Tool)
    second = NoteToolAdapter(workspace=tmp_path / "notes", tool_factory=Tool)

    assert first.create_task_note(
        task_id=1,
        title="first",
        content="one",
        operation_scope=scope,
    ) == "note-1"
    assert second.create_conclusion_note(
        title="report",
        content="two",
        operation_scope=scope,
    ) == "note-1"

    assert NoteSubAgent is NoteToolAdapter
    assert constructed_after_started == [True]
    assert [payload["note_type"] for payload in payloads] == [
        "task_state",
        "conclusion",
    ]
    started = [
        event
        for event in session.events
        if event.kind is EventKind.OPERATION_STARTED
    ]
    assert [event.payload["operation_name"] for event in started] == [
        "notes.create",
        "notes.create",
    ]
    serialized = str([event.as_dict() for event in started])
    assert "first" not in serialized
    assert "report" not in serialized
    assert "one" not in serialized
    assert "two" not in serialized


def test_note_adapters_serialize_concurrent_runs_for_same_workspace(
    tmp_path: Path,
) -> None:
    """The index-bearing shared tool may never run concurrently per workspace."""
    from research.operations import GovernedOperations, OperationScope
    from services.note_agent import NoteToolAdapter

    session = _started_session()
    operations = GovernedOperations(session, AllowingPolicy())
    first_scope = OperationScope(
        operations=operations,
        task_id=1,
        task_attempt=1,
        fallback_index=1,
    )
    second_scope = OperationScope(
        operations=operations,
        task_id=2,
        task_attempt=1,
        fallback_index=1,
    )
    first_entered = Event()
    second_entered = Event()
    release = Event()
    counter_lock = Lock()
    active = 0
    calls = 0

    class Tool:
        def __init__(self, *, workspace: str) -> None:
            assert workspace == str(tmp_path / "notes")

        def run(self, payload: dict[str, Any]) -> str:
            nonlocal active, calls
            if payload.get("title") == "warmup":
                return "Created note. ID: warmup"
            with counter_lock:
                active += 1
                calls += 1
                call_number = calls
            if call_number == 1:
                first_entered.set()
            else:
                second_entered.set()
            release.wait(timeout=2)
            with counter_lock:
                active -= 1
            return f"Created note. ID: note-{call_number}"

    first = NoteToolAdapter(workspace=tmp_path / "notes", tool_factory=Tool)
    second = NoteToolAdapter(workspace=tmp_path / "notes", tool_factory=Tool)
    first.create_conclusion_note(
        title="warmup",
        content="warmup",
        operation_scope=first_scope,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(
            first.create_task_note,
            task_id=1,
            title="first",
            content="one",
            operation_scope=first_scope,
        )
        assert first_entered.wait(timeout=1)
        second_future = executor.submit(
            second.create_task_note,
            task_id=2,
            title="second",
            content="two",
            operation_scope=second_scope,
        )
        assert not second_entered.wait(timeout=0.1)
        release.set()
        assert first_future.result(timeout=2) == "note-1"
        assert second_future.result(timeout=2) == "note-2"

    assert active == 0


def test_note_batch_read_never_swallows_operation_rejection(tmp_path: Path) -> None:
    """Optional batch handling cannot turn policy denial into a soft note error."""
    from research.operations import (
        GovernedOperations,
        OperationRejectedError,
        OperationScope,
    )
    from services.note_agent import NoteToolAdapter

    class Tool:
        def __init__(self, *, workspace: str) -> None:
            del workspace

        def run(self, payload: dict[str, Any]) -> str:
            if payload["action"] == "create":
                return "Created note. ID: note-1"
            return "note content"

    adapter = NoteToolAdapter(workspace=tmp_path / "notes", tool_factory=Tool)
    allowing_scope = _operation_scope(task_id=1)
    adapter.create_task_note(
        task_id=1,
        title="title",
        operation_scope=allowing_scope,
    )

    denied_session = _started_session()
    denied_scope = OperationScope(
        operations=GovernedOperations(
            denied_session,
            OutcomePolicy({"notes:read": "deny"}),
        ),
        task_id=1,
        task_attempt=1,
        fallback_index=1,
    )

    with pytest.raises(OperationRejectedError):
        adapter.read_all_task_notes(
            ["note-1"],
            operation_scope=denied_scope,
        )


def test_real_hello_agents_simple_agent_scope_contract_in_subprocess() -> None:
    """Real 0.2.9 SimpleAgent forwards scope only as far as our wrapper."""
    backend_dir = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(backend_dir / "src")
    environment["PYTHONIOENCODING"] = "utf-8"
    script = r'''
import json
from importlib.metadata import version

from hello_agents import SimpleAgent
from config import Configuration
from models import ResearchState
from research.adapters import GovernedHelloAgentsLLM
from research.contracts import ResearchCommand
from research.operations import GovernedOperations, OperationScope
from research.session import RunSession


class Policy:
    def evaluate_capability(self, capability, command):
        del command
        return {
            "capability": capability,
            "outcome": "allow",
            "reason": "Allowed by real framework contract.",
        }


class Delegate:
    model = "contract-model"

    def __init__(self):
        self.kwargs = []

    def invoke(self, messages, **kwargs):
        self.kwargs.append(dict(kwargs))
        return "complete"

    def stream_invoke(self, messages, **kwargs):
        self.kwargs.append(dict(kwargs))
        yield "stream"


command = ResearchCommand(
    topic="framework contract",
    config=Configuration(enable_notes=False),
)
session = RunSession(
    command=command,
    state=ResearchState(research_topic=command.topic),
)
session.start()
scope = OperationScope(
    operations=GovernedOperations(session, Policy()),
    task_attempt=1,
    fallback_index=1,
)
delegate = Delegate()
agent = SimpleAgent(
    name="contract",
    llm=GovernedHelloAgentsLLM(
        delegate,
        role="planner",
        model_id="contract-model",
    ),
    system_prompt="system",
    enable_tool_calling=False,
    tool_registry=None,
)
complete = agent.run("prompt", _research_operation_scope=scope, temperature=0.2)
agent.clear_history()
stream = list(
    agent.stream_run(
        "stream prompt",
        _research_operation_scope=scope,
        temperature=0.3,
    )
)
assert all("_research_operation_scope" not in item for item in delegate.kwargs)
print(json.dumps({
    "version": version("hello-agents"),
    "complete": complete,
    "stream": stream,
    "kwargs": delegate.kwargs,
}))
'''
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=backend_dir,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout.strip())
    assert payload == {
        "version": "0.2.9",
        "complete": "complete",
        "stream": ["stream"],
        "kwargs": [{"temperature": 0.2}, {"temperature": 0.3}],
    }
