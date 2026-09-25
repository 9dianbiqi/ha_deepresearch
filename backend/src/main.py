"""FastAPI entrypoint exposing the DeepResearchAgent via HTTP."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping
from hmac import compare_digest
from pathlib import Path, PurePosixPath
from threading import BoundedSemaphore
from time import monotonic
from typing import Any, AsyncIterator, Callable, Dict, Iterator, Literal
from urllib.parse import quote, urlsplit

import anyio
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field, field_validator

from config import Configuration, SearchAPI
from harness import HarnessRunner, HarnessRunRequest, build_default_scenarios
from research.artifacts import FileArtifactStore, normalize_artifact_id
from research.contracts import normalize_run_id
from research.intelligence import ArtifactDescriptorV2
from research.memory import (
    MemoryNotFoundError,
    MemoryStateError,
    MemoryStoreUnavailableError,
    MemoryValidationError,
)
from research.profiles import ResearchMode
from research.repository import (
    CorruptRunRecordError,
    InvalidRunIdError,
    RunNotFoundError,
    RunRepositoryError,
    UnsupportedSchemaError,
)
from services.search import sweep_search_cache

load_dotenv()

_DEFAULT_CORS_ORIGINS = (
    "http://localhost:5173",
    "http://localhost:5174",
    "http://localhost:3000",
)
_MAX_TOPIC_LENGTH = 4000
_MAX_METADATA_BYTES = 16 * 1024
_PUBLIC_PROBE_PATHS = frozenset({"/healthz", "/readyz"})
_UNCONFIGURED_API_KEY_SENTINEL = "\x00app-api-key-unconfigured\x00"

# 添加控制台日志处理程序
logger.add(
    sys.stderr,
    level="INFO",
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <4}</level> | <cyan>using_function:{function}</cyan> | <cyan>{file}:{line}</cyan> | <level>{message}</level>",
    colorize=True,
)


class ResearchRequest(BaseModel):
    """Payload for triggering a research run."""

    topic: str = Field(
        ...,
        min_length=1,
        max_length=_MAX_TOPIC_LENGTH,
        description="Research topic supplied by the user",
    )
    research_mode: ResearchMode | None = Field(
        default=None,
        description="Optional explicit research mode; omitted means automatic detection",
    )
    research_profile: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$",
        description="Optional ID of a registered research profile",
    )
    search_api: SearchAPI | None = Field(
        default=None,
        description="Override the default search backend configured via env",
    )
    parent_run_id: str | None = Field(
        default=None,
        description="Previous run_id for multi-turn follow-up research",
    )
    use_history_memory: bool = Field(
        default=True,
        description="Automatically recall related completed research for planning",
    )
    memory_scope: str = Field(
        default="default",
        min_length=1,
        max_length=64,
        description="Local user-memory scope used by the planner",
    )


class ResearchResponse(BaseModel):
    """HTTP response containing the generated report and structured tasks."""

    report_markdown: str = Field(
        ..., description="Markdown-formatted research report including sections"
    )
    todo_items: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Structured TODO items with summaries and sources",
    )
    research_mode: str | None = Field(
        default=None,
        description="Resolved research mode for the run",
    )
    research_profile_id: str | None = Field(
        default=None,
        description="Resolved research profile ID",
    )
    source_context: dict[str, Any] = Field(
        default_factory=dict,
        description="Provider-neutral source context metadata",
    )
    research_intelligence: dict[str, Any] = Field(
        default_factory=dict,
        description="Provider-neutral schema-v2 intelligence bundle",
    )
    github_intelligence: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional versioned GitHub evidence intelligence bundle",
    )
    structured_summary: dict[str, Any] = Field(default_factory=dict)
    quality_assessment: dict[str, Any] = Field(default_factory=dict)


class ContinueRequest(BaseModel):
    """Payload for continuing a previous research run with a follow-up topic."""

    topic: str = Field(
        ...,
        min_length=1,
        max_length=_MAX_TOPIC_LENGTH,
        description="Follow-up research topic",
    )
    parent_run_id: str = Field(..., description="run_id of the previous research to build upon")
    research_mode: ResearchMode | None = Field(
        default=None,
        description="Optional explicit research mode for the follow-up",
    )
    research_profile: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$",
        description="Optional ID of a registered research profile",
    )
    search_api: SearchAPI | None = Field(
        default=None,
        description="Override the default search backend",
    )
    use_history_memory: bool = Field(
        default=True,
        description="Automatically recall related completed research for planning",
    )
    memory_scope: str = Field(
        default="default",
        min_length=1,
        max_length=64,
        description="Local user-memory scope used by the planner",
    )


class RecoveryRequest(BaseModel):
    """Payload for resuming one failed run from its durable checkpoint."""

    run_id: str = Field(..., description="Persisted run ID to recover")


class MemoryCandidateRequest(BaseModel):
    """Payload for an explicit user memory candidate."""

    text: str = Field(..., min_length=1, max_length=240)
    kind: Literal["preference", "fact"] = "preference"
    scope: str = Field(default="default", min_length=1, max_length=64)


class HarnessRequest(ResearchRequest):
    """Payload for a harness-managed run."""

    permission_mode: str = Field(
        default="default",
        description="Permission policy mode, for example default or strict.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional caller metadata attached to the run record.",
    )

    @field_validator("metadata")
    @classmethod
    def _validate_metadata_size(
        cls,
        value: dict[str, Any],
    ) -> dict[str, Any]:
        """Reject oversized metadata before it reaches the research lifecycle."""
        try:
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Metadata must be JSON serializable.") from exc
        if len(encoded.encode("utf-8")) > _MAX_METADATA_BYTES:
            raise ValueError("Metadata exceeds the configured size limit.")
        return value



class HarnessResponse(BaseModel):
    """HTTP response for a harness-managed run."""

    run_id: str
    status: str
    report_markdown: str = ""
    todo_items: list[dict[str, Any]] = Field(default_factory=list)
    research_mode: str | None = None
    research_profile_id: str | None = None
    source_context: dict[str, Any] = Field(default_factory=dict)
    research_intelligence: dict[str, Any] = Field(default_factory=dict)
    github_intelligence: dict[str, Any] = Field(default_factory=dict)
    structured_summary: dict[str, Any] = Field(default_factory=dict)
    quality_assessment: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    findings: list[dict[str, Any]] = Field(default_factory=list)
    compressed_context: dict[str, Any] = Field(default_factory=dict)
    policy_decisions: list[dict[str, Any]] = Field(default_factory=list)
    resumable: bool = False
    recovery_resumable: bool = False
    last_resumable_parent: str | None = None
    mode: str = "internal"


def _configuration_presence(value: str | None) -> str:
    """Describe whether a sensitive setting exists without returning its value."""
    return "configured" if value else "unset"


def _configured_app_api_key() -> str | None:
    """Read the HTTP API key without retaining it in application configuration."""
    value = os.getenv("APP_API_KEY")
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _configured_cors_origins(raw_value: str | None = None) -> list[str]:
    """Parse an explicit browser-origin allowlist and reject wildcard access."""
    configured = os.getenv("CORS_ORIGINS") if raw_value is None else raw_value
    candidates = (
        configured.split(",")
        if configured is not None and configured.strip()
        else list(_DEFAULT_CORS_ORIGINS)
    )
    origins: list[str] = []
    for candidate in candidates:
        origin = candidate.strip().rstrip("/")
        if not origin:
            continue
        if origin == "*":
            raise ValueError("CORS_ORIGINS must contain explicit origins, not '*'.")
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "CORS_ORIGINS entries must be HTTP(S) origins without paths or credentials."
            )
        if origin not in origins:
            origins.append(origin)
    if not origins:
        raise ValueError("CORS_ORIGINS must contain at least one explicit origin.")
    return origins


def _server_bind_config() -> tuple[str, int]:
    """Return an explicit development bind address with a loopback-safe default."""
    host = (os.getenv("HOST") or "127.0.0.1").strip()
    if not host:
        host = "127.0.0.1"
    raw_port = (os.getenv("PORT") or "8000").strip()
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError("PORT must be an integer between 1 and 65535.") from exc
    if not 1 <= port <= 65535:
        raise ValueError("PORT must be an integer between 1 and 65535.")
    return host, port


def _build_config(payload: ResearchRequest) -> Configuration:
    overrides: Dict[str, Any] = {}

    if payload.search_api is not None:
        overrides["search_api"] = payload.search_api

    return Configuration.from_env(overrides=overrides)


def _serialize_todo_items(items: list[Any]) -> list[dict[str, Any]]:
    """Normalize todo items for API responses."""
    return [item.to_dict() if hasattr(item, "to_dict") else item for item in items]


def _normalize_harness_request(
    payload: ResearchRequest,
    *,
    caller_mode: str,
    permission_mode: str = "default",
    metadata: dict[str, Any] | None = None,
) -> HarnessRunRequest:
    """Convert API payloads into the unified harness request contract."""
    return HarnessRunRequest(
        topic=payload.topic,
        config=_build_config(payload),
        metadata=dict(metadata or {}),
        permission_mode=permission_mode,
        caller_mode=caller_mode,
        parent_run_id=payload.parent_run_id,
        research_mode=payload.research_mode,
        research_profile_id=payload.research_profile,
        use_history_memory=payload.use_history_memory,
        memory_scope=payload.memory_scope,
    )


def _build_harness_response(result: Any, *, mode: str) -> HarnessResponse:
    """Convert a harness result into the public HTTP response model."""
    output = result.output
    return HarnessResponse(
        run_id=result.run_id,
        status=result.status,
        report_markdown=(output.report_markdown or output.running_summary or "") if output else "",
        todo_items=_serialize_todo_items(output.todo_items if output else []),
        research_mode=(
            output.research_mode
            if output and getattr(output, "research_mode", None)
            else None
        ),
        research_profile_id=(
            output.research_profile_id
            if output and getattr(output, "research_profile_id", None)
            else None
        ),
        source_context=(
            dict(output.source_context)
            if output and getattr(output, "source_context", None)
            else {}
        ),
        research_intelligence=(
            dict(output.research_intelligence)
            if output and getattr(output, "research_intelligence", None)
            else {}
        ),
        github_intelligence=(
            dict(output.github_intelligence)
            if output and getattr(output, "github_intelligence", None)
            else {}
        ),
        structured_summary=(
            dict(output.structured_summary)
            if output and getattr(output, "structured_summary", None)
            else {}
        ),
        quality_assessment=(
            dict(output.quality_assessment)
            if output and getattr(output, "quality_assessment", None)
            else {}
        ),
        metrics=result.metrics,
        findings=[
            {
                "severity": item.severity,
                "message": item.message,
                "code": item.code,
            }
            for item in result.findings
        ],
        compressed_context=result.compressed_context,
        policy_decisions=result.policy_decisions,
        resumable=bool(getattr(result, "resumable", False)),
        recovery_resumable=bool(getattr(result, "recovery_resumable", False)),
        last_resumable_parent=getattr(result, "last_resumable_parent", None),
        mode=mode,
    )


_RUN_ERROR_RESPONSES: dict[str, tuple[int, str]] = {
    "invalid_command": (400, "The research command is invalid."),
    "invalid_run_id": (400, "The recovery run ID is invalid."),
    "policy_rejected": (403, "The research command was rejected by policy."),
    "operation_rejected": (403, "A research operation was rejected by policy."),
    "parent_not_found": (404, "The parent run was not found."),
    "parent_not_resumable": (409, "The parent run is not resumable."),
    "parent_pending": (409, "The parent run is not yet available."),
    "run_already_active": (409, "A run with this ID is already active."),
    "runner_busy": (409, "The research service is busy."),
    "cancelled": (409, "The research run was cancelled."),
    "deadline_exceeded": (408, "The research run deadline was exceeded."),
    "parent_corrupt": (500, "The parent run record is unavailable."),
    "checkpoint_not_found": (404, "The recovery checkpoint was not found."),
    "checkpoint_corrupt": (500, "The recovery checkpoint is corrupt."),
    "checkpoint_version_unsupported": (501, "The recovery checkpoint version is unsupported."),
    "checkpoint_not_resumable": (409, "The recovery checkpoint is not resumable."),
    "run_not_resumable": (409, "The run has no valid recovery checkpoint."),
    "recovery_unsupported": (501, "Run recovery is not supported by this coordinator."),
    "recovery_state_conflict": (409, "The recovery state is no longer valid."),
    "checkpoint_persistence_failed": (500, "The recovery checkpoint could not be persisted."),
    "repository_error": (500, "The run repository is unavailable."),
    "persistence_failed": (500, "The research run could not be persisted."),
    "policy_error": (500, "Research policy evaluation failed."),
    "coordinator_failed": (500, "Research coordination failed."),
    "terminal_validation_failed": (500, "Research result validation failed."),
    "context_projection_failed": (500, "Research context projection failed."),
    "report_incomplete": (500, "The research report is incomplete."),
    "application_error": (500, "The research run failed."),
    "missing_terminal": (500, "The research run ended unexpectedly."),
}


def _safe_http_error(
    *,
    status_code: int,
    code: str,
    message: str,
) -> HTTPException:
    """Build a stable HTTP error without reflecting exception text."""
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )


class _RunCapacity:
    """Non-blocking process-local admission control for top-level runs."""

    def __init__(self, limit: int) -> None:
        """Create a bounded run gate."""
        if limit < 1:
            raise ValueError("MAX_CONCURRENT_RUNS must be positive.")
        self.limit = limit
        self._semaphore = BoundedSemaphore(limit)

    def try_acquire(self) -> bool:
        """Reserve one run slot without waiting for another request."""
        return self._semaphore.acquire(blocking=False)

    def release(self) -> None:
        """Release one previously reserved run slot."""
        self._semaphore.release()


def _configured_run_capacity() -> int:
    """Read and validate the single-process run admission limit."""
    try:
        return Configuration.from_env().max_concurrent_runs
    except (TypeError, ValueError) as exc:
        raise ValueError("MAX_CONCURRENT_RUNS must be an integer between 1 and 16.") from exc


def _request_validation_response(
    _request: Request,
    _exc: Exception,
) -> JSONResponse:
    """Return a stable validation error without echoing request bodies."""
    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "code": "invalid_request",
                "message": "The request payload is invalid.",
            }
        },
    )


def _raise_for_failed_result(result: Any) -> None:
    """Translate a typed non-success run result to a stable HTTP error."""
    if getattr(result, "status", None) == "completed":
        return

    raw_code = getattr(result, "error_code", None)
    code = raw_code if raw_code in _RUN_ERROR_RESPONSES else "application_error"
    status_code, message = _RUN_ERROR_RESPONSES[code]
    detail: dict[str, Any] = {"code": code, "message": message}
    anchor = getattr(result, "last_resumable_parent", None)
    if isinstance(anchor, str) and anchor:
        detail["last_resumable_parent"] = anchor
    raise HTTPException(status_code=status_code, detail=detail)


def _stream_error_event(
    request: HarnessRunRequest | None,
    *,
    sequence: int,
    code: str = "stream_failed",
    run_id: str | None = None,
) -> dict[str, Any]:
    """Return the public, non-reflective stream failure envelope."""
    resolved_run_id = request.run_id if request is not None else run_id
    if not isinstance(resolved_run_id, str) or not resolved_run_id:
        resolved_run_id = "unknown"
    return {
        "type": "error",
        "run_id": resolved_run_id,
        "schema_version": 1,
        "sequence": sequence,
        "code": code,
        "detail": "The research stream ended unexpectedly.",
    }


_STREAM_END = object()


def _next_or_end(iterator: Iterator[dict[str, Any]]) -> dict[str, Any] | object:
    """Advance a synchronous iterator without leaking ``StopIteration``."""
    try:
        return next(iterator)
    except StopIteration:
        return _STREAM_END


async def _iter_sse_events(
    harness_runner: Any,
    request: HarnessRunRequest | None,
    *,
    stream_factory: Callable[[], Iterator[dict[str, Any]]] | None = None,
    run_id: str | None = None,
    on_close: Callable[[], None] | None = None,
) -> AsyncIterator[str]:
    """Serialize one runner stream and own its terminal/error boundary."""
    iterator: Iterator[dict[str, Any]] | None = None
    next_sequence = 1
    stream_started_at = monotonic()
    event_count = 0
    bytes_sent = 0
    first_event_latency_ms: float | None = None
    resolved_run_id = request.run_id if request is not None else run_id

    def encode_event(event: dict[str, Any], *, terminal: bool = False) -> str:
        """Serialize one SSE frame and attach bounded transport telemetry."""
        nonlocal bytes_sent, event_count, first_event_latency_ms
        event_count += 1
        if first_event_latency_ms is None:
            first_event_latency_ms = round(
                max(0.0, monotonic() - stream_started_at) * 1000,
                3,
            )
        rendered_event = dict(event)
        previous_bytes = bytes_sent
        if terminal:
            telemetry = {
                "duration_ms": round(
                    max(0.0, monotonic() - stream_started_at) * 1000,
                    3,
                ),
                "event_count": event_count,
                "bytes_sent": 0,
                "first_event_latency_ms": first_event_latency_ms,
                "stream_completed": rendered_event.get("type") == "done",
                "terminal_type": rendered_event.get("type"),
            }
            rendered_event["stream_telemetry"] = telemetry
        rendered = (
            f"data: {json.dumps(rendered_event, ensure_ascii=False)}\n\n"
        )
        if terminal:
            terminal_telemetry = rendered_event.get("stream_telemetry")
            if isinstance(terminal_telemetry, dict):
                terminal_telemetry["bytes_sent"] = previous_bytes + len(
                    rendered.encode("utf-8")
                )
                rendered = (
                    f"data: {json.dumps(rendered_event, ensure_ascii=False)}\n\n"
                )
                bytes_sent = previous_bytes + len(rendered.encode("utf-8"))
        else:
            bytes_sent = previous_bytes + len(rendered.encode("utf-8"))
        return rendered

    try:
        if stream_factory is not None:
            iterator = iter(stream_factory())
        else:
            if request is None:
                raise ValueError("A research request is required for a normal stream.")
            iterator = iter(harness_runner.stream(request))
        while True:
            item = await anyio.to_thread.run_sync(
                _next_or_end,
                iterator,
                abandon_on_cancel=True,
            )
            if item is _STREAM_END:
                yield encode_event(
                    _stream_error_event(
                        request,
                        sequence=next_sequence,
                        run_id=resolved_run_id,
                    ),
                    terminal=True,
                )
                return
            event = item
            if not isinstance(event, dict):
                raise TypeError("Research stream events must be objects.")
            sequence = event.get("sequence") if isinstance(event, dict) else None
            if isinstance(sequence, int) and not isinstance(sequence, bool):
                next_sequence = max(next_sequence, sequence + 1)
            terminal = event.get("type") in {"done", "error"}
            yield encode_event(event, terminal=terminal)
            if terminal:
                return
    except Exception:
        logger.warning("Research stream failed at the SSE boundary.")
        yield encode_event(
            _stream_error_event(
                request,
                sequence=next_sequence,
                run_id=resolved_run_id,
            ),
            terminal=True,
        )
    finally:
        close = getattr(iterator, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                logger.warning("Research stream iterator close failed.")
        if on_close is not None:
            on_close()


def _streaming_response(
    harness_runner: Any,
    request: HarnessRunRequest | None,
    *,
    stream_factory: Callable[[], Iterator[dict[str, Any]]] | None = None,
    run_id: str | None = None,
    on_close: Callable[[], None] | None = None,
) -> StreamingResponse:
    """Build the shared SSE response for initial and follow-up research."""
    return StreamingResponse(
        _iter_sse_events(
            harness_runner,
            request,
            stream_factory=stream_factory,
            run_id=run_id,
            on_close=on_close,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


_ARTIFACT_FILENAMES = {
    "report_markdown": "report.md",
    "evidence_json": "evidence.json",
    "report_html": "report.html",
    "chart_svg": "languages.svg",
    "mermaid": "repository-structure.mmd",
}
_MIME_SUFFIXES = {
    "application/json": ".json",
    "text/markdown": ".md",
    "text/html": ".html",
    "image/svg+xml": ".svg",
    "text/plain": ".txt",
    "application/pdf": ".pdf",
}


def _artifact_descriptor_for_run(
    record: Mapping[str, Any],
    artifact_id: str,
) -> ArtifactDescriptorV2 | None:
    """Return a schema-v2 descriptor only when it belongs to the run record."""
    output = record.get("output")
    if not isinstance(output, Mapping):
        return None
    intelligence = output.get("research_intelligence")
    if not isinstance(intelligence, Mapping):
        return None
    manifest = intelligence.get("artifact_manifest")
    if not isinstance(manifest, Mapping) or manifest.get("schema_version") != 2:
        return None
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, (list, tuple)):
        return None
    for raw_descriptor in artifacts:
        if not isinstance(raw_descriptor, Mapping):
            continue
        if raw_descriptor.get("artifact_id") != artifact_id:
            continue
        try:
            return ArtifactDescriptorV2.from_dict(raw_descriptor)
        except (TypeError, ValueError):
            return None
    return None


def _artifact_filename(descriptor: ArtifactDescriptorV2) -> str:
    """Build a stable download filename without trusting persisted path text."""
    path_name = PurePosixPath(descriptor.path).name
    if path_name and "." in path_name and path_name not in {".", ".."}:
        candidate = path_name
    else:
        candidate = _ARTIFACT_FILENAMES.get(descriptor.artifact_type, "")
        if not candidate:
            candidate = f"artifact{_MIME_SUFFIXES.get(descriptor.mime_type, '')}"
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", candidate).strip("._")
    return (sanitized or "artifact")[:160]


def _artifact_content_disposition(filename: str) -> str:
    """Return a header with both ASCII and RFC 5987 filename forms."""
    fallback = re.sub(r"[^A-Za-z0-9._-]+", "_", filename) or "artifact"
    return (
        f'attachment; filename="{fallback}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )


def _artifact_store_for_runner(runner: Any) -> Any | None:
    """Resolve the runner's durable FileArtifactStore without touching disk paths from HTTP."""
    store = getattr(runner, "artifact_store", None)
    if store is not None and callable(getattr(store, "get", None)):
        return store
    repository = getattr(runner, "repository", None)
    if repository is None:
        return None
    return FileArtifactStore(repository)


