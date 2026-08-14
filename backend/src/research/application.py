"""Single authoritative application lifecycle for integrated research runs."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from enum import Enum
from inspect import Parameter, signature
from threading import RLock
from time import perf_counter
from typing import Any

from models import ResearchState

from .context import FollowupContext, FollowupContextProjector
from .contracts import (
    EventKind,
    ResearchCommand,
    ResearchEvent,
    ResearchRunResult,
    RunError,
    RunSnapshot,
    RunStatus,
)
from .history import ResearchHistoryStore
from .intelligence import ResearchIntelligenceBundle
from .memory import UserMemoryStore
from .observers import NULL_OBSERVER
from .operations import OperationRejectedError
from .ports import (
    CommandPolicy,
    ResearchCoordinator,
    ResearchEventObserver,
    RunRepository,
)
from .quality import EvidenceGateBlockedError
from .report_document import StructuredSummaryDocument, SummaryQualityAssessment
from .report_validation import (
    CitationGate,
    ReportValidationResult,
    validate_citations,
    validate_report,
    validate_structured_citations,
)
from .repository import (
    CorruptRunRecordError,
    InvalidRunIdError,
    RunNotFoundError,
    RunRepositoryError,
    UnsupportedSchemaError,
)
from .session import (
    NEVER_CANCELLED,
    CancellationRequestedError,
    CancellationToken,
    CheckpointPersistenceError,
    DeadlineExceededError,
    RunSession,
)
from .validation import TerminalStateError, validate_terminal_state

_LOGGER = logging.getLogger(__name__)
_DECISION_FIELDS = ("capability", "outcome", "reason")
_POLICY_REASON_BY_OUTCOME = {
    "allow": "Capability allowed by policy.",
    "deny": "Capability denied by policy.",
    "ask": "Capability requires explicit approval.",
}
_FOLLOWUP_FIELDS = {
    "schema_version",
    "source_run_id",
    "key_findings",
    "key_sources",
    "open_questions",
}
_MISSING = object()


class _ApplicationFailure(RuntimeError):
    """Carry a stable application error without exposing cause details."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        last_resumable_parent: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.last_resumable_parent = last_resumable_parent


class RecoveryFailure(RuntimeError):
    """Carry a stable recovery error to the HTTP/compatibility boundary."""

    def __init__(self, code: str, message: str) -> None:
        """Initialize one safe, machine-readable recovery failure."""
        super().__init__(message)
        self.code = code
        self.safe_message = message


def _is_sensitive_key(key: str) -> bool:
    normalized = key.strip().casefold().replace("-", "_")
    if normalized.startswith("raw_"):
        return True
    if normalized in {
        "api_key",
        "authorization",
        "body",
        "content",
        "cookie",
        "headers",
        "metadata",
        "password",
        "prompt",
        "secret",
        "token",
    }:
        return True
    return normalized.endswith(
        ("_api_key", "_password", "_secret", "_token")
    )


def _safe_json_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        safe: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or _is_sensitive_key(key):
                continue
            projected = _safe_json_value(item)
            if projected is not _MISSING:
                safe[key] = projected
        return safe
    if isinstance(value, (list, tuple)):
        safe_items: list[object] = []
        for item in value:
            projected = _safe_json_value(item)
            if projected is not _MISSING:
                safe_items.append(projected)
        return safe_items
    return _MISSING


def _safe_mapping(value: Mapping[str, object]) -> dict[str, Any]:
    projected = _safe_json_value(value)
    if not isinstance(projected, dict):
        return {}
    json.dumps(projected)
    return projected


def _safe_decision(decision: object) -> dict[str, Any]:
    if isinstance(decision, Mapping):
        raw = decision
    else:
        serializer = getattr(decision, "as_dict", None)
        if not callable(serializer):
            raise TypeError("Policy decisions must provide as_dict().")
        raw = serializer()
    if not isinstance(raw, Mapping):
        raise TypeError("Policy decision serialization must be an object.")

    safe: dict[str, Any] = {}
    for field in _DECISION_FIELDS:
        item = raw.get(field)
        if not isinstance(item, str):
            raise TypeError(f"Policy decision {field!r} must be text.")
        safe[field] = item
    outcome = safe["outcome"]
    if outcome not in _POLICY_REASON_BY_OUTCOME:
        raise ValueError("Policy decision outcome is invalid.")
    safe["reason"] = _POLICY_REASON_BY_OUTCOME[outcome]
    json.dumps(safe)
    return safe


