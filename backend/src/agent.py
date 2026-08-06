"""Orchestrator coordinating the deep research workflow."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from inspect import Parameter, signature
from queue import Empty, Full, Queue
from threading import Event, Lock
from typing import Any, cast

from hello_agents import HelloAgentsLLM, SimpleAgent

from config import Configuration
from harness.policy import HarnessPolicy
from models import SummaryState, SummaryStateOutput, TodoItem
from prompts import (
    report_writer_instructions,
    task_summarizer_instructions,
    todo_planner_system_prompt,
)
from research.adapters import (
    GovernedGitHubAdapter,
    GovernedHelloAgentsLLM,
    HelloAgentsSearchAdapter,
)
from research.context import FollowupContext, ResearchContextAssembler
from research.contracts import EventKind, ResearchCommand, ResearchEvent, RunStatus
from research.legacy_sse import project_legacy_event as _project_legacy_event
from research.operations import (
    GovernedOperations,
    OperationRejectedError,
    OperationScope,
)
from research.session import (
    CancellationRequestedError,
    CancellationToken,
    DeadlineExceededError,
    InvalidTransitionError,
    RunSession,
)
from services.github_research import (
    GitHubRepositoryContext,
    GitHubRepositoryTarget,
    parse_github_repository,
)
from services.note_agent import NoteSubAgent
from services.planner import PlanningService
from services.reporter import ReportingService
from services.search import (
    SEARCH_NOTICE_CODE,
    SEARCH_NOTICE_MESSAGE,
    SEARCH_NOTICE_MESSAGES,
    dispatch_search,
    prepare_research_context,
)
from services.summarizer import SummarizationService, TaskSummaryInput

logger = logging.getLogger(__name__)

_DEFAULT_DEPENDENCY = object()
_QUEUE_POLL_SECONDS = 0.05
_GITHUB_NOTICE_CODE = "github_api_notice"
_GITHUB_NOTICE_MESSAGE = "GitHub API returned a notice."
_GITHUB_CONTEXT_FAILED_CODE = "github_api_context_failed"
_GITHUB_CONTEXT_FAILED_MESSAGE = "GitHub API context collection failed."
_GITHUB_NOTICE_MESSAGES = {
    _GITHUB_NOTICE_CODE: _GITHUB_NOTICE_MESSAGE,
    _GITHUB_CONTEXT_FAILED_CODE: _GITHUB_CONTEXT_FAILED_MESSAGE,
}
_OPERATION_CONTROL_ERRORS = (
    OperationRejectedError,
    DeadlineExceededError,
    CancellationRequestedError,
)


def _call_with_operation_scope(
    callback: Callable[..., Any],
    *args: object,
    operation_scope: OperationScope,
    **kwargs: object,
) -> Any:
    """Pass scope to governed APIs while retaining legacy injected fakes."""
    parameters: tuple[Parameter, ...]
    try:
        parameters = tuple(signature(callback).parameters.values())
    except (TypeError, ValueError):
        parameters = ()
    accepts_scope = any(
        parameter.name == "operation_scope"
        or parameter.kind is Parameter.VAR_KEYWORD
        for parameter in parameters
    )
    if accepts_scope:
        kwargs["operation_scope"] = operation_scope
    return callback(*args, **kwargs)


def _safe_github_notice_fields(
    notices: Sequence[object],
    notice_codes: Sequence[object] = (),
) -> tuple[list[str], list[str]]:
    """Map GitHub-controlled notice content onto a small trusted vocabulary."""
    codes = [
        code
        for code in notice_codes
        if isinstance(code, str) and code in _GITHUB_NOTICE_MESSAGES
    ]
    if not codes and notices:
        codes = [_GITHUB_NOTICE_CODE]
    codes = list(dict.fromkeys(codes))
    return [_GITHUB_NOTICE_MESSAGES[code] for code in codes], codes


def _safe_search_notice_fields(
    notices: Sequence[object],
    notice_codes: Sequence[object] = (),
) -> tuple[list[str], list[str]]:
    """Map search-controlled notice content onto trusted codes and messages."""
    codes = [
        code
        for code in notice_codes
        if isinstance(code, str) and code in SEARCH_NOTICE_MESSAGES
    ]
    if not codes and notices:
        codes = [SEARCH_NOTICE_CODE]
    codes = list(dict.fromkeys(codes))
    messages = [SEARCH_NOTICE_MESSAGES.get(code, SEARCH_NOTICE_MESSAGE) for code in codes]
    return messages, codes


class _WorkerMessageKind(str, Enum):
    """Result facts sent from detached workers to the coordinator."""

    SOURCES = "sources"
    SUMMARY_DELTA = "summary_delta"
    RETRY = "retry"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True, kw_only=True)
class _TaskWorkItem:
    """Immutable task input safe to hand to a worker thread."""

    task_id: int
    topic: str
    title: str
    intent: str
    original_query: str
    note_id: str | None
    note_path: str | None
    note_content: str
    source_strategy: str | None
    repository: str | None
    github_markdown: str
    loop_offset: int
    config: Configuration
    operations: GovernedOperations
    search_adapter: Any | None


@dataclass(frozen=True, slots=True, kw_only=True)
class _WorkerMessage:
    """One typed worker result consumed only by the coordinator thread."""

    kind: _WorkerMessageKind
    task_id: int
    payload: dict[str, Any]


class _LegacyCollectingObserver:
    """Bounded ordered collector for the direct legacy stream adapter."""

    def __init__(self, *, capacity: int, cancellation: CancellationToken) -> None:
        self._events: Queue[ResearchEvent] = Queue(maxsize=capacity)
        self._cancellation = cancellation

    @property
    def capacity(self) -> int:
        """Return the configured hard queue bound."""
        return self._events.maxsize

    def __call__(self, event: ResearchEvent) -> None:
        """Apply bounded backpressure without blocking forever on cancellation."""
        while not self._cancellation.is_cancelled:
            try:
                self._events.put(event, timeout=_QUEUE_POLL_SECONDS)
                return
            except Full:
                continue

    def iter_legacy_events_until(
        self,
        future: Future[None],
    ) -> Iterator[dict[str, Any]]:
        """Drain ordered typed events, then propagate the worker outcome."""
        while not future.done() or not self._events.empty():
            try:
                event = self._events.get(timeout=_QUEUE_POLL_SECONDS)
            except Empty:
                continue
            projected = _project_legacy_event(event)
            if projected is not None:
                yield projected
        future.result()


class DeepResearchAgent:
    """Coordinator orchestrating TODO-based research workflow using HelloAgents."""

    def __init__(
        self,
        config: Configuration | None = None,
        *,
        planner: Any | None = None,
        search_adapter: Callable[..., tuple[dict[str, Any] | None, list[str], str | None, str]] | None = None,
        context_preparer: Callable[
            [dict[str, Any] | None, str | None, Configuration],
            tuple[str, str],
        ]
        | None = None,
        summarizer: Any | None = None,
        reporting: Any | None = None,
        note_agent: Any = _DEFAULT_DEPENDENCY,
        github_adapter: Any = _DEFAULT_DEPENDENCY,
        operation_authorizer: Any | None = None,
        legacy_event_queue_capacity: int = 64,
    ) -> None:
        """Initialise production defaults or accept a fully injected coordinator."""
        self.config = config or Configuration.from_env()
        if legacy_event_queue_capacity < 1:
            raise ValueError("Legacy event queue capacity must be positive.")
        self._legacy_event_queue_capacity = legacy_event_queue_capacity
        self._last_session: RunSession | None = None
        self._last_session_lock = Lock()
        self._search_adapter = search_adapter or dispatch_search
        self._uses_default_search_adapter = search_adapter is None
        self._context_preparer = context_preparer or prepare_research_context
        self._operation_authorizer = (
            operation_authorizer
            if operation_authorizer is not None
            else HarnessPolicy()
        )

        fully_injected = all(
            dependency is not None
            for dependency in (planner, summarizer, reporting)
        )
        if fully_injected:
            self.llm: HelloAgentsLLM | None = None
            self._reporter_llm: HelloAgentsLLM | None = None
            self.todo_agent: SimpleAgent | None = None
            self.report_agent: SimpleAgent | None = None
            self._summarizer_factory: Callable[[], SimpleAgent] | None = None
        else:
            self.llm = self._init_llm()
            if self.config.llm_reporter_model_id:
                self._reporter_llm = self._init_llm(
                    model_id=self.config.llm_reporter_model_id,
                    max_tokens=3000,
                )
            else:
                self._reporter_llm = self.llm

            self.todo_agent = self._create_role_agent(
                name="研究规划专家",
                system_prompt=todo_planner_system_prompt.strip(),
                role="planner",
            )
            self.report_agent = self._create_role_agent(
                name="报告撰写专家",
                system_prompt=report_writer_instructions.strip(),
                llm=self._reporter_llm,
                role="reporter",
            )
            self._summarizer_factory = lambda: self._create_role_agent(
                name="任务总结专家",
                system_prompt=task_summarizer_instructions.strip(),
                role="summarizer",
            )

        if planner is not None:
            self.planner = planner
        else:
            todo_agent = self.todo_agent
            if todo_agent is None:
                raise RuntimeError("Planner agent was not initialized.")
            self.planner = PlanningService(todo_agent, self.config)
        if summarizer is not None:
            self.summarizer = summarizer
        else:
            summarizer_factory = self._summarizer_factory
            if summarizer_factory is None:
                raise RuntimeError("Summarizer factory was not initialized.")
            self.summarizer = SummarizationService(
                summarizer_factory,
                self.config,
            )
        if reporting is not None:
            self.reporting = reporting
        else:
            report_agent = self.report_agent
            if report_agent is None:
                raise RuntimeError("Reporting agent was not initialized.")
            self.reporting = ReportingService(
                report_agent,
                self.config,
            )

        if note_agent is _DEFAULT_DEPENDENCY:
            self.note_agent = (
                NoteSubAgent(workspace=self.config.notes_workspace)
                if self.config.enable_notes
                else None
            )
        else:
            self.note_agent = note_agent

        self._github_adapter = github_adapter

    @property
    def last_session(self) -> RunSession | None:
        """Return the most recently completed direct legacy session."""
        with self._last_session_lock:
            return self._last_session

    def _set_last_session(self, session: RunSession) -> None:
        with self._last_session_lock:
            self._last_session = session

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def _init_llm(
        self,
        *,
        model_id: str | None = None,
        max_tokens: int | None = None,
    ) -> HelloAgentsLLM:
        """Instantiate HelloAgentsLLM following configuration preferences.

        Args:
            model_id: Override the default model (e.g. for Reporter).
            max_tokens: Override the default max_tokens limit.
        """
        llm_kwargs: dict[str, Any] = {
            "temperature": 0.0,
            "timeout": self.config.llm_timeout,
            "max_tokens": max_tokens or self.config.llm_max_tokens,
        }

        resolved_model = model_id or self.config.llm_model_id or self.config.local_llm
        if resolved_model:
            llm_kwargs["model"] = resolved_model

        provider = (self.config.llm_provider or "").strip()
        if provider:
            llm_kwargs["provider"] = provider

        if provider == "ollama":
            llm_kwargs["base_url"] = self.config.sanitized_ollama_url()
            if self.config.llm_api_key:
                llm_kwargs["api_key"] = self.config.llm_api_key
            else:
                llm_kwargs["api_key"] = "ollama"
        elif provider == "lmstudio":
            llm_kwargs["base_url"] = self.config.lmstudio_base_url
            if self.config.llm_api_key:
                llm_kwargs["api_key"] = self.config.llm_api_key
        else:
            if self.config.llm_base_url:
                llm_kwargs["base_url"] = self.config.llm_base_url
            if self.config.llm_api_key:
                llm_kwargs["api_key"] = self.config.llm_api_key

        return HelloAgentsLLM(**llm_kwargs)

    def _create_role_agent(
        self,
        *,
        name: str,
        system_prompt: str,
        llm: HelloAgentsLLM | None = None,
        role: str = "llm",
    ) -> SimpleAgent:
        """Instantiate a role-specific agent that never interprets tool markers.

        Note operations are handled by NoteSubAgent separately — agents
        produce clean text output only, never ``[TOOL_CALL:...]`` markers.
        """
        delegate = llm or self.llm
        if delegate is None:
            raise RuntimeError("A role agent requires an LLM delegate.")
        model_id = (
            self.config.llm_reporter_model_id
            if role == "reporter" and self.config.llm_reporter_model_id
            else self.config.resolved_model()
        )
        governed_llm = GovernedHelloAgentsLLM(
            delegate,
            role=role,
            model_id=model_id,
        )
        return SimpleAgent(
            name=name,
            llm=cast(HelloAgentsLLM, governed_llm),
            system_prompt=system_prompt,
            enable_tool_calling=False,
            tool_registry=None,
        )

    def run(
        self,
        topic: str,
        prior_context: dict[str, Any] | None = None,
    ) -> SummaryStateOutput:
        """Project the canonical coordinator into the direct legacy response."""
        session = self._new_legacy_session(topic)
        self.execute(session, FollowupContext.from_legacy(prior_context))
        self._set_last_session(session)
        return session.to_legacy_output()

    def run_stream(
        self,
        topic: str,
        prior_context: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Project one canonical execution into a bounded legacy event stream."""
        cancellation = CancellationToken()
        observer = _LegacyCollectingObserver(
            capacity=self._legacy_event_queue_capacity,
            cancellation=cancellation,
        )
        session = self._new_legacy_session(
            topic,
            observer=observer,
            cancellation=cancellation,
        )
        executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="legacy-research",
        )
        future = executor.submit(
            self.execute,
            session,
            FollowupContext.from_legacy(prior_context),
        )
        completed = False
        try:
            yield from observer.iter_legacy_events_until(future)
            self._set_last_session(session)
            completed = True
        finally:
            if not completed:
                session.request_cancellation()
            executor.shutdown(wait=completed, cancel_futures=True)

        # The direct adapter has no application/persistence boundary, so this is
        # a non-canonical compatibility completion sentinel. It is projected
        # from an immutable typed event but never added to ``session.events``.
        completion = _project_legacy_event(
            ResearchEvent(
                kind=EventKind.RUN_COMPLETED,
                run_id=session.run_id,
                sequence=len(session.events) + 1,
                occurred_at=datetime.now(timezone.utc),
            )
        )
        if completion is None:  # pragma: no cover - exhaustive projector guard
            raise RuntimeError("Legacy completion projection is unavailable.")
        yield completion

    # ------------------------------------------------------------------
    # Execution helpers
    # ------------------------------------------------------------------
    def _new_legacy_session(
        self,
        topic: str,
        *,
        observer: Callable[[ResearchEvent], None] | None = None,
        cancellation: CancellationToken | None = None,
    ) -> RunSession:
        """Create and start one session owned by a direct legacy adapter."""
        session = RunSession(
            command=ResearchCommand(topic=topic, config=self.config),
            state=SummaryState(research_topic=topic),
            cancellation_token=cancellation or CancellationToken(),
        )
        if observer is not None:
            session.add_observer(observer)
        session.start()
        return session

    def execute(
        self,
        session: RunSession,
        prior_context: FollowupContext | None,
    ) -> None:
        """Coordinate one run with an explicitly run-bound governance scope."""
        if session.status is not RunStatus.RUNNING:
            raise InvalidTransitionError("Coordinator requires an already-started session.")
        session.raise_if_run_controlled()
        operations = GovernedOperations(session, self._operation_authorizer)
        root_scope = OperationScope(operations=operations)
        run_search_adapter = (
            HelloAgentsSearchAdapter()
            if self._uses_default_search_adapter
            else None
        )
        try:
            self._execute_governed(
                session,
                prior_context,
                operations=operations,
                root_scope=root_scope,
                run_search_adapter=run_search_adapter,
            )
        except OperationRejectedError as error:
            preferred = error
            try:
                session.raise_if_run_controlled()
            except OperationRejectedError as first_rejection:
                if first_rejection.operation_id != error.operation_id:
                    preferred = first_rejection
            except (CancellationRequestedError, DeadlineExceededError):
                pass
            session.request_cancellation()
            if preferred is error:
                raise
            raise preferred
        except (CancellationRequestedError, DeadlineExceededError):
            try:
                session.raise_if_run_controlled()
            except OperationRejectedError as rejection:
                session.request_cancellation()
                raise rejection
            except (CancellationRequestedError, DeadlineExceededError):
                pass
            session.request_cancellation()
            raise

    def _execute_governed(
        self,
        session: RunSession,
        prior_context: FollowupContext | None,
        *,
        operations: GovernedOperations,
        root_scope: OperationScope,
        run_search_adapter: HelloAgentsSearchAdapter | None,
    ) -> None:
        """Execute the workflow without storing run scope on the coordinator."""
        session.raise_if_run_controlled()

        planning_state = session.state
        session.raise_if_run_controlled()
        github_context = self._prepare_github_context(
            planning_state,
            config=session.command.config,
            operation_scope=root_scope,
        )
        session.raise_if_run_controlled()
        if github_context is not None:
            serialized = self._serialize_github_context(github_context)
            repository_event = self._github_repository_event(github_context)
            session.record_repository(
                github_context=serialized,
                repository=dict(repository_event["repository"]),
                notices=list(repository_event["notices"]),
                notice_codes=list(repository_event["notice_codes"]),
            )
            tasks = self._create_github_research_tasks(github_context.target)
        else:
            assembled_prior = ResearchContextAssembler().assemble(prior_context)
            session.raise_if_run_controlled()
            tasks = _call_with_operation_scope(
                self.planner.plan_todo_list,
                planning_state,
                prior_context=assembled_prior,
                operation_scope=root_scope,
            )
            session.raise_if_run_controlled()

        if not tasks:
            logger.info("No TODO items generated; falling back to single task")
            tasks = [self.planner.create_fallback_task(planning_state)]

        planned = [TodoItem(**task.to_dict()) for task in tasks]
        for task in planned:
            task.status = "pending"
            task.summary = None
            task.sources_summary = None
            task.stream_token = f"task_{task.id}"

        session.raise_if_run_controlled()
        self._create_task_notes_for_tasks(
            planned,
            cancellation=session.cancellation,
            operations=operations,
        )
        session.raise_if_run_controlled()
        session.install_plan(planned)
        work_items = self._prepare_work_items(
            session,
            operations=operations,
            search_adapter=run_search_adapter,
        )
        self._execute_work_items(session, work_items)
        session.raise_if_run_controlled()

        session.raise_if_run_controlled()
        notes_context = self._read_all_task_notes(
            session.state,
            operation_scope=root_scope,
        )
        session.raise_if_run_controlled()
        report = _call_with_operation_scope(
            self.reporting.generate_report,
            session.state,
            notes_context,
            operation_scope=root_scope,
        )
        session.raise_if_run_controlled()
        note_metadata = self._save_conclusion_note(
            session.command.topic,
            report,
            cancellation=session.cancellation,
            operation_scope=root_scope,
        )
        session.raise_if_run_controlled()
        note_id: str | None = None
        note_path: str | None = None
        if note_metadata is not None:
            note_id, note_path, note_title = note_metadata
            session.record_report_note(
                note_id=note_id,
                note_path=note_path,
                title=note_title,
            )
        session.set_report(report, note_id=note_id, note_path=note_path)

    def _prepare_work_items(
        self,
        session: RunSession,
        *,
        operations: GovernedOperations,
        search_adapter: HelloAgentsSearchAdapter | None,
    ) -> list[_TaskWorkItem]:
        """Read note inputs on the coordinator thread and detach all worker data."""
        github_markdown = str(
            (session.state.github_context or {}).get("markdown") or ""
        )
        items: list[_TaskWorkItem] = []
        for index, task in enumerate(session.state.todo_items):
            session.raise_if_run_controlled()
            note_content = ""
            note_context = self._read_task_note(
                task,
                operation_scope=OperationScope(
                    operations=operations,
                    task_id=task.id,
                ),
            )
            session.raise_if_run_controlled()
            if task.note_id:
                note_data = note_context.get(task.note_id)
                if isinstance(note_data, dict):
                    candidate = note_data.get("content")
                    if isinstance(candidate, str):
                        note_content = candidate
            items.append(
                _TaskWorkItem(
                    task_id=task.id,
                    topic=session.command.topic,
                    title=task.title,
                    intent=task.intent,
                    original_query=task.query,
                    note_id=task.note_id,
                    note_path=task.note_path,
                    note_content=note_content,
                    source_strategy=task.source_strategy,
                    repository=task.repository,
                    github_markdown=github_markdown,
                    loop_offset=index * 3,
                    config=session.command.config,
                    operations=operations,
                    search_adapter=search_adapter,
                )
            )
        return items

    def _execute_work_items(
        self,
        session: RunSession,
        work_items: list[_TaskWorkItem],
    ) -> None:
        """Run detached workers with bounded submissions and coordinator-only merges."""
        if not work_items:
            return
        operations = work_items[0].operations
        max_workers = min(
            session.command.config.max_concurrent_tasks,
            len(work_items),
        )
        result_queue: Queue[_WorkerMessage] = Queue(
            maxsize=max(1, max_workers * 4)
        )
        pending = iter(work_items)
        active: dict[Future[None], _TaskWorkItem] = {}
        stop_event = Event()
        executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=f"research-{session.run_id[:8]}",
        )

        def fill_active_slots() -> None:
            while len(active) < max_workers:
                session.raise_if_run_controlled()
                try:
                    item = next(pending)
                except StopIteration:
                    return
                session.start_task(item.task_id)
                future = executor.submit(
                    self._run_task_worker,
                    item,
                    result_queue,
                    session.cancellation,
                    stop_event,
                )
                active[future] = item

        try:
            fill_active_slots()
            while active:
                session.raise_if_run_controlled()
                try:
                    message = result_queue.get(timeout=_QUEUE_POLL_SECONDS)
                except Empty:
                    message = None
                if message is not None:
                    session.raise_if_run_controlled()
                    self._merge_worker_message(
                        session,
                        message,
                        operations=operations,
                    )

                completed = [future for future in active if future.done()]
                for future in completed:
                    item = active.pop(future)
                    try:
                        future.result()
                    except _OPERATION_CONTROL_ERRORS:
                        stop_event.set()
                        session.request_cancellation()
                        raise
                    except Exception:
                        session.raise_if_run_controlled()
                        logger.error("Research worker failed unexpectedly")
                        session.fail_task(
                            item.task_id,
                            message="Task execution failed.",
                            code="task_failed",
                            original_query=item.original_query,
                        )
                        session.raise_if_run_controlled()
                        self._update_task_note(
                            self._task_from_session(session, item.task_id),
                            operation_scope=OperationScope(
                                operations=operations,
                                task_id=item.task_id,
                            ),
                        )
                fill_active_slots()

            while True:
                try:
                    message = result_queue.get_nowait()
                except Empty:
                    break
                session.raise_if_run_controlled()
                self._merge_worker_message(
                    session,
                    message,
                    operations=operations,
                )
            session.raise_if_run_controlled()
        except _OPERATION_CONTROL_ERRORS:
            stop_event.set()
            session.request_cancellation()
            raise
        finally:
            stop_event.set()
            executor.shutdown(wait=True, cancel_futures=True)

    def _run_task_worker(
        self,
        item: _TaskWorkItem,
        result_queue: Queue[_WorkerMessage],
        cancellation: CancellationToken,
        stop_event: Event,
    ) -> None:
        """Produce detached result messages without touching canonical state."""
        try:
            self._research_task_worker(
                item,
                result_queue,
                cancellation,
                stop_event,
            )
        except _OPERATION_CONTROL_ERRORS:
            raise
        except Exception:
            logger.error("Task %s execution failed", item.task_id)
            self._put_worker_message(
                result_queue,
                _WorkerMessage(
                    kind=_WorkerMessageKind.FAILED,
                    task_id=item.task_id,
                    payload={
                        "message": "Task execution failed.",
                        "code": "task_failed",
                        "original_query": item.original_query,
                    },
                ),
                cancellation,
                stop_event,
            )

    def _research_task_worker(
        self,
        item: _TaskWorkItem,
        result_queue: Queue[_WorkerMessage],
        cancellation: CancellationToken,
        stop_event: Event,
    ) -> None:
        max_attempts = 3
        query = item.original_query
        local_task = TodoItem(
            id=item.task_id,
            title=item.title,
            intent=item.intent,
            query=query,
            note_id=item.note_id,
            note_path=item.note_path,
            source_strategy=item.source_strategy,
            repository=item.repository,
        )
        latest_sources: str | None = None

        for attempt in range(max_attempts):
            operation_scope = OperationScope(
                operations=item.operations,
                task_id=item.task_id,
                task_attempt=attempt + 1,
            )
            cancellation.raise_if_cancelled()
            if stop_event.is_set():
                return
            search_kwargs: dict[str, object] = {
                # Search retry/backoff must observe both explicit cancellation
                # and the run's monotonic deadline.
                "cancellation": item.operations.session,
            }
            if item.search_adapter is not None:
                search_kwargs["search_adapter"] = item.search_adapter
            search_result, notices, answer_text, backend = _call_with_operation_scope(
                self._search_adapter,
                query,
                item.config,
                item.loop_offset + attempt,
                operation_scope=operation_scope,
                **search_kwargs,
            )
            if stop_event.is_set():
                return
            item.operations.session.raise_if_cancelled()
            raw_notice_codes = (
                search_result.get("notice_codes")
                if isinstance(search_result, dict)
                else ()
            )
            safe_notices, safe_notice_codes = _safe_search_notice_fields(
                notices if isinstance(notices, (list, tuple)) else (),
                raw_notice_codes
                if isinstance(raw_notice_codes, (list, tuple))
                else (),
            )
            if isinstance(search_result, dict):
                search_result = dict(search_result)
                search_result["notices"] = safe_notices
                search_result["notice_codes"] = safe_notice_codes

            if not search_result or not search_result.get("results"):
                if attempt < max_attempts - 1:
                    previous_query = query
                    local_task.query = query
                    query = self._refine_query(local_task, attempt)
                    self._put_worker_message(
                        result_queue,
                        _WorkerMessage(
                            kind=_WorkerMessageKind.RETRY,
                            task_id=item.task_id,
                            payload={
                                "previous_query": previous_query,
                                "refined_query": query,
                                "attempt": attempt + 1,
                                "reason": "no_search_results",
                            },
                        ),
                        cancellation,
                        stop_event,
                    )
                    continue
                self._put_worker_message(
                    result_queue,
                    _WorkerMessage(
                        kind=_WorkerMessageKind.SKIPPED,
                        task_id=item.task_id,
                        payload={
                            "reason": "no_search_results",
                            "original_query": item.original_query,
                        },
                    ),
                    cancellation,
                    stop_event,
                )
                return

            latest_sources, context = self._context_preparer(
                search_result,
                answer_text,
                item.config,
            )
            if item.github_markdown:
                context = (
                    f"{item.github_markdown}\n\n## Web Search Context\n{context}"
                )
            self._put_worker_message(
                result_queue,
                _WorkerMessage(
                    kind=_WorkerMessageKind.SOURCES,
                    task_id=item.task_id,
                    payload={
                        "context": context,
                        "latest_sources": latest_sources,
                        "backend": backend,
                        "notices": safe_notices,
                        "notice_codes": safe_notice_codes,
                    },
                ),
                cancellation,
                stop_event,
            )
            if stop_event.is_set():
                return
            item.operations.session.raise_if_cancelled()

            request = TaskSummaryInput(
                topic=item.topic,
                title=item.title,
                intent=item.intent,
                query=query,
                context=context,
                note_id=item.note_id,
                note_content=item.note_content,
            )
            summary_stream, summary_getter = _call_with_operation_scope(
                self.summarizer.stream_summary,
                request,
                operation_scope=operation_scope,
            )
            try:
                for chunk in summary_stream:
                    cancellation.raise_if_cancelled()
                    if chunk:
                        self._put_worker_message(
                            result_queue,
                            _WorkerMessage(
                                kind=_WorkerMessageKind.SUMMARY_DELTA,
                                task_id=item.task_id,
                                payload={"chunk": chunk},
                            ),
                            cancellation,
                            stop_event,
                        )
            finally:
                close = getattr(summary_stream, "close", None)
                if callable(close):
                    close()

            summary = summary_getter().strip() or "暂无可用信息"
            if item.config.enable_quality_gate:
                quality = self._check_summary_quality(summary)
                if not quality["passed"] and attempt < max_attempts - 1:
                    previous_query = query
                    local_task.query = query
                    query = self._refine_query(local_task, attempt)
                    self._put_worker_message(
                        result_queue,
                        _WorkerMessage(
                            kind=_WorkerMessageKind.RETRY,
                            task_id=item.task_id,
                            payload={
                                "previous_query": previous_query,
                                "refined_query": query,
                                "attempt": attempt + 1,
                                "reason": ",".join(quality["reasons"]),
                            },
                        ),
                        cancellation,
                        stop_event,
                    )
                    continue

            self._put_worker_message(
                result_queue,
                _WorkerMessage(
                    kind=_WorkerMessageKind.COMPLETED,
                    task_id=item.task_id,
                    payload={
                        "summary": summary,
                        "sources_summary": latest_sources,
                        "original_query": item.original_query,
                    },
                ),
                cancellation,
                stop_event,
            )
            return

    @staticmethod
    def _put_worker_message(
        result_queue: Queue[_WorkerMessage],
        message: _WorkerMessage,
        cancellation: CancellationToken,
        stop_event: Event,
    ) -> None:
        """Put with bounded waits so cancellation cannot strand a producer."""
        while not stop_event.is_set():
            cancellation.raise_if_cancelled()
            try:
                result_queue.put(message, timeout=_QUEUE_POLL_SECONDS)
                return
            except Full:
                continue

    def _merge_worker_message(
        self,
        session: RunSession,
        message: _WorkerMessage,
        *,
        operations: GovernedOperations,
    ) -> None:
        """Apply one worker result through canonical transitions on this thread."""
        session.raise_if_run_controlled()
        payload = dict(message.payload)
        if message.kind is _WorkerMessageKind.SOURCES:
            context = payload.pop("context", None)
            session.record_sources(
                message.task_id,
                context=context if isinstance(context, str) else None,
                **payload,
            )
            return
        if message.kind is _WorkerMessageKind.SUMMARY_DELTA:
            session.append_task_summary(message.task_id, str(payload["chunk"]))
            return
        if message.kind is _WorkerMessageKind.RETRY:
            session.record_retry(
                message.task_id,
                previous_query=str(payload["previous_query"]),
                refined_query=str(payload["refined_query"]),
                attempt=int(payload["attempt"]),
                reason=str(payload["reason"]),
            )
            return
        if message.kind is _WorkerMessageKind.COMPLETED:
            session.complete_task(
                message.task_id,
                summary=str(payload["summary"]),
                sources_summary=(
                    str(payload["sources_summary"])
                    if payload.get("sources_summary") is not None
                    else None
                ),
                original_query=str(payload["original_query"]),
            )
        elif message.kind is _WorkerMessageKind.SKIPPED:
            session.skip_task(
                message.task_id,
                reason=str(payload["reason"]),
                original_query=str(payload["original_query"]),
            )
        elif message.kind is _WorkerMessageKind.FAILED:
            session.fail_task(
                message.task_id,
                message=str(payload["message"]),
                code=str(payload["code"]),
                original_query=str(payload["original_query"]),
            )
        else:  # pragma: no cover - exhaustive enum guard
            raise ValueError(f"Unknown worker message kind: {message.kind}")
        session.raise_if_run_controlled()
        task = self._task_from_session(session, message.task_id)
        self._update_task_note(
            task,
            operation_scope=OperationScope(
                operations=operations,
                task_id=message.task_id,
                task_attempt=max(1, task.retry_count + 1),
            ),
        )

    @staticmethod
    def _task_from_session(session: RunSession, task_id: int) -> TodoItem:
        for task in session.state.todo_items:
            if task.id == task_id:
                return task
        raise KeyError(f"Unknown task ID: {task_id}")

    # ------------------------------------------------------------------
    # Summary quality & query refinement
    # ------------------------------------------------------------------

    @staticmethod
    def _check_summary_quality(summary: str) -> dict[str, Any]:
        """Rule-based quality check for a task summary.

        Returns a dict with ``passed`` (bool) and ``reasons`` (list[str]).
        """
        passed = True
        reasons: list[str] = []

        if not summary or summary.strip() == "暂无可用信息":
            passed = False
            reasons.append("empty_or_fallback")

        if len(summary.strip()) < 30:
            passed = False
            reasons.append("too_short")

        has_structure = any(
            marker in summary for marker in ("###", "- ", "* ", "1. ", "2. ")
        )
        if not has_structure:
            passed = False
            reasons.append("no_structure")

        return {"passed": passed, "reasons": reasons}

    @staticmethod
    def _refine_query(task: TodoItem, attempt: int) -> str:
        """Generate a broader or alternative search query after a failed attempt.

        * attempt 0 — extract keywords from ``intent``
        * attempt 1 — use the task title plus English fallback keywords
        """
        if attempt == 0:
            keywords = (
                task.intent.replace("，", ",")
                .replace("、", ",")
                .replace("；", ",")
                .split(",")
            )
            refined = " ".join(k.strip() for k in keywords if k.strip())
            return refined or f"{task.title} 深入分析"

        # attempt >= 1: broader English-oriented query
        return f"{task.title} overview latest research"

    # ------------------------------------------------------------------
    # GitHub research helpers
    # ------------------------------------------------------------------

    def _prepare_github_context(
        self,
        state: SummaryState,
        *,
        config: Configuration | None = None,
        operation_scope: OperationScope,
    ) -> GitHubRepositoryContext | None:
        """Collect GitHub repository context when the topic names a repository."""
        run_config = config or self.config
        if not getattr(run_config, "enable_github_research", True):
            return None

        target = parse_github_repository(state.research_topic)
        if not target:
            return None

        if self._github_adapter is _DEFAULT_DEPENDENCY:
            client: Any = GovernedGitHubAdapter()
        else:
            client = self._github_adapter
        if client is None:
            return None
        try:
            github_kwargs: dict[str, object] = {}
            if self._github_adapter is _DEFAULT_DEPENDENCY:
                github_kwargs.update(
                    {
                        "token": run_config.github_token,
                        "base_url": run_config.github_api_base_url,
                    }
                )
            context = _call_with_operation_scope(
                client.collect_repository_context,
                target,
                operation_scope=operation_scope,
                **github_kwargs,
            )
        except _OPERATION_CONTROL_ERRORS:
            raise
        except Exception:  # pragma: no cover - defensive guardrail
            logger.error("GitHub repository context collection failed")
            context = GitHubRepositoryContext(
                target=target,
                notices=[_GITHUB_CONTEXT_FAILED_MESSAGE],
                notice_codes=[_GITHUB_CONTEXT_FAILED_CODE],
            )

        safe_notices, safe_notice_codes = _safe_github_notice_fields(
            context.notices,
            context.notice_codes,
        )
        context = replace(
            context,
            notices=safe_notices,
            notice_codes=safe_notice_codes,
        )

        logger.info("GitHub research mode enabled for %s", target.full_name)
        return context

    @staticmethod
    def _create_github_research_tasks(
        target: GitHubRepositoryTarget,
    ) -> list[TodoItem]:
        """Return fixed tasks for GitHub-first repository research."""
        repository = target.full_name
        task_specs = [
            (
                "仓库概览与定位",
                "梳理项目用途、核心能力、许可证、语言栈、README 中的主要承诺与当前活跃度。",
                f"{repository} GitHub repository overview README features",
            ),
            (
                "架构与代码结构",
                "结合目录树和文档分析项目的主要模块、运行方式、扩展点与技术边界。",
                f"{repository} architecture directory structure modules",
            ),
            (
                "演进时间线与路线图",
                "根据 commits、releases、issues 和 PRs 梳理近期变化、维护节奏、待解决问题和路线图信号。",
                f"{repository} commits releases issues roadmap",
            ),
            (
                "社区评价与替代方案",
                "补充外部文章、社区讨论和竞品信息，评估采用价值、风险与适用场景。",
                f"{repository} community adoption alternatives comparison",
            ),
        ]

        return [
            TodoItem(
                id=index,
                title=title,
                intent=intent,
                query=query,
                source_strategy="github_api_then_web",
                repository=repository,
            )
            for index, (title, intent, query) in enumerate(task_specs, start=1)
        ]

    @staticmethod
    def _serialize_github_context(
        context: GitHubRepositoryContext,
    ) -> dict[str, Any]:
        """Serialize GitHub context into the run state."""
        notices, notice_codes = _safe_github_notice_fields(
            context.notices,
            context.notice_codes,
        )
        safe_context = replace(
            context,
            notices=notices,
            notice_codes=notice_codes,
        )
        return {
            "target": {
                "owner": context.target.owner,
                "repo": context.target.repo,
                "full_name": context.target.full_name,
                "url": context.target.html_url,
            },
            "repository": dict(context.repository),
            "languages": dict(context.languages),
            "contributors": list(context.contributors),
            "commits": list(context.commits),
            "issues": list(context.issues),
            "pull_requests": list(context.pull_requests),
            "releases": list(context.releases),
            "notices": notices,
            "notice_codes": notice_codes,
            "markdown": safe_context.to_markdown(),
        }

    @staticmethod
    def _github_repository_event(context: GitHubRepositoryContext) -> dict[str, Any]:
        """Build the SSE payload announcing GitHub research mode."""
        notices, notice_codes = _safe_github_notice_fields(
            context.notices,
            context.notice_codes,
        )
        repository = {
            "owner": context.target.owner,
            "repo": context.target.repo,
            "full_name": context.target.full_name,
            "url": context.target.html_url,
            "stars": context.repository.get("stars"),
            "forks": context.repository.get("forks"),
            "open_issues": context.repository.get("open_issues"),
            "default_branch": context.repository.get("default_branch"),
            "language": context.repository.get("language"),
        }
        return {
            "type": "github_repository",
            "message": f"已识别 GitHub 仓库：{context.target.full_name}",
            "repository": repository,
            "notices": notices,
            "notice_codes": notice_codes,
        }

    # ------------------------------------------------------------------
    # Note sub-agent helpers
    # ------------------------------------------------------------------

    def _create_task_notes_for_tasks(
        self,
        tasks: list[TodoItem],
        *,
        cancellation: CancellationToken,
        operations: GovernedOperations,
    ) -> None:
        """Create task notes on the coordinator thread as an optional effect."""
        if not self.note_agent:
            return
        for task in tasks:
            cancellation.raise_if_cancelled()
            try:
                note_id = _call_with_operation_scope(
                    self.note_agent.create_task_note,
                    task_id=task.id,
                    title=task.title,
                    operation_scope=OperationScope(
                        operations=operations,
                        task_id=task.id,
                    ),
                    content=f"任务概览：{task.intent}\n检索查询：{task.query}",
                )
            except _OPERATION_CONTROL_ERRORS:
                raise
            except Exception:
                logger.error("Optional task note creation failed")
                continue
            cancellation.raise_if_cancelled()
            if note_id:
                task.note_id = note_id
                try:
                    cancellation.raise_if_cancelled()
                    task.note_path = self.note_agent.note_path(note_id)
                except Exception:
                    logger.error("Optional task note path lookup failed")
                cancellation.raise_if_cancelled()

    def _read_task_note(
        self,
        task: TodoItem,
        *,
        operation_scope: OperationScope,
    ) -> dict[str, Any]:
        """Read a single task's note content."""
        if not self.note_agent or not task.note_id:
            return {}
        try:
            return {
                task.note_id: _call_with_operation_scope(
                    self.note_agent.read_note,
                    task.note_id,
                    operation_scope=operation_scope,
                )
            }
        except _OPERATION_CONTROL_ERRORS:
            raise
        except Exception:
            logger.error("Optional task note read failed")
            return {}

    def _read_all_task_notes(
        self,
        state: SummaryState,
        *,
        operation_scope: OperationScope,
    ) -> dict[str, Any]:
        """Read all task notes for report generation."""
        if not self.note_agent:
            return {}
        note_ids = [t.note_id for t in state.todo_items if t.note_id]
        try:
            return _call_with_operation_scope(
                self.note_agent.read_all_task_notes,
                note_ids,
                operation_scope=operation_scope,
            )
        except _OPERATION_CONTROL_ERRORS:
            raise
        except Exception:
            logger.error("Optional task note batch read failed")
            return {}

    def _update_task_note(
        self,
        task: TodoItem,
        *,
        operation_scope: OperationScope,
    ) -> None:
        """Update a task's note with the latest summary."""
        if not self.note_agent or not task.note_id:
            return
        content_parts = [f"任务状态：{task.status}"]
        if task.summary:
            content_parts.append(f"\n任务总结：\n{task.summary}")
        if task.sources_summary:
            content_parts.append(f"\n来源概览：\n{task.sources_summary}")
        try:
            _call_with_operation_scope(
                self.note_agent.update_note,
                task.note_id,
                task_id=task.id,
                operation_scope=operation_scope,
                title=f"任务 {task.id}: {task.title}",
                content="\n".join(content_parts),
            )
        except _OPERATION_CONTROL_ERRORS:
            raise
        except Exception:
            logger.error("Optional task note update failed")

    def _save_conclusion_note(
        self,
        topic: str,
        report: str,
        *,
        cancellation: CancellationToken,
        operation_scope: OperationScope,
    ) -> tuple[str, str | None, str] | None:
        """Persist an optional conclusion note without mutating canonical state."""
        if not self.note_agent or not report or not report.strip():
            return None

        note_title = f"研究报告：{topic}".strip() or "研究报告"
        cancellation.raise_if_cancelled()
        try:
            note_id = _call_with_operation_scope(
                self.note_agent.create_conclusion_note,
                title=note_title,
                content=report.strip(),
                operation_scope=operation_scope,
            )
        except _OPERATION_CONTROL_ERRORS:
            raise
        except Exception:
            logger.error("Optional conclusion note creation failed")
            return None
        cancellation.raise_if_cancelled()

        if not note_id:
            return None

        try:
            cancellation.raise_if_cancelled()
            note_path = self.note_agent.note_path(note_id)
        except Exception:
            logger.error("Optional conclusion note path lookup failed")
            note_path = None
        cancellation.raise_if_cancelled()
        return note_id, note_path, note_title
