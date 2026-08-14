"""Deterministic completeness checks for generated research reports."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from models import TodoItem

from .intelligence import ClaimRecord, EvidenceRecord, ResearchIntelligenceBundle
from .report_document import StructuredSummaryDocument

_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$")
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_CITATION_RE = re.compile(
    r"(?i)(?:\[[^\]]+\]\(https?://|\b(?:sources?|references?)\s*:\s*|"
    + r"(?:\u6765\u6e90|\u53c2\u8003)\s*[:\uff1a])"
)
_TRUNCATION_SUFFIXES = (
    "...",
    "\u2026",
    ":",
    "\uff1a",
    ",",
    "\uff0c",
    "(",
    "\uff08",
    "-",
    "\u2014",
)
_MIN_REPORT_CHARS = 160
_MIN_SECTIONS = 2
_EVIDENCE_ID_RE = re.compile(r"\b(?:ev|evidence)[_-][A-Za-z0-9][A-Za-z0-9._-]*\b")
_SYSTEM_CITATION_MARKER_RE = re.compile(r"\[E\d+\]", re.IGNORECASE)


def _citation_url(value: str) -> str:
    """Strip Markdown punctuation that is not part of a URL."""
    return value.rstrip(".,;:!?)]}>")


@dataclass(frozen=True, slots=True)
class CitationGate:
    """Deterministic citation allowlist result for one generated report."""

    valid: bool
    sanitized_report: str
    allowed_evidence_ids: tuple[str, ...] = ()
    used_evidence_ids: tuple[str, ...] = ()
    unknown_evidence_ids: tuple[str, ...] = ()
    allowed_locator_urls: tuple[str, ...] = ()
    illegal_urls: tuple[str, ...] = ()
    missing_evidence_ids: tuple[str, ...] = ()
    failure_reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-ready citation gate decision."""
        return {
            "valid": self.valid,
            "allowed_evidence_ids": list(self.allowed_evidence_ids),
            "used_evidence_ids": list(self.used_evidence_ids),
            "unknown_evidence_ids": list(self.unknown_evidence_ids),
            "allowed_locator_urls": list(self.allowed_locator_urls),
            "illegal_urls": list(self.illegal_urls),
            "missing_evidence_ids": list(self.missing_evidence_ids),
            "failure_reasons": list(self.failure_reasons),
        }


# Descriptive alias for callers that prefer the result-oriented name.
CitationValidationResult = CitationGate


@dataclass(frozen=True, slots=True)
class ParagraphCitationValidation:
    """Describe claim and citation validation for one structured paragraph."""

    paragraph_id: str
    required_citations: bool
    claim_ids: tuple[str, ...] = ()
    citation_ids: tuple[str, ...] = ()
    allowed_evidence_ids: tuple[str, ...] = ()
    unknown_claim_ids: tuple[str, ...] = ()
    unknown_evidence_ids: tuple[str, ...] = ()
    unbound_citation_ids: tuple[str, ...] = ()
    illegal_urls: tuple[str, ...] = ()
    failure_reasons: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        """Return whether the paragraph can be rendered safely."""
        return not self.failure_reasons

    def as_dict(self) -> dict[str, object]:
        """Return a detached JSON-ready paragraph decision."""
        return {
            "paragraph_id": self.paragraph_id,
            "valid": self.valid,
            "required_citations": self.required_citations,
            "claim_ids": list(self.claim_ids),
            "citation_ids": list(self.citation_ids),
            "allowed_evidence_ids": list(self.allowed_evidence_ids),
            "unknown_claim_ids": list(self.unknown_claim_ids),
            "unknown_evidence_ids": list(self.unknown_evidence_ids),
            "unbound_citation_ids": list(self.unbound_citation_ids),
            "illegal_urls": list(self.illegal_urls),
            "failure_reasons": list(self.failure_reasons),
        }


