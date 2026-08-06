"""Single authoritative application lifecycle for integrated research runs."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from enum import Enum
from threading import RLock
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
from .observers import NULL_OBSERVER
from .operations import OperationRejectedError
from .ports import (
    CommandPolicy,
    ResearchCoordinator,
    ResearchEventObserver,
    RunRepository,
)
from .repository import (
    CorruptRunRecordError,
    RunNotFoundError,
    RunRepositoryError,
    UnsupportedSchemaError,
)
from .session import (
    NEVER_CANCELLED,
    CancellationRequestedError,
    CancellationToken,
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

    def __init__(self, code: str, message: str) -> None:
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
        terminal_validator: Callable[[RunSession], None] = validate_terminal_state,
    ) -> None:
        """Bind the coordinator, repository, policy, and lifecycle collaborators."""
        if policy is None:
            raise ValueError("A command policy is required.")
        self._coordinator = coordinator
        self._repository = repository
        self._policy = policy
        self._context_projector = context_projector or FollowupContextProjector()
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
            )
            session.policy_decisions.extend(
                dict(decision) for decision in policy_decisions
            )
            session.add_observer(observer)
            session.start()

            try:
                session.raise_if_run_controlled()
                self._coordinator.execute(session, prior_context)
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
                    code="coordinator_failed",
                    message="Research coordination failed.",
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

            session.confirm_terminal(prepared)
            return self._session_result(session, error=None)
        finally:
            self._unregister(command.run_id)

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

        try:
            snapshot = self._repository.load(parent_run_id)
        except RunNotFoundError:
            try:
                snapshot = self._repository.load(parent_run_id)
            except RunNotFoundError as exc:
                if self._is_active(parent_run_id):
                    raise _ApplicationFailure(
                        "parent_pending",
                        "Parent run is active but not yet durable.",
                    ) from exc
                try:
                    snapshot = self._repository.load(parent_run_id)
                except RunNotFoundError as final_exc:
                    raise _ApplicationFailure(
                        "parent_not_found",
                        "Parent run was not found.",
                    ) from final_exc
                except (CorruptRunRecordError, UnsupportedSchemaError) as final_exc:
                    raise _ApplicationFailure(
                        "parent_corrupt",
                        "Parent run record is corrupt or unsupported.",
                    ) from final_exc
                except (RunRepositoryError, Exception) as final_exc:
                    raise _ApplicationFailure(
                        "repository_error",
                        "Parent run repository access failed.",
                    ) from final_exc
            except (CorruptRunRecordError, UnsupportedSchemaError) as exc:
                raise _ApplicationFailure(
                    "parent_corrupt",
                    "Parent run record is corrupt or unsupported.",
                ) from exc
            except (RunRepositoryError, Exception) as exc:
                raise _ApplicationFailure(
                    "repository_error",
                    "Parent run repository access failed.",
                ) from exc
        except (CorruptRunRecordError, UnsupportedSchemaError) as exc:
            raise _ApplicationFailure(
                "parent_corrupt",
                "Parent run record is corrupt or unsupported.",
            ) from exc
        except (RunRepositoryError, Exception) as exc:
            raise _ApplicationFailure(
                "repository_error",
                "Parent run repository access failed.",
            ) from exc

        if not isinstance(snapshot, RunSnapshot):
            raise _ApplicationFailure(
                "parent_corrupt",
                "Parent run record is corrupt or unsupported.",
            )
        if (
            snapshot.run_id != parent_run_id
            or snapshot.status is not RunStatus.COMPLETED
            or snapshot.completed_at is None
        ):
            raise _ApplicationFailure(
                "parent_corrupt",
                "Parent run record is not a completed canonical snapshot.",
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
    ) -> ResearchRunResult:
        event = ResearchEvent(
            kind=kind,
            run_id=command.run_id,
            sequence=1,
            occurred_at=datetime.now(timezone.utc),
            payload={"code": code, "message": message},
        )
        event.as_dict()
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
    ) -> ResearchRunResult:
        session.metrics = _safe_mapping(session.metrics)
        session.followup_context = _safe_mapping(session.followup_context)
        prepared = session.prepare_terminal(
            status,
            kind,
            code=code,
            message=message,
        )
        session.confirm_terminal(prepared)
        return self._session_result(
            session,
            error=RunError(code=code, message=message),
        )

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
        )


__all__ = ["ResearchApplicationService"]
