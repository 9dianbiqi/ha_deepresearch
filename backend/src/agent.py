"""Orchestrator coordinating the deep research workflow."""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Callable, Iterator, Mapping, Sequence
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
from research.artifacts import (
    ArtifactPayload,
    ArtifactStore,
    persist_research_artifacts,
)
from research.claim_verifier import (
    StructuredFactualSupportVerifier,
    StructuredSemanticSupportScorer,
)
from research.context import FollowupContext, ResearchContextAssembler
from research.contracts import EventKind, ResearchCommand, ResearchEvent, RunStatus
from research.evidence import (
    GitHubEvidenceBundle,
    build_github_evidence_bundle,
    canonicalize_github_report,
    freeze_github_evidence,
    github_evidence_bundle_from_dict,
    render_github_artifacts,
    supplement_github_evidence,
)
from research.intelligence import (
    ArtifactManifestV2,
    ClaimRecord,
    EvidenceRecord,
    ResearchIntelligenceBundle,
)
from research.legacy_sse import project_legacy_event as _project_legacy_event
from research.operations import (
    GovernedOperations,
    OperationRejectedError,
    OperationScope,
)
from research.pipeline import PreparedResearch, ResearchKernel
from research.profiles import ResearchMode, built_in_profile_registry
from research.providers.github import GitHubSourceProvider
from research.providers.web import WebSourceProvider
from research.quality import EvidenceGateBlockedError
from research.report_document import (
    ParagraphQualityAssessment,
    StructuredSummaryDocument,
    SummaryParagraph,
    SummaryQualityAssessment,
)
from research.report_validation import validate_citations
from research.session import (
    CancellationRequestedError,
    CancellationToken,
    DeadlineExceededError,
    InvalidTransitionError,
    RunSession,
)
from research.sources import (
    SourceCollection,
    SourceProviderRegistry,
    SourceSearchResult,
)
from research.summary_quality import (
    SummaryQualityGateV1,
    SummaryQualityThresholds,
)
from research.telemetry import TelemetryHelloAgentsLLM, llm_telemetry_scope
from research.web_capture import WebCaptureResult
from services.github_research import (
    GitHubRepositoryContext,
    GitHubRepositoryTarget,
    parse_github_repositories,
    parse_github_repository,
)
from services.note_agent import NoteSubAgent
from services.planner import PlanningService
from services.reporter import ReportingService, StructuredReportGenerationError
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
    dimension: str
    github_markdown: str
    loop_offset: int
    config: Configuration
    operations: GovernedOperations
    search_adapter: Any | None
    research_kernel: ResearchKernel | None = None
    prepared_research: PreparedResearch | None = None


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
        source_provider_registry: SourceProviderRegistry | None = None,
        research_kernel: ResearchKernel | None = None,
        artifact_store: ArtifactStore | None = None,
        summary_quality_gate: SummaryQualityGateV1 | None = None,
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
        # Quality agents are shared production dependencies.  Their conversation
        # history must never be mutated by two concurrent runs at once.
        self._quality_agent_lock = Lock()
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
            self._quality_semantic_agent: SimpleAgent | None = None
            self._quality_factual_agent: SimpleAgent | None = None
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
            self._quality_semantic_agent = self._create_role_agent(
                name="Evidence semantic scorer",
                system_prompt=(
                    "Score whether the supplied evidence is semantically relevant to "
                    "the supplied claim. Return strict JSON only."
                ),
                role="quality_semantic",
            )
            self._quality_factual_agent = self._create_role_agent(
                name="Evidence factual verifier",
                system_prompt=(
                    "Verify a claim only against supplied evidence. Return strict JSON "
                    "only and copy support spans exactly from evidence excerpts."
                ),
                role="quality_factual",
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
        if research_kernel is not None:
            self._research_kernel = research_kernel
            self._source_provider_registry = (
                source_provider_registry or research_kernel.provider_registry
            )
        else:
            effective_registry = source_provider_registry
            if effective_registry is None:
                github_dependency = (
                    None
                    if github_adapter is _DEFAULT_DEPENDENCY
                    else github_adapter
                )
                effective_registry = SourceProviderRegistry(
                    (
                        GitHubSourceProvider(
                            adapter=github_dependency,
                            token=self.config.github_token,
                            base_url=self.config.github_api_base_url,
                        ),
                        WebSourceProvider(
                            dispatcher=self._search_adapter,
                        ),
                    )
                )
            self._source_provider_registry = effective_registry
            self._research_kernel = ResearchKernel(
                provider_registry=effective_registry,
            )
        self._artifact_store = artifact_store
        self._summary_quality_gate = summary_quality_gate

    @property
    def last_session(self) -> RunSession | None:
        """Return the most recently completed direct legacy session."""
        with self._last_session_lock:
            return self._last_session

    @property
    def source_provider_registry(self) -> SourceProviderRegistry | None:
        """Return the optional provider registry reserved for kernel routing."""
        return self._source_provider_registry

    @property
    def research_kernel(self) -> ResearchKernel | None:
        """Return the optional kernel injected by the production composition."""
        return self._research_kernel

    @property
    def artifact_store(self) -> ArtifactStore | None:
        """Return the optional external artifact store used by v2 runs."""
        return self._artifact_store

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

        return TelemetryHelloAgentsLLM(**llm_kwargs)

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

    def resume(
        self,
        session: RunSession,
        prior_context: FollowupContext | None,
    ) -> None:
        """Resume from a validated checkpoint without replaying safe work."""
        if session.status is not RunStatus.RUNNING:
            raise InvalidTransitionError("Coordinator requires a running recovery session.")
        session.raise_if_run_controlled()
        operations = GovernedOperations(session, self._operation_authorizer)
        root_scope = OperationScope(operations=operations)
        phase = session.checkpoint_phase
        if phase in {
            "evidence_completed",
            "summary_quality_completed",
            "report_before_generation",
            "report_retry",
            "report_retry_completed",
        }:
            self._generate_report(
                session,
                operations=operations,
                root_scope=root_scope,
            )
            return
        if phase == "report_generated":
            return
        run_search_adapter = (
            HelloAgentsSearchAdapter()
            if self._uses_default_search_adapter
            else None
        )
        self._execute_governed(
            session,
            prior_context,
            operations=operations,
            root_scope=root_scope,
            run_search_adapter=run_search_adapter,
            resume_from_checkpoint=True,
        )

    def retry_report(
        self,
        session: RunSession,
        prior_context: FollowupContext | None = None,
    ) -> None:
        """Regenerate only the final report after a structural validation failure.

        Research tasks have already completed at this point.  Retrying the
        reporter in-place keeps the run identity and task checkpoints stable
        and avoids repeating network research merely because the first LLM
        response was truncated.
        """
        del prior_context  # The completed task notes are the retry context.
        if session.status is not RunStatus.RUNNING:
            raise InvalidTransitionError("Report retry requires a running session.")
        session.raise_if_run_controlled()
        operations = GovernedOperations(session, self._operation_authorizer)
        root_scope = OperationScope(operations=operations)
        if self._structured_reporting_enabled(session):
            session.persist_checkpoint("report_retry")
            self._generate_report(
                session,
                operations=operations,
                root_scope=root_scope,
            )
            return
        session.persist_checkpoint("report_retry")
        notes_context = self._read_all_task_notes(
            session.state,
            operation_scope=root_scope,
        )
        session.raise_if_run_controlled()
        with llm_telemetry_scope(session, role="reporter", retry_count=1):
            report = _call_with_operation_scope(
                self.reporting.generate_report,
                session.state,
                notes_context,
                operation_scope=root_scope,
            )
        report = self._canonicalize_github_report(session, report)
        session.raise_if_run_controlled()
        note_metadata = self._save_conclusion_note(
            session.command.topic,
            report,
            cancellation=session.cancellation,
            operation_scope=root_scope,
        )
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
        self._refresh_github_artifacts(session, report)
        session.persist_checkpoint("report_retry_completed")

    def _generate_report(
        self,
        session: RunSession,
        *,
        operations: GovernedOperations,
        root_scope: OperationScope,
    ) -> None:
        """Generate only the report after research evidence is durable."""
        quality_already_completed = (
            session.checkpoint_phase == "summary_quality_completed"
        )
        if not quality_already_completed:
            session.persist_checkpoint("report_before_generation")
        notes_context = self._read_all_task_notes(
            session.state,
            operation_scope=root_scope,
        )
        session.raise_if_run_controlled()
        if self._structured_reporting_enabled(session):
            report = self._generate_quality_gated_report(
                session,
                notes_context=notes_context,
                operation_scope=root_scope,
                quality_already_completed=quality_already_completed,
            )
        else:
            report = _call_with_operation_scope(
                self.reporting.generate_report,
                session.state,
                notes_context,
                operation_scope=root_scope,
            )
        report = self._canonicalize_github_report(session, report)
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
        self._refresh_github_artifacts(session, report)
        session.persist_checkpoint("report_generated")

    def _structured_reporting_enabled(self, session: RunSession) -> bool:
        """Return whether this run can use the gated structured boundary."""
        if not callable(getattr(self.reporting, "generate_structured_document", None)):
            return False
        if not callable(getattr(self.reporting, "render_structured_document", None)):
            return False
        raw_bundle = session.state.research_intelligence
        if not isinstance(raw_bundle, Mapping) or raw_bundle.get("schema_version") != 2:
            return False
        profile_id = session.state.research_profile_id
        if profile_id == "web.evidence.v1":
            return session.command.config.enable_evidence_web
        return session.state.research_mode == ResearchMode.GITHUB.value

    def _generate_quality_gated_report(
        self,
        session: RunSession,
        *,
        notes_context: dict[str, Any],
        operation_scope: OperationScope,
        quality_already_completed: bool,
    ) -> str:
        """Generate, verify, persist, and deterministically render one report."""
        bundle = ResearchIntelligenceBundle.from_dict(
            session.state.research_intelligence
        )
        if not bundle.evidence_frozen:
            raise EvidenceGateBlockedError(
                replace(
                    bundle.coverage,
                    allow_report=False,
                    blockers=tuple(
                        dict.fromkeys((*bundle.coverage.blockers, "evidence_not_frozen"))
                    ),
                )
            )

        document: StructuredSummaryDocument | None = None
        assessment: SummaryQualityAssessment | None = None
        if quality_already_completed:
            document = StructuredSummaryDocument.from_dict(
                session.state.structured_summary
            )
            assessment = SummaryQualityAssessment.from_dict(
                session.state.quality_assessment
            )
            if not self._quality_binding_is_valid(
                session,
                bundle=bundle,
                document=document,
                assessment=assessment,
            ):
                raise EvidenceGateBlockedError(
                    replace(
                        bundle.coverage,
                        allow_report=False,
                        blockers=tuple(
                            dict.fromkeys(
                                (
                                    *bundle.coverage.blockers,
                                    "summary_quality_binding_invalid",
                                )
                            )
                        ),
                    )
                )
            if self._strict_summary_quality(session) and not assessment.passed:
                raise EvidenceGateBlockedError(
                    replace(
                        bundle.coverage,
                        allow_report=False,
                        blockers=tuple(
                            dict.fromkeys(
                                (
                                    *bundle.coverage.blockers,
                                    "summary_quality_failed",
                                )
                            )
                        ),
                    )
                )
        else:
            gate = self._summary_quality_gate_for_scope(operation_scope)
            feedback: tuple[str, ...] = ()
            for attempt in range(2):
                try:
                    with llm_telemetry_scope(
                        session,
                        role="structured_reporter",
                        retry_count=attempt,
                    ):
                        generated = _call_with_operation_scope(
                            self.reporting.generate_structured_document,
                            session.state,
                            notes_context,
                            quality_feedback=feedback,
                            operation_scope=operation_scope,
                        )
                    if not isinstance(generated, StructuredSummaryDocument):
                        raise StructuredReportGenerationError(
                            "Structured reporter returned an invalid document."
                        )
                    document = generated
                except StructuredReportGenerationError:
                    if attempt == 0:
                        feedback = ("structured_generation_failed",)
                        continue
                    document = self._deterministic_summary_document(bundle)
                assessment = gate.evaluate(document, bundle)
                if assessment.passed:
                    break
                feedback = self._quality_feedback(assessment)

            if document is None or assessment is None:  # pragma: no cover - loop invariant
                raise RuntimeError("Structured quality generation did not produce output.")

            if not assessment.passed:
                document = self._align_verified_citations(document, assessment)
                assessment = gate.evaluate(document, bundle)

            if not assessment.passed and self._strict_summary_quality(session):
                self._record_structured_quality(
                    session,
                    bundle=bundle,
                    document=document,
                    assessment=assessment,
                )
                session.persist_checkpoint("summary_quality_completed")
                raise EvidenceGateBlockedError(
                    replace(
                        bundle.coverage,
                        allow_report=False,
                        blockers=tuple(
                            dict.fromkeys(
                                (
                                    *bundle.coverage.blockers,
                                    "summary_quality_failed",
                                )
                            )
                        ),
                    )
                )
            if not assessment.passed:
                document, assessment = self._degrade_structured_document(
                    document,
                    assessment,
                )

            self._record_structured_quality(
                session,
                bundle=bundle,
                document=document,
                assessment=assessment,
            )
            session.persist_checkpoint("summary_quality_completed")

        rendered = self.reporting.render_structured_document(
            session.state,
            document,
            notes_context,
        )
        markdown = getattr(rendered, "markdown", None)
        if not isinstance(markdown, str) or not markdown.strip():
            raise StructuredReportGenerationError(
                "Structured renderer returned no Markdown."
            )
        return markdown

    def _summary_quality_gate_for_scope(
        self,
        operation_scope: OperationScope,
    ) -> SummaryQualityGateV1:
        """Build one run-scoped, cached production verifier boundary."""
        if self._summary_quality_gate is not None:
            return self._summary_quality_gate
        run_config = operation_scope.operations.session.command.config
        thresholds = SummaryQualityThresholds(
            semantic=run_config.summary_semantic_threshold,
            factual=run_config.summary_factual_threshold,
            citation=run_config.summary_citation_threshold,
            overall=run_config.summary_overall_threshold,
        )
        semantic_agent = self._quality_semantic_agent
        factual_agent = self._quality_factual_agent
        if semantic_agent is None or factual_agent is None:
            return SummaryQualityGateV1(thresholds=thresholds)
        semantic_cache: dict[str, str] = {}
        factual_cache: dict[str, str] = {}

        def cache_key(
            claim: ClaimRecord,
            evidence: tuple[EvidenceRecord, ...],
        ) -> str:
            return "|".join((claim.claim_id, *(item.evidence_id for item in evidence)))

        def run_agent(agent: SimpleAgent, prompt: str, *, role: str) -> str:
            quality_session = operation_scope.operations.session
            with self._quality_agent_lock:
                try:
                    with llm_telemetry_scope(quality_session, role=role):
                        response = agent.run(
                            prompt,
                            _research_operation_scope=operation_scope,
                        )
                finally:
                    agent.clear_history()
            return response if isinstance(response, str) else ""

        def semantic_invoker(
            claim: ClaimRecord,
            evidence: tuple[EvidenceRecord, ...],
        ) -> str:
            key = cache_key(claim, evidence)
            if key not in semantic_cache:
                semantic_cache[key] = run_agent(
                    semantic_agent,
                    self._semantic_quality_prompt(claim, evidence),
                    role="quality_semantic",
                )
            return semantic_cache[key]

        def factual_invoker(
            claim: ClaimRecord,
            evidence: tuple[EvidenceRecord, ...],
        ) -> str:
            key = cache_key(claim, evidence)
            if key not in factual_cache:
                factual_cache[key] = run_agent(
                    factual_agent,
                    self._factual_quality_prompt(claim, evidence),
                    role="quality_factual",
                )
            return factual_cache[key]

        model_name = self.config.resolved_model() or "configured-llm"
        return SummaryQualityGateV1(
            semantic_scorer=StructuredSemanticSupportScorer(
                semantic_invoker,
                name=model_name,
                version="semantic-support-v1",
            ),
            factual_verifier=StructuredFactualSupportVerifier(
                factual_invoker,
                name=model_name,
                version="factual-support-v1",
                prompt_version="claim-evidence-v1",
            ),
            thresholds=thresholds,
        )

    @staticmethod
    def _semantic_quality_prompt(
        claim: ClaimRecord,
        evidence: tuple[EvidenceRecord, ...],
    ) -> str:
        """Build a strict bounded semantic scorer prompt."""
        payload = {
            "claim": claim.statement,
            "evidence": [
                {"evidence_id": item.evidence_id, "excerpt": item.excerpt}
                for item in evidence
            ],
        }
        return (
            "Return exactly one JSON object with only semantic_score (0.0 to 1.0). "
            "Score whether the evidence discusses the same factual proposition.\n"
            + json.dumps(payload, ensure_ascii=False)
        )

    @staticmethod
    def _factual_quality_prompt(
        claim: ClaimRecord,
        evidence: tuple[EvidenceRecord, ...],
    ) -> str:
        """Build a strict bounded factual verifier prompt."""
        payload = {
            "claim_id": claim.claim_id,
            "claim": claim.statement,
            "supporting_candidates": list(claim.evidence_ids),
            "conflicting_candidates": list(claim.conflicting_evidence_ids),
            "evidence": [
                {"evidence_id": item.evidence_id, "excerpt": item.excerpt}
                for item in evidence
            ],
        }
        return (
            "Return exactly one JSON object with only these fields: verdict, "
            "factual_score, supporting_evidence_ids, conflicting_evidence_ids, "
            "support_spans, reasons. verdict must be supported, partial, "
            "unsupported, contradicted, or unverified. Every support_spans item "
            "must contain only evidence_id and exact_text copied verbatim from its "
            "excerpt. Never cite an ID outside its candidate list.\n"
            + json.dumps(payload, ensure_ascii=False)
        )

    @staticmethod
    def _quality_feedback(
        assessment: SummaryQualityAssessment,
    ) -> tuple[str, ...]:
        """Return bounded internal feedback for the single structured rewrite."""
        feedback = [
            blocker
            for item in assessment.paragraph_assessments
            for blocker in item.blockers
        ]
        feedback.extend(
            f"{item.claim_id}:verified_support={','.join(item.supporting_evidence_ids)}"
            for item in assessment.claim_assessments
            if item.supporting_evidence_ids
        )
        return tuple(dict.fromkeys(feedback))[:32]

    @staticmethod
    def _align_verified_citations(
        document: StructuredSummaryDocument,
        assessment: SummaryQualityAssessment,
    ) -> StructuredSummaryDocument:
        """Replace model-selected citations with verifier-selected positive support."""
        support_by_claim = {
            item.claim_id: item.supporting_evidence_ids
            for item in assessment.claim_assessments
        }
        paragraphs: list[SummaryParagraph] = []
        for paragraph in document.paragraphs:
            if paragraph.paragraph_type == "limitation":
                paragraphs.append(paragraph)
                continue
            citations = tuple(
                dict.fromkeys(
                    evidence_id
                    for claim_id in paragraph.claim_ids
                    for evidence_id in support_by_claim.get(claim_id, ())
                )
            )
            paragraphs.append(
                SummaryParagraph(
                    section_id=paragraph.section_id,
                    paragraph_type=paragraph.paragraph_type,
                    text=paragraph.text,
                    claim_ids=paragraph.claim_ids,
                    citation_ids=citations,
                )
            )
        return StructuredSummaryDocument(
            task_id=document.task_id,
            paragraphs=tuple(paragraphs),
            claim_ids=document.claim_ids,
        )

    @staticmethod
    def _deterministic_summary_document(
        bundle: ResearchIntelligenceBundle,
    ) -> StructuredSummaryDocument:
        """Create a safe fallback document from bounded atomic claims only."""
        paragraphs = tuple(
            SummaryParagraph(
                section_id=claim.dimension,
                paragraph_type="factual",
                text=claim.statement,
                claim_ids=(claim.claim_id,),
                citation_ids=claim.evidence_ids,
            )
            for claim in bundle.claims[:12]
            if claim.reportable and claim.evidence_ids
        )
        return StructuredSummaryDocument(
            task_id="report",
            paragraphs=paragraphs,
            claim_ids=tuple(
                claim_id
                for paragraph in paragraphs
                for claim_id in paragraph.claim_ids
            ),
        )

    @staticmethod
    def _degrade_structured_document(
        document: StructuredSummaryDocument,
        assessment: SummaryQualityAssessment,
    ) -> tuple[StructuredSummaryDocument, SummaryQualityAssessment]:
        """Remove blocked paragraphs and append one explicit limitation."""
        assessment_by_id = {
            item.paragraph_id: item for item in assessment.paragraph_assessments
        }
        kept = tuple(
            paragraph
            for paragraph in document.paragraphs
            if not assessment_by_id.get(paragraph.paragraph_id)
            or not assessment_by_id[paragraph.paragraph_id].blockers
        )
        limitation = SummaryParagraph(
            section_id="limitations",
            paragraph_type="limitation",
            text=(
                "Some factual paragraphs were omitted because their evidence did "
                "not pass the configured semantic, factual, and citation gates."
            ),
        )
        final_paragraphs = (*kept, limitation)
        claim_ids = tuple(
            dict.fromkeys(
                claim_id
                for paragraph in kept
                for claim_id in paragraph.claim_ids
            )
        )
        final_document = StructuredSummaryDocument(
            task_id=document.task_id,
            paragraphs=final_paragraphs,
            claim_ids=claim_ids,
        )
        retained_assessments = tuple(
            assessment_by_id[item.paragraph_id]
            for item in kept
            if item.paragraph_id in assessment_by_id
        )
        limitation_assessment = ParagraphQualityAssessment(
            paragraph_id=limitation.paragraph_id,
            semantic_score=0.0,
            factual_score=0.0,
            citation_score=0.0,
            support_confidence=0.0,
            level="unverified",
            warnings=("quality_gate_degraded",),
        )
        claim_assessments = tuple(
            item
            for item in assessment.claim_assessments
            if item.claim_id in claim_ids
        )
        overall_score = (
            sum(item.support_confidence for item in retained_assessments)
            / len(retained_assessments)
            if retained_assessments
            else 0.0
        )
        return (
            final_document,
            SummaryQualityAssessment(
                passed=False,
                overall_score=overall_score,
                thresholds=assessment.thresholds,
                paragraph_assessments=(
                    *retained_assessments,
                    limitation_assessment,
                ),
                claim_assessments=claim_assessments,
                verifier=assessment.verifier,
                verifier_version=assessment.verifier_version,
                prompt_version=assessment.prompt_version,
            ),
        )

    @staticmethod
    def _strict_summary_quality(session: RunSession) -> bool:
        """Use fail-closed reporting only when the caller explicitly requests it."""
        return session.command.permission_mode == "strict"

    def _record_structured_quality(
        self,
        session: RunSession,
        *,
        bundle: ResearchIntelligenceBundle,
        document: StructuredSummaryDocument,
        assessment: SummaryQualityAssessment,
    ) -> None:
        """Persist quality output, binding hashes, and external JSON artifacts."""
        document_payload = document.as_dict()
        assessment_payload = assessment.as_dict()
        document_hash = self._json_hash(document_payload)
        evidence_hash = self._json_hash(
            [item.as_dict() for item in bundle.evidence]
        )
        session.metrics["summary_quality_binding"] = {
            "task_id": document.task_id,
            "document_hash": document_hash,
            "evidence_hash": evidence_hash,
            "claims_hash": self._json_hash(
                [item.as_dict() for item in bundle.claims]
            ),
            "assessment_hash": self._json_hash(assessment_payload),
            "passed": assessment.passed,
            "threshold_keys": sorted(assessment.thresholds),
        }
        descriptors = []
        if self._artifact_store is not None:
            payloads = (
                ArtifactPayload(
                    artifact_id="artifact_structured_summary_json",
                    artifact_type="structured_summary",
                    mime_type="application/json",
                    title="Structured research summary",
                    content=json.dumps(
                        document_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    source_ids=tuple(item.source_id for item in bundle.sources),
                ),
                ArtifactPayload(
                    artifact_id="artifact_quality_assessment_json",
                    artifact_type="quality_assessment",
                    mime_type="application/json",
                    title="Summary quality assessment",
                    content=json.dumps(
                        assessment_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    source_ids=tuple(item.source_id for item in bundle.sources),
                ),
            )
            descriptors = [
                self._artifact_store.put(session.run_id, payload)
                for payload in payloads
            ]
            merged = {
                item.artifact_id: item
                for item in (*bundle.artifact_manifest.artifacts, *descriptors)
            }
            bundle = replace(
                bundle,
                artifact_manifest=ArtifactManifestV2(
                    artifacts=tuple(merged.values())
                ),
            )
            session.replace_research_intelligence(bundle.as_dict())
        session.record_summary_quality(document_payload, assessment_payload)
        recorded = {
            event.payload.get("artifact_id")
            for event in session.events
            if event.kind is EventKind.ARTIFACT_READY
        }
        for descriptor in descriptors:
            if descriptor.artifact_id not in recorded:
                session.record_artifact(descriptor.as_dict())
                recorded.add(descriptor.artifact_id)

    def _quality_binding_is_valid(
        self,
        session: RunSession,
        *,
        bundle: ResearchIntelligenceBundle,
        document: StructuredSummaryDocument,
        assessment: SummaryQualityAssessment,
    ) -> bool:
        """Fail closed when restored quality output is stale or inconsistent."""
        expected_thresholds = {
            "semantic_score": session.command.config.summary_semantic_threshold,
            "factual_score": session.command.config.summary_factual_threshold,
            "citation_score": session.command.config.summary_citation_threshold,
            "overall_score": session.command.config.summary_overall_threshold,
        }
        if dict(assessment.thresholds) != expected_thresholds:
            return False
        paragraph_ids = {item.paragraph_id for item in document.paragraphs}
        assessed_paragraph_ids = {
            item.paragraph_id for item in assessment.paragraph_assessments
        }
        if paragraph_ids != assessed_paragraph_ids:
            return False
        if {item.claim_id for item in assessment.claim_assessments} != set(
            document.claim_ids
        ):
            return False
        claim_by_id = {item.claim_id: item for item in bundle.claims}
        for item in assessment.claim_assessments:
            claim = claim_by_id.get(item.claim_id)
            if claim is None:
                return False
            if item.verdict == "unverified" and (
                item.factual_score != 0.0 or item.support_confidence != 0.0
            ):
                return False
            if item.verdict in {"supported", "partial"} and not item.supporting_evidence_ids:
                return False
            if item.verdict == "contradicted" and not item.conflicting_evidence_ids:
                return False
            if not set(item.supporting_evidence_ids).issubset(claim.evidence_ids):
                return False
            if not set(item.conflicting_evidence_ids).issubset(
                claim.conflicting_evidence_ids
            ):
                return False
        paragraph_by_id = {
            item.paragraph_id: item for item in document.paragraphs
        }
        scorable = tuple(
            item
            for item in assessment.paragraph_assessments
            if paragraph_by_id[item.paragraph_id].paragraph_type != "limitation"
            and paragraph_by_id[item.paragraph_id].claim_ids
        )
        recomputed_overall = (
            sum(item.support_confidence for item in scorable) / len(scorable)
            if scorable
            else 0.0
        )
        if not math.isclose(
            assessment.overall_score,
            recomputed_overall,
            abs_tol=1e-9,
        ):
            return False
        if assessment.passed and (
            not scorable
            or assessment.overall_score < expected_thresholds["overall_score"]
            or any(item.blockers for item in assessment.paragraph_assessments)
        ):
            return False
        binding = session.metrics.get("summary_quality_binding")
        if not isinstance(binding, Mapping):
            return False
        return bool(
            binding.get("task_id") == document.task_id
            and binding.get("document_hash") == self._json_hash(document.as_dict())
            and binding.get("evidence_hash")
            == self._json_hash([item.as_dict() for item in bundle.evidence])
            and binding.get("claims_hash")
            == self._json_hash([item.as_dict() for item in bundle.claims])
            and binding.get("assessment_hash")
            == self._json_hash(assessment.as_dict())
            and binding.get("passed") is assessment.passed
            and binding.get("threshold_keys") == sorted(expected_thresholds)
        )

    @staticmethod
    def _json_hash(value: object) -> str:
        """Return one deterministic SHA-256 JSON binding."""
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _canonicalize_github_report(session: RunSession, report: str) -> str:
        """Keep report URLs and citations bound to the evidence ledger."""
        raw_generic = session.state.research_intelligence
        if isinstance(raw_generic, dict) and raw_generic.get("schema_version") == 2:
            try:
                bundle = ResearchIntelligenceBundle.from_dict(raw_generic)
                if not bundle.evidence_frozen:
                    bundle = replace(bundle, evidence_frozen=True)
                return validate_citations(report, bundle).sanitized_report or report
            except (TypeError, ValueError):
                return report
        raw_bundle = session.state.github_intelligence
        if not raw_bundle:
            return report
        try:
            github_bundle = github_evidence_bundle_from_dict(raw_bundle)
            return (
                canonicalize_github_report(report, github_bundle)
                if github_bundle
                else report
            )
        except (TypeError, ValueError):
            return report

    def _refresh_github_artifacts(self, session: RunSession, report: str) -> None:
        """Replace the deterministic HTML artifact with the final report text."""
        raw_generic = session.state.research_intelligence
        if isinstance(raw_generic, dict) and raw_generic.get("schema_version") == 2:
            try:
                bundle = ResearchIntelligenceBundle.from_dict(raw_generic)
                if not bundle.evidence_frozen:
                    bundle = replace(bundle, evidence_frozen=True)
                raw_legacy = session.state.github_intelligence
                legacy_bundle = github_evidence_bundle_from_dict(raw_legacy)
                if legacy_bundle is not None:
                    task_sources = [
                        str(task.sources_summary or "")
                        for task in session.state.todo_items
                        if task.sources_summary
                    ]
                    if (
                        legacy_bundle.coverage.gap_queries
                        and legacy_bundle.coverage.retry_count < 1
                    ):
                        legacy_bundle = supplement_github_evidence(
                            legacy_bundle,
                            task_sources,
                        )
                    legacy_bundle = render_github_artifacts(
                        freeze_github_evidence(legacy_bundle),
                        report_markdown=report,
                    )
                    session.replace_legacy_github_intelligence(
                        legacy_bundle.as_dict()
                    )
                    bundle = self._persist_legacy_github_artifacts(
                        session,
                        bundle=bundle,
                        legacy_bundle=legacy_bundle,
                    )
                if self._artifact_store is not None:
                    bundle = persist_research_artifacts(
                        self._artifact_store,
                        session.run_id,
                        bundle,
                        report_markdown=report,
                    )
                session.replace_research_intelligence(bundle.as_dict())
                recorded_artifacts = {
                    event.payload.get("artifact_id")
                    for event in session.events
                    if event.kind is EventKind.ARTIFACT_READY
                }
                for artifact in bundle.artifact_manifest.artifacts:
                    if artifact.artifact_id in recorded_artifacts:
                        continue
                    session.record_artifact(artifact.as_dict())
            except (TypeError, ValueError, OSError):
                logger.warning("Unable to persist generic research artifacts")
            return
        raw_bundle = session.state.github_intelligence
        if not raw_bundle:
            return
        try:
            github_bundle = github_evidence_bundle_from_dict(raw_bundle)
            if github_bundle is not None:
                task_sources = [
                    str(task.sources_summary or "")
                    for task in session.state.todo_items
                    if task.sources_summary
                ]
                if (
                    github_bundle.coverage.gap_queries
                    and github_bundle.coverage.retry_count < 1
                ):
                    github_bundle = supplement_github_evidence(
                        github_bundle,
                        task_sources,
                    )
                github_bundle = freeze_github_evidence(github_bundle)
                session.replace_github_intelligence(
                    render_github_artifacts(
                        github_bundle,
                        report_markdown=report,
                    ).as_dict()
                )
        except (TypeError, ValueError):
            logger.warning("Unable to refresh GitHub artifacts")

    def _persist_legacy_github_artifacts(
        self,
        session: RunSession,
        *,
        bundle: ResearchIntelligenceBundle,
        legacy_bundle: GitHubEvidenceBundle,
    ) -> ResearchIntelligenceBundle:
        """Write every GitHub v1 inline artifact represented by a v2 descriptor."""
        if self._artifact_store is None or not legacy_bundle.artifacts:
            return bundle
        descriptors_by_id = {
            item.artifact_id: item for item in bundle.artifact_manifest.artifacts
        }
        for artifact in legacy_bundle.artifacts:
            mapped = descriptors_by_id.get(artifact.artifact_id)
            descriptor = self._artifact_store.put(
                session.run_id,
                ArtifactPayload(
                    artifact_id=artifact.artifact_id,
                    artifact_type=artifact.artifact_type,
                    mime_type=artifact.mime_type,
                    title=artifact.title or artifact.artifact_type,
                    description=artifact.description,
                    content=artifact.content,
                    source_ids=mapped.source_ids if mapped is not None else (),
                ),
            )
            descriptors_by_id[descriptor.artifact_id] = descriptor
        return replace(
            bundle,
            artifact_manifest=ArtifactManifestV2(
                artifacts=tuple(descriptors_by_id.values())
            ),
        )

    def _execute_governed(
        self,
        session: RunSession,
        prior_context: FollowupContext | None,
        *,
        operations: GovernedOperations,
        root_scope: OperationScope,
        run_search_adapter: HelloAgentsSearchAdapter | None,
        resume_from_checkpoint: bool = False,
    ) -> None:
        """Execute the workflow without storing run scope on the coordinator."""
        session.raise_if_run_controlled()
        if (
            session.command.research_profile_id == "web.evidence.v1"
            and not session.command.config.enable_evidence_web
        ):
            session.state.research_profile_id = "web.default.v1"
            session.metrics["evidence_web"] = {
                "enabled": False,
                "outcome": "compatibility_fallback",
                "reason": "feature_disabled",
            }

        planning_state = session.state
        checkpoint_phase = session.checkpoint_phase
        resuming_tasks = (
            resume_from_checkpoint
            and checkpoint_phase
            in {"planning_completed", "research_tasks_progress"}
            and bool(session.state.todo_items)
        )
        prepared_research: PreparedResearch | None = None
        if resuming_tasks:
            planned = [TodoItem(**task.to_dict()) for task in session.state.todo_items]
            pending_tasks = [
                task
                for task in planned
                if task.status in {"pending", "in_progress"}
            ]
            self._create_task_notes_for_tasks(
                [task for task in pending_tasks if not task.note_id],
                cancellation=session.cancellation,
                operations=operations,
            )
        else:
            session.raise_if_run_controlled()
            if self._should_use_research_kernel(session):
                prepared_research = self._prepare_kernel_research(
                    session,
                    operation_scope=root_scope,
                )
                session.raise_if_run_controlled()
                self._install_kernel_baseline(session, prepared_research)
                tasks = [
                    TodoItem(
                        id=item.id,
                        title=item.title,
                        intent=item.intent,
                        query=item.query,
                        source_strategy=item.source_strategy,
                        repository=item.repository,
                    )
                    for item in prepared_research.tasks
                ]
            else:
                github_contexts = self._prepare_github_contexts(
                    planning_state,
                    config=session.command.config,
                    operation_scope=root_scope,
                )
                session.raise_if_run_controlled()
                if github_contexts:
                    github_context = github_contexts[0]
                    serialized = self._serialize_github_contexts(github_contexts)
                    repository_event = self._github_repository_event(github_context)
                    session.record_repository(
                        github_context=serialized,
                        repository=dict(repository_event["repository"]),
                        notices=list(repository_event["notices"]),
                        notice_codes=list(repository_event["notice_codes"]),
                    )
                    bundle = render_github_artifacts(
                        build_github_evidence_bundle(github_contexts)
                    )
                    session.record_github_intelligence(
                        bundle.as_dict(),
                        artifact_count=len(bundle.artifacts),
                    )
                    for artifact in bundle.artifacts:
                        session.record_artifact(artifact.as_dict())
                    tasks = self._create_github_research_tasks(
                        github_context.target,
                        comparison_targets=[context.target for context in github_contexts[1:]],
                    )
                else:
                    assembled_prior = ResearchContextAssembler().assemble(prior_context)
                    related_history = session.related_history_context or None
                    user_memories = session.user_memory_context or None
                    session.raise_if_run_controlled()
                    planner_kwargs: dict[str, object] = {
                        "prior_context": assembled_prior,
                    }
                    if related_history:
                        planner_kwargs["related_history"] = related_history
                    if user_memories:
                        planner_kwargs["user_memories"] = user_memories
                    tasks = _call_with_operation_scope(
                        self.planner.plan_todo_list,
                        planning_state,
                        **planner_kwargs,
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
            session.persist_checkpoint("planning_completed")
        work_items = self._prepare_work_items(
            session,
            operations=operations,
            search_adapter=run_search_adapter,
            research_kernel=self._research_kernel if prepared_research else None,
            prepared_research=prepared_research,
        )
        search_results = self._execute_work_items(session, work_items)
        session.raise_if_run_controlled()
        if prepared_research is not None:
            self._finalize_kernel_research(
                session,
                prepared_research,
                search_results=search_results,
            )
            session.raise_if_run_controlled()
        elif session.command.config.enable_summary_quality_shadow:
            session.metrics["summary_quality_shadow"] = {
                "enabled": True,
                "blocking": False,
                "outcome": "not_applicable",
                "reason": "no_evidence_bundle",
            }
        session.persist_checkpoint("evidence_completed")
        self._generate_report(
            session,
            operations=operations,
            root_scope=root_scope,
        )

    def _prepare_work_items(
        self,
        session: RunSession,
        *,
        operations: GovernedOperations,
        search_adapter: HelloAgentsSearchAdapter | None,
        research_kernel: ResearchKernel | None = None,
        prepared_research: PreparedResearch | None = None,
    ) -> list[_TaskWorkItem]:
        """Read note inputs on the coordinator thread and detach all worker data."""
        github_markdown = str(
            (session.state.github_context or {}).get("markdown") or ""
        )
        items: list[_TaskWorkItem] = []
        dimension_by_task = {
            item.id: item.dimension
            for item in (prepared_research.tasks if prepared_research else ())
        }
        for index, task in enumerate(session.state.todo_items):
            session.raise_if_run_controlled()
            if task.status in {"completed", "failed", "skipped", "cancelled"}:
                continue
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
                    dimension=dimension_by_task.get(task.id, "overview"),
                    github_markdown=github_markdown,
                    loop_offset=index * 3,
                    config=session.command.config,
                    operations=operations,
                    search_adapter=search_adapter,
                    research_kernel=research_kernel,
                    prepared_research=prepared_research,
                )
            )
        return items

    def _execute_work_items(
        self,
        session: RunSession,
        work_items: list[_TaskWorkItem],
    ) -> list[SourceSearchResult]:
        """Run detached workers with bounded submissions and coordinator-only merges."""
        if not work_items:
            return []
        search_results: list[SourceSearchResult] = []
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
                        search_results=search_results,
                    )
                    if message.kind in {
                        _WorkerMessageKind.COMPLETED,
                        _WorkerMessageKind.SKIPPED,
                        _WorkerMessageKind.FAILED,
                    }:
                        session.persist_checkpoint("research_tasks_progress")

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
                        session.persist_checkpoint("research_tasks_progress")
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
                    search_results=search_results,
                )
            session.raise_if_run_controlled()
        except _OPERATION_CONTROL_ERRORS:
            stop_event.set()
            session.request_cancellation()
            raise
        finally:
            stop_event.set()
            executor.shutdown(wait=True, cancel_futures=True)
        return search_results

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
            if item.research_kernel is not None and item.prepared_research is not None:
                typed_search = _call_with_operation_scope(
                    item.research_kernel.search,
                    item.prepared_research,
                    query=query,
                    topic=item.topic,
                    config=item.config,
                    loop_count=item.loop_offset + attempt,
                    use_cache=False,
                    operation_scope=operation_scope,
                )
                if not isinstance(typed_search, SourceSearchResult):
                    raise TypeError("Research kernel returned an invalid search result.")
                search_result = {
                    "results": [dict(result) for result in typed_search.results],
                    "answer": typed_search.answer,
                    "backend": typed_search.backend,
                    "notice_codes": list(typed_search.notice_codes),
                }
                notices = list(typed_search.notices)
                answer_text = typed_search.answer
                backend = typed_search.backend
            else:
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
            raw_results = search_result.get("results")
            typed_results = raw_results if isinstance(raw_results, list) else []
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
                        "_source_search_result": SourceSearchResult(
                            provider_id=(
                                typed_search.provider_id
                                if item.research_kernel is not None
                                and item.prepared_research is not None
                                else "web"
                            ),
                            results=tuple(
                                {
                                    **dict(result),
                                    "dimension": item.dimension,
                                }
                                for result in typed_results
                                if isinstance(result, Mapping)
                            ),
                            answer=(
                                answer_text if isinstance(answer_text, str) else None
                            ),
                            backend=(backend if isinstance(backend, str) else "none"),
                            notices=tuple(safe_notices),
                            notice_codes=tuple(safe_notice_codes),
                        ),
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
        search_results: list[SourceSearchResult] | None = None,
    ) -> None:
        """Apply one worker result through canonical transitions on this thread."""
        session.raise_if_run_controlled()
        payload = dict(message.payload)
        if message.kind is _WorkerMessageKind.SOURCES:
            context = payload.pop("context", None)
            typed_search = payload.pop("_source_search_result", None)
            if isinstance(typed_search, SourceSearchResult) and search_results is not None:
                search_results.append(typed_search)
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

    def _should_use_research_kernel(self, session: RunSession) -> bool:
        """Resolve the explicit-mode-first boundary for the shared kernel."""
        command = session.command
        if command.research_profile_id is not None:
            try:
                profile = self._research_kernel.profile_registry.get(
                    command.research_profile_id
                )
            except KeyError:
                return True
            if profile.profile_id == "web.evidence.v1":
                return command.config.enable_evidence_web
            return profile.mode is not ResearchMode.WEB
        if command.research_mode is not None:
            if command.research_mode is ResearchMode.WEB:
                return command.config.enable_evidence_web
            return True
        if not command.config.enable_github_research:
            return False
        return bool(parse_github_repositories(command.topic))

    def _prepare_kernel_research(
        self,
        session: RunSession,
        *,
        operation_scope: OperationScope,
    ) -> PreparedResearch:
        """Prepare one explicit or automatically detected research profile."""
        command = session.command
        mode = command.research_mode
        profile_id = command.research_profile_id
        if mode is None and profile_id is not None:
            mode = self._research_kernel.profile_registry.get(profile_id).mode
        if mode is None:
            mode = ResearchMode.GITHUB
        if profile_id is None and mode is ResearchMode.GITHUB:
            profile_id = "github.repository.v1"
        if profile_id is None and mode is ResearchMode.WEB:
            profile_id = "web.evidence.v1"
        return self._research_kernel.prepare(
            command.topic,
            mode=mode,
            profile_id=profile_id,
            run_id=session.run_id,
            config=command.config,
            cancellation=session.cancellation,
            operation_scope=operation_scope,
        )

    def _install_kernel_baseline(
        self,
        session: RunSession,
        prepared: PreparedResearch,
    ) -> None:
        """Persist the kernel's source context before task execution for recovery."""
        if not prepared.targets:
            raise ValueError("Research kernel did not produce a source target.")
        serialized = self._serialize_kernel_contexts(prepared)
        primary = serialized[0]
        source_context = dict(prepared.source_context)
        source_context["target"] = dict(primary.get("target") or {})
        if prepared.profile.mode is ResearchMode.GITHUB:
            source_context["repositories"] = serialized
            source_context["markdown"] = "\n\n".join(
                str(item.get("markdown") or "")
                for item in serialized
                if item.get("markdown")
            )
            repository_event = self._kernel_repository_event(primary)
            session.record_repository(
                github_context=source_context,
                repository=repository_event["repository"],
                notices=repository_event["notices"],
                notice_codes=repository_event["notice_codes"],
            )
        else:
            session.record_source_context(
                source_context,
                provider_ids=(prepared.provider.provider_id,),
                source_count=len(prepared.targets),
                research_mode=prepared.profile.mode,
                profile_id=prepared.profile.profile_id,
            )
        baseline = self._research_kernel.finalize(prepared)
        session.replace_research_intelligence(baseline.as_dict())
        legacy_bundle = self._legacy_bundle_for_kernel(prepared)
        if legacy_bundle is not None:
            session.replace_legacy_github_intelligence(legacy_bundle.as_dict())

    def _finalize_kernel_research(
        self,
        session: RunSession,
        prepared: PreparedResearch,
        *,
        search_results: Sequence[SourceSearchResult] = (),
    ) -> None:
        """Finalize provider evidence after workers and emit the canonical events."""
        task_results: list[object] = []
        legacy_task_sources: list[str] = []
        dimension_by_task = {item.id: item.dimension for item in prepared.tasks}
        for task in session.state.todo_items:
            if task.summary:
                legacy_task_sources.append(task.summary)
                task_results.append(
                    {
                        "summary": task.summary,
                        "dimension": dimension_by_task.get(task.id, "overview"),
                    }
                )
            if task.sources_summary:
                legacy_task_sources.append(task.sources_summary)
        captured_collections: tuple[SourceCollection, ...] = ()
        if prepared.profile.profile_id == "web.evidence.v1":
            captured_collections = self._research_kernel.collect_search_evidence(
                prepared,
                search_results,
            )
        snapshot_descriptors = self._persist_web_snapshots(
            session,
            captured_collections,
        )
        bundle = self._research_kernel.finalize(
            prepared,
            task_results=task_results,
            collections=captured_collections,
        )
        if snapshot_descriptors:
            bundle = replace(
                bundle,
                artifact_manifest=ArtifactManifestV2(
                    artifacts=tuple(
                        {
                            item.artifact_id: item
                            for item in (
                                *bundle.artifact_manifest.artifacts,
                                *snapshot_descriptors,
                            )
                        }.values()
                    )
                ),
            )
        session.record_research_intelligence(
            bundle.as_dict(),
            provider_ids=(prepared.provider.provider_id,),
        )
        legacy_bundle = self._legacy_bundle_for_kernel(
            prepared,
            task_sources=tuple(legacy_task_sources),
        )
        if legacy_bundle is not None:
            session.replace_legacy_github_intelligence(legacy_bundle.as_dict())

    def _persist_web_snapshots(
        self,
        session: RunSession,
        collections: Sequence[SourceCollection],
    ) -> tuple[Any, ...]:
        """Write captured Web snapshot bodies and emit descriptor-only events."""
        if self._artifact_store is None:
            return ()
        descriptors = []
        recorded = {
            event.payload.get("artifact_id")
            for event in session.events
            if event.kind is EventKind.ARTIFACT_READY
        }
        for collection in collections:
            capture = collection.provider_payload
            if not isinstance(capture, WebCaptureResult) or capture.snapshot is None:
                continue
            snapshot = capture.snapshot
            artifact_id = f"artifact_web_snapshot_{snapshot.content_hash[:24]}"
            descriptor = self._artifact_store.put(
                session.run_id,
                ArtifactPayload(
                    artifact_id=artifact_id,
                    artifact_type="web_page_snapshot",
                    mime_type=snapshot.mime_type,
                    title=snapshot.page_title,
                    description="Normalized captured Web page.",
                    content=snapshot.content,
                    source_ids=(snapshot.source_id,),
                ),
            )
            descriptors.append(descriptor)
            if descriptor.artifact_id not in recorded:
                session.record_artifact(descriptor.as_dict())
                recorded.add(descriptor.artifact_id)
        return tuple(descriptors)

    @staticmethod
    def _serialize_kernel_contexts(
        prepared: PreparedResearch,
    ) -> list[dict[str, Any]]:
        """Project provider payloads into the existing safe GitHub context shape."""
        serialized: list[dict[str, Any]] = []
        for collection in prepared.collections:
            target = collection.target
            payload = collection.provider_payload
            if isinstance(payload, GitHubRepositoryContext):
                serialized.append(
                    DeepResearchAgent._serialize_github_context(payload)
                )
                continue

            def value(name: str, default: object = None) -> object:
                if isinstance(payload, Mapping):
                    return payload.get(name, default)
                return getattr(payload, name, default)

            def mappings(name: str) -> list[dict[str, Any]]:
                raw = value(name, ())
                if not isinstance(raw, (list, tuple)):
                    return []
                return [dict(item) for item in raw if isinstance(item, Mapping)]

            raw_repository = value("repository", {})
            repository = (
                dict(raw_repository)
                if isinstance(raw_repository, Mapping)
                else {}
            )
            raw_notices = value("notices", collection.notices)
            raw_codes = value("notice_codes", collection.notice_codes)
            notice_values: Sequence[object] = (
                tuple(raw_notices)
                if isinstance(raw_notices, (list, tuple))
                else ()
            )
            code_values: Sequence[object] = (
                tuple(raw_codes)
                if isinstance(raw_codes, (list, tuple))
                else ()
            )
            notices, notice_codes = _safe_github_notice_fields(
                notice_values,
                code_values,
            )
            if collection.collection_status == "failed":
                notices = [_GITHUB_CONTEXT_FAILED_MESSAGE]
                notice_codes = [_GITHUB_CONTEXT_FAILED_CODE]
            markdown = value("markdown", "")
            if not isinstance(markdown, str) or not markdown.strip():
                markdown = (
                    "## GitHub Repository Context\n"
                    f"- Repository: {target.source_id}\n"
                    f"- URL: {target.canonical_url}"
                )
            owner, separator, repo = target.source_id.partition("/")
            serialized.append(
                {
                    "target": {
                        "owner": target.metadata.get("owner", owner),
                        "repo": target.metadata.get("repo", repo if separator else target.source_id),
                        "full_name": target.source_id,
                        "url": target.canonical_url,
                    },
                    "commit_sha": collection.resolved_version,
                    "repository": repository,
                    "file_manifest": mappings("file_manifest"),
                    "file_contents": mappings("file_contents"),
                    "languages": (
                        dict(raw_languages)
                        if isinstance(
                            raw_languages := value("languages", {}),
                            Mapping,
                        )
                        else {}
                    ),
                    "contributors": mappings("contributors"),
                    "commits": mappings("commits"),
                    "issues": mappings("issues"),
                    "pull_requests": mappings("pull_requests"),
                    "releases": mappings("releases"),
                    "notices": notices,
                    "notice_codes": notice_codes,
                    "markdown": markdown,
                }
            )
        return serialized

    @staticmethod
    def _kernel_repository_event(primary: Mapping[str, Any]) -> dict[str, Any]:
        """Build the allowlisted legacy repository event from kernel context."""
        target = primary.get("target")
        target_mapping = target if isinstance(target, Mapping) else {}
        repository = primary.get("repository")
        repository_mapping = repository if isinstance(repository, Mapping) else {}
        projected = {
            "owner": target_mapping.get("owner"),
            "repo": target_mapping.get("repo"),
            "full_name": target_mapping.get("full_name"),
            "url": target_mapping.get("url"),
            "stars": repository_mapping.get("stars"),
            "forks": repository_mapping.get("forks"),
            "open_issues": repository_mapping.get("open_issues"),
            "default_branch": repository_mapping.get("default_branch"),
            "language": repository_mapping.get("language"),
        }
        raw_notices = primary.get("notices")
        raw_notice_codes = primary.get("notice_codes")
        notice_values: Sequence[object] = (
            tuple(raw_notices)
            if isinstance(raw_notices, (list, tuple))
            else ()
        )
        code_values: Sequence[object] = (
            tuple(raw_notice_codes)
            if isinstance(raw_notice_codes, (list, tuple))
            else ()
        )
        notices, notice_codes = _safe_github_notice_fields(
            notice_values,
            code_values,
        )
        return {
            "repository": projected,
            "notices": notices,
            "notice_codes": notice_codes,
        }

    @staticmethod
    def _legacy_bundle_for_kernel(
        prepared: PreparedResearch,
        *,
        task_sources: Sequence[str] = (),
        report_markdown: str = "",
    ) -> Any | None:
        """Create the v1 GitHub payload only at the compatibility boundary."""
        contexts: list[GitHubRepositoryContext] = []
        for collection in prepared.collections:
            payload = collection.provider_payload
            if not isinstance(payload, GitHubRepositoryContext):
                continue
            notices, notice_codes = _safe_github_notice_fields(
                collection.notices or payload.notices,
                collection.notice_codes or payload.notice_codes,
            )
            contexts.append(
                replace(
                    payload,
                    notices=notices,
                    notice_codes=notice_codes,
                )
            )
        if len(contexts) != len(prepared.collections) or not contexts:
            return None
        try:
            bundle = build_github_evidence_bundle(
                contexts,
                task_sources=task_sources,
            )
            return render_github_artifacts(
                bundle,
                report_markdown=report_markdown,
            )
        except (TypeError, ValueError):
            logger.warning("Unable to build the legacy GitHub compatibility projection")
            return None

    # ------------------------------------------------------------------
    # GitHub research helpers
    # ------------------------------------------------------------------

    def _prepare_github_context(
        self,
        state: SummaryState,
        *,
        config: Configuration | None = None,
        operation_scope: OperationScope,
        target: GitHubRepositoryTarget | None = None,
    ) -> GitHubRepositoryContext | None:
        """Collect GitHub repository context when the topic names a repository."""
        run_config = config or self.config
        if not getattr(run_config, "enable_github_research", True):
            return None

        target = target or parse_github_repository(state.research_topic)
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

    def _prepare_github_contexts(
        self,
        state: SummaryState,
        *,
        config: Configuration | None = None,
        operation_scope: OperationScope,
    ) -> list[GitHubRepositoryContext]:
        """Collect up to five distinct GitHub repositories for comparison mode."""
        targets = parse_github_repositories(state.research_topic)
        contexts: list[GitHubRepositoryContext] = []
        for target in targets:
            context = self._prepare_github_context(
                state,
                config=config,
                operation_scope=operation_scope,
                target=target,
            )
            if context is not None:
                contexts.append(context)
        return contexts

    @staticmethod
    def _create_github_research_tasks(
        target: GitHubRepositoryTarget,
        comparison_targets: Sequence[GitHubRepositoryTarget] = (),
    ) -> list[TodoItem]:
        """Render the versioned GitHub Profile into legacy TODO objects."""
        profile = built_in_profile_registry().get("github.repository.v1")
        rendered = profile.render_tasks(
            repository=target.full_name,
            comparison_repositories=tuple(item.full_name for item in comparison_targets),
        )
        return [
            TodoItem(
                id=item.id,
                title=item.title,
                intent=item.intent,
                query=item.query,
                source_strategy=item.source_strategy,
                repository=item.repository,
            )
            for item in rendered
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
            "commit_sha": context.commit_sha,
            "repository": dict(context.repository),
            "file_manifest": [dict(item) for item in context.file_manifest],
            "file_contents": [dict(item) for item in context.file_contents],
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

    @classmethod
    def _serialize_github_contexts(
        cls,
        contexts: Sequence[GitHubRepositoryContext],
    ) -> dict[str, Any]:
        """Serialize one or more contexts while preserving legacy primary fields."""
        serialized = [cls._serialize_github_context(context) for context in contexts]
        primary = dict(serialized[0]) if serialized else {}
        primary["repositories"] = serialized
        primary["markdown"] = "\n\n".join(
            str(item.get("markdown") or "") for item in serialized if item.get("markdown")
        )
        return primary

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