class ResearchApplicationService:
    """Own the only public execution lifecycle for one research command."""

    def __init__(
        self,
        coordinator: ResearchCoordinator,
        repository: RunRepository,
        policy: CommandPolicy,
        *,
        context_projector: FollowupContextProjector | None = None,
        history_store: ResearchHistoryStore | None = None,
        memory_store: UserMemoryStore | None = None,
        terminal_validator: Callable[[RunSession], None] = validate_terminal_state,
    ) -> None:
        """Bind the coordinator, repository, policy, and lifecycle collaborators."""
        if policy is None:
            raise ValueError("A command policy is required.")
        self._coordinator = coordinator
        self._repository = repository
        self._policy = policy
        self._context_projector = context_projector or FollowupContextProjector()
        self._history_store = history_store
        self._memory_store = memory_store
        self._terminal_validator = terminal_validator
        self._active_lock = RLock()
        self._active_run_ids: set[str] = set()

    @property
    def repository(self) -> RunRepository:
        """Return the canonical repository shared with compatibility readers."""
        return self._repository

    def is_run_active(self, run_id: str) -> bool:
        """Return whether this application currently owns the run lifecycle."""
        return self._is_active(run_id)

    def execute(
        self,
        command: ResearchCommand,
        *,
        observer: ResearchEventObserver = NULL_OBSERVER,
        cancellation: CancellationToken = NEVER_CANCELLED,
    ) -> ResearchRunResult:
        """Execute one command through the sole synchronous lifecycle."""
        if not self._register(command.run_id):
            return self._prestart_terminal(
                command,
                observer=observer,
                status=RunStatus.REJECTED,
                kind=EventKind.RUN_REJECTED,
                code="run_already_active",
                message="A run with this ID is already active.",
            )

        try:
            if cancellation.is_cancelled:
                return self._prestart_terminal(
                    command,
                    observer=observer,
                    status=RunStatus.CANCELLED,
                    kind=EventKind.RUN_CANCELLED,
                    code="cancelled",
                    message="Research run was cancelled.",
                )

            policy_decisions: tuple[dict[str, Any], ...] = ()
            try:
                raw_decisions = tuple(self._policy.evaluate(command))
                policy_decisions = tuple(
                    _safe_decision(decision) for decision in raw_decisions
                )
                self._policy.assert_executable(raw_decisions)
            except PermissionError:
                return self._prestart_terminal(
                    command,
                    observer=observer,
                    status=RunStatus.REJECTED,
                    kind=EventKind.RUN_REJECTED,
                    code="policy_rejected",
                    message="Research command was rejected by policy.",
                    policy_decisions=policy_decisions,
                )
            except Exception:
                return self._prestart_terminal(
                    command,
                    observer=observer,
                    status=RunStatus.FAILED,
                    kind=EventKind.RUN_FAILED,
                    code="policy_error",
                    message="Research command policy preflight failed.",
                    policy_decisions=policy_decisions,
                )

            if cancellation.is_cancelled:
                return self._prestart_terminal(
                    command,
                    observer=observer,
                    status=RunStatus.CANCELLED,
                    kind=EventKind.RUN_CANCELLED,
                    code="cancelled",
                    message="Research run was cancelled.",
                    policy_decisions=policy_decisions,
                )

            try:
                prior_context = self._load_prior_context(command.parent_run_id)
            except _ApplicationFailure as exc:
                return self._prestart_terminal(
                    command,
                    observer=observer,
                    status=RunStatus.FAILED,
                    kind=EventKind.RUN_FAILED,
                    code=exc.code,
                    message=exc.safe_message,
                    policy_decisions=policy_decisions,
                    last_resumable_parent=exc.last_resumable_parent,
                )

            if cancellation.is_cancelled:
                return self._prestart_terminal(
                    command,
                    observer=observer,
                    status=RunStatus.CANCELLED,
                    kind=EventKind.RUN_CANCELLED,
                    code="cancelled",
                    message="Research run was cancelled.",
                    policy_decisions=policy_decisions,
                )

            session = RunSession(
                command=command,
                state=ResearchState(research_topic=command.topic),
                cancellation_token=cancellation,
                checkpoint_writer=self._checkpoint_writer(),
            )
            if prior_context is not None:
                # The validated parent is the anchor to retain if this child
                # later fails.  A failed child must never become the next
                # continuation target merely because it is the newest run.
                session.last_resumable_parent = command.parent_run_id
            if command.parent_run_id is not None:
                session.metrics["followup"] = {
                    "requested": True,
                    "parent_context_loaded": prior_context is not None,
                }
            session.policy_decisions.extend(
                dict(decision) for decision in policy_decisions
            )
            session.add_observer(observer)
            session.start()
            self._attach_related_history(session, command)
            self._attach_user_memory(session, command)
            try:
                session.persist_checkpoint("run_created")
            except CheckpointPersistenceError:
                return self._finish_started(
                    session,
                    status=RunStatus.FAILED,
                    kind=EventKind.RUN_FAILED,
                    code="checkpoint_persistence_failed",
                    message="The initial research checkpoint could not be persisted.",
                )
            return self._run_started_session(
                session,
                prior_context,
                coordinator=self._coordinator.execute,
            )
        finally:
            self._unregister(command.run_id)

    def resume(
        self,
        run_id: str,
        *,
        observer: ResearchEventObserver = NULL_OBSERVER,
        cancellation: CancellationToken = NEVER_CANCELLED,
    ) -> ResearchRunResult:
        """Resume one failed or interrupted run from its last safe checkpoint."""
        try:
            snapshot = self._repository.load(run_id)
        except InvalidRunIdError as exc:
            raise RecoveryFailure(
                "invalid_run_id",
                "The recovery run ID is invalid.",
            ) from exc
        except RunNotFoundError as exc:
            raise RecoveryFailure(
                "checkpoint_not_found",
                "No persisted recovery checkpoint was found for this run.",
            ) from exc
        except UnsupportedSchemaError as exc:
            raise RecoveryFailure(
                "checkpoint_version_unsupported",
                "The persisted recovery checkpoint version is unsupported.",
            ) from exc
        except CorruptRunRecordError as exc:
            raise RecoveryFailure(
                "checkpoint_corrupt",
                "The persisted recovery checkpoint is corrupt.",
            ) from exc
        except RunRepositoryError as exc:
            raise RecoveryFailure(
                "repository_error",
                "The recovery checkpoint could not be read.",
            ) from exc

        checkpoint = snapshot.checkpoint_state
        if checkpoint is None:
            raise RecoveryFailure(
                "checkpoint_not_found",
                "This run has no recovery checkpoint.",
            )
        if snapshot.status in {RunStatus.COMPLETED, RunStatus.REJECTED}:
            raise RecoveryFailure(
                "run_not_resumable",
                "Only failed or interrupted runs can be recovered.",
            )
        if snapshot.recovery_resumable is not True:
            raise RecoveryFailure(
                "run_not_resumable",
                "This run does not have a valid resumable checkpoint.",
            )
        if not self._register(snapshot.run_id):
            raise RecoveryFailure(
                "run_already_active",
                "This run is already being recovered.",
            )

        try:
            session = RunSession.restore_from_snapshot(
                snapshot,
                checkpoint_writer=self._checkpoint_writer(),
                cancellation_token=cancellation,
            )
            session.add_observer(observer)
            session.start_recovery()
            prior_context = self._restore_followup_context(snapshot)
            resume = getattr(self._coordinator, "resume", None)
            if not callable(resume):
                raise RecoveryFailure(
                    "recovery_unsupported",
                    "The configured coordinator does not support recovery.",
                )
            return self._run_started_session(
                session,
                prior_context,
                coordinator=resume,
            )
        finally:
            self._unregister(snapshot.run_id)

    def _checkpoint_writer(self) -> Callable[[RunSnapshot], None] | None:
        """Return the repository's optional checkpoint hook."""
        writer = getattr(self._repository, "save_checkpoint", None)
        return writer if callable(writer) else None

    def _restore_followup_context(
        self,
        snapshot: RunSnapshot,
    ) -> FollowupContext | None:
        """Restore optional continuation context without making it a parent lookup."""
        if not snapshot.followup_context:
            return None
        try:
            return self._parse_followup_context(
                snapshot.followup_context,
                expected_source_run_id=snapshot.run_id,
            )
        except (TypeError, ValueError):
            return None

    def _run_started_session(
        self,
        session: RunSession,
        prior_context: FollowupContext | None,
        *,
        coordinator: Callable[[RunSession, FollowupContext | None], None],
    ) -> ResearchRunResult:
        """Run coordination and shared terminalization for new and recovered runs."""
        try:
            session.raise_if_run_controlled()
            coordinator(session, prior_context)
            session.raise_if_run_controlled()
        except (
            OperationRejectedError,
            DeadlineExceededError,
            CancellationRequestedError,
        ) as exc:
            return self._finish_started_control(session, exc)
        except CheckpointPersistenceError:
            return self._finish_started(
                session,
                status=RunStatus.FAILED,
                kind=EventKind.RUN_FAILED,
                code="checkpoint_persistence_failed",
                message="A recovery checkpoint could not be persisted.",
            )
        except EvidenceGateBlockedError:
            return self._finish_started(
                session,
                status=RunStatus.REPORT_INCOMPLETE,
                kind=EventKind.RUN_FAILED,
                code="report_incomplete",
                message="Research evidence did not meet the report quality gate.",
            )
        except Exception:
            return self._finish_started(
                session,
                status=RunStatus.FAILED,
                kind=EventKind.RUN_FAILED,
                code="coordinator_failed",
                message="Research coordination failed.",
            )

        try:
            report_validation = self._validate_report_with_retry(
                session,
                prior_context,
            )
            session.raise_if_run_controlled()
        except (
            OperationRejectedError,
            DeadlineExceededError,
            CancellationRequestedError,
        ) as exc:
            return self._finish_started_control(session, exc)
        except Exception:
            return self._finish_started(
                session,
                status=RunStatus.REPORT_INCOMPLETE,
                kind=EventKind.RUN_FAILED,
                code="report_incomplete",
                message="Research report validation failed.",
            )

        if not report_validation.valid:
            return self._finish_started(
                session,
                status=RunStatus.REPORT_INCOMPLETE,
                kind=EventKind.RUN_FAILED,
                code="report_incomplete",
                message="Research report is incomplete.",
            )

        try:
            self._terminal_validator(session)
        except (TerminalStateError, Exception):
            return self._finish_started(
                session,
                status=RunStatus.FAILED,
                kind=EventKind.RUN_FAILED,
                code="terminal_validation_failed",
                message="Research terminal state validation failed.",
            )

        try:
            projected = self._context_projector.project(session)
            current_context = self._parse_followup_context(
                projected.as_dict(),
                expected_source_run_id=session.run_id,
            )
            session.followup_context = current_context.as_dict()
            session.metrics = _safe_mapping(session.metrics)
            session.raise_if_run_controlled()
        except (
            OperationRejectedError,
            DeadlineExceededError,
            CancellationRequestedError,
        ) as exc:
            return self._finish_started_control(session, exc)
        except Exception:
            return self._finish_started(
                session,
                status=RunStatus.FAILED,
                kind=EventKind.RUN_FAILED,
                code="context_projection_failed",
                message="Research follow-up context projection failed.",
            )

        self._set_followup_success(session, succeeded=True)
        prepared = session.prepare_terminal(
            RunStatus.COMPLETED,
            EventKind.RUN_COMPLETED,
        )
        try:
            session.raise_if_run_controlled()
        except (
            OperationRejectedError,
            DeadlineExceededError,
            CancellationRequestedError,
        ) as exc:
            return self._finish_started_control(session, exc)

        try:
            self._repository.save(prepared.snapshot)
        except Exception:
            return self._finish_started(
                session,
                status=RunStatus.FAILED,
                kind=EventKind.RUN_FAILED,
                code="persistence_failed",
                message="The completed research run could not be persisted.",
            )

        if self._history_store is not None:
            try:
                self._history_store.index_snapshot(prepared.snapshot)
            except Exception:
                _LOGGER.warning(
                    "Research history index update failed; run remains durable."
                )

        session.confirm_terminal(prepared)
        return self._session_result(session, error=None)

    def _attach_related_history(
        self,
        session: RunSession,
        command: ResearchCommand,
    ) -> None:
        """Best-effort recall of related runs without affecting execution."""
        history_store = self._history_store
        if history_store is None:
            return
        started_at = perf_counter()
        if not command.use_history_memory:
            session.metrics["history_recall"] = {
                "enabled": False,
                "match_count": 0,
                "latency_ms": 0.0,
                "context_chars": 0,
                "outcome": "disabled",
            }
            return
        excluded = (command.parent_run_id,) if command.parent_run_id else ()
        recall_failed = False
        try:
            matches = history_store.recall(command.topic, exclude_run_ids=excluded)
        except Exception:
            _LOGGER.warning(
                "Research history recall failed; continuing without memory."
            )
            matches = ()
            recall_failed = True
        session.related_history_context = {"matches": [dict(match) for match in matches]}
        context_chars = len(
            json.dumps(
                session.related_history_context,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        ) if matches else 0
        session.metrics["history_recall"] = {
            "enabled": True,
            "match_count": len(matches),
            "latency_ms": round(max(0.0, perf_counter() - started_at) * 1000, 3),
            "context_chars": context_chars,
            "parent_excluded": bool(excluded),
            "outcome": "error" if recall_failed else ("hit" if matches else "miss"),
        }
        if matches:
            session.record_history_recall(matches=matches)

    def _attach_user_memory(
        self,
        session: RunSession,
        command: ResearchCommand,
    ) -> None:
        """Attach only confirmed user memory; candidates never enter planning."""
        memory_store = self._memory_store
        if memory_store is None:
            return
        started_at = perf_counter()
        failed = False
        try:
            memories = memory_store.confirmed_context(scope=command.memory_scope)
        except Exception:
            _LOGGER.warning(
                "Confirmed user memory lookup failed; continuing without memory."
            )
            memories = ()
            failed = True
        if memories:
            session.user_memory_context = {
                "memories": [dict(memory) for memory in memories]
            }
        else:
            session.user_memory_context = {}
        context_chars = len(
            json.dumps(
                session.user_memory_context,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        ) if memories else 0
        session.metrics["user_memory"] = {
            "enabled": True,
            "scope": command.memory_scope,
            "confirmed_count": len(memories),
            "context_chars": context_chars,
            "latency_ms": round(max(0.0, perf_counter() - started_at) * 1000, 3),
            "outcome": "error" if failed else ("hit" if memories else "miss"),
        }

    def _register(self, run_id: str) -> bool:
        with self._active_lock:
            if run_id in self._active_run_ids:
                return False
            self._active_run_ids.add(run_id)
            return True

    def _unregister(self, run_id: str) -> None:
        with self._active_lock:
            self._active_run_ids.discard(run_id)

    def _is_active(self, run_id: str) -> bool:
        with self._active_lock:
            return run_id in self._active_run_ids

    def _load_prior_context(
        self,
        parent_run_id: str | None,
    ) -> FollowupContext | None:
        if parent_run_id is None:
            return None

        def load_snapshot() -> RunSnapshot:
            try:
                loaded = self._repository.load(parent_run_id)
            except (CorruptRunRecordError, UnsupportedSchemaError) as exc:
                raise _ApplicationFailure(
                    "parent_corrupt",
                    "Parent run record is corrupt or unsupported.",
                ) from exc
            except RunNotFoundError:
                raise
            except RunRepositoryError as exc:
                raise _ApplicationFailure(
                    "repository_error",
                    "Parent run repository access failed.",
                ) from exc
            except Exception as exc:
                raise _ApplicationFailure(
                    "repository_error",
                    "Parent run repository access failed.",
                ) from exc
            if not isinstance(loaded, RunSnapshot):
                raise _ApplicationFailure(
                    "parent_corrupt",
                    "Parent run record is corrupt or unsupported.",
                )
            return loaded

        try:
            snapshot = load_snapshot()
        except RunNotFoundError:
            try:
                snapshot = load_snapshot()
            except RunNotFoundError:
                if self._is_active(parent_run_id):
                    raise _ApplicationFailure(
                        "parent_pending",
                        "Parent run is active but not yet durable.",
                    )
                try:
                    snapshot = load_snapshot()
                except RunNotFoundError as exc:
                    raise _ApplicationFailure(
                        "parent_not_found",
                        "Parent run was not found.",
                    ) from exc

        if not isinstance(snapshot, RunSnapshot):
            raise _ApplicationFailure(
                "parent_corrupt",
                "Parent run record is corrupt or unsupported.",
            )
        if snapshot.run_id != parent_run_id:
            raise _ApplicationFailure(
                "parent_corrupt",
                "Parent run record does not match the requested ID.",
            )
        if (
            snapshot.status is not RunStatus.COMPLETED
            or snapshot.completed_at is None
            or snapshot.resumable is False
        ):
            raise _ApplicationFailure(
                "parent_not_resumable",
                "Parent run exists but is not resumable.",
                last_resumable_parent=self._best_effort_resumable_parent(snapshot),
            )
        try:
            return self._parse_followup_context(
                snapshot.followup_context,
                expected_source_run_id=parent_run_id,
            )
        except (TypeError, ValueError) as exc:
            raise _ApplicationFailure(
                "parent_corrupt",
                "Parent follow-up context is invalid.",
            ) from exc

    def _best_effort_resumable_parent(
        self,
        snapshot: RunSnapshot,
    ) -> str | None:
        """Find the nearest known completed anchor without failing the request."""
        if snapshot.last_resumable_parent:
            return snapshot.last_resumable_parent
        parent_id = snapshot.parent_run_id
        if not parent_id:
            return None
        try:
            parent = self._repository.load(parent_id)
        except Exception:
            return None
        if not isinstance(parent, RunSnapshot):
            return None
        if (
            parent.status is RunStatus.COMPLETED
            and parent.completed_at is not None
            and parent.resumable is not False
        ):
            return parent.run_id
        return parent.last_resumable_parent

    def _validate_report_with_retry(
        self,
        session: RunSession,
        prior_context: FollowupContext | None,
    ) -> ReportValidationResult:
        """Validate the report and invoke at most one coordinator retry."""
        structured_validation = self._validate_structured_report(session)
        if structured_validation is not None:
            session.metrics["report_validation"] = {
                "attempts": [
                    {"attempt": 1, **structured_validation.as_dict()}
                ],
                "retry_count": 0,
                "final": structured_validation.as_dict(),
            }
            return structured_validation
        attempts: list[dict[str, object]] = []
        citation_gate = self._citation_gate(session)
        validation = validate_report(
            session.state.structured_report,
            session.state.todo_items,
            finish_reason=session.latest_llm_finish_reason(role="reporter"),
            citation_gate=citation_gate,
        )
        attempts.append({"attempt": 1, **validation.as_dict()})
        existing_validation = session.metrics.get("report_validation")
        existing_retry_count = 0
        if isinstance(existing_validation, dict):
            raw_retry_count = existing_validation.get("retry_count")
            if isinstance(raw_retry_count, int) and raw_retry_count >= 0:
                existing_retry_count = raw_retry_count
        retry_count = existing_retry_count
        if not validation.valid:
            if existing_retry_count >= 1:
                session.metrics["report_validation"] = {
                    "attempts": attempts,
                    "retry_count": existing_retry_count,
                    "final": validation.as_dict(),
                }
                return validation
            retry_report = getattr(self._coordinator, "retry_report", None)
            if callable(retry_report):
                retry_count = 1
                try:
                    session.persist_checkpoint("report_retry")
                    try:
                        parameters = tuple(signature(retry_report).parameters.values())
                    except (TypeError, ValueError):
                        parameters = ()
                    accepts_context = any(
                        parameter.kind
                        in {Parameter.VAR_POSITIONAL, Parameter.VAR_KEYWORD}
                        or parameter.name == "prior_context"
                        for parameter in parameters
                    )
                    if accepts_context:
                        retry_report(session, prior_context)
                    else:
                        retry_report(session)
                    session.persist_checkpoint("report_retry_completed")
                except (
                    OperationRejectedError,
                    DeadlineExceededError,
                    CancellationRequestedError,
                ):
                    raise
                except Exception:
                    # The failed retry remains represented by the first
                    # deterministic validation result and a stable metric.
                    attempts.append(
                        {
                            "attempt": 2,
                            "valid": False,
                            "failure_reason": "report_retry_failed",
                            "failure_reasons": ["report_retry_failed"],
                            "finish_reason": session.latest_llm_finish_reason(
                                role="reporter"
                            ),
                        }
                    )
                    session.metrics["report_validation"] = {
                        "attempts": attempts,
                        "retry_count": retry_count,
                        "final": attempts[-1],
                    }
                    return validation
                citation_gate = self._citation_gate(session)
                validation = validate_report(
                    session.state.structured_report,
                    session.state.todo_items,
                    finish_reason=session.latest_llm_finish_reason(role="reporter"),
                    citation_gate=citation_gate,
                )
                attempts.append({"attempt": 2, **validation.as_dict()})

        session.metrics["report_validation"] = {
            "attempts": attempts,
            "retry_count": retry_count,
            "final": validation.as_dict(),
        }
        return validation

    @staticmethod
    def _validate_structured_report(
        session: RunSession,
    ) -> ReportValidationResult | None:
        """Validate the explicit structured path without legacy citation parsing."""
        if not session.state.structured_summary:
            return None
        text = session.state.structured_report or ""
        reasons: list[str] = []
        try:
            document = StructuredSummaryDocument.from_dict(
                session.state.structured_summary
            )
            assessment = SummaryQualityAssessment.from_dict(
                session.state.quality_assessment
            )
            bundle = ResearchIntelligenceBundle.from_dict(
                session.state.research_intelligence
            )
            citation_gate = validate_structured_citations(
                document,
                bundle.claims,
                bundle.evidence,
                evidence_frozen=bundle.evidence_frozen,
            )
            if not citation_gate.valid:
                reasons.extend(
                    f"citation_{item}" for item in citation_gate.failure_reasons
                )
            if {item.paragraph_id for item in document.paragraphs} != {
                item.paragraph_id for item in assessment.paragraph_assessments
            }:
                reasons.append("quality_paragraph_mismatch")
        except (TypeError, ValueError):
            document = None
            assessment = None
            citation_gate = None
            reasons.append("structured_output_invalid")
        if not text.strip():
            reasons.append("empty_report")
        if document is not None and not document.paragraphs:
            reasons.append("empty_structured_document")
        section_count = sum(
            1 for line in text.splitlines() if line.startswith("## ")
        )
        has_title = next(
            (line.startswith("# ") for line in text.splitlines() if line.strip()),
            False,
        )
        if not has_title:
            reasons.append("missing_title")
        has_citations = "[E" in text
        requires_citations = bool(
            document
            and any(
                item.paragraph_type != "limitation" and item.claim_ids
                for item in document.paragraphs
            )
        )
        if requires_citations and not has_citations:
            reasons.append("citations_missing")
        return ReportValidationResult(
            valid=not reasons,
            output_chars=len(text),
            has_title=has_title,
            section_count=section_count,
            completed_sections=section_count,
            covered_tasks=len(session.state.todo_items),
            total_tasks=len(session.state.todo_items),
            requires_citations=requires_citations,
            has_citations=has_citations,
            finish_reason=session.latest_llm_finish_reason(
                role="structured_reporter"
            ),
            failure_reasons=tuple(dict.fromkeys(reasons)),
            citation_valid=bool(citation_gate and citation_gate.valid),
            citation_failure_reasons=(
                citation_gate.failure_reasons if citation_gate is not None else ()
            ),
        )

    @staticmethod
    def _citation_gate(session: RunSession) -> CitationGate | None:
        """Build a citation gate only for a persisted schema-v2 bundle."""
        raw_bundle = session.state.research_intelligence
        if not isinstance(raw_bundle, Mapping) or raw_bundle.get("schema_version") != 2:
            return None
        return validate_citations(session.state.structured_report, raw_bundle)

    @staticmethod
    def _parse_followup_context(
        raw: Mapping[str, object],
        *,
        expected_source_run_id: str,
    ) -> FollowupContext:
        if not isinstance(raw, Mapping) or set(raw) != _FOLLOWUP_FIELDS:
            raise ValueError("Follow-up context has an invalid shape.")
        schema_version = raw.get("schema_version")
        if type(schema_version) is not int or schema_version != 1:
            raise ValueError("Follow-up context schema version is invalid.")
        source_run_id = raw.get("source_run_id")
        if source_run_id != expected_source_run_id:
            raise ValueError("Follow-up context source run ID does not match.")

        def bounded_text(
            field: str,
            *,
            count: int,
            item_limit: int | None,
        ) -> tuple[str, ...]:
            values = raw.get(field)
            if not isinstance(values, (list, tuple)) or len(values) > count:
                raise ValueError(f"Follow-up context {field!r} exceeds its budget.")
            items: list[str] = []
            for value in values:
                if (
                    not isinstance(value, str)
                    or not value.strip()
                    or value != value.strip()
                    or (
                        item_limit is not None
                        and len(value) > item_limit
                    )
                ):
                    raise ValueError(
                        f"Follow-up context {field!r} contains invalid text."
                    )
                items.append(value)
            return tuple(items)

        return FollowupContext(
            source_run_id=expected_source_run_id,
            key_findings=bounded_text(
                "key_findings",
                count=FollowupContextProjector.MAX_FINDINGS,
                item_limit=FollowupContextProjector.MAX_ITEM_CHARS,
            ),
            key_sources=bounded_text(
                "key_sources",
                count=FollowupContextProjector.MAX_SOURCES,
                item_limit=FollowupContextProjector.MAX_ITEM_CHARS,
            ),
            open_questions=bounded_text(
                "open_questions",
                count=FollowupContextProjector.MAX_OPEN_QUESTIONS,
                item_limit=None,
            ),
        )

    def _prestart_terminal(
        self,
        command: ResearchCommand,
        *,
        observer: ResearchEventObserver,
        status: RunStatus,
        kind: EventKind,
        code: str,
        message: str,
        policy_decisions: tuple[dict[str, Any], ...] = (),
        last_resumable_parent: str | None = None,
    ) -> ResearchRunResult:
        event = ResearchEvent(
            kind=kind,
            run_id=command.run_id,
            sequence=1,
            occurred_at=datetime.now(timezone.utc),
            payload={
                "code": code,
                "message": message,
                "checkpoint": kind.value,
                "resumable": False,
                "last_resumable_parent": last_resumable_parent,
            },
        )
        event.as_dict()
        snapshot = RunSnapshot(
            run_id=command.run_id,
            topic=command.topic,
            status=status,
            started_at=event.occurred_at,
            completed_at=event.occurred_at,
            parent_run_id=command.parent_run_id,
            output={
                "running_summary": None,
                "report_markdown": None,
                "todo_items": [],
            },
            followup_context={},
            metrics={
                "checkpoint": kind.value,
                "failure_reason": code,
                "resumable": False,
                "last_resumable_parent": last_resumable_parent,
            },
            policy_decisions=policy_decisions,
            config_snapshot=command.config.safe_snapshot(),
            events=(event,),
            error=RunError(code=code, message=message),
            failure_reason=code,
            checkpoint=kind.value,
            resumable=False,
            last_resumable_parent=last_resumable_parent,
        )
        try:
            self._repository.save(snapshot)
        except Exception:
            _LOGGER.warning(
                "Prestart terminal could not be persisted: run_id=%s code=%s",
                command.run_id,
                code,
            )
        try:
            observer(event)
        except Exception:
            _LOGGER.error(
                "Research event observer failure isolated: run_id=%s sequence=%s",
                event.run_id,
                event.sequence,
            )
        return ResearchRunResult(
            run_id=command.run_id,
            status=status,
            output=None,
            error=RunError(code=code, message=message),
            metrics={},
            followup_context={},
            policy_decisions=policy_decisions,
            resumable=False,
            last_resumable_parent=last_resumable_parent,
        )

    def _finish_started_control(
        self,
        session: RunSession,
        error: OperationRejectedError
        | DeadlineExceededError
        | CancellationRequestedError,
    ) -> ResearchRunResult:
        """Finish with rejection taking priority over competing run controls."""
        preferred = error
        try:
            session.raise_if_run_controlled()
        except OperationRejectedError as rejection:
            if (
                not isinstance(error, OperationRejectedError)
                or rejection.operation_id != error.operation_id
            ):
                preferred = rejection
        except (DeadlineExceededError, CancellationRequestedError):
            pass

        if isinstance(preferred, OperationRejectedError):
            return self._finish_started(
                session,
                status=RunStatus.REJECTED,
                kind=EventKind.RUN_REJECTED,
                code="operation_rejected",
                message="A research operation was rejected by policy.",
            )
        if isinstance(preferred, DeadlineExceededError):
            return self._finish_started(
                session,
                status=RunStatus.CANCELLED,
                kind=EventKind.RUN_CANCELLED,
                code="deadline_exceeded",
                message="Research run deadline was exceeded.",
            )
        return self._finish_started(
            session,
            status=RunStatus.CANCELLED,
            kind=EventKind.RUN_CANCELLED,
            code="cancelled",
            message="Research run was cancelled.",
        )

    def _finish_started(
        self,
        session: RunSession,
        *,
        status: RunStatus,
        kind: EventKind,
        code: str,
        message: str,
        last_resumable_parent: str | None = None,
    ) -> ResearchRunResult:
        anchor = last_resumable_parent or session.last_resumable_parent
        if anchor is not None:
            session.last_resumable_parent = anchor
        session.metrics = _safe_mapping(session.metrics)
        self._set_followup_success(
            session,
            succeeded=status is RunStatus.COMPLETED,
        )
        session.followup_context = _safe_mapping(session.followup_context)
        prepared = session.prepare_terminal(
            status,
            kind,
            code=code,
            message=message,
        )
        try:
            self._repository.save(prepared.snapshot)
        except Exception:
            _LOGGER.warning(
                "Failed terminal run could not be persisted: run_id=%s code=%s",
                session.run_id,
                code,
            )
        session.confirm_terminal(prepared)
        return self._session_result(
            session,
            error=RunError(code=code, message=message),
        )

    @staticmethod
    def _set_followup_success(session: RunSession, *, succeeded: bool) -> None:
        """Persist whether a requested follow-up completed successfully."""
        if session.command.parent_run_id is None:
            return
        followup = session.metrics.get("followup")
        if isinstance(followup, dict):
            followup["success"] = succeeded

    @staticmethod
    def _session_result(
        session: RunSession,
        *,
        error: RunError | None,
    ) -> ResearchRunResult:
        return ResearchRunResult(
            run_id=session.run_id,
            status=session.status,
            output=session.to_legacy_output(),
            error=error,
            metrics=dict(session.metrics),
            followup_context=dict(session.followup_context),
            policy_decisions=tuple(
                dict(decision) for decision in session.policy_decisions
            ),
            resumable=session.status is RunStatus.COMPLETED,
            recovery_resumable=session.recovery_resumable,
            last_resumable_parent=session.last_resumable_parent,
        )


__all__ = ["RecoveryFailure", "ResearchApplicationService"]
