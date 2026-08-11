"""Bounded compatibility facade over the canonical application lifecycle."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Full, Queue
from threading import BoundedSemaphore, Event, Lock
from typing import Any, Iterator

from agent import DeepResearchAgent
from config import Configuration
from research.application import RecoveryFailure, ResearchApplicationService
from research.contracts import ResearchCommand, ResearchEvent, ResearchRunResult
from research.history import ResearchHistoryStore
from research.legacy_sse import LegacySseProjector
from research.memory import (
    UserMemory,
    UserMemoryStore,
)
from research.ports import RunRepository
from research.repository import FileRunRepository
from research.session import CancellationToken

from .models import HarnessRunRequest, HarnessRunResult
from .policy import HarnessPolicy

_QUEUE_POLL_SECONDS = 0.05
_TERMINAL_TYPES = frozenset({"done", "error"})
_SAFE_ERROR_CODES = frozenset(
    {
        "application_error",
        "cancelled",
        "deadline_exceeded",
        "context_projection_failed",
        "checkpoint_corrupt",
        "checkpoint_not_found",
        "checkpoint_persistence_failed",
        "checkpoint_not_resumable",
        "checkpoint_version_unsupported",
        "coordinator_failed",
        "invalid_command",
        "invalid_run_id",
        "missing_terminal",
        "parent_corrupt",
        "parent_not_found",
        "parent_not_resumable",
        "parent_pending",
        "operation_rejected",
        "persistence_failed",
        "policy_error",
        "policy_rejected",
        "report_incomplete",
        "recovery_state_conflict",
        "recovery_unsupported",
        "repository_error",
        "run_already_active",
        "run_failed",
        "run_not_resumable",
        "run_rejected",
        "runner_busy",
        "terminal_validation_failed",
    }
)


class _RunAlreadyReservedError(RuntimeError):
    """Signal one facade-local in-flight run ID reservation conflict."""


class _ParentRunPendingError(RuntimeError):
    """Signal an active parent whose durable snapshot is not available yet."""


@dataclass(slots=True)
class _Submission:
    """Track one application Future and its completed facade release."""

    future: Future[ResearchRunResult]
    released: Event = field(default_factory=Event)
    terminal_wakeup: Event = field(default_factory=Event)


class _HarnessStreamIterator:
    """Thread-safe closeable iterator over one canonical application future."""

    def __init__(
        self,
        *,
        run_id: str,
        cancellation: CancellationToken,
        closed: Event,
        event_queue: Queue[ResearchEvent],
        projector: LegacySseProjector,
        submission: _Submission | None,
        initial_error: dict[str, Any] | None = None,
    ) -> None:
        self._run_id = run_id
        self._cancellation = cancellation
        self._closed = closed
        self._event_queue = event_queue
        self._projector = projector
        self._submission = submission
        self._initial_error = initial_error
        self._lock = Lock()
        self._terminal_emitted = False
        self._exhausted = False
        self._last_sequence = 0

    def __iter__(self) -> _HarnessStreamIterator:
        return self

    def __next__(self) -> dict[str, Any]:
        with self._lock:
            if self._closed.is_set() or self._exhausted:
                raise StopIteration
            if self._initial_error is not None:
                initial_event = self._initial_error
                self._initial_error = None
                self._record_event_locked(initial_event)
                return initial_event

        submission = self._submission
        if submission is None:
            raise StopIteration
        future = submission.future

        while not self._closed.is_set():
            try:
                event = self._event_queue.get(timeout=_QUEUE_POLL_SECONDS)
            except Empty:
                if not future.done():
                    continue
                try:
                    event = self._event_queue.get_nowait()
                except Empty:
                    return self._future_terminal(submission)

            projected = self._projector.project(event)
            if projected is None:
                continue
            if projected.get("type") in _TERMINAL_TYPES:
                self._wait_for_terminal_release(submission)
            with self._lock:
                if self._closed.is_set():
                    raise StopIteration
                self._record_event_locked(projected)
            return projected

        raise StopIteration

    def close(self) -> None:
        """Cancel nonterminal work and wake a concurrently blocked ``next``."""
        with self._lock:
            if self._closed.is_set():
                return
            self._closed.set()
            should_cancel = not self._terminal_emitted
            submission = self._submission
        if submission is not None:
            submission.terminal_wakeup.set()
        if should_cancel:
            self._cancellation.cancel()
            if submission is not None:
                submission.future.cancel()

    def _future_terminal(
        self,
        submission: _Submission,
    ) -> dict[str, Any]:
        future = submission.future
        try:
            result = future.result()
        except RecoveryFailure as exc:
            code = exc.code
            detail = exc.safe_message
            last_resumable_parent = None
        except Exception:
            code = "application_error"
            detail = "Research execution failed."
            last_resumable_parent = None
        else:
            code = result.error.code if result.error is not None else "missing_terminal"
            detail = "Research execution ended without a terminal event."
            last_resumable_parent = result.last_resumable_parent
        self._wait_for_terminal_release(submission)
        with self._lock:
            if self._closed.is_set():
                raise StopIteration
            event = HarnessRunner._error_event(
                self._run_id,
                code=code,
                detail=detail,
                sequence=self._last_sequence + 1,
                last_resumable_parent=last_resumable_parent,
            )
            self._record_event_locked(event)
            return event

    def _wait_for_terminal_release(self, submission: _Submission) -> None:
        """Wait without polling until release completes or close wakes the stream."""
        if not submission.released.is_set():
            submission.terminal_wakeup.wait()
        if self._closed.is_set():
            raise StopIteration
        if not submission.released.is_set():  # pragma: no cover - invariant guard
            raise RuntimeError("Terminal wakeup occurred before facade release.")

    def _record_event_locked(self, event: dict[str, Any]) -> None:
        sequence = event.get("sequence")
        if isinstance(sequence, int) and not isinstance(sequence, bool):
            self._last_sequence = max(self._last_sequence, sequence)
        if event.get("type") in _TERMINAL_TYPES:
            self._terminal_emitted = True
            self._exhausted = True


@dataclass(kw_only=True)
class HarnessRunner:
    """Adapt one application lifecycle to historical run and stream APIs."""

    application: ResearchApplicationService
    repository: RunRepository
    max_workers: int = 1
    queue_capacity: int = 64
    admission_capacity: int | None = None
    history_store: ResearchHistoryStore | None = None
    memory_store: UserMemoryStore | None = None
    _executor: ThreadPoolExecutor = field(init=False, repr=False)
    _admission: BoundedSemaphore = field(init=False, repr=False)
    _projector: LegacySseProjector = field(init=False, repr=False)
    _reservation_lock: Lock = field(init=False, repr=False)
    _reserved_run_ids: set[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Validate capacity and initialize bounded compatibility resources."""
        if self.max_workers < 1:
            raise ValueError("Harness max_workers must be positive.")
        if self.queue_capacity < 1:
            raise ValueError("Harness queue_capacity must be positive.")
        capacity = self.admission_capacity
        if capacity is None:
            capacity = self.max_workers
            self.admission_capacity = capacity
        if capacity < 1:
            raise ValueError("Harness admission_capacity must be positive.")
        if capacity > self.max_workers:
            raise ValueError(
                "Harness admission_capacity cannot exceed max_workers."
            )

        application_repository = getattr(self.application, "repository", None)
        if application_repository is not self.repository:
            raise ValueError(
                "HarnessRunner and ResearchApplicationService must share one repository."
            )

        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="research-application",
        )
        self._admission = BoundedSemaphore(capacity)
        self._projector = LegacySseProjector()
        self._reservation_lock = Lock()
        self._reserved_run_ids = set()

    @classmethod
    def build_default(cls, *, base_path: str | Path = "./runs") -> HarnessRunner:
        """Compose the production coordinator, application, policy, and repository."""
        repository = FileRunRepository(base_path)
        history_store = ResearchHistoryStore(repository)
        memory_store = UserMemoryStore(repository.root)
        policy = HarnessPolicy()
        coordinator = DeepResearchAgent(
            config=Configuration.from_env(),
            operation_authorizer=policy,
        )
        application = ResearchApplicationService(
            coordinator=coordinator,
            repository=repository,
            policy=policy,
            history_store=history_store,
            memory_store=memory_store,
        )
        # Keep one top-level run active per shared coordinator. Task workers still
        # use the configured bounded parallelism within that run, while this
        # conservative boundary avoids interleaving stateful role-agent histories.
        return cls(
            application=application,
            repository=repository,
            max_workers=1,
            history_store=history_store,
            memory_store=memory_store,
        )

    def run(self, request: HarnessRunRequest) -> HarnessRunResult:
        """Execute the canonical application exactly once and adapt its result."""
        cancellation = CancellationToken()
        try:
            submission = self._submit(
                request,
                observer=lambda event: None,
                cancellation=cancellation,
            )
        except _RunAlreadyReservedError:
            return self._error_result(
                request.run_id,
                code="run_already_active",
                message="A run with this ID is already active.",
            )
        except _ParentRunPendingError:
            return self._error_result(
                request.run_id,
                code="parent_pending",
                message="Parent run is active but not yet durable.",
            )
        except Exception:
            return self._error_result(
                request.run_id,
                code="application_error",
                message="Research execution failed.",
            )
        if submission is None:
            return self._error_result(
                request.run_id,
                code="runner_busy",
                message="Research execution capacity is busy.",
            )
        try:
            result = submission.future.result()
        except RecoveryFailure as exc:
            submission.released.wait()
            return self._error_result(
                request.run_id,
                code=exc.code,
                message=exc.safe_message,
            )
        except Exception:
            submission.released.wait()
            return self._error_result(
                request.run_id,
                code="application_error",
                message="Research execution failed.",
            )
        submission.released.wait()
        return self._adapt_result(result)

    def resume(self, run_id: str) -> HarnessRunResult:
        """Recover one persisted failed run from its trusted checkpoint."""
        cancellation = CancellationToken()
        try:
            submission = self._submit_recovery(
                run_id,
                observer=lambda event: None,
                cancellation=cancellation,
            )
        except RecoveryFailure as exc:
            return self._error_result(
                run_id,
                code=exc.code,
                message=exc.safe_message,
            )
        except _RunAlreadyReservedError:
            return self._error_result(
                run_id,
                code="run_already_active",
                message="A run with this ID is already active.",
            )
        except Exception:
            return self._error_result(
                run_id,
                code="application_error",
                message="Research recovery failed.",
            )
        if submission is None:
            return self._error_result(
                run_id,
                code="runner_busy",
                message="Research execution capacity is busy.",
            )
        try:
            result = submission.future.result()
        except RecoveryFailure as exc:
            submission.released.wait()
            return self._error_result(
                run_id,
                code=exc.code,
                message=exc.safe_message,
            )
        except Exception:
            submission.released.wait()
            return self._error_result(
                run_id,
                code="application_error",
                message="Research recovery failed.",
            )
        submission.released.wait()
        return self._adapt_result(result)

    def stream(self, request: HarnessRunRequest) -> Iterator[dict[str, Any]]:
        """Return a closeable iterator over one bounded canonical execution."""
        cancellation = CancellationToken()
        event_queue: Queue[ResearchEvent] = Queue(maxsize=self.queue_capacity)
        closed = Event()

        def observe(event: ResearchEvent) -> None:
            while not closed.is_set():
                try:
                    event_queue.put(event, timeout=_QUEUE_POLL_SECONDS)
                    return
                except Full:
                    continue

        try:
            submission = self._submit(
                request,
                observer=observe,
                cancellation=cancellation,
            )
        except _RunAlreadyReservedError:
            return _HarnessStreamIterator(
                run_id=request.run_id,
                cancellation=cancellation,
                closed=closed,
                event_queue=event_queue,
                projector=self._projector,
                submission=None,
                initial_error=self._error_event(
                    request.run_id,
                    code="run_already_active",
                    detail="A run with this ID is already active.",
                ),
            )
        except _ParentRunPendingError:
            return _HarnessStreamIterator(
                run_id=request.run_id,
                cancellation=cancellation,
                closed=closed,
                event_queue=event_queue,
                projector=self._projector,
                submission=None,
                initial_error=self._error_event(
                    request.run_id,
                    code="parent_pending",
                    detail="Parent run is active but not yet durable.",
                ),
            )
        except Exception:
            return _HarnessStreamIterator(
                run_id=request.run_id,
                cancellation=cancellation,
                closed=closed,
                event_queue=event_queue,
                projector=self._projector,
                submission=None,
                initial_error=self._error_event(
                    request.run_id,
                    code="application_error",
                    detail="Research execution failed.",
                ),
            )

        if submission is None:
            return _HarnessStreamIterator(
                run_id=request.run_id,
                cancellation=cancellation,
                closed=closed,
                event_queue=event_queue,
                projector=self._projector,
                submission=None,
                initial_error=self._error_event(
                    request.run_id,
                    code="runner_busy",
                    detail="Research execution capacity is busy.",
                ),
            )

        return _HarnessStreamIterator(
            run_id=request.run_id,
            cancellation=cancellation,
            closed=closed,
            event_queue=event_queue,
            projector=self._projector,
            submission=submission,
        )

    def resume_stream(self, run_id: str) -> Iterator[dict[str, Any]]:
        """Stream recovery events for one persisted run."""
        cancellation = CancellationToken()
        event_queue: Queue[ResearchEvent] = Queue(maxsize=self.queue_capacity)
        closed = Event()

        def observe(event: ResearchEvent) -> None:
            while not closed.is_set():
                try:
                    event_queue.put(event, timeout=_QUEUE_POLL_SECONDS)
                    return
                except Full:
                    continue

        try:
            submission = self._submit_recovery(
                run_id,
                observer=observe,
                cancellation=cancellation,
            )
        except RecoveryFailure as exc:
            return _HarnessStreamIterator(
                run_id=run_id,
                cancellation=cancellation,
                closed=closed,
                event_queue=event_queue,
                projector=self._projector,
                submission=None,
                initial_error=self._error_event(
                    run_id,
                    code=exc.code,
                    detail=exc.safe_message,
                ),
            )
        except _RunAlreadyReservedError:
            return _HarnessStreamIterator(
                run_id=run_id,
                cancellation=cancellation,
                closed=closed,
                event_queue=event_queue,
                projector=self._projector,
                submission=None,
                initial_error=self._error_event(
                    run_id,
                    code="run_already_active",
                    detail="A run with this ID is already active.",
                ),
            )
        except Exception:
            return _HarnessStreamIterator(
                run_id=run_id,
                cancellation=cancellation,
                closed=closed,
                event_queue=event_queue,
                projector=self._projector,
                submission=None,
                initial_error=self._error_event(
                    run_id,
                    code="application_error",
                    detail="Research recovery failed.",
                ),
            )

        if submission is None:
            return _HarnessStreamIterator(
                run_id=run_id,
                cancellation=cancellation,
                closed=closed,
                event_queue=event_queue,
                projector=self._projector,
                submission=None,
                initial_error=self._error_event(
                    run_id,
                    code="runner_busy",
                    detail="Research execution capacity is busy.",
                ),
            )

        return _HarnessStreamIterator(
            run_id=run_id,
            cancellation=cancellation,
            closed=closed,
            event_queue=event_queue,
            projector=self._projector,
            submission=submission,
        )

    def load_record(self, run_id: str) -> dict[str, Any]:
        """Return a detached JSON-ready view of one canonical snapshot."""
        return deepcopy(self.repository.load(run_id).as_dict())

    def list_history(
        self,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Return a detached page of completed run summaries."""
        if self.history_store is not None:
            page = self.history_store.list_runs(limit=limit, cursor=cursor)
        else:
            list_summaries = getattr(self.repository, "list_summaries", None)
            if not callable(list_summaries):
                return {"items": [], "next_cursor": None}
            page = list_summaries(limit=limit, cursor=cursor)
        as_dict = getattr(page, "as_dict", None)
        if callable(as_dict):
            return deepcopy(as_dict())
        if isinstance(page, dict):
            return deepcopy(page)
        return {"items": [], "next_cursor": None}

    def list_memories(
        self,
        *,
        scope: str = "default",
        include_pending: bool = True,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Return user memory items without exposing the store object."""
        if self.memory_store is None:
            return {"items": [], "scope": scope, "limit": limit}
        items = self.memory_store.list(
            scope=scope,
            include_pending=include_pending,
            limit=limit,
        )
        return {
            "items": [item.as_dict() for item in items],
            "scope": scope,
            "limit": limit,
        }

    def create_memory_candidate(
        self,
        *,
        text: str,
        kind: str,
        scope: str,
    ) -> UserMemory:
        """Create a pending memory candidate through the explicit write gate."""
        if self.memory_store is None:
            raise RuntimeError("User memory is unavailable.")
        return self.memory_store.create_candidate(text=text, kind=kind, scope=scope)

    def confirm_memory(self, memory_id: str, *, scope: str) -> UserMemory:
        """Confirm one pending memory candidate."""
        if self.memory_store is None:
            raise RuntimeError("User memory is unavailable.")
        return self.memory_store.confirm(memory_id, scope=scope)

    def delete_memory(self, memory_id: str, *, scope: str) -> bool:
        """Delete one memory candidate or confirmed memory."""
        if self.memory_store is None:
            return False
        return self.memory_store.delete(memory_id, scope=scope)

    def _submit(
        self,
        request: ResearchCommand,
        *,
        observer: Any,
        cancellation: CancellationToken,
    ) -> _Submission | None:
        with self._reservation_lock:
            if request.run_id in self._reserved_run_ids:
                raise _RunAlreadyReservedError(request.run_id)
            if not self._admission.acquire(blocking=False):
                parent_run_id = request.parent_run_id
                if (
                    parent_run_id is not None
                    and self.application.is_run_active(parent_run_id)
                ):
                    raise _ParentRunPendingError(parent_run_id)
                return None
            self._reserved_run_ids.add(request.run_id)
        try:
            future = self._executor.submit(
                self.application.execute,
                request,
                observer=observer,
                cancellation=cancellation,
            )
        except Exception:
            with self._reservation_lock:
                self._reserved_run_ids.discard(request.run_id)
            self._admission.release()
            raise

        submission = _Submission(future=future)

        def release_submission(completed: Future[ResearchRunResult]) -> None:
            try:
                self._release_submission(completed, request.run_id)
            finally:
                submission.released.set()
                submission.terminal_wakeup.set()

        future.add_done_callback(release_submission)
        return submission

    def _submit_recovery(
        self,
        run_id: str,
        *,
        observer: Any,
        cancellation: CancellationToken,
    ) -> _Submission | None:
        """Submit recovery without constructing a second application runtime."""
        with self._reservation_lock:
            if run_id in self._reserved_run_ids:
                raise _RunAlreadyReservedError(run_id)
            if not self._admission.acquire(blocking=False):
                return None
            self._reserved_run_ids.add(run_id)
        try:
            future = self._executor.submit(
                self.application.resume,
                run_id,
                observer=observer,
                cancellation=cancellation,
            )
        except Exception:
            with self._reservation_lock:
                self._reserved_run_ids.discard(run_id)
            self._admission.release()
            raise

        submission = _Submission(future=future)

        def release_submission(completed: Future[ResearchRunResult]) -> None:
            try:
                self._release_submission(completed, run_id)
            finally:
                submission.released.set()
                submission.terminal_wakeup.set()

        future.add_done_callback(release_submission)
        return submission

    def _release_submission(
        self,
        future: Future[ResearchRunResult],
        run_id: str,
    ) -> None:
        del future
        with self._reservation_lock:
            self._reserved_run_ids.discard(run_id)
        self._admission.release()

    @staticmethod
    def _adapt_result(result: ResearchRunResult) -> HarnessRunResult:
        error = result.error
        return HarnessRunResult(
            run_id=result.run_id,
            status=result.status.value,
            output=deepcopy(result.output),
            error=error.message if error is not None else None,
            error_code=error.code if error is not None else None,
            metrics=deepcopy(result.metrics),
            findings=[],
            compressed_context=deepcopy(result.followup_context),
            policy_decisions=[deepcopy(item) for item in result.policy_decisions],
            resumable=result.resumable,
            recovery_resumable=result.recovery_resumable,
            last_resumable_parent=result.last_resumable_parent,
        )

    @staticmethod
    def _error_result(run_id: str, *, code: str, message: str) -> HarnessRunResult:
        return HarnessRunResult(
            run_id=run_id,
            status="failed",
            error=message,
            error_code=code,
            resumable=False,
        )

    @staticmethod
    def _error_event(
        run_id: str,
        *,
        code: str,
        detail: str,
        sequence: int = 1,
        last_resumable_parent: str | None = None,
    ) -> dict[str, Any]:
        event = {
            "type": "error",
            "run_id": run_id,
            "schema_version": 1,
            "sequence": sequence,
            "code": code if code in _SAFE_ERROR_CODES else "application_error",
            "detail": detail,
        }
        if last_resumable_parent is not None:
            event["resumable"] = False
            event["last_resumable_parent"] = last_resumable_parent
        return event


__all__ = ["HarnessRunner"]
