"""Provider-neutral report generation with a compatibility state wrapper."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from threading import Lock
from types import MappingProxyType
from typing import Any

from hello_agents import SimpleAgent

from config import Configuration
from models import SummaryState, TodoItem
from research.intelligence import (
    ClaimRecord,
    CoverageDecision,
    EvidenceRecord,
    GenericReportSpec,
    ResearchIntelligenceBundle,
)
from research.operations import OperationRejectedError, OperationScope
from research.profiles import (
    ResearchMode,
    ResearchProfile,
    built_in_profile_registry,
)
from research.report_document import StructuredSummaryDocument
from research.report_renderer import (
    RenderedStructuredReport,
    render_structured_report,
)
from research.report_validation import validate_citations
from research.session import CancellationRequestedError, DeadlineExceededError
from utils import strip_thinking_tokens

logger = logging.getLogger(__name__)


def _compact_text(value: object, limit: int) -> str:
    """Return bounded text suitable for a reporter prompt."""
    text = value.strip() if isinstance(value, str) else ""
    return text if len(text) <= limit else text[:limit] + "\n\n... [truncated]"


@dataclass(frozen=True, slots=True, kw_only=True)
class GenericReportingContext:
    """Immutable, provider-neutral input consumed by ``ReportingService``."""

    topic: str
    profile: ResearchProfile
    bundle: ResearchIntelligenceBundle | None = None
    tasks: tuple[TodoItem, ...] = ()
    claims: tuple[ClaimRecord, ...] = ()
    evidence: tuple[EvidenceRecord, ...] = ()
    coverage: CoverageDecision = field(default_factory=CoverageDecision)
    report_spec: GenericReportSpec | None = None
    notes: Mapping[str, Any] = field(default_factory=dict)
    source_context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate and detach context data before it reaches an LLM."""
        if not isinstance(self.topic, str) or not self.topic.strip():
            raise ValueError("Reporting topic must not be empty.")
        if not isinstance(self.profile, ResearchProfile):
            raise TypeError("Reporting profile must be a ResearchProfile.")
        if self.bundle is not None and not isinstance(
            self.bundle,
            ResearchIntelligenceBundle,
        ):
            raise TypeError("Reporting bundle must be schema-v2 intelligence.")
        normalized_tasks = tuple(self.tasks)
        if any(not isinstance(item, TodoItem) for item in normalized_tasks):
            raise TypeError("Reporting tasks must be TodoItem objects.")
        object.__setattr__(self, "tasks", normalized_tasks)
        normalized_claims = tuple(self.claims)
        normalized_evidence = tuple(self.evidence)
        if any(not isinstance(item, ClaimRecord) for item in normalized_claims):
            raise TypeError("Reporting claims must be ClaimRecord objects.")
        if any(not isinstance(item, EvidenceRecord) for item in normalized_evidence):
            raise TypeError("Reporting evidence must be EvidenceRecord objects.")
        if self.bundle is not None:
            if not normalized_claims:
                normalized_claims = self.bundle.claims
            if not normalized_evidence:
                normalized_evidence = self.bundle.evidence
            if self.coverage == CoverageDecision():
                object.__setattr__(self, "coverage", self.bundle.coverage)
            if self.report_spec is None:
                object.__setattr__(self, "report_spec", self.bundle.report_spec)
        if self.report_spec is None:
            object.__setattr__(self, "report_spec", GenericReportSpec(title=self.topic))
        object.__setattr__(self, "claims", normalized_claims)
        object.__setattr__(self, "evidence", normalized_evidence)
        for field_name in ("notes", "source_context"):
            value = getattr(self, field_name)
            if not isinstance(value, Mapping):
                raise TypeError(f"Reporting {field_name} must be a mapping.")
            object.__setattr__(
                self,
                field_name,
                MappingProxyType(dict(value)),
            )

    @classmethod
    def from_state(
        cls,
        state: SummaryState,
        *,
        notes_context: Mapping[str, Any] | None = None,
    ) -> GenericReportingContext:
        """Adapt legacy mutable state at the compatibility boundary only."""
        registry = built_in_profile_registry()
        raw_mode = state.research_mode
        try:
            profile = registry.resolve(
                profile_id=state.research_profile_id,
                mode=raw_mode,
            )
        except (KeyError, TypeError, ValueError):
            profile = registry.resolve(mode=ResearchMode.WEB)

        bundle: ResearchIntelligenceBundle | None = None
        raw_bundle = state.research_intelligence
        if isinstance(raw_bundle, Mapping) and raw_bundle.get("schema_version") == 2:
            try:
                bundle = ResearchIntelligenceBundle.from_dict(raw_bundle)
            except (TypeError, ValueError):
                bundle = None
        return cls(
            topic=state.research_topic or "Research report",
            profile=profile,
            bundle=bundle,
            tasks=tuple(state.todo_items),
            notes=notes_context or {},
            source_context=state.source_context,
        )