@dataclass(frozen=True, slots=True)
class StructuredCitationGate:
    """Fail-closed citation decision for a structured report document."""

    valid: bool
    paragraph_results: tuple[ParagraphCitationValidation, ...] = ()
    allowed_claim_ids: tuple[str, ...] = ()
    allowed_evidence_ids: tuple[str, ...] = ()
    unknown_claim_ids: tuple[str, ...] = ()
    unknown_evidence_ids: tuple[str, ...] = ()
    illegal_urls: tuple[str, ...] = ()
    failure_reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        """Return a detached JSON-ready structured citation decision."""
        return {
            "valid": self.valid,
            "paragraph_results": [
                item.as_dict() for item in self.paragraph_results
            ],
            "allowed_claim_ids": list(self.allowed_claim_ids),
            "allowed_evidence_ids": list(self.allowed_evidence_ids),
            "unknown_claim_ids": list(self.unknown_claim_ids),
            "unknown_evidence_ids": list(self.unknown_evidence_ids),
            "illegal_urls": list(self.illegal_urls),
            "failure_reasons": list(self.failure_reasons),
        }


def _unique_records(
    records: Sequence[ClaimRecord] | Sequence[EvidenceRecord],
    *,
    identifier: str,
) -> tuple[dict[str, ClaimRecord | EvidenceRecord], bool]:
    """Index typed records while reporting duplicate stable identifiers."""
    indexed: dict[str, ClaimRecord | EvidenceRecord] = {}
    duplicated = False
    for record in records:
        key = getattr(record, identifier)
        if key in indexed:
            duplicated = True
            continue
        indexed[key] = record
    return indexed, duplicated


