"""FastAPI entrypoint exposing the DeepResearchAgent via HTTP."""

from __future__ import annotations

import json
import os
import sys
from typing import Any, AsyncIterator, Dict, Iterator
from urllib.parse import urlsplit

import anyio
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field

from config import Configuration, SearchAPI
from harness import HarnessRunner, HarnessRunRequest, build_default_scenarios
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

# 添加控制台日志处理程序
logger.add(
    sys.stderr,
    level="INFO",
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <4}</level> | <cyan>using_function:{function}</cyan> | <cyan>{file}:{line}</cyan> | <level>{message}</level>",
    colorize=True,
)


class ResearchRequest(BaseModel):
    """Payload for triggering a research run."""

    topic: str = Field(..., description="Research topic supplied by the user")
    search_api: SearchAPI | None = Field(
        default=None,
        description="Override the default search backend configured via env",
    )
    parent_run_id: str | None = Field(
        default=None,
        description="Previous run_id for multi-turn follow-up research",
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


class ContinueRequest(BaseModel):
    """Payload for continuing a previous research run with a follow-up topic."""

    topic: str = Field(..., description="Follow-up research topic")
    parent_run_id: str = Field(..., description="run_id of the previous research to build upon")
    search_api: SearchAPI | None = Field(
        default=None,
        description="Override the default search backend",
    )


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


class HarnessResponse(BaseModel):
    """HTTP response for a harness-managed run."""

    run_id: str
    status: str
    report_markdown: str = ""
    todo_items: list[dict[str, Any]] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    findings: list[dict[str, Any]] = Field(default_factory=list)
    compressed_context: dict[str, Any] = Field(default_factory=dict)
    policy_decisions: list[dict[str, Any]] = Field(default_factory=list)
    mode: str = "internal"


def _configuration_presence(value: str | None) -> str:
    """Describe whether a sensitive setting exists without returning its value."""
    return "configured" if value else "unset"


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
    )


def _build_harness_response(result: Any, *, mode: str) -> HarnessResponse:
    """Convert a harness result into the public HTTP response model."""
    output = result.output
    return HarnessResponse(
        run_id=result.run_id,
        status=result.status,
        report_markdown=(output.report_markdown or output.running_summary or "") if output else "",
        todo_items=_serialize_todo_items(output.todo_items if output else []),
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
        mode=mode,
    )