def _probe_directory(path: Path) -> bool:
    """Verify one application-owned directory can be created, written, and read."""
    temporary_path: Path | None = None
    try:
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            return False
        with tempfile.NamedTemporaryFile(
            mode="w+b",
            prefix=".readyz-",
            dir=path,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(b"ready")
            handle.flush()
            os.fsync(handle.fileno())
        return temporary_path.read_bytes() == b"ready"
    except (OSError, ValueError):
        return False
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _readiness_paths(runner: Any) -> tuple[Path, ...]:
    """Resolve only the repository and artifact directories used by this app."""
    repository = getattr(runner, "repository", None)
    root = getattr(repository, "root", None)
    if not isinstance(root, Path):
        return ()
    store = _artifact_store_for_runner(runner)
    artifact_root = getattr(store, "artifact_root", root / "artifacts")
    if not isinstance(artifact_root, Path):
        artifact_root = root / "artifacts"
    return (root, root / "runs", artifact_root)


def create_app(harness_runner: HarnessRunner | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(title="HelloAgents Deep Researcher")
    app.add_exception_handler(RequestValidationError, _request_validation_response)
    run_capacity = _RunCapacity(_configured_run_capacity())
    configured_app_api_key = _configured_app_api_key()

    @app.middleware("http")
    async def authenticate_request(request: Request, call_next: Any) -> Any:
        """Require a configured bearer key for every non-probe HTTP route."""
        if request.method != "OPTIONS" and request.url.path not in _PUBLIC_PROBE_PATHS:
            authorization = request.headers.get("Authorization", "")
            scheme, separator, token = authorization.partition(" ")
            candidate = token if separator and scheme.casefold() == "bearer" else ""
            expected = configured_app_api_key or _UNCONFIGURED_API_KEY_SENTINEL
            authorized = bool(configured_app_api_key) and compare_digest(
                candidate,
                expected,
            )
            if not authorized:
                return JSONResponse(
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                    content={
                        "detail": {
                            "code": "unauthorized",
                            "message": "A valid bearer token is required.",
                        }
                    },
                )
        return await call_next(request)

    uses_default_runner = harness_runner is None
    if harness_runner is None:
        configured_data_dir = Configuration.from_env().data_dir
        harness_runner = HarnessRunner.build_default(
            base_path=configured_data_dir,
        )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_configured_cors_origins(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Accept", "Authorization"],
        expose_headers=["Content-Disposition"],
    )

    @app.on_event("startup")
    def log_startup_configuration() -> None:
        config = Configuration.from_env()
        if uses_default_runner:
            try:
                sweep_search_cache(config)
            except Exception:
                logger.warning("Search cache startup sweep failed.")

        if config.llm_provider == "ollama":
            base_url = config.sanitized_ollama_url()
        elif config.llm_provider == "lmstudio":
            base_url = config.lmstudio_base_url
        else:
            base_url = config.llm_base_url or "unset"

        logger.info(
            "DeepResearch configuration loaded: provider={} model={} endpoint={} search_api={} "
            "max_loops={} fetch_full_page={} search_cache={} cache_ttl={} "
            "task_quality_mode={} tool_calling={} strip_thinking={} api_key={}",
            config.llm_provider,
            config.resolved_model() or "unset",
            _configuration_presence(base_url),
            (config.search_api.value if isinstance(config.search_api, SearchAPI) else config.search_api),
            config.max_web_research_loops,
            config.fetch_full_page,
            config.enable_search_cache,
            config.search_cache_ttl_seconds,
            config.task_quality_mode,
            config.use_tool_calling,
            config.strip_thinking_tokens,
            _configuration_presence(config.llm_api_key),
        )

    def reserve_run_slot() -> Callable[[], None]:
        """Reserve one HTTP run slot or raise the public capacity error."""
        if not run_capacity.try_acquire():
            raise _safe_http_error(
                status_code=429,
                code="capacity_exceeded",
                message="The maximum number of active research runs has been reached.",
            )
        released = False

        def release() -> None:
            nonlocal released
            if released:
                return
            released = True
            run_capacity.release()

        return release

    @app.get("/healthz")
    def health_check() -> Dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readiness_check() -> dict[str, str]:
        """Report whether durable run and artifact directories are writable."""
        paths = _readiness_paths(harness_runner)
        if not paths or not all(_probe_directory(path) for path in paths):
            raise _safe_http_error(
                status_code=503,
                code="not_ready",
                message="Durable application storage is not ready.",
            )
        return {"status": "ready"}

    @app.post("/research", response_model=ResearchResponse)
    def run_research(payload: ResearchRequest) -> ResearchResponse:
        release_slot: Callable[[], None] | None = None
        try:
            request = _normalize_harness_request(payload, caller_mode="public")
            release_slot = reserve_run_slot()
            result = harness_runner.run(request)
            _raise_for_failed_result(result)
        except HTTPException:
            raise
        except ValueError as exc:  # Likely due to unsupported configuration
            raise _safe_http_error(
                status_code=400,
                code="invalid_command",
                message="The research command is invalid.",
            ) from exc
        except PermissionError as exc:
            raise _safe_http_error(
                status_code=403,
                code="policy_rejected",
                message="The research command was rejected by policy.",
            ) from exc
        except Exception as exc:  # pragma: no cover - defensive guardrail
            raise _safe_http_error(
                status_code=500,
                code="application_error",
                message="The research run failed.",
            ) from exc
        finally:
            if release_slot is not None:
                release_slot()

        output = result.output
        return ResearchResponse(
            report_markdown=(output.report_markdown or output.running_summary or "") if output else "",
            todo_items=_serialize_todo_items(output.todo_items if output else []),
            research_mode=(
                output.research_mode
                if output and getattr(output, "research_mode", None)
                else None
            ),
            research_profile_id=(
                output.research_profile_id
                if output and getattr(output, "research_profile_id", None)
                else None
            ),
            source_context=(
                dict(output.source_context)
                if output and getattr(output, "source_context", None)
                else {}
            ),
            research_intelligence=(
                dict(output.research_intelligence)
                if output and getattr(output, "research_intelligence", None)
                else {}
            ),
            github_intelligence=(
                dict(output.github_intelligence)
                if output and getattr(output, "github_intelligence", None)
                else {}
            ),
            structured_summary=(
                dict(output.structured_summary)
                if output and getattr(output, "structured_summary", None)
                else {}
            ),
            quality_assessment=(
                dict(output.quality_assessment)
                if output and getattr(output, "quality_assessment", None)
                else {}
            ),
        )

    @app.post("/research/stream")
    def stream_research(payload: ResearchRequest) -> StreamingResponse:
        try:
            request = _normalize_harness_request(payload, caller_mode="public")
        except ValueError as exc:
            raise _safe_http_error(
                status_code=400,
                code="invalid_command",
                message="The research command is invalid.",
            ) from exc

        release_slot = reserve_run_slot()
        try:
            return _streaming_response(
                harness_runner,
                request,
                on_close=release_slot,
            )
        except Exception:
            release_slot()
            raise

    @app.post("/research/continue/stream")
    def stream_continue_research(payload: ContinueRequest) -> StreamingResponse:
        """SSE endpoint for follow-up research that builds on a previous run."""
        try:
            research_payload = ResearchRequest(
                topic=payload.topic,
                search_api=payload.search_api,
                parent_run_id=payload.parent_run_id,
                research_mode=payload.research_mode,
                research_profile=payload.research_profile,
                use_history_memory=payload.use_history_memory,
                memory_scope=payload.memory_scope,
            )
            request = _normalize_harness_request(research_payload, caller_mode="public")
        except ValueError as exc:
            raise _safe_http_error(
                status_code=400,
                code="invalid_command",
                message="The research command is invalid.",
            ) from exc

        release_slot = reserve_run_slot()
        try:
            return _streaming_response(
                harness_runner,
                request,
                on_close=release_slot,
            )
        except Exception:
            release_slot()
            raise

    @app.post("/research/recover", response_model=HarnessResponse)
    def recover_research(payload: RecoveryRequest) -> HarnessResponse:
        """Recover a failed run from its latest trusted checkpoint."""
        release_slot: Callable[[], None] | None = None
        try:
            release_slot = reserve_run_slot()
            result = harness_runner.resume(payload.run_id)
            _raise_for_failed_result(result)
        except HTTPException:
            raise
        except ValueError as exc:
            raise _safe_http_error(
                status_code=400,
                code="invalid_command",
                message="The recovery request is invalid.",
            ) from exc
        except Exception as exc:  # pragma: no cover - defensive boundary
            raise _safe_http_error(
                status_code=500,
                code="application_error",
                message="The research run could not be recovered.",
            ) from exc
        finally:
            if release_slot is not None:
                release_slot()
        return _build_harness_response(result, mode="recovery")

    @app.post("/research/recover/stream")
    def recover_research_stream(payload: RecoveryRequest) -> StreamingResponse:
        """Stream recovery events without creating a new run ID."""
        if not isinstance(payload.run_id, str) or not payload.run_id.strip():
            raise _safe_http_error(
                status_code=400,
                code="invalid_command",
                message="The recovery request is invalid.",
            )
        release_slot = reserve_run_slot()
        try:
            return _streaming_response(
                harness_runner,
                None,
                stream_factory=lambda: harness_runner.resume_stream(payload.run_id),
                run_id=payload.run_id,
                on_close=release_slot,
            )
        except Exception:
            release_slot()
            raise

    @app.post("/harness/run", response_model=HarnessResponse)
    def run_harness(payload: HarnessRequest) -> HarnessResponse:
        release_slot: Callable[[], None] | None = None
        try:
            request = _normalize_harness_request(
                payload,
                caller_mode="internal",
                permission_mode=payload.permission_mode,
                metadata=payload.metadata,
            )
            release_slot = reserve_run_slot()
            result = harness_runner.run(request)
            _raise_for_failed_result(result)
        except HTTPException:
            raise
        except PermissionError as exc:
            raise _safe_http_error(
                status_code=403,
                code="policy_rejected",
                message="The research command was rejected by policy.",
            ) from exc
        except ValueError as exc:
            raise _safe_http_error(
                status_code=400,
                code="invalid_command",
                message="The research command is invalid.",
            ) from exc
        except Exception as exc:  # pragma: no cover - defensive guardrail
            raise _safe_http_error(
                status_code=500,
                code="application_error",
                message="The research run failed.",
            ) from exc
        finally:
            if release_slot is not None:
                release_slot()

        return _build_harness_response(result, mode="internal")

    @app.post("/harness/recover", response_model=HarnessResponse)
    def recover_harness(payload: RecoveryRequest) -> HarnessResponse:
        """Recover one harness run using the shared application service."""
        release_slot: Callable[[], None] | None = None
        try:
            release_slot = reserve_run_slot()
            result = harness_runner.resume(payload.run_id)
            _raise_for_failed_result(result)
        except HTTPException:
            raise
        except ValueError as exc:
            raise _safe_http_error(
                status_code=400,
                code="invalid_command",
                message="The recovery request is invalid.",
            ) from exc
        except Exception as exc:  # pragma: no cover - defensive boundary
            raise _safe_http_error(
                status_code=500,
                code="application_error",
                message="The research run could not be recovered.",
            ) from exc
        finally:
            if release_slot is not None:
                release_slot()
        return _build_harness_response(result, mode="recovery")

    @app.post("/harness/recover/stream")
    def recover_harness_stream(payload: RecoveryRequest) -> StreamingResponse:
        """Stream recovery events for a harness run."""
        if not isinstance(payload.run_id, str) or not payload.run_id.strip():
            raise _safe_http_error(
                status_code=400,
                code="invalid_command",
                message="The recovery request is invalid.",
            )
        release_slot = reserve_run_slot()
        try:
            return _streaming_response(
                harness_runner,
                None,
                stream_factory=lambda: harness_runner.resume_stream(payload.run_id),
                run_id=payload.run_id,
                on_close=release_slot,
            )
        except Exception:
            release_slot()
            raise

    @app.get("/memories")
    def list_user_memories(
        scope: str = "default",
        include_pending: bool = True,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List explicit candidates and confirmed local user memory."""
        try:
            list_memories = getattr(harness_runner, "list_memories")
            result = list_memories(
                scope=scope,
                include_pending=include_pending,
                limit=limit,
            )
            if not isinstance(result, dict):
                raise TypeError("Memory listing returned an invalid shape.")
            return result
        except MemoryValidationError as exc:
            raise _safe_http_error(
                status_code=400,
                code="invalid_memory",
                message="The memory request is invalid.",
            ) from exc
        except (MemoryStoreUnavailableError, RuntimeError, AttributeError) as exc:
            raise _safe_http_error(
                status_code=503,
                code="memory_unavailable",
                message="User memory is unavailable.",
            ) from exc
        except Exception as exc:  # pragma: no cover - defensive boundary
            raise _safe_http_error(
                status_code=500,
                code="memory_error",
                message="User memory could not be read.",
            ) from exc

    @app.post("/memories/candidates")
    def create_memory_candidate(payload: MemoryCandidateRequest) -> dict[str, Any]:
        """Create a pending candidate; confirmation is a separate user action."""
        try:
            create_candidate = getattr(harness_runner, "create_memory_candidate")
            memory = create_candidate(
                text=payload.text,
                kind=payload.kind,
                scope=payload.scope,
            )
            return memory.as_dict()
        except MemoryValidationError as exc:
            raise _safe_http_error(
                status_code=400,
                code="invalid_memory",
                message="The memory candidate is invalid.",
            ) from exc
        except (MemoryStoreUnavailableError, RuntimeError, AttributeError) as exc:
            raise _safe_http_error(
                status_code=503,
                code="memory_unavailable",
                message="User memory is unavailable.",
            ) from exc
        except Exception as exc:  # pragma: no cover - defensive boundary
            raise _safe_http_error(
                status_code=500,
                code="memory_error",
                message="The memory candidate could not be created.",
            ) from exc

    @app.post("/memories/{memory_id}/confirm")
    def confirm_user_memory(memory_id: str, scope: str = "default") -> dict[str, Any]:
        """Promote one pending candidate after explicit confirmation."""
        try:
            confirm_memory = getattr(harness_runner, "confirm_memory")
            memory = confirm_memory(memory_id, scope=scope)
            return memory.as_dict()
        except MemoryNotFoundError as exc:
            raise _safe_http_error(
                status_code=404,
                code="memory_not_found",
                message="The memory was not found.",
            ) from exc
        except MemoryStateError as exc:
            raise _safe_http_error(
                status_code=409,
                code="memory_state_invalid",
                message="The memory cannot be confirmed in its current state.",
            ) from exc
        except MemoryValidationError as exc:
            raise _safe_http_error(
                status_code=400,
                code="invalid_memory",
                message="The memory request is invalid.",
            ) from exc
        except (MemoryStoreUnavailableError, RuntimeError, AttributeError) as exc:
            raise _safe_http_error(
                status_code=503,
                code="memory_unavailable",
                message="User memory is unavailable.",
            ) from exc
        except Exception as exc:  # pragma: no cover - defensive boundary
            raise _safe_http_error(
                status_code=500,
                code="memory_error",
                message="The memory could not be confirmed.",
            ) from exc

    @app.delete("/memories/{memory_id}")
    def delete_user_memory(memory_id: str, scope: str = "default") -> dict[str, Any]:
        """Delete a pending or confirmed memory inside its scope."""
        try:
            delete_memory = getattr(harness_runner, "delete_memory")
            deleted = delete_memory(memory_id, scope=scope)
            if not deleted:
                raise MemoryNotFoundError(memory_id)
            return {"deleted": True, "memory_id": memory_id}
        except MemoryNotFoundError as exc:
            raise _safe_http_error(
                status_code=404,
                code="memory_not_found",
                message="The memory was not found.",
            ) from exc
        except MemoryValidationError as exc:
            raise _safe_http_error(
                status_code=400,
                code="invalid_memory",
                message="The memory request is invalid.",
            ) from exc
        except (MemoryStoreUnavailableError, RuntimeError, AttributeError) as exc:
            raise _safe_http_error(
                status_code=503,
                code="memory_unavailable",
                message="User memory is unavailable.",
            ) from exc
        except Exception as exc:  # pragma: no cover - defensive boundary
            raise _safe_http_error(
                status_code=500,
                code="memory_error",
                message="The memory could not be deleted.",
            ) from exc

    @app.get("/runs")
    def list_harness_runs(
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List completed run summaries for the local history browser."""
        try:
            list_history = getattr(harness_runner, "list_history")
            result = list_history(limit=limit, cursor=cursor)
            if not isinstance(result, dict):
                raise TypeError("History listing returned an invalid shape.")
            return result
        except ValueError as exc:
            raise _safe_http_error(
                status_code=400,
                code="invalid_history_cursor",
                message="The history pagination request is invalid.",
            ) from exc
        except Exception as exc:  # pragma: no cover - defensive boundary
            raise _safe_http_error(
                status_code=500,
                code="repository_error",
                message="The run repository is unavailable.",
            ) from exc

    @app.get("/runs/{run_id}")
    @app.get("/harness/runs/{run_id}", deprecated=True)
    def get_harness_run(run_id: str) -> dict[str, Any]:
        try:
            return harness_runner.load_record(run_id)
        except InvalidRunIdError as exc:
            raise _safe_http_error(
                status_code=400,
                code="invalid_run_id",
                message="The run ID is invalid.",
            ) from exc
        except RunNotFoundError as exc:
            raise _safe_http_error(
                status_code=404,
                code="run_not_found",
                message="The run was not found.",
            ) from exc
        except (CorruptRunRecordError, UnsupportedSchemaError) as exc:
            raise _safe_http_error(
                status_code=500,
                code="corrupt_run_record",
                message="The stored run record is unavailable.",
            ) from exc
        except FileNotFoundError as exc:
            raise _safe_http_error(
                status_code=404,
                code="run_not_found",
                message="The run was not found.",
            ) from exc
        except RunRepositoryError as exc:
            raise _safe_http_error(
                status_code=500,
                code="repository_error",
                message="The run repository is unavailable.",
            ) from exc
        except Exception as exc:  # pragma: no cover - defensive boundary
            raise _safe_http_error(
                status_code=500,
                code="repository_error",
                message="The run repository is unavailable.",
            ) from exc

    @app.get("/runs/{run_id}/artifacts/{artifact_id}")
    def download_run_artifact(run_id: str, artifact_id: str) -> Response:
        """Download one schema-v2 artifact after validating run ownership."""
        try:
            normalized_run_id = normalize_run_id(run_id)
        except (TypeError, ValueError) as exc:
            raise _safe_http_error(
                status_code=400,
                code="invalid_run_id",
                message="The run ID is invalid.",
            ) from exc
        try:
            normalized_artifact_id = normalize_artifact_id(artifact_id)
        except (TypeError, ValueError) as exc:
            raise _safe_http_error(
                status_code=400,
                code="invalid_artifact_id",
                message="The artifact ID is invalid.",
            ) from exc

        try:
            record = harness_runner.load_record(run_id)
        except (InvalidRunIdError, RunNotFoundError, FileNotFoundError) as exc:
            raise _safe_http_error(
                status_code=404,
                code="artifact_not_found",
                message="The artifact was not found.",
            ) from exc
        except (CorruptRunRecordError, UnsupportedSchemaError) as exc:
            raise _safe_http_error(
                status_code=500,
                code="corrupt_run_record",
                message="The stored run record is unavailable.",
            ) from exc
        except RunRepositoryError as exc:
            raise _safe_http_error(
                status_code=500,
                code="repository_error",
                message="The run repository is unavailable.",
            ) from exc
        except Exception as exc:  # pragma: no cover - defensive boundary
            raise _safe_http_error(
                status_code=500,
                code="repository_error",
                message="The run repository is unavailable.",
            ) from exc

        if not isinstance(record, Mapping):
            raise _safe_http_error(
                status_code=404,
                code="artifact_not_found",
                message="The artifact was not found.",
            )
        descriptor = _artifact_descriptor_for_run(record, normalized_artifact_id)
        if descriptor is None:
            raise _safe_http_error(
                status_code=404,
                code="artifact_not_found",
                message="The artifact was not found.",
            )

        store = _artifact_store_for_runner(harness_runner)
        if store is None:
            raise _safe_http_error(
                status_code=503,
                code="artifact_store_unavailable",
                message="The artifact store is unavailable.",
            )
        try:
            body = store.get(normalized_run_id, normalized_artifact_id)
        except (FileNotFoundError, TypeError, ValueError) as exc:
            raise _safe_http_error(
                status_code=404,
                code="artifact_not_found",
                message="The artifact was not found.",
            ) from exc
        except OSError as exc:
            raise _safe_http_error(
                status_code=500,
                code="artifact_store_unavailable",
                message="The artifact store is unavailable.",
            ) from exc

        return Response(
            content=body,
            media_type=descriptor.mime_type,
            headers={
                "Content-Disposition": _artifact_content_disposition(
                    _artifact_filename(descriptor)
                ),
            },
        )

    @app.get("/harness/scenarios")
    def list_harness_scenarios() -> list[dict[str, Any]]:
        scenarios = build_default_scenarios()
        return [
            {
                "name": item.name,
                "topic": item.topic,
                "description": item.description,
                "search_api": item.search_api.value if item.search_api else None,
                "metadata": item.metadata,
            }
            for item in scenarios
        ]

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    server_host, server_port = _server_bind_config()
    uvicorn.run(
        "main:app",
        host=server_host,
        port=server_port,
        reload=True,
        log_level="info",
    )
