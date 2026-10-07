"""Strict research plan schema boundaries for planner responses.

The planner boundary converts one free-form LLM response into validated,
bounded ``ResearchPlanTask`` objects.  Parsing fails open: malformed tasks
are rejected individually and never abort the surrounding research run,
mirroring the planning layer's fail-open policy (the evidence layer, by
contrast, fails closed).

Parsing degrades deterministically across three strategies:

1. direct: the whole response is one JSON document;
2. fenced: the response embeds one fenced ```` ```json ```` code block;
3. brace: the response embeds a JSON object or array between braces.

An explicit empty plan (``{"tasks": []}``) means "the topic carries too
little information to plan" and is preserved as a distinct outcome from
an unparsable response.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

_FENCED_BLOCK_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)
_TITLE_MAX_CHARS = 128
_TEXT_MAX_CHARS = 512
_MAX_TASKS = 16
_PARSE_STRATEGIES = ("direct", "fenced", "brace", "empty", "unparsed")


class PlanSchemaError(ValueError):
    """Reject malformed or out-of-bounds planner schema payloads."""


def _bounded_text(value: object, *, field_name: str, limit: int) -> str:
    """Return normalized non-empty text or raise ``PlanSchemaError``."""
    if not isinstance(value, str):
        raise PlanSchemaError(f"{field_name} must be text.")
    normalized = value.strip()
    if not normalized:
        raise PlanSchemaError(f"{field_name} must not be empty.")
    if len(normalized) > limit:
        raise PlanSchemaError(f"{field_name} exceeds its length bound.")
    return normalized


@dataclass(frozen=True, kw_only=True, slots=True)
class ResearchPlanTask:
    """One bounded, validated research task from a planner response."""

    title: str
    intent: str
    query: str

    def __post_init__(self) -> None:
        """Validate bounded non-empty fields."""
        object.__setattr__(
            self,
            "title",
            _bounded_text(self.title, field_name="title", limit=_TITLE_MAX_CHARS),
        )
        object.__setattr__(
            self,
            "intent",
            _bounded_text(self.intent, field_name="intent", limit=_TEXT_MAX_CHARS),
        )
        object.__setattr__(
            self,
            "query",
            _bounded_text(self.query, field_name="query", limit=_TEXT_MAX_CHARS),
        )

    @classmethod
    def from_payload(cls, payload: object) -> ResearchPlanTask:
        """Build one task from an untrusted mapping, rejecting bad fields."""
        if not isinstance(payload, Mapping):
            raise PlanSchemaError("Plan task must be a JSON object.")
        raw_title = payload.get("title")
        raw_intent = payload.get("intent")
        raw_query = payload.get("query")
        if (
            not isinstance(raw_title, str)
            or not isinstance(raw_intent, str)
            or not isinstance(raw_query, str)
        ):
            raise PlanSchemaError("Plan task fields must all be present text.")
        return cls(title=raw_title, intent=raw_intent, query=raw_query)


@dataclass(frozen=True, kw_only=True, slots=True)
class ResearchPlan:
    """A validated plan of zero or more bounded research tasks."""

    tasks: tuple[ResearchPlanTask, ...]
    parse_strategy: str
    rejected_task_count: int = 0
    truncated_task_count: int = 0

    def __post_init__(self) -> None:
        """Validate the strategy vocabulary and bounded task tuple."""
        if self.parse_strategy not in _PARSE_STRATEGIES:
            raise PlanSchemaError("parse_strategy is unsupported.")
        if len(self.tasks) > _MAX_TASKS:
            raise PlanSchemaError("Plan exceeds the bounded task count.")
        for field_name in ("rejected_task_count", "truncated_task_count"):
            value = getattr(self, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                raise PlanSchemaError(f"{field_name} must be a non-negative integer.")

    @property
    def is_explicit_empty(self) -> bool:
        """Return whether the planner explicitly reported no plannable tasks."""
        return (
            not self.tasks
            and self.parse_strategy in {"direct", "fenced", "brace"}
            and self.rejected_task_count == 0
            and self.truncated_task_count == 0
        )


def parse_plan_payload(text: str) -> ResearchPlan:
    """Parse one planner response into a validated plan, failing open.

    Unknown payload fields are tolerated per task; missing or invalid
    required fields reject only that task.  An unparsable response returns
    an empty plan with strategy ``unparsed`` so the caller can fall back.
    """
    normalized = (text or "").strip()
    if not normalized:
        return ResearchPlan(tasks=(), parse_strategy="empty")
    for strategy, candidate in _iter_json_candidates(normalized):
        payload = _try_loads(candidate)
        if payload is not None:
            plan = _plan_from_payload(payload, strategy)
            if plan is not None:
                return plan
    return ResearchPlan(tasks=(), parse_strategy="unparsed")


def _iter_json_candidates(text: str) -> Iterator[tuple[str, str]]:
    """Yield descending-fidelity JSON extraction candidates."""
    yield "direct", text
    for match in _FENCED_BLOCK_RE.finditer(text):
        yield "fenced", match.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        yield "brace", text[start : end + 1]
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end > start:
        yield "brace", text[start : end + 1]


def _try_loads(candidate: str) -> Any:
    """Return a decoded JSON container or ``None`` for any failure."""
    try:
        decoded = json.loads(candidate)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if isinstance(decoded, (dict, list)):
        return decoded
    return None


def _plan_from_payload(payload: Any, strategy: str) -> ResearchPlan | None:
    """Validate one decoded container, rejecting invalid shapes as ``None``."""
    raw_tasks: Any
    if isinstance(payload, Mapping):
        if "tasks" not in payload:
            return None
        raw_tasks = payload.get("tasks")
        if raw_tasks is None:
            raw_tasks = []
    elif isinstance(payload, list):
        raw_tasks = payload
    else:
        return None
    if not isinstance(raw_tasks, (list, tuple)):
        return None
    tasks: list[ResearchPlanTask] = []
    rejected = 0
    truncated = 0
    for item in raw_tasks:
        try:
            task = ResearchPlanTask.from_payload(item)
        except PlanSchemaError:
            rejected += 1
            continue
        if len(tasks) >= _MAX_TASKS:
            truncated += 1
            continue
        tasks.append(task)
    return ResearchPlan(
        tasks=tuple(tasks),
        parse_strategy=strategy,
        rejected_task_count=rejected,
        truncated_task_count=truncated,
    )


__all__ = [
    "PlanSchemaError",
    "ResearchPlan",
    "ResearchPlanTask",
    "parse_plan_payload",
]