_RUN_ERROR_RESPONSES: dict[str, tuple[int, str]] = {
    "invalid_command": (400, "The research command is invalid."),
    "policy_rejected": (403, "The research command was rejected by policy."),
    "operation_rejected": (403, "A research operation was rejected by policy."),
    "parent_not_found": (404, "The parent run was not found."),
    "parent_pending": (409, "The parent run is not yet available."),
    "run_already_active": (409, "A run with this ID is already active."),
    "runner_busy": (409, "The research service is busy."),
    "cancelled": (409, "The research run was cancelled."),
    "deadline_exceeded": (408, "The research run deadline was exceeded."),
    "parent_corrupt": (500, "The parent run record is unavailable."),
    "repository_error": (500, "The run repository is unavailable."),
    "persistence_failed": (500, "The research run could not be persisted."),
    "policy_error": (500, "Research policy evaluation failed."),
    "coordinator_failed": (500, "Research coordination failed."),
    "terminal_validation_failed": (500, "Research result validation failed."),
    "context_projection_failed": (500, "Research context projection failed."),
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


def _raise_for_failed_result(result: Any) -> None:
    """Translate a typed non-success run result to a stable HTTP error."""
    if getattr(result, "status", None) == "completed":
        return

    raw_code = getattr(result, "error_code", None)
    code = raw_code if raw_code in _RUN_ERROR_RESPONSES else "application_error"
    status_code, message = _RUN_ERROR_RESPONSES[code]
    raise _safe_http_error(status_code=status_code, code=code, message=message)


def _stream_error_event(
    request: HarnessRunRequest,
    *,
    sequence: int,
    code: str = "stream_failed",
) -> dict[str, Any]:
    """Return the public, non-reflective stream failure envelope."""
    return {
        "type": "error",
        "run_id": request.run_id,
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
    request: HarnessRunRequest,
) -> AsyncIterator[str]:
    """Serialize one runner stream and own its terminal/error boundary."""
    iterator: Iterator[dict[str, Any]] | None = None
    next_sequence = 1
    try:
        iterator = iter(harness_runner.stream(request))
        while True:
            item = await anyio.to_thread.run_sync(
                _next_or_end,
                iterator,
                abandon_on_cancel=True,
            )
            if item is _STREAM_END:
                yield f"data: {json.dumps(_stream_error_event(request, sequence=next_sequence), ensure_ascii=False)}\n\n"
                return
            event = item
            if not isinstance(event, dict):
                raise TypeError("Research stream events must be objects.")
            sequence = event.get("sequence") if isinstance(event, dict) else None
            if isinstance(sequence, int) and not isinstance(sequence, bool):
                next_sequence = max(next_sequence, sequence + 1)
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            if event.get("type") in {"done", "error"}:
                return
    except Exception:
        logger.warning("Research stream failed at the SSE boundary.")
        yield f"data: {json.dumps(_stream_error_event(request, sequence=next_sequence), ensure_ascii=False)}\n\n"
    finally:
        close = getattr(iterator, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                logger.warning("Research stream iterator close failed.")


def _streaming_response(
    harness_runner: Any,
    request: HarnessRunRequest,
) -> StreamingResponse:
    """Build the shared SSE response for initial and follow-up research."""
    return StreamingResponse(
        _iter_sse_events(harness_runner, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


def create_app(harness_runner: HarnessRunner | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(title="HelloAgents Deep Researcher")
    uses_default_runner = harness_runner is None
    if harness_runner is None:
        harness_runner = HarnessRunner.build_default(
            base_path="./output/harness_runs"
        )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_configured_cors_origins(),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Accept"],
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
            "max_loops={} fetch_full_page={} tool_calling={} strip_thinking={} api_key={}",
            config.llm_provider,
            config.resolved_model() or "unset",
            _configuration_presence(base_url),
            (config.search_api.value if isinstance(config.search_api, SearchAPI) else config.search_api),
            config.max_web_research_loops,
            config.fetch_full_page,
            config.use_tool_calling,
            config.strip_thinking_tokens,
            _configuration_presence(config.llm_api_key),
        )

    @app.get("/healthz")
    def health_check() -> Dict[str, str]:
        return {"status": "ok"}

    @app.post("/research", response_model=ResearchResponse)
    def run_research(payload: ResearchRequest) -> ResearchResponse:
        try:
            request = _normalize_harness_request(payload, caller_mode="public")
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

        output = result.output
        return ResearchResponse(
            report_markdown=(output.report_markdown or output.running_summary or "") if output else "",
            todo_items=_serialize_todo_items(output.todo_items if output else []),
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

        return _streaming_response(harness_runner, request)

    @app.post("/research/continue/stream")
    def stream_continue_research(payload: ContinueRequest) -> StreamingResponse:
        """SSE endpoint for follow-up research that builds on a previous run."""
        try:
            research_payload = ResearchRequest(
                topic=payload.topic,
                search_api=payload.search_api,
                parent_run_id=payload.parent_run_id,
            )
            request = _normalize_harness_request(research_payload, caller_mode="public")
        except ValueError as exc:
            raise _safe_http_error(
                status_code=400,
                code="invalid_command",
                message="The research command is invalid.",
            ) from exc

        return _streaming_response(harness_runner, request)

    @app.post("/harness/run", response_model=HarnessResponse)
    def run_harness(payload: HarnessRequest) -> HarnessResponse:
        try:
            request = _normalize_harness_request(
                payload,
                caller_mode="internal",
                permission_mode=payload.permission_mode,
                metadata=payload.metadata,
            )
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

        return _build_harness_response(result, mode="internal")

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