def validate_structured_citations(
    document: StructuredSummaryDocument,
    claims: Sequence[ClaimRecord],
    evidence: Sequence[EvidenceRecord],
    *,
    evidence_frozen: bool = True,
) -> StructuredCitationGate:
    """Validate explicit paragraph-to-claim-to-evidence relationships.

    Citation identity comes exclusively from ``citation_ids``. Free-form text
    is inspected only to reject injected markers and URLs outside the exact
    locator allowlist; it is never parsed to infer a citation.
    """
    if not isinstance(document, StructuredSummaryDocument):
        raise TypeError("document must be a StructuredSummaryDocument.")
    normalized_claims = tuple(claims)
    normalized_evidence = tuple(evidence)
    if any(not isinstance(item, ClaimRecord) for item in normalized_claims):
        raise TypeError("claims must contain ClaimRecord objects.")
    if any(not isinstance(item, EvidenceRecord) for item in normalized_evidence):
        raise TypeError("evidence must contain EvidenceRecord objects.")
    if not isinstance(evidence_frozen, bool):
        raise TypeError("evidence_frozen must be boolean.")

    raw_claims, duplicate_claims = _unique_records(
        normalized_claims,
        identifier="claim_id",
    )
    raw_evidence, duplicate_evidence = _unique_records(
        normalized_evidence,
        identifier="evidence_id",
    )
    claim_by_id = {
        key: value for key, value in raw_claims.items() if isinstance(value, ClaimRecord)
    }
    evidence_by_id = {
        key: value
        for key, value in raw_evidence.items()
        if isinstance(value, EvidenceRecord)
    }
    unknown_document_claims = tuple(
        item for item in document.claim_ids if item not in claim_by_id
    )
    paragraph_results: list[ParagraphCitationValidation] = []
    all_unknown_claims: list[str] = list(unknown_document_claims)
    all_unknown_evidence: list[str] = []
    all_illegal_urls: list[str] = []

    for paragraph in document.paragraphs:
        unknown_claims = tuple(
            item for item in paragraph.claim_ids if item not in claim_by_id
        )
        unknown_evidence = tuple(
            item for item in paragraph.citation_ids if item not in evidence_by_id
        )
        known_claims = tuple(
            claim_by_id[item]
            for item in paragraph.claim_ids
            if item in claim_by_id
        )
        allowed_ids = tuple(
            dict.fromkeys(
                evidence_id
                for claim in known_claims
                for evidence_id in (
                    *claim.evidence_ids,
                    *claim.conflicting_evidence_ids,
                )
                if evidence_id in evidence_by_id
            )
        )
        unbound_ids = tuple(
            item
            for item in paragraph.citation_ids
            if item in evidence_by_id and item not in allowed_ids
        )
        allowed_urls = {
            evidence_by_id[item].locator.url
            for item in paragraph.citation_ids
            if item in allowed_ids
            if evidence_by_id[item].locator.url
        }
        raw_urls = tuple(
            dict.fromkeys(
                _citation_url(item) for item in _URL_RE.findall(paragraph.text)
            )
        )
        illegal_urls = tuple(item for item in raw_urls if item not in allowed_urls)
        citations_required = paragraph.paragraph_type == "factual" or (
            paragraph.paragraph_type == "analysis" and bool(paragraph.claim_ids)
        )
        reasons: list[str] = []
        if paragraph.paragraph_type == "factual" and not paragraph.claim_ids:
            reasons.append("factual_claim_missing")
        if citations_required and not paragraph.citation_ids:
            reasons.append("paragraph_citation_missing")
        if unknown_claims:
            reasons.append("unknown_claim_id")
        if unknown_evidence:
            reasons.append("unknown_evidence_id")
        if unbound_ids:
            reasons.append("citation_not_bound_to_claim")
        if illegal_urls:
            reasons.append("illegal_locator_url")
        if _SYSTEM_CITATION_MARKER_RE.search(paragraph.text):
            reasons.append("system_citation_marker_in_text")
        paragraph_results.append(
            ParagraphCitationValidation(
                paragraph_id=paragraph.paragraph_id,
                required_citations=citations_required,
                claim_ids=paragraph.claim_ids,
                citation_ids=paragraph.citation_ids,
                allowed_evidence_ids=allowed_ids,
                unknown_claim_ids=unknown_claims,
                unknown_evidence_ids=unknown_evidence,
                unbound_citation_ids=unbound_ids,
                illegal_urls=illegal_urls,
                failure_reasons=tuple(dict.fromkeys(reasons)),
            )
        )
        all_unknown_claims.extend(unknown_claims)
        all_unknown_evidence.extend(unknown_evidence)
        all_illegal_urls.extend(illegal_urls)

    reasons = [
        reason
        for result in paragraph_results
        for reason in result.failure_reasons
    ]
    if not evidence_frozen:
        reasons.append("evidence_not_frozen")
    if duplicate_claims:
        reasons.append("duplicate_claim_id")
    if duplicate_evidence:
        reasons.append("duplicate_evidence_id")
    if unknown_document_claims:
        reasons.append("unknown_claim_id")
    unique_reasons = tuple(dict.fromkeys(reasons))
    return StructuredCitationGate(
        valid=not unique_reasons,
        paragraph_results=tuple(paragraph_results),
        allowed_claim_ids=tuple(sorted(claim_by_id)),
        allowed_evidence_ids=tuple(sorted(evidence_by_id)),
        unknown_claim_ids=tuple(dict.fromkeys(all_unknown_claims)),
        unknown_evidence_ids=tuple(dict.fromkeys(all_unknown_evidence)),
        illegal_urls=tuple(dict.fromkeys(all_illegal_urls)),
        failure_reasons=unique_reasons,
    )


def _bundle_from_value(value: object) -> ResearchIntelligenceBundle | None:
    """Resolve a v2 bundle from a bundle, context, or JSON mapping."""
    if isinstance(value, ResearchIntelligenceBundle):
        return value
    candidate = getattr(value, "bundle", None)
    if isinstance(candidate, ResearchIntelligenceBundle):
        return candidate
    if isinstance(value, Mapping):
        try:
            if value.get("schema_version") == 2:
                return ResearchIntelligenceBundle.from_dict(value)
        except (TypeError, ValueError):
            return None
    return None