def _task_block(tasks: Sequence[TodoItem]) -> str:
    """Render bounded task summaries without exposing worker-only content."""
    lines: list[str] = []
    for task in tasks:
        lines.append(
            f"- Task {task.id}: {task.title} ({task.status})\n"
            f"  Intent: {_compact_text(task.intent, 240)}\n"
            f"  Summary: {_compact_text(task.summary or 'No summary', 800)}"
        )
        if task.sources_summary:
            lines.append(f"  Source references: {_compact_text(task.sources_summary, 600)}")
    return "\n".join(lines) or "- No completed tasks were recorded."


def _generic_prompt(context: GenericReportingContext) -> str:
    """Build one provider-neutral prompt from frozen generic contracts."""
    sections = context.profile.report_sections
    section_lines = "\n".join(
        f"- {section.id}: {section.title} (dimension={section.dimension})"
        for section in sections
    ) or "- Findings\n- Limitations"
    claims = "\n".join(
        f"- {claim.claim_id}: {claim.statement} "
        f"[evidence: {', '.join(claim.evidence_ids) or 'none'}]"
        for claim in context.claims
        if claim.reportable
    ) or "- No structured claims are available."
    evidence = "\n".join(
        f"- {item.evidence_id}: {item.title}; locator={item.locator.url}; "
        f"excerpt={_compact_text(item.excerpt, 700)}"
        for item in context.evidence
    ) or "- No frozen evidence is available."
    coverage = context.coverage
    notes = "\n".join(
        f"- {key}: {_compact_text(value, 400)}"
        for key, value in context.notes.items()
    ) or "- No note references."
    return (
        f"Research topic: {context.topic}\n"
        f"Research mode: {context.profile.mode.value}\n"
        f"Profile: {context.profile.profile_id} v{context.profile.version}\n\n"
        "Required report sections:\n"
        f"{section_lines}\n\n"
        "Completed task summaries:\n"
        f"{_task_block(context.tasks)}\n\n"
        "Structured claims (do not invent claim or evidence IDs):\n"
        f"{claims}\n\n"
        "Frozen evidence (cite only the listed IDs and locator URLs):\n"
        f"{evidence}\n\n"
        f"Coverage score: {coverage.coverage_score:.3f}; "
        f"missing dimensions: {', '.join(coverage.missing_dimensions) or 'none'}; "
        f"warnings: {', '.join(coverage.warnings) or 'none'}\n\n"
        "Note references:\n"
        f"{notes}\n\n"
        "Write concise Markdown. Do not create new URLs, Evidence IDs, claims, "
        "or unsupported conclusions."
    )


