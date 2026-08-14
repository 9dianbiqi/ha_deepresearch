"""Deterministic Markdown rendering for structured research documents."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .intelligence import ClaimRecord, EvidenceLocator, EvidenceRecord
from .report_document import StructuredSummaryDocument
from .report_validation import StructuredCitationGate, validate_structured_citations

_MARKDOWN_INLINE_RE = re.compile(r"([\\`*_{\}\[\]<>#|])")


def _plain_markdown(value: str) -> str:
    """Render untrusted prose as one Markdown-safe paragraph line."""
    normalized = " ".join(value.split())
    return _MARKDOWN_INLINE_RE.sub(r"\\\1", normalized)


def _section_title(section_id: str, section_titles: Mapping[str, str]) -> str:
    """Resolve a declared section title or a stable humanized fallback."""
    supplied = section_titles.get(section_id)
    if isinstance(supplied, str) and supplied.strip():
        return _plain_markdown(supplied.strip())
    return _plain_markdown(section_id.replace("_", " ").replace("-", " ").title())


def _locator_label(locator: EvidenceLocator) -> str:
    """Return a concise deterministic locator description."""
    parts: list[str] = [locator.locator_type]
    if locator.file_path:
        parts.append(locator.file_path)
    if locator.line_start is not None and locator.line_end is not None:
        parts.append(f"L{locator.line_start}-L{locator.line_end}")
    if locator.page_start is not None and locator.page_end is not None:
        parts.append(f"pages {locator.page_start}-{locator.page_end}")
    if locator.section:
        parts.append(f"section {locator.section}")
    if locator.paragraph:
        parts.append(f"paragraph {locator.paragraph}")
    if locator.fragment:
        parts.append(f"fragment {locator.fragment}")
    return " · ".join(_plain_markdown(item) for item in parts)


@dataclass(frozen=True, slots=True)
class EvidenceCitationTrace:
    """One rendered citation with its frozen source and locator trace."""

    marker: str
    evidence_id: str
    claim_ids: tuple[str, ...]
    provider_id: str
    source_kind: str
    source_id: str
    title: str
    locator: EvidenceLocator
    excerpt: str

    def as_dict(self) -> dict[str, object]:
        """Return a detached JSON-ready evidence trace."""
        return {
            "marker": self.marker,
            "evidence_id": self.evidence_id,
            "claim_ids": list(self.claim_ids),
            "provider_id": self.provider_id,
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "title": self.title,
            "locator": self.locator.as_dict(),
            "excerpt": self.excerpt,
        }


@dataclass(frozen=True, slots=True)
class ParagraphCitationTrace:
    """Trace one rendered paragraph through claims to frozen evidence."""

    paragraph_id: str
    section_id: str
    paragraph_type: str
    claim_ids: tuple[str, ...]
    citations: tuple[EvidenceCitationTrace, ...]

    def as_dict(self) -> dict[str, object]:
        """Return a detached JSON-ready paragraph trace."""
        return {
            "paragraph_id": self.paragraph_id,
            "section_id": self.section_id,
            "paragraph_type": self.paragraph_type,
            "claim_ids": list(self.claim_ids),
            "citations": [item.as_dict() for item in self.citations],
        }


@dataclass(frozen=True, slots=True)
class RenderedStructuredReport:
    """Final Markdown plus explicit paragraph-level citation mappings."""

    markdown: str
    paragraph_traces: tuple[ParagraphCitationTrace, ...]
    citation_gate: StructuredCitationGate

    def as_dict(self) -> dict[str, object]:
        """Return a detached JSON-ready render result."""
        return {
            "markdown": self.markdown,
            "paragraph_traces": [
                item.as_dict() for item in self.paragraph_traces
            ],
            "citation_gate": self.citation_gate.as_dict(),
        }


class StructuredReportValidationError(ValueError):
    """Raised when a structured document fails the citation gate."""

    def __init__(self, gate: StructuredCitationGate) -> None:
        """Preserve the machine-readable gate on a concise exception."""
        self.gate = gate
        reasons = ", ".join(gate.failure_reasons) or "unknown"
        super().__init__(f"Structured report citation validation failed: {reasons}.")


def _citation_trace(
    *,
    evidence: EvidenceRecord,
    marker: str,
    paragraph_claims: Sequence[ClaimRecord],
) -> EvidenceCitationTrace:
    """Build one explicit trace without parsing generated prose."""
    claim_ids = tuple(
        claim.claim_id
        for claim in paragraph_claims
        if evidence.evidence_id in claim.evidence_ids
    )
    return EvidenceCitationTrace(
        marker=marker,
        evidence_id=evidence.evidence_id,
        claim_ids=claim_ids,
        provider_id=evidence.source.provider_id,
        source_kind=evidence.source.source_kind,
        source_id=evidence.source.source_id,
        title=evidence.title,
        locator=evidence.locator,
        excerpt=evidence.excerpt,
    )


def render_structured_report(
    document: StructuredSummaryDocument,
    *,
    title: str,
    claims: Sequence[ClaimRecord],
    evidence: Sequence[EvidenceRecord],
    section_titles: Mapping[str, str] | None = None,
    evidence_frozen: bool = True,
) -> RenderedStructuredReport:
    """Validate and render a structured report with system-owned citations."""
    if not isinstance(title, str) or not title.strip():
        raise ValueError("Structured report title must not be empty.")
    normalized_claims = tuple(claims)
    normalized_evidence = tuple(evidence)
    normalized_sections = dict(section_titles or {})
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in normalized_sections.items()):
        raise TypeError("section_titles must map text IDs to text titles.")
    gate = validate_structured_citations(
        document,
        normalized_claims,
        normalized_evidence,
        evidence_frozen=evidence_frozen,
    )
    if not gate.valid:
        raise StructuredReportValidationError(gate)

    claim_by_id = {item.claim_id: item for item in normalized_claims}
    evidence_by_id = {item.evidence_id: item for item in normalized_evidence}
    bound_evidence_ids = sorted(
        {
            evidence_id
            for claim_id in document.claim_ids
            if claim_id in claim_by_id
            for evidence_id in claim_by_id[claim_id].evidence_ids
            if evidence_id in evidence_by_id
        }
    )
    marker_by_id = {
        evidence_id: f"E{position}"
        for position, evidence_id in enumerate(bound_evidence_ids, start=1)
    }
    marker_position = {
        evidence_id: position
        for position, evidence_id in enumerate(bound_evidence_ids, start=1)
    }

    lines = [f"# {_plain_markdown(title.strip())}"]
    traces: list[ParagraphCitationTrace] = []
    current_section: str | None = None
    for paragraph in document.paragraphs:
        if paragraph.section_id != current_section:
            lines.extend(
                [
                    "",
                    f"## {_section_title(paragraph.section_id, normalized_sections)}",
                ]
            )
            current_section = paragraph.section_id
        paragraph_claims = tuple(
            claim_by_id[item]
            for item in paragraph.claim_ids
            if item in claim_by_id
        )
        ordered_citation_ids = tuple(
            sorted(
                paragraph.citation_ids,
                key=lambda item: marker_position[item],
            )
        )
        citations = tuple(
            _citation_trace(
                evidence=evidence_by_id[evidence_id],
                marker=marker_by_id[evidence_id],
                paragraph_claims=paragraph_claims,
            )
            for evidence_id in ordered_citation_ids
        )
        marker_suffix = "".join(f"[{item.marker}]" for item in citations)
        rendered_text = _plain_markdown(paragraph.text)
        if marker_suffix:
            rendered_text = f"{rendered_text} {marker_suffix}"
        lines.extend(["", rendered_text])
        traces.append(
            ParagraphCitationTrace(
                paragraph_id=paragraph.paragraph_id,
                section_id=paragraph.section_id,
                paragraph_type=paragraph.paragraph_type,
                claim_ids=paragraph.claim_ids,
                citations=citations,
            )
        )

    lines.extend(["", "## Claim–Evidence Index"])
    for claim_id in document.claim_ids:
        claim = claim_by_id[claim_id]
        markers = "".join(
            f"[{marker_by_id[item]}]"
            for item in claim.evidence_ids
            if item in marker_by_id
        )
        evidence_label = markers or "none"
        lines.append(
            f"- {_plain_markdown(claim.statement)} — Evidence: {evidence_label}"
        )
    if not document.claim_ids:
        lines.append("- No reportable claims.")

    lines.extend(["", "## Evidence References"])
    for evidence_id in bound_evidence_ids:
        item = evidence_by_id[evidence_id]
        marker = marker_by_id[evidence_id]
        locator = _locator_label(item.locator)
        reference = (
            f"- [{marker}] `{_plain_markdown(evidence_id)}` — "
            f"[{_plain_markdown(item.title)}]({item.locator.url}) — {locator}"
        )
        if item.excerpt:
            reference += f" — Excerpt: {_plain_markdown(item.excerpt)}"
        lines.append(reference)
    if not bound_evidence_ids:
        lines.append("- No frozen evidence references.")

    return RenderedStructuredReport(
        markdown="\n".join(lines).strip() + "\n",
        paragraph_traces=tuple(traces),
        citation_gate=gate,
    )


__all__ = [
    "EvidenceCitationTrace",
    "ParagraphCitationTrace",
    "RenderedStructuredReport",
    "StructuredReportValidationError",
    "render_structured_report",
]
