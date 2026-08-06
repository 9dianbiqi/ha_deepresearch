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
from research.application import ResearchApplicationService
from research.contracts import ResearchCommand, ResearchEvent, ResearchRunResult
from research.legacy_sse import LegacySseProjector
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
        "coordinator_failed",
        "invalid_command",
        "missing_terminal",
        "parent_corrupt",
        "parent_not_found",
        "parent_pending",
        "operation_rejected",
        "persistence_failed",
        "policy_error",
        "policy_rejected",
        "repository_error",
        "run_already_active",
        "run_failed",
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
        except Exception:
            code = "application_error"
            detail = "Research execution failed."
        else:
            code = result.error.code if result.error is not None else "missing_terminal"
            detail = "Research execution ended without a terminal event."
        self._wait_for_terminal_release(submission)
        with self._lock:
            if self._closed.is_set():
                raise StopIteration
            event = HarnessRunner._error_event(
                self._run_id,
                code=code,
                detail=detail,
                sequence=self._last_sequence + 1,
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
        policy = HarnessPolicy()
        coordinator = DeepResearchAgent(
            config=Configuration.from_env(),
            operation_authorizer=policy,
        )
        application = ResearchApplicationService(
            coordinator=coordinator,
            repository=repository,
            policy=policy,
        )
        # Keep one top-level run active per shared coordinator. Task workers still
        # use the configured bounded parallelism within that run, while this
        # conservative boundary avoids interleaving stateful role-agent histories.
        return cls(application=application, repository=repository, max_workers=1)

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
        except Exception:
            submission.released.wait()
            return self._error_result(
                request.run_id,
                code="application_error",
                message="Research execution failed.",
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

    def load_record(self, run_id: str) -> dict[str, Any]:
        """Return a detached JSON-ready view of one canonical snapshot."""
        return deepcopy(self.repository.load(run_id).as_dict())

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
        )

    @staticmethod
    def _error_result(run_id: str, *, code: str, message: str) -> HarnessRunResult:
        return HarnessRunResult(
            run_id=run_id,
            status="failed",
            error=message,
            error_code=code,
        )

    @staticmethod
    def _error_event(
        run_id: str,
        *,
        code: str,
        detail: str,
        sequence: int = 1,
    ) -> dict[str, Any]:
        return {
            "type": "error",
            "run_id": run_id,
            "schema_version": 1,
            "sequence": sequence,
            "code": code if code in _SAFE_ERROR_CODES else "application_error",
            "detail": detail,
        }


__all__ = ["HarnessRunner"]