def _structured_prompt(context: GenericReportingContext) -> str:
    """Build a strict JSON prompt for the opt-in structured boundary."""
    markdown_instruction = (
        "Write concise Markdown. Do not create new URLs, Evidence IDs, claims, "
        "or unsupported conclusions."
    )
    prompt = _generic_prompt(context)
    if not prompt.endswith(markdown_instruction):  # pragma: no cover - internal invariant
        raise RuntimeError("Generic reporter prompt suffix changed unexpectedly.")
    schema_instruction = (
        "Return exactly one JSON object with schema_version=1, task_id=\"report\", "
        "claim_ids, and paragraphs. Each paragraph must contain section_id, "
        "paragraph_type, text, claim_ids, and citation_ids. Use only the claim "
        "and Evidence IDs listed above. A factual paragraph always needs a claim "
        "and citation; an analysis paragraph with claim_ids also needs citations; "
        "a limitation paragraph may omit both. Do not include Markdown fences, "
        "citation markers, or URLs in paragraph text."
    )
    return prompt[: -len(markdown_instruction)] + schema_instruction


class StructuredReportGenerationError(RuntimeError):
    """Raised when the opt-in structured reporter cannot produce valid JSON."""


def _deterministic_sections(context: GenericReportingContext) -> str:
    """Append deterministic traceability sections after model prose."""
    if context.report_spec is None:  # pragma: no cover - normalized in __post_init__
        report_spec = GenericReportSpec(title=context.topic)
    else:
        report_spec = context.report_spec
    claims_lines = [
        f"- {claim.statement} — Evidence: "
        + (", ".join(f"`{item}`" for item in claim.evidence_ids) or "none")
        for claim in context.claims
        if claim.reportable
    ]
    references_lines = [
        f"- `{item.evidence_id}` — [{item.title}]({item.locator.url})"
        for item in context.evidence
        if context.bundle is not None and context.bundle.evidence_frozen
    ]
    coverage = context.coverage
    limitation_lines = list(report_spec.limitations)
    limitation_lines.extend(coverage.warnings)
    limitation_lines.extend(
        f"Missing dimension: {item}" for item in coverage.missing_dimensions
    )
    if not limitation_lines:
        limitation_lines.append("No additional limitations were recorded.")
    return (
        "\n\n## Claim—Evidence Index\n"
        + ("\n".join(claims_lines) if claims_lines else "- No reportable claims.")
        + "\n\n## Evidence References\n"
        + ("\n".join(references_lines) if references_lines else "- No frozen evidence references.")
        + "\n\n## Coverage and Limitations\n"
        + "\n".join(f"- {item}" for item in limitation_lines)
    )