def validate_citations(
    report: object,
    bundle_or_context: object,
    *,
    require_evidence_ids: bool = True,
) -> CitationGate:
    """Allow only frozen Evidence IDs and their locator URLs in a report."""
    text = report if isinstance(report, str) else ""
    bundle = _bundle_from_value(bundle_or_context)
    if bundle is None:
        return CitationGate(
            valid=not require_evidence_ids,
            sanitized_report=text,
            failure_reasons=("intelligence_bundle_missing",) if require_evidence_ids else (),
        )
    evidence_by_id = {
        evidence.evidence_id: evidence
        for evidence in bundle.evidence
        if bundle.evidence_frozen
    }
    allowed_ids = tuple(sorted(evidence_by_id))
    allowed_urls = tuple(
        sorted(
            {
                evidence.locator.url
                for evidence in evidence_by_id.values()
                if evidence.locator.url
            }
        )
    )
    raw_ids = tuple(dict.fromkeys(_EVIDENCE_ID_RE.findall(text)))
    unknown_ids = tuple(item for item in raw_ids if item not in evidence_by_id)
    used_ids = tuple(item for item in raw_ids if item in evidence_by_id)
    raw_urls = tuple(dict.fromkeys(_citation_url(item) for item in _URL_RE.findall(text)))
    illegal_urls = tuple(item for item in raw_urls if item not in allowed_urls)
    required_ids = tuple(
        dict.fromkeys(
            evidence_id
            for claim in bundle.claims
            if claim.reportable
            for evidence_id in claim.evidence_ids
        )
    )
    missing_ids = (
        tuple(item for item in required_ids if item not in used_ids)
        if require_evidence_ids
        else ()
    )

    sanitized = text
    if unknown_ids:
        unknown_set = set(unknown_ids)
        sanitized = _EVIDENCE_ID_RE.sub(
            lambda match: match.group(0)
            if match.group(0) not in unknown_set
            else "[unapproved evidence removed]",
            sanitized,
        )
    if illegal_urls:
        illegal_set = set(illegal_urls)

        def replace_url(match: re.Match[str]) -> str:
            raw = match.group(0)
            normalized = _citation_url(raw)
            if normalized not in illegal_set:
                return raw
            suffix = raw[len(normalized) :]
            return "[unapproved source removed]" + suffix

        sanitized = _URL_RE.sub(replace_url, sanitized)

    reasons: list[str] = []
    if not bundle.evidence_frozen:
        reasons.append("evidence_not_frozen")
    if unknown_ids:
        reasons.append("unknown_evidence_id")
    if illegal_urls:
        reasons.append("illegal_locator_url")
    if missing_ids:
        reasons.append("claim_evidence_missing")
    return CitationGate(
        valid=not reasons,
        sanitized_report=sanitized,
        allowed_evidence_ids=allowed_ids,
        used_evidence_ids=used_ids,
        unknown_evidence_ids=unknown_ids,
        allowed_locator_urls=allowed_urls,
        illegal_urls=illegal_urls,
        missing_evidence_ids=missing_ids,
        failure_reasons=tuple(dict.fromkeys(reasons)),
    )


