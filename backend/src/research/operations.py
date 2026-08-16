"""Run-bound policy, audit, cancellation, and deadline governance."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from time import monotonic
from types import MappingProxyType
from typing import Any, TypeVar
from uuid import uuid4

from .contracts import normalize_run_id
from .session import (
    CancellationRequestedError,
    DeadlineExceededError,
    RunSession,
)

T = TypeVar("T")

_CAPABILITY_PATTERN = re.compile(r"[a-z][a-z0-9_-]*:[a-z][a-z0-9_-]*")
_OPERATION_PATTERN = re.compile(
    r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+"
)
_ERROR_CODE_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")

_LLM_RESOURCE_FIELDS = frozenset({"role", "model_id", "prompt_hash"})
_SEARCH_RESOURCE_FIELDS = frozenset(
    {"query_hash", "backend", "cache_hit", "stored"}
)
_GITHUB_RESOURCE_FIELDS = frozenset({"owner", "repo", "resource_kind"})
_NOTES_RESOURCE_FIELDS = frozenset({"action", "note_kind", "note_id"})
_TEXT_RESOURCE_FIELDS = frozenset(
    {
        "action",
        "backend",
        "model_id",
        "note_id",
        "note_kind",
        "owner",
        "repo",
        "resource_kind",
        "role",
    }
)
_BOOLEAN_RESOURCE_FIELDS = frozenset({"cache_hit", "stored"})
_HASH_RESOURCE_FIELDS = frozenset({"prompt_hash", "query_hash"})
_POLICY_REASON_BY_OUTCOME = {
    "allow": "Capability allowed by policy.",
    "deny": "Capability denied by policy.",
    "ask": "Capability requires explicit approval.",
}

_SAFE_REPLAY_OPERATION_PREFIXES = (
    "planner.",
    "summarizer.",
    "reporter.",
    "quality_",
    "llm.",
    "search.",
    "github.",
    "notes.read",
)
_SIDE_EFFECTING_OPERATION_PREFIXES = (
    "notes.create",
    "notes.update",
)


def operation_replay_safety(operation_name: object) -> str:
    """Classify whether an operation may be safely replayed after recovery."""
    if not isinstance(operation_name, str):
        return "uncertain"
    if operation_name.startswith(_SIDE_EFFECTING_OPERATION_PREFIXES):
        return "side_effecting"
    if operation_name.startswith(_SAFE_REPLAY_OPERATION_PREFIXES):
        return "safe_replay"
    return "uncertain"


class OperationRejectedError(RuntimeError):
    """Signal that dynamic policy denied one governed operation."""

    def __init__(self, operation_id: str | None = None) -> None:
        """Initialize the rejection with the denied operation identifier."""
        super().__init__("The governed operation was rejected by policy.")
        self.operation_id = operation_id


def _positive_integer(value: object, *, field_name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{field_name} must be an integer.")
    if value < 1:
        raise ValueError(f"{field_name} must be at least 1.")
    return value


def _optional_positive_integer(value: object, *, field_name: str) -> int | None:
    if value is None:
        return None
    return _positive_integer(value, field_name=field_name)


def _normalized_capabilities(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError("Capabilities must be an iterable of capability names.")
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not _CAPABILITY_PATTERN.fullmatch(value):
            raise ValueError("Capability names must use the 'domain:action' form.")
        if value not in seen:
            normalized.append(value)
            seen.add(value)
    if not normalized:
        raise ValueError("At least one required capability is required.")
    return tuple(normalized)


def _resource_allowlist(
    operation_name: str,
    capabilities: tuple[str, ...],
) -> frozenset[str]:
    categories: set[str] = set()
    if "llm:invoke" in capabilities or operation_name.startswith(
        ("planner.", "summarizer.", "reporter.", "llm.")
    ):
        categories.add("llm")
    if any(capability.startswith("search:") for capability in capabilities) or (
        operation_name.startswith(("search.", "cache."))
    ):
        categories.add("search")
    if any(capability.startswith("github:") for capability in capabilities) or (
        operation_name.startswith("github.")
    ):
        categories.add("github")
    if any(capability.startswith("notes:") for capability in capabilities) or (
        operation_name.startswith(("notes.", "note."))
    ):
        categories.add("notes")
    if len(categories) > 1:
        raise ValueError("An operation may target only one resource category.")
    if not categories:
        return frozenset()
    category = next(iter(categories))
    return {
        "llm": _LLM_RESOURCE_FIELDS,
        "search": _SEARCH_RESOURCE_FIELDS,
        "github": _GITHUB_RESOURCE_FIELDS,
        "notes": _NOTES_RESOURCE_FIELDS,
    }[category]


def _validated_resource(
    operation_name: str,
    capabilities: tuple[str, ...],
    resource: Mapping[str, object],
) -> Mapping[str, object]:
    if not isinstance(resource, Mapping):
        raise TypeError("Operation resource must be a mapping.")
    if not all(isinstance(key, str) for key in resource):
        raise TypeError("Operation resource keys must be text.")
    allowlist = _resource_allowlist(operation_name, capabilities)
    unknown = set(resource) - allowlist
    if unknown:
        raise ValueError("Operation resource contains fields outside its allowlist.")

    detached: dict[str, object] = {}
    for key, value in resource.items():
        if key in _HASH_RESOURCE_FIELDS:
            if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
                raise ValueError(f"Operation resource {key!r} must be a SHA-256 hash.")
        elif key in _BOOLEAN_RESOURCE_FIELDS:
            if type(value) is not bool:
                raise TypeError(f"Operation resource {key!r} must be boolean.")
        elif key in _TEXT_RESOURCE_FIELDS:
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or len(value) > 256
            ):
                raise ValueError(
                    f"Operation resource {key!r} must be bounded non-empty text."
                )
        else:
            raise ValueError("Operation resource field is not supported.")
        detached[key] = value
    return MappingProxyType(detached)


@dataclass(frozen=True, kw_only=True)
class OperationSpec:
    """Immutable identity and safe metadata for one physical operation attempt."""

    operation_name: str
    capabilities: tuple[str, ...]
    resource: Mapping[str, object] = field(default_factory=dict)
    operation_id: str = field(default_factory=lambda: uuid4().hex)
    task_id: int | None = None
    task_attempt: int = 1
    operation_attempt: int = 1
    fallback_index: int = 1

    def __post_init__(self) -> None:
        """Normalize identity and reject unsafe metadata at construction."""
        if (
            not isinstance(self.operation_name, str)
            or not _OPERATION_PATTERN.fullmatch(self.operation_name)
        ):
            raise ValueError("Operation name must be a stable dotted identifier.")
        capabilities = _normalized_capabilities(self.capabilities)
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "operation_id", normalize_run_id(self.operation_id))
        object.__setattr__(
            self,
            "task_id",
            _optional_positive_integer(self.task_id, field_name="task_id"),
        )
        object.__setattr__(
            self,
            "task_attempt",
            _positive_integer(self.task_attempt, field_name="task_attempt"),
        )
        object.__setattr__(
            self,
            "operation_attempt",
            _positive_integer(
                self.operation_attempt,
                field_name="operation_attempt",
            ),
        )
        object.__setattr__(
            self,
            "fallback_index",
            _positive_integer(self.fallback_index, field_name="fallback_index"),
        )
        object.__setattr__(
            self,
            "resource",
            _validated_resource(self.operation_name, capabilities, self.resource),
        )

    @property
    def pairing_key(self) -> tuple[str, int, int, int]:
        """Return the exact key used for start/terminal audit pairing."""
        return (
            self.operation_id,
            self.task_attempt,
            self.fallback_index,
            self.operation_attempt,
        )

    def event_payload(self) -> dict[str, object]:
        """Return the trusted, detached operation event envelope."""
        return {
            "operation_name": self.operation_name,
            "capabilities": list(self.capabilities),
            "task_attempt": self.task_attempt,
            "operation_attempt": self.operation_attempt,
            "fallback_index": self.fallback_index,
            "resource": dict(self.resource),
        }


def _safe_policy_decision(decision: object, capability: str) -> dict[str, str]:
    if isinstance(decision, Mapping):
        raw = decision
    else:
        serializer = getattr(decision, "as_dict", None)
        if not callable(serializer):
            raise TypeError("Policy decisions must provide as_dict().")
        raw = serializer()
    if not isinstance(raw, Mapping):
        raise TypeError("Policy decision serialization must be a mapping.")

    decision_capability = raw.get("capability")
    outcome = raw.get("outcome")
    reason = raw.get("reason")
    if decision_capability != capability:
        raise ValueError("Policy decision capability does not match its request.")
    if outcome not in _POLICY_REASON_BY_OUTCOME:
        raise ValueError("Policy decision outcome is invalid.")
    if (
        not isinstance(reason, str)
        or not reason
        or reason != reason.strip()
        or len(reason) > 512
    ):
        raise ValueError("Policy decision reason must be bounded non-empty text.")
    return {
        "capability": capability,
        "outcome": outcome,
        "reason": _POLICY_REASON_BY_OUTCOME[outcome],
    }


class GovernedOperations:
    """Authorize and audit side effects against one authoritative run session."""

    def __init__(
        self,
        session: RunSession,
        policy: object,
        *,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        """Bind one run session to its policy and monotonic clock."""
        self._session = session
        self._policy = policy
        self._monotonic_clock = monotonic_clock

    @property
    def session(self) -> RunSession:
        """Return the run session governed by this instance."""
        return self._session

    def call(self, spec: OperationSpec, callback: Callable[[], T]) -> T:
        """Authorize, invoke once, and terminally audit a typed callback."""
        self._session.raise_if_cancelled()
        self._authorize(spec)
        self._session.raise_if_cancelled()
        started_at = self._monotonic_clock()
        self._session.start_operation(spec)
        try:
            self._session.raise_if_cancelled()
            result = callback()
            self._session.raise_if_cancelled()
        except BaseException as exc:
            self._session.fail_operation(
                spec,
                duration_seconds=self._duration_since(started_at),
                code=self._failure_code(exc),
            )
            raise
        self._session.complete_operation(
            spec,
            duration_seconds=self._duration_since(started_at),
        )
        return result

    def stream(
        self,
        spec: OperationSpec,
        iterator_factory: Callable[[], Iterable[T]],
    ) -> Iterator[T]:
        """Return a lazily authorized and terminally audited stream."""
        return self._stream(spec, iterator_factory)

    def _stream(
        self,
        spec: OperationSpec,
        iterator_factory: Callable[[], Iterable[T]],
    ) -> Iterator[T]:
        self._session.raise_if_cancelled()
        self._authorize(spec)
        self._session.raise_if_cancelled()
        started_at = self._monotonic_clock()
        self._session.start_operation(spec)
        delegate: Iterator[T] | None = None
        terminal = False
        try:
            self._session.raise_if_cancelled()
            delegate = iter(iterator_factory())
            while True:
                self._session.raise_if_cancelled()
                try:
                    chunk = next(delegate)
                except StopIteration:
                    self._close_delegate(delegate)
                    self._session.raise_if_cancelled()
                    self._session.complete_operation(
                        spec,
                        duration_seconds=self._duration_since(started_at),
                    )
                    terminal = True
                    return
                self._session.raise_if_cancelled()
                yield chunk
        except BaseException as exc:
            self._close_delegate(delegate)
            if not terminal:
                self._session.fail_operation(
                    spec,
                    duration_seconds=self._duration_since(started_at),
                    code=self._failure_code(exc),
                )
                terminal = True
            raise

    def _authorize(self, spec: OperationSpec) -> None:
        evaluator = getattr(self._policy, "evaluate_capability", None)
        if not callable(evaluator):
            raise TypeError("Operation policy must provide evaluate_capability().")
        decisions = [
            _safe_policy_decision(
                evaluator(capability, self._session.command),
                capability,
            )
            for capability in spec.capabilities
        ]
        self._session.append_policy_decisions(decisions)
        if any(item["outcome"] in {"deny", "ask"} for item in decisions):
            self._session.reject_operation(spec, code="operation_rejected")
            raise OperationRejectedError(spec.operation_id)

    def _duration_since(self, started_at: float) -> float:
        duration = self._monotonic_clock() - started_at
        if not math.isfinite(duration):
            return 0.0
        return max(0.0, duration)

    @staticmethod
    def _failure_code(exc: BaseException) -> str:
        if isinstance(exc, DeadlineExceededError):
            return "deadline_exceeded"
        if isinstance(exc, CancellationRequestedError):
            return "cancelled"
        if isinstance(exc, GeneratorExit):
            return "stream_closed"
        if isinstance(exc, OperationRejectedError):
            return "operation_rejected"
        return "operation_failed"

    @staticmethod
    def _close_delegate(delegate: Iterator[Any] | None) -> None:
        if delegate is None:
            return
        close = getattr(delegate, "close", None)
        if not callable(close):
            return
        try:
            close()
        except Exception:
            return


@dataclass(frozen=True, kw_only=True)
class OperationScope:
    """Immutable per-invocation context passed explicitly to adapters."""

    operations: GovernedOperations
    task_id: int | None = None
    task_attempt: int = 1
    fallback_index: int = 1

    def __post_init__(self) -> None:
        """Validate task coordinates without storing mutable current state."""
        if not isinstance(self.operations, GovernedOperations):
            raise TypeError("OperationScope requires GovernedOperations.")
        object.__setattr__(
            self,
            "task_id",
            _optional_positive_integer(self.task_id, field_name="task_id"),
        )
        object.__setattr__(
            self,
            "task_attempt",
            _positive_integer(self.task_attempt, field_name="task_attempt"),
        )
        object.__setattr__(
            self,
            "fallback_index",
            _positive_integer(self.fallback_index, field_name="fallback_index"),
        )

    @property
    def governance(self) -> GovernedOperations:
        """Expose a descriptive alias for the run-bound operations object."""
        return self.operations

    def spec(
        self,
        *,
        operation_name: str,
        capabilities: Iterable[str],
        resource: Mapping[str, object] | None = None,
        operation_id: str | None = None,
        operation_attempt: int = 1,
    ) -> OperationSpec:
        """Build one immutable physical-attempt spec from this scope."""
        values: dict[str, object] = {
            "operation_name": operation_name,
            "capabilities": tuple(capabilities),
            "resource": resource or {},
            "task_id": self.task_id,
            "task_attempt": self.task_attempt,
            "operation_attempt": operation_attempt,
            "fallback_index": self.fallback_index,
        }
        if operation_id is not None:
            values["operation_id"] = operation_id
        return OperationSpec(**values)  # type: ignore[arg-type]


def validate_operation_error_code(code: object) -> str:
    """Return a stable operation error code or reject unsafe dynamic text."""
    if not isinstance(code, str) or not _ERROR_CODE_PATTERN.fullmatch(code):
        raise ValueError("Operation error code is invalid.")
    return code


__all__ = [
    "GovernedOperations",
    "OperationRejectedError",
    "OperationScope",
    "OperationSpec",
    "operation_replay_safety",
    "validate_operation_error_code",
]