class ReportingService:
    """Generate one generic report while preserving the legacy call boundary."""

    def __init__(self, report_agent: SimpleAgent, config: Configuration) -> None:
        """Initialize the service with its reporting agent and configuration."""
        self._agent = report_agent
        self._config = config
        self._agent_lock = Lock()

    def generate_report(
        self,
        context_or_state: GenericReportingContext | SummaryState,
        notes_context: dict[str, Any] | None = None,
        *,
        operation_scope: OperationScope | None = None,
    ) -> str:
        """Generate a report from generic context or adapt legacy state once."""
        if isinstance(context_or_state, GenericReportingContext):
            context = context_or_state
        else:
            context = GenericReportingContext.from_state(
                context_or_state,
                notes_context=notes_context,
            )
        prompt = _generic_prompt(context)
        with self._agent_lock:
            try:
                if operation_scope is None:
                    response = self._agent.run(prompt)
                else:
                    response = self._agent.run(
                        prompt,
                        _research_operation_scope=operation_scope,
                    )
            except (
                OperationRejectedError,
                CancellationRequestedError,
                DeadlineExceededError,
            ):
                raise
            except Exception:
                logger.error("Reporter LLM call failed")
                return (
                    "# Report generation failed\n\n"
                    "## Limitations\n"
                    "The report agent could not produce a response."
                )
            finally:
                self._agent.clear_history()

        report_text = response.strip() if isinstance(response, str) else ""
        if getattr(self._config, "strip_thinking_tokens", False):
            report_text = strip_thinking_tokens(report_text)
        if context.bundle is not None:
            citation_bundle = context.bundle
            if not citation_bundle.evidence_frozen:
                # The legacy coordinator freezes immediately after the report
                # call.  Validate against the captured ledger now; the
                # session-level gate rechecks the frozen replacement later.
                citation_bundle = replace(citation_bundle, evidence_frozen=True)
            citation_gate = validate_citations(report_text, citation_bundle)
            report_text = citation_gate.sanitized_report
            if report_text:
                report_text += _deterministic_sections(context)
        return report_text or "# Report generation failed\n\n## Limitations\nNo report content was returned."

    def render_structured_document(
        self,
        context_or_state: GenericReportingContext | SummaryState,
        document: StructuredSummaryDocument,
        notes_context: dict[str, Any] | None = None,
    ) -> RenderedStructuredReport:
        """Render one pre-built document against frozen context evidence."""
        if isinstance(context_or_state, GenericReportingContext):
            context = context_or_state
        else:
            context = GenericReportingContext.from_state(
                context_or_state,
                notes_context=notes_context,
            )
        if context.bundle is None or not context.bundle.evidence_frozen:
            raise StructuredReportGenerationError(
                "Structured reports require a frozen intelligence bundle."
            )
        report_spec = context.report_spec or GenericReportSpec(title=context.topic)
        section_titles = {
            item.id: item.title for item in context.profile.report_sections
        }
        return render_structured_report(
            document,
            title=report_spec.title,
            claims=context.claims,
            evidence=context.evidence,
            section_titles=section_titles,
            evidence_frozen=True,
        )

    def generate_structured_report(
        self,
        context_or_state: GenericReportingContext | SummaryState,
        notes_context: dict[str, Any] | None = None,
        *,
        operation_scope: OperationScope | None = None,
    ) -> RenderedStructuredReport:
        """Generate strict structured JSON, validate it, and render Markdown.

        This is an opt-in integration boundary. The existing ``generate_report``
        string interface remains unchanged until the research kernel adopts the
        structured flow explicitly.
        """
        if isinstance(context_or_state, GenericReportingContext):
            context = context_or_state
        else:
            context = GenericReportingContext.from_state(
                context_or_state,
                notes_context=notes_context,
            )
        if context.bundle is None or not context.bundle.evidence_frozen:
            raise StructuredReportGenerationError(
                "Structured reports require a frozen intelligence bundle."
            )
        prompt = _structured_prompt(context)
        with self._agent_lock:
            try:
                if operation_scope is None:
                    response = self._agent.run(prompt)
                else:
                    response = self._agent.run(
                        prompt,
                        _research_operation_scope=operation_scope,
                    )
            except (
                OperationRejectedError,
                CancellationRequestedError,
                DeadlineExceededError,
            ):
                raise
            except Exception as exc:
                logger.error("Structured reporter LLM call failed")
                raise StructuredReportGenerationError(
                    "Structured report agent failed."
                ) from exc
            finally:
                self._agent.clear_history()

        response_text = response.strip() if isinstance(response, str) else ""
        if getattr(self._config, "strip_thinking_tokens", False):
            response_text = strip_thinking_tokens(response_text)
        try:
            payload = json.loads(response_text)
        except (TypeError, ValueError) as exc:
            raise StructuredReportGenerationError(
                "Structured report agent returned invalid JSON."
            ) from exc
        if not isinstance(payload, Mapping):
            raise StructuredReportGenerationError(
                "Structured report JSON must be an object."
            )
        try:
            document = StructuredSummaryDocument.from_dict(payload)
        except (TypeError, ValueError) as exc:
            raise StructuredReportGenerationError(
                "Structured report JSON violates the document contract."
            ) from exc
        if not document.paragraphs:
            raise StructuredReportGenerationError(
                "Structured report must contain at least one paragraph."
            )
        return self.render_structured_document(context, document)


__all__ = [
    "GenericReportingContext",
    "ReportingService",
    "StructuredReportGenerationError",
]