@dataclass(frozen=True, slots=True)
class ReportValidationResult:
    """Describe deterministic report completeness checks."""

    valid: bool
    output_chars: int
    has_title: bool
    section_count: int
    completed_sections: int
    covered_tasks: int
    total_tasks: int
    requires_citations: bool
    has_citations: bool
    finish_reason: str | None = None
    failure_reasons: tuple[str, ...] = ()
    citation_valid: bool = True
    citation_failure_reasons: tuple[str, ...] = ()

    @property
    def failure_reason(self) -> str | None:
        """Return the first stable failure code, if validation failed."""
        return self.failure_reasons[0] if self.failure_reasons else None

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-ready validation summary."""
        return {
            "valid": self.valid,
            "output_chars": self.output_chars,
            "has_title": self.has_title,
            "section_count": self.section_count,
            "completed_sections": self.completed_sections,
            "covered_tasks": self.covered_tasks,
            "total_tasks": self.total_tasks,
            "requires_citations": self.requires_citations,
            "has_citations": self.has_citations,
            "finish_reason": self.finish_reason,
            "failure_reason": self.failure_reason,
            "failure_reasons": list(self.failure_reasons),
            "citation_valid": self.citation_valid,
            "citation_failure_reasons": list(self.citation_failure_reasons),
        }


def _task_is_covered(text: str, task: TodoItem) -> bool:
    """Return whether a report contains a task title or explicit task marker."""
    title = task.title.strip()
    if title and title.casefold() in text.casefold():
        return True
    task_id = re.escape(str(task.id))
    marker = (
        rf"(?i)(?:\btask\s*[-#]?\s*{task_id}\b|"
        rf"{chr(0x4EFB)}{chr(0x52A1)}\s*{task_id}(?:\b|[^\d]))"
    )
    return bool(re.search(marker, text))


def validate_report(
    report: object,
    tasks: Sequence[TodoItem],
    *,
    finish_reason: str | None = None,
    citation_gate: CitationGate | None = None,
) -> ReportValidationResult:
    r"""Validate one report without asking another model to judge it.

    The checks intentionally focus on stable structural guarantees.  They
    reject the observed ``# title\n\n## 1.`` output while allowing normal
    Markdown prose and non-English section headings.
    """
    text = report.strip() if isinstance(report, str) else ""
    lines = text.splitlines()
    headings: list[tuple[int, str, int]] = []
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if match:
            headings.append((len(match.group(1)), match.group(2).strip(), index))

    first_nonempty_index = next(
        (index for index, line in enumerate(lines) if line.strip()),
        -1,
    )
    first_heading = headings[0] if headings else None
    has_title = bool(
        first_heading
        and first_heading[2] == first_nonempty_index
        and first_heading[0] == 1
        and first_heading[1].strip(
            " .:;!?" + "\uff1a\uff1b\uff01\uff1f"
        )
    )

    section_headings = [heading for heading in headings if heading[0] >= 2]
    completed_sections = 0
    unfinished_heading = False
    for position, (level, title, line_index) in enumerate(section_headings):
        next_index = len(lines)
        for next_level, _, next_line_index in section_headings[position + 1 :]:
            if next_level <= level:
                next_index = next_line_index
                break
        body = [line.strip() for line in lines[line_index + 1 : next_index] if line.strip()]
        if re.fullmatch(r"\d+[.)]?", title.strip()) or not body:
            unfinished_heading = True
        if body:
            completed_sections += 1

    completed_tasks = [
        task for task in tasks if task.status in {"completed", "skipped"}
    ]
    covered_tasks = sum(_task_is_covered(text, task) for task in completed_tasks)
    requires_citations = any(
        isinstance(task.sources_summary, str) and task.sources_summary.strip()
        for task in completed_tasks
    )
    has_citations = bool(_URL_RE.search(text) or _CITATION_RE.search(text))

    final_line = next((line.strip() for line in reversed(lines) if line.strip()), "")
    reasons: list[str] = []
    if not text:
        reasons.append("empty_report")
    if len(text) < _MIN_REPORT_CHARS:
        reasons.append("report_too_short")
    if not has_title:
        reasons.append("missing_title")
    if len(section_headings) < _MIN_SECTIONS:
        reasons.append("insufficient_sections")
    if unfinished_heading:
        reasons.append("unfinished_section")
    if text.count("```") % 2:
        reasons.append("unclosed_code_fence")
    if final_line.endswith(_TRUNCATION_SUFFIXES) or (
        re.search(r"(?i)(?:to be continued|incomplete)$", final_line)
        or final_line.endswith(("\u5f85\u7eed", "\u672a\u5b8c"))
    ):
        reasons.append("truncated_report")
    if completed_tasks and covered_tasks < len(completed_tasks):
        reasons.append("task_coverage_missing")
    if requires_citations and not has_citations:
        reasons.append("citations_missing")

    citation_valid = True
    citation_failure_reasons: tuple[str, ...] = ()
    if citation_gate is not None:
        citation_valid = citation_gate.valid
        citation_failure_reasons = citation_gate.failure_reasons
        if not citation_valid:
            reasons.extend(f"citation_{item}" for item in citation_failure_reasons)

    return ReportValidationResult(
        valid=not reasons,
        output_chars=len(text),
        has_title=has_title,
        section_count=len(section_headings),
        completed_sections=completed_sections,
        covered_tasks=covered_tasks,
        total_tasks=len(completed_tasks),
        requires_citations=requires_citations,
        has_citations=has_citations,
        finish_reason=finish_reason,
        failure_reasons=tuple(dict.fromkeys(reasons)),
        citation_valid=citation_valid,
        citation_failure_reasons=citation_failure_reasons,
    )


__all__ = [
    "CitationGate",
    "CitationValidationResult",
    "ParagraphCitationValidation",
    "ReportValidationResult",
    "StructuredCitationGate",
    "validate_citations",
    "validate_report",
    "validate_structured_citations",
]
