"""FastAPI entrypoint exposing the DeepResearchAgent via HTTP."""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, Iterator, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field

from config import Configuration, SearchAPI
from harness import HarnessRunner, HarnessRunRequest, build_default_scenarios

# 添加控制台日志处理程序
logger.add(
    sys.stderr,
    level="INFO",
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <4}</level> | <cyan>using_function:{function}</cyan> | <cyan>{file}:{line}</cyan> | <level>{message}</level>",
    colorize=True,
)


# 添加错误日志文件处理程序
logger.add(
    sink=sys.stderr,
    level="ERROR",
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


class ResearchResponse(BaseModel):
    """HTTP response containing the generated report and structured tasks."""

    report_markdown: str = Field(
        ..., description="Markdown-formatted research report including sections"
    )
    todo_items: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Structured TODO items with summaries and sources",
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


def _mask_secret(value: Optional[str], visible: int = 4) -> str:
    """Mask sensitive tokens while keeping leading and trailing characters."""
    if not value:
        return "unset"

    if len(value) <= visible * 2:
        return "*" * len(value)

    return f"{value[:visible]}...{value[-visible:]}"


def _build_config(payload: ResearchRequest) -> Configuration:
    overrides: Dict[str, Any] = {}

    if payload.search_api is not None:
        overrides["search_api"] = payload.search_api

    return Configuration.from_env(overrides=overrides)


def _serialize_todo_items(items: list[Any]) -> list[dict[str, Any]]:
    """Normalize todo items for API responses."""
    return [
        {
            "id": item.id,
            "title": item.title,
            "intent": item.intent,
            "query": item.query,
            "status": item.status,
            "summary": item.summary,
            "sources_summary": item.sources_summary,
            "note_id": item.note_id,
            "note_path": item.note_path,
        }
        for item in items
    ]


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


def create_app() -> FastAPI:
    app = FastAPI(title="HelloAgents Deep Researcher")
    harness_runner = HarnessRunner.build_default(base_path="./output/harness_runs")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.on_event("startup")
    def log_startup_configuration() -> None:
        config = Configuration.from_env()

        if config.llm_provider == "ollama":
            base_url = config.sanitized_ollama_url()
        elif config.llm_provider == "lmstudio":
            base_url = config.lmstudio_base_url
        else:
            base_url = config.llm_base_url or "unset"

        logger.info(
            "DeepResearch configuration loaded: provider=%s model=%s base_url=%s search_api=%s "
            "max_loops=%s fetch_full_page=%s tool_calling=%s strip_thinking=%s api_key=%s",
            config.llm_provider,
            config.resolved_model() or "unset",
            base_url,
            (config.search_api.value if isinstance(config.search_api, SearchAPI) else config.search_api),
            config.max_web_research_loops,
            config.fetch_full_page,
            config.use_tool_calling,
            config.strip_thinking_tokens,
            _mask_secret(config.llm_api_key),
        )

    @app.get("/healthz")
    def health_check() -> Dict[str, str]:
        return {"status": "ok"}

    @app.post("/research", response_model=ResearchResponse)
    def run_research(payload: ResearchRequest) -> ResearchResponse:
        try:
            request = _normalize_harness_request(payload, caller_mode="public")
            result = harness_runner.run(request)
        except ValueError as exc:  # Likely due to unsupported configuration
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except Exception as exc:  # pragma: no cover - defensive guardrail
            raise HTTPException(status_code=500, detail="Research failed") from exc

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
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        def event_iterator() -> Iterator[str]:
            try:
                for event in harness_runner.stream(request):
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except PermissionError as exc:
                error_payload = {"type": "error", "detail": str(exc)}
                yield f"data: {json.dumps(error_payload, ensure_ascii=False)}\n\n"
            except Exception as exc:  # pragma: no cover - defensive guardrail
                logger.exception("Streaming research failed")
                error_payload = {"type": "error", "detail": str(exc)}
                yield f"data: {json.dumps(error_payload, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            event_iterator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )

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
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # pragma: no cover - defensive guardrail
            raise HTTPException(status_code=500, detail="Harness run failed") from exc

        return _build_harness_response(result, mode="internal")

    @app.get("/harness/runs/{run_id}")
    def get_harness_run(run_id: str) -> dict[str, Any]:
        try:
            return harness_runner.load_record(run_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc

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

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )
