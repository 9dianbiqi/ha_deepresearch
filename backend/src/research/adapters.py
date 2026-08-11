"""Typed adapters for hello-agents and other external side-effect ports."""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Callable, Iterable, Iterator
from threading import Lock, local
from typing import Any

from hello_agents.tools import SearchTool

from .operations import OperationScope
from .telemetry import current_llm_telemetry_retry_count, llm_telemetry_scope

OPERATION_SCOPE_KWARG = "_research_operation_scope"
_SEARCH_TOOL_INIT_LOCK = Lock()
_SEARCH_TOOL_NOTICE_SAMPLE = "⚠️"
_LLM_PUBLIC_DATA_ATTRIBUTES = frozenset({"provider"})


class MissingOperationScopeError(RuntimeError):
    """Signal that a governed framework call omitted its per-run scope."""


def _require_operation_scope(value: object) -> OperationScope:
    if not isinstance(value, OperationScope):
        raise MissingOperationScopeError(
            "A governed framework operation requires an explicit run scope."
        )
    return value


def _preserve_stdout_for_search_tool_notices() -> None:
    """Make real console encoding lossless without replacing process stdout."""
    stdout = sys.stdout
    encoding = getattr(stdout, "encoding", None)
    errors = getattr(stdout, "errors", None)
    if not isinstance(encoding, str):
        return
    error_handler = errors if isinstance(errors, str) else "strict"
    try:
        _SEARCH_TOOL_NOTICE_SAMPLE.encode(encoding, errors=error_handler)
        return
    except (LookupError, UnicodeEncodeError):
        pass

    reconfigure = getattr(stdout, "reconfigure", None)
    if not callable(reconfigure):
        raise RuntimeError(
            "SearchTool requires a reconfigurable stdout for this console encoding."
        )
    reconfigure(errors="backslashreplace")


def _messages_hash(messages: Iterable[dict[str, str]]) -> str:
    serialized = json.dumps(
        list(messages),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class GovernedHelloAgentsLLM:
    """Govern public hello-agents LLM methods without private-field access."""

    def __init__(
        self,
        delegate: object,
        *,
        role: str,
        model_id: str | None = None,
    ) -> None:
        """Wrap one public LLM delegate for the named research role."""
        self._delegate = delegate
        self.role = role
        self.model = model_id or getattr(delegate, "model", None)

    def __getattr__(self, name: str) -> Any:
        """Expose only reviewed, non-callable framework data attributes."""
        if name not in _LLM_PUBLIC_DATA_ATTRIBUTES:
            raise AttributeError(name)
        delegate = object.__getattribute__(self, "_delegate")
        value = getattr(delegate, name)
        if callable(value):
            raise AttributeError(name)
        return value

    def invoke(
        self,
        messages: list[dict[str, str]],
        **kwargs: Any,
    ) -> str:
        """Authorize one non-streaming invocation before public delegation."""
        scope = _require_operation_scope(kwargs.pop(OPERATION_SCOPE_KWARG, None))
        spec = scope.spec(
            operation_name=f"{self.role}.complete",
            capabilities=("llm:invoke",),
            resource=self._resource(messages),
        )
        invoke = getattr(self._delegate, "invoke")
        with llm_telemetry_scope(
            scope.operations.session,
            role=self.role,
            retry_count=current_llm_telemetry_retry_count(),
        ):
            return scope.operations.call(spec, lambda: invoke(messages, **kwargs))

    def stream_invoke(
        self,
        messages: list[dict[str, str]],
        **kwargs: Any,
    ) -> Iterator[str]:
        """Return a lazily authorized stream using the public delegate method."""
        scope = _require_operation_scope(kwargs.pop(OPERATION_SCOPE_KWARG, None))
        spec = scope.spec(
            operation_name=f"{self.role}.stream",
            capabilities=("llm:invoke",),
            resource=self._resource(messages),
        )
        stream_invoke = getattr(self._delegate, "stream_invoke")
        def governed_stream() -> Iterator[str]:
            with llm_telemetry_scope(
                scope.operations.session,
                role=self.role,
                retry_count=current_llm_telemetry_retry_count(),
            ):
                yield from scope.operations.stream(
                    spec,
                    lambda: stream_invoke(messages, **kwargs),
                )

        return governed_stream()

    def _resource(self, messages: list[dict[str, str]]) -> dict[str, object]:
        resource: dict[str, object] = {
            "role": self.role,
            "prompt_hash": _messages_hash(messages),
        }
        if isinstance(self.model, str) and self.model.strip():
            resource["model_id"] = self.model.strip()
        return resource


class HelloAgentsSearchAdapter:
    """Expose SearchTool's public ``run`` API with per-thread lazy instances."""

    def __init__(
        self,
        *,
        tool_factory: Callable[..., object] = SearchTool,
    ) -> None:
        """Initialize thread-local search tool storage."""
        self._tool_factory = tool_factory
        self._local = local()

    def run(self, parameters: dict[str, Any]) -> str | dict[str, Any]:
        """Run one structured search after its caller has authorized the attempt."""
        tool = self._tool()
        run = getattr(tool, "run")
        return run(dict(parameters))

    def _tool(self) -> object:
        tool = getattr(self._local, "tool", None)
        if tool is not None:
            return tool
        with _SEARCH_TOOL_INIT_LOCK:
            tool = getattr(self._local, "tool", None)
            if tool is None:
                # hello-agents 0.2.9 prints emoji-bearing setup notices during
                # construction. Preserve them using the real stdout with a
                # non-lossy error handler; never swap the process-global stream.
                _preserve_stdout_for_search_tool_notices()
                tool = self._tool_factory(backend="hybrid")
                self._local.tool = tool
        return tool


class GovernedGitHubAdapter:
    """Authorize one repository aggregation before constructing its client."""

    def __init__(
        self,
        *,
        client_factory: Callable[..., object] | None = None,
    ) -> None:
        """Store the optional repository client factory."""
        self._client_factory = client_factory

    def collect_repository_context(
        self,
        target: Any,
        *,
        operation_scope: OperationScope,
        token: str | None = None,
        base_url: str = "https://api.github.com",
    ) -> Any:
        """Collect one bounded repository context under ``github:read``."""
        spec = operation_scope.spec(
            operation_name="github.collect",
            capabilities=("github:read",),
            resource={
                "owner": target.owner,
                "repo": target.repo,
                "resource_kind": "repository_context",
            },
        )

        def collect() -> Any:
            factory = self._client_factory
            client: object
            if factory is None:
                from services.github_research import GitHubResearchClient

                client = GitHubResearchClient(
                    token=token,
                    base_url=base_url,
                    checkpoint=operation_scope.operations.session.raise_if_cancelled,
                )
            else:
                client = factory(token=token, base_url=base_url)
            collect_repository_context = getattr(
                client,
                "collect_repository_context",
            )
            return collect_repository_context(target)

        return operation_scope.operations.call(spec, collect)


__all__ = [
    "GovernedHelloAgentsLLM",
    "GovernedGitHubAdapter",
    "HelloAgentsSearchAdapter",
    "MissingOperationScopeError",
    "OPERATION_SCOPE_KWARG",
]
