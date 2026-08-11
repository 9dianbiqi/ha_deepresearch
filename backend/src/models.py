"""State models used by the deep research workflow."""

from dataclasses import dataclass, field
from typing import Any, List


@dataclass(kw_only=True)
class TodoItem:
    """Represent one actionable research task."""

    id: int
    title: str
    intent: str
    query: str
    status: str = field(default="pending")
    summary: str | None = field(default=None)
    sources_summary: str | None = field(default=None)
    notices: list[str] = field(default_factory=list)
    notice_codes: list[str] = field(default_factory=list)
    note_id: str | None = field(default=None)
    note_path: str | None = field(default=None)
    stream_token: str | None = field(default=None)
    retry_count: int = field(default=0)
    refined_queries: list[str] = field(default_factory=list)
    source_strategy: str | None = field(default=None)
    repository: str | None = field(default=None)

    def to_dict(self) -> dict[str, Any]:
        """Convert task to a JSON-serializable dict (single source of truth)."""
        return {
            "id": self.id,
            "title": self.title,
            "intent": self.intent,
            "query": self.query,
            "status": self.status,
            "summary": self.summary,
            "sources_summary": self.sources_summary,
            "notices": list(self.notices),
            "notice_codes": list(self.notice_codes),
            "note_id": self.note_id,
            "note_path": self.note_path,
            "stream_token": self.stream_token,
            "retry_count": self.retry_count,
            "refined_queries": list(self.refined_queries),
            "source_strategy": self.source_strategy,
            "repository": self.repository,
        }


@dataclass(kw_only=True)
class ResearchState:
    """Hold mutable state for one research workflow."""

    research_topic: str | None = field(default=None)  # Report topic
    search_query: str | None = field(default=None)  # Deprecated placeholder
    web_research_results: list[str] = field(default_factory=list)
    sources_gathered: list[str] = field(default_factory=list)
    research_loop_count: int = field(default=0)  # Research loop count
    running_summary: str | None = field(default=None)  # Legacy summary field
    todo_items: list[TodoItem] = field(default_factory=list)
    structured_report: str | None = field(default=None)
    report_note_id: str | None = field(default=None)
    report_note_path: str | None = field(default=None)
    github_context: dict[str, Any] = field(default_factory=dict)


SummaryState = ResearchState


@dataclass(kw_only=True)
class SummaryStateOutput:
    """Represent the report and task data returned by a completed run."""

    running_summary: str | None = field(default=None)  # Backward-compatible文本
    report_markdown: str | None = field(default=None)
    todo_items: List[TodoItem] = field(default_factory=list)
