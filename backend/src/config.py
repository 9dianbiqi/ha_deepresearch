"""Environment-backed configuration for the research application."""

import os
from enum import Enum
from math import isfinite
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

SAFE_CONFIGURATION_FIELDS = (
    "llm_provider",
    "llm_model_id",
    "llm_reporter_model_id",
    "search_api",
    "max_web_research_loops",
    "max_concurrent_tasks",
    "fetch_full_page",
    "strip_thinking_tokens",
    "use_tool_calling",
    "enable_notes",
    "enable_quality_gate",
    "task_quality_mode",
    "enable_search_cache",
    "search_cache_ttl_seconds",
    "enable_github_research",
    "enable_evidence_web",
    "enable_summary_quality_shadow",
    "summary_semantic_threshold",
    "summary_factual_threshold",
    "summary_citation_threshold",
    "summary_overall_threshold",
    "run_timeout_seconds",
    "max_concurrent_runs",
    "retention_days",
)


class SearchAPI(Enum):
    """Supported research search backends."""

    PERPLEXITY = "perplexity"
    TAVILY = "tavily"
    DUCKDUCKGO = "duckduckgo"
    SEARXNG = "searxng"
    ADVANCED = "advanced"


class Configuration(BaseModel):
    """Configuration options for the deep research assistant."""

    model_config = ConfigDict(frozen=True)

    max_web_research_loops: int = Field(
        default=3,
        title="Research Depth",
        description="Number of research iterations to perform",
    )
    max_concurrent_tasks: int = Field(
        default=4,
        ge=1,
        le=16,
        title="Maximum Concurrent Tasks",
        description="Maximum number of research tasks running concurrently",
    )
    max_concurrent_runs: int = Field(
        default=1,
        ge=1,
        le=16,
        title="Maximum Concurrent Runs",
        description="Maximum number of research runs active in this process",
    )
    data_dir: str = Field(
        default="./data",
        min_length=1,
        max_length=1024,
        title="Data Directory",
        description="Durable root for runs, artifacts, history, and memory",
    )
    retention_days: int = Field(
        default=30,
        ge=1,
        le=3650,
        title="Retention Days",
        description="Number of days to retain durable run data",
    )
    local_llm: str = Field(
        default="llama3.2",
        title="Local Model Name",
        description="Name of the locally hosted LLM (Ollama/LMStudio)",
    )
    llm_provider: str = Field(
        default="ollama",
        title="LLM Provider",
        description="Provider identifier (ollama, lmstudio, or custom)",
    )
    search_api: SearchAPI = Field(
        default=SearchAPI.DUCKDUCKGO,
        title="Search API",
        description="Web search API to use",
    )
    enable_notes: bool = Field(
        default=True,
        title="Enable Notes",
        description="Whether to store task progress in NoteTool",
    )
    notes_workspace: str = Field(
        default="./notes",
        title="Notes Workspace",
        description="Directory for NoteTool to persist task notes",
    )
    fetch_full_page: bool = Field(
        default=True,
        title="Fetch Full Page",
        description="Include the full page content in the search results",
    )
    ollama_base_url: str = Field(
        default="http://localhost:11434",
        title="Ollama Base URL",
        description="Base URL for Ollama API (without /v1 suffix)",
    )
    lmstudio_base_url: str = Field(
        default="http://localhost:1234/v1",
        title="LMStudio Base URL",
        description="Base URL for LMStudio OpenAI-compatible API",
    )
    strip_thinking_tokens: bool = Field(
        default=True,
        title="Strip Thinking Tokens",
        description="Whether to strip <think> tokens from model responses",
    )
    use_tool_calling: bool = Field(
        default=False,
        title="Use Tool Calling",
        description="Use tool calling instead of JSON mode for structured output",
    )
    llm_api_key: str | None = Field(
        default=None,
        title="LLM API Key",
        description="Optional API key when using custom OpenAI-compatible services",
    )
    llm_base_url: str | None = Field(
        default=None,
        title="LLM Base URL",
        description="Optional base URL when using custom OpenAI-compatible services",
    )
    llm_model_id: str | None = Field(
        default=None,
        title="LLM Model ID",
        description="Optional model identifier for custom OpenAI-compatible services",
    )
    llm_timeout: float = Field(
        default=60.0,
        title="LLM Timeout",
        description="Request timeout in seconds for LLM API calls",
    )
    run_timeout_seconds: float | None = Field(
        default=None,
        gt=0,
        le=86400,
        title="Run Timeout",
        description="Optional monotonic deadline for a complete research run",
    )
    llm_max_tokens: int = Field(
        default=2000,
        title="LLM Max Tokens",
        description="Maximum output tokens per LLM call",
    )
    llm_reporter_model_id: str | None = Field(
        default=None,
        title="LLM Reporter Model ID",
        description="Optional faster model for the Reporter agent",
    )
    enable_quality_gate: bool = Field(
        default=True,
        title="Enable Quality Gate",
        description="Whether to run summary quality checks and auto-retry on poor results",
    )
    task_quality_mode: str = Field(
        default="evidence",
        pattern="^(basic|evidence|strict)$",
        title="Task Quality Mode",
        description="Validation strength for ordinary task summaries",
    )
    enable_search_cache: bool = Field(
        default=True,
        title="Enable Search Cache",
        description="Use the safe local search cache unless freshness requires bypass",
    )
    search_cache_ttl_seconds: int = Field(
        default=86400,
        ge=60,
        le=604800,
        title="Search Cache TTL",
        description="Maximum search-cache age in seconds",
    )
    enable_github_research: bool = Field(
        default=True,
        title="Enable GitHub Research",
        description="Automatically use GitHub API context for repository topics",
    )
    enable_evidence_web: bool = Field(
        default=False,
        title="Enable Evidence Web",
        description="Use the evidence-first Web profile for explicit Web mode requests",
    )
    enable_summary_quality_shadow: bool = Field(
        default=True,
        title="Enable Summary Quality Shadow",
        description="Evaluate evidence quality without blocking compatibility Web reports",
    )
    summary_semantic_threshold: float = Field(
        default=0.72,
        ge=0.0,
        le=1.0,
        title="Summary Semantic Threshold",
    )
    summary_factual_threshold: float = Field(
        default=0.75,
        ge=0.0,
        le=1.0,
        title="Summary Factual Threshold",
    )
    summary_citation_threshold: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        title="Summary Citation Threshold",
    )
    summary_overall_threshold: float = Field(
        default=0.78,
        ge=0.0,
        le=1.0,
        title="Summary Overall Threshold",
    )
    github_token: str | None = Field(
        default=None,
        title="GitHub Token",
        description="Optional GitHub token for higher API limits and private repo access",
    )
    github_api_base_url: str = Field(
        default="https://api.github.com",
        title="GitHub API Base URL",
        description="Base URL for GitHub-compatible REST API",
    )

    @field_validator("run_timeout_seconds", mode="before")
    @classmethod
    def validate_run_timeout_seconds(cls, value: object) -> object:
        """Accept numeric environment text while rejecting bool and non-finite data."""
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError("Run timeout must be a finite number of seconds.")
        try:
            numeric = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Run timeout must be a finite number of seconds."
            ) from exc
        if not isfinite(numeric):
            raise ValueError("Run timeout must be a finite number of seconds.")
        return numeric

    @classmethod
    def from_env(cls, overrides: dict[str, Any] | None = None) -> "Configuration":
        """Create a configuration object using environment variables and overrides."""
        raw_values: dict[str, Any] = {}

        # Load values from environment variables based on field names
        for field_name in cls.model_fields.keys():
            env_key = field_name.upper()
            if env_key in os.environ:
                raw_values[field_name] = os.environ[env_key]

        if overrides:
            for key, value in overrides.items():
                if value is not None:
                    raw_values[key] = value

        return cls(**raw_values)

    def sanitized_ollama_url(self) -> str:
        """Ensure Ollama base URL includes the /v1 suffix required by OpenAI clients."""
        base = self.ollama_base_url.rstrip("/")
        if not base.endswith("/v1"):
            base = f"{base}/v1"
        return base

    def resolved_model(self) -> str | None:
        """Best-effort resolution of the model identifier to use."""
        return self.llm_model_id or self.local_llm

    def safe_snapshot(self) -> dict[str, Any]:
        """Return the exact non-secret configuration persistence projection."""
        values = self.model_dump(mode="json")
        return {
            field_name: values[field_name]
            for field_name in SAFE_CONFIGURATION_FIELDS
        }
