"""Immutable structured-summary and evidence-quality contracts."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, TypeVar

SUMMARY_DOCUMENT_SCHEMA_VERSION = 1
SUMMARY_QUALITY_SCHEMA_VERSION = 1

PARAGRAPH_TYPES = frozenset({"factual", "analysis", "limitation"})
CLAIM_VERDICTS = frozenset(
    {"supported", "partial", "unsupported", "contradicted", "unverified"}
)
CONFIDENCE_LEVELS = frozenset({"high", "medium", "low", "unverified"})

_T = TypeVar("_T")


def _bounded_text(value: object, *, field_name: str, limit: int = 4096) -> str:
    """Return normalized non-empty text within a fixed boundary."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be empty.")
    normalized = value.strip()
    if len(normalized) > limit:
        raise ValueError(f"{field_name} exceeds its bounded length.")
    return normalized


def _string_tuple(
    value: object,
    *,
    field_name: str,
    item_limit: int = 512,
) -> tuple[str, ...]:
    """Validate and detach one ordered unique string sequence."""
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"{field_name} must be a sequence.")
    normalized = tuple(
        _bounded_text(item, field_name=f"{field_name} item", limit=item_limit)
        for item in value
    )
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{field_name} must not contain duplicates.")
    return normalized


def _typed_tuple(value: object, *, field_name: str, item_type: type[_T]) -> tuple[_T, ...]:
    """Validate and detach one typed object sequence."""
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"{field_name} must be a sequence.")
    normalized = tuple(value)
    if any(not isinstance(item, item_type) for item in normalized):
        raise TypeError(f"{field_name} contains an invalid item type.")
    return normalized


def _score(value: object, *, field_name: str) -> float:
    """Return one finite inclusive unit-interval score."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{field_name} must be numeric.")
    normalized = float(value)
    if not math.isfinite(normalized) or not 0.0 <= normalized <= 1.0:
        raise ValueError(f"{field_name} must be finite and between 0 and 1.")
    return normalized


def _threshold_mapping(value: object) -> Mapping[str, float]:
    """Return an immutable, deterministically ordered threshold mapping."""
    if not isinstance(value, Mapping):
        raise TypeError("thresholds must be a mapping.")
    normalized: dict[str, float] = {}
    for key, raw_score in value.items():
        normalized_key = _bounded_text(key, field_name="threshold name", limit=128)
        if normalized_key in normalized:
            raise ValueError("threshold names must be unique after normalization.")
        normalized[normalized_key] = _score(
            raw_score,
            field_name=f"threshold {normalized_key}",
        )
    return MappingProxyType(dict(sorted(normalized.items())))


def _require_mapping(value: object, *, field_name: str) -> Mapping[str, Any]:
    """Require a mapping before a JSON restoration boundary."""
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be an object.")
    return value


def _required(value: Mapping[str, Any], *, field_name: str) -> Any:
    """Return one required JSON field with a stable validation error."""
    if field_name not in value:
        raise ValueError(f"{field_name} is required.")
    return value[field_name]


def _restore_sequence(
    value: object,
    *,
    field_name: str,
    item_type: type[_T],
) -> tuple[_T, ...]:
    """Restore a strict sequence of contract objects from JSON mappings."""
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field_name} must be an array.")
    restored: list[_T] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError(f"{field_name} entries must be objects.")
        from_dict = getattr(item_type, "from_dict")
        restored.append(from_dict(item))
    return tuple(restored)


def stable_paragraph_id(
    *,
    section_id: str,
    text: str,
    claim_ids: Sequence[str] = (),
) -> str:
    """Return a deterministic ID derived from section, text, and claims."""
    normalized_section = _bounded_text(
        section_id,
        field_name="section_id",
        limit=256,
    ).casefold()
    normalized_text = " ".join(
        _bounded_text(text, field_name="paragraph text", limit=20_000).split()
    )
    normalized_claims = tuple(sorted(_string_tuple(claim_ids, field_name="claim_ids")))
    encoded = json.dumps(
        [normalized_section, normalized_text, normalized_claims],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"para_{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:20]}"


@dataclass(frozen=True, kw_only=True)
class SummaryParagraph:
    """One structured report paragraph linked to claims and evidence."""

    section_id: str
    paragraph_type: str
    text: str
    claim_ids: tuple[str, ...] = ()
    citation_ids: tuple[str, ...] = ()
    paragraph_id: str = ""

    def __post_init__(self) -> None:
        """Validate content and enforce its deterministic paragraph ID."""
        object.__setattr__(
            self,
            "section_id",
            _bounded_text(self.section_id, field_name="section_id", limit=256),
        )
        object.__setattr__(
            self,
            "paragraph_type",
            _bounded_text(
                self.paragraph_type,
                field_name="paragraph_type",
                limit=32,
            ),
        )
        if self.paragraph_type not in PARAGRAPH_TYPES:
            raise ValueError("paragraph_type is unsupported.")
        object.__setattr__(
            self,
            "text",
            _bounded_text(self.text, field_name="paragraph text", limit=20_000),
        )
        object.__setattr__(
            self,
            "claim_ids",
            _string_tuple(self.claim_ids, field_name="claim_ids"),
        )
        object.__setattr__(
            self,
            "citation_ids",
            _string_tuple(self.citation_ids, field_name="citation_ids"),
        )
        expected_id = stable_paragraph_id(
            section_id=self.section_id,
            text=self.text,
            claim_ids=self.claim_ids,
        )
        if self.paragraph_id:
            supplied_id = _bounded_text(
                self.paragraph_id,
                field_name="paragraph_id",
                limit=128,
            )
            if supplied_id != expected_id:
                raise ValueError("paragraph_id does not match the paragraph content.")
        object.__setattr__(self, "paragraph_id", expected_id)

    def as_dict(self) -> dict[str, Any]:
        """Return a detached JSON-compatible paragraph."""
        return {
            "paragraph_id": self.paragraph_id,
            "section_id": self.section_id,
            "paragraph_type": self.paragraph_type,
            "text": self.text,
            "claim_ids": list(self.claim_ids),
            "citation_ids": list(self.citation_ids),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SummaryParagraph:
        """Restore one structured paragraph from JSON."""
        raw = dict(_require_mapping(value, field_name="Summary paragraph"))
        return cls(
            paragraph_id=raw.get("paragraph_id", ""),
            section_id=raw.get("section_id", ""),
            paragraph_type=raw.get("paragraph_type", ""),
            text=raw.get("text", ""),
            claim_ids=raw.get("claim_ids", ()),
            citation_ids=raw.get("citation_ids", ()),
        )


@dataclass(frozen=True, kw_only=True)
class StructuredSummaryDocument:
    """Versioned structured summary for one research task."""

    task_id: str
    paragraphs: tuple[SummaryParagraph, ...] = ()
    claim_ids: tuple[str, ...] = ()
    schema_version: int = SUMMARY_DOCUMENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        """Validate version, references, and paragraph identity uniqueness."""
        if self.schema_version != SUMMARY_DOCUMENT_SCHEMA_VERSION:
            raise ValueError("Structured summary schema version is unsupported.")
        object.__setattr__(
            self,
            "task_id",
            _bounded_text(self.task_id, field_name="task_id", limit=512),
        )
        object.__setattr__(
            self,
            "paragraphs",
            _typed_tuple(
                self.paragraphs,
                field_name="paragraphs",
                item_type=SummaryParagraph,
            ),
        )
        object.__setattr__(
            self,
            "claim_ids",
            _string_tuple(self.claim_ids, field_name="claim_ids"),
        )
        paragraph_ids = tuple(item.paragraph_id for item in self.paragraphs)
        if len(paragraph_ids) != len(set(paragraph_ids)):
            raise ValueError("paragraph IDs must be unique.")
        known_claims = set(self.claim_ids)
        if any(
            claim_id not in known_claims
            for paragraph in self.paragraphs
            for claim_id in paragraph.claim_ids
        ):
            raise ValueError("Paragraph references an unknown document claim ID.")

    def as_dict(self) -> dict[str, Any]:
        """Return a detached versioned JSON document."""
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "paragraphs": [item.as_dict() for item in self.paragraphs],
            "claim_ids": list(self.claim_ids),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> StructuredSummaryDocument:
        """Restore one structured summary document from JSON."""
        raw = _require_mapping(value, field_name="Structured summary")
        return cls(
            schema_version=_required(raw, field_name="schema_version"),
            task_id=raw.get("task_id", ""),
            paragraphs=_restore_sequence(
                raw.get("paragraphs", ()),
                field_name="paragraphs",
                item_type=SummaryParagraph,
            ),
            claim_ids=raw.get("claim_ids", ()),
        )


@dataclass(frozen=True, kw_only=True)
class ClaimSupportAssessment:
    """Three-dimensional evidence support assessment for one claim."""

    claim_id: str
    semantic_score: float
    factual_score: float
    citation_score: float
    support_confidence: float
    verdict: str
    supporting_evidence_ids: tuple[str, ...] = ()
    conflicting_evidence_ids: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate score boundaries, verdict, references, and reasons."""
        object.__setattr__(
            self,
            "claim_id",
            _bounded_text(self.claim_id, field_name="claim_id", limit=512),
        )
        for field_name in (
            "semantic_score",
            "factual_score",
            "citation_score",
            "support_confidence",
        ):
            object.__setattr__(
                self,
                field_name,
                _score(getattr(self, field_name), field_name=field_name),
            )
        object.__setattr__(
            self,
            "verdict",
            _bounded_text(self.verdict, field_name="verdict", limit=32),
        )
        if self.verdict not in CLAIM_VERDICTS:
            raise ValueError("Claim assessment verdict is unsupported.")
        for field_name in (
            "supporting_evidence_ids",
            "conflicting_evidence_ids",
            "reasons",
        ):
            object.__setattr__(
                self,
                field_name,
                _string_tuple(
                    getattr(self, field_name),
                    field_name=field_name,
                    item_limit=4096 if field_name == "reasons" else 512,
                ),
            )

    def as_dict(self) -> dict[str, Any]:
        """Return a detached JSON-compatible claim assessment."""
        return {
            "claim_id": self.claim_id,
            "semantic_score": self.semantic_score,
            "factual_score": self.factual_score,
            "citation_score": self.citation_score,
            "support_confidence": self.support_confidence,
            "verdict": self.verdict,
            "supporting_evidence_ids": list(self.supporting_evidence_ids),
            "conflicting_evidence_ids": list(self.conflicting_evidence_ids),
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ClaimSupportAssessment:
        """Restore one claim support assessment from JSON."""
        raw = _require_mapping(value, field_name="Claim assessment")
        return cls(
            claim_id=raw.get("claim_id", ""),
            semantic_score=_required(raw, field_name="semantic_score"),
            factual_score=_required(raw, field_name="factual_score"),
            citation_score=_required(raw, field_name="citation_score"),
            support_confidence=_required(raw, field_name="support_confidence"),
            verdict=raw.get("verdict", ""),
            supporting_evidence_ids=raw.get("supporting_evidence_ids", ()),
            conflicting_evidence_ids=raw.get("conflicting_evidence_ids", ()),
            reasons=raw.get("reasons", ()),
        )


@dataclass(frozen=True, kw_only=True)
class ParagraphQualityAssessment:
    """Three-dimensional evidence quality assessment for one paragraph."""

    paragraph_id: str
    semantic_score: float
    factual_score: float
    citation_score: float
    support_confidence: float
    level: str
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate paragraph reference, scores, level, and diagnostics."""
        object.__setattr__(
            self,
            "paragraph_id",
            _bounded_text(self.paragraph_id, field_name="paragraph_id", limit=128),
        )
        for field_name in (
            "semantic_score",
            "factual_score",
            "citation_score",
            "support_confidence",
        ):
            object.__setattr__(
                self,
                field_name,
                _score(getattr(self, field_name), field_name=field_name),
            )
        object.__setattr__(
            self,
            "level",
            _bounded_text(self.level, field_name="level", limit=32),
        )
        if self.level not in CONFIDENCE_LEVELS:
            raise ValueError("Paragraph assessment level is unsupported.")
        for field_name in ("blockers", "warnings"):
            object.__setattr__(
                self,
                field_name,
                _string_tuple(
                    getattr(self, field_name),
                    field_name=field_name,
                    item_limit=4096,
                ),
            )

    def as_dict(self) -> dict[str, Any]:
        """Return a detached JSON-compatible paragraph assessment."""
        return {
            "paragraph_id": self.paragraph_id,
            "semantic_score": self.semantic_score,
            "factual_score": self.factual_score,
            "citation_score": self.citation_score,
            "support_confidence": self.support_confidence,
            "level": self.level,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ParagraphQualityAssessment:
        """Restore one paragraph quality assessment from JSON."""
        raw = _require_mapping(value, field_name="Paragraph assessment")
        return cls(
            paragraph_id=raw.get("paragraph_id", ""),
            semantic_score=_required(raw, field_name="semantic_score"),
            factual_score=_required(raw, field_name="factual_score"),
            citation_score=_required(raw, field_name="citation_score"),
            support_confidence=_required(raw, field_name="support_confidence"),
            level=raw.get("level", ""),
            blockers=raw.get("blockers", ()),
            warnings=raw.get("warnings", ()),
        )


@dataclass(frozen=True, kw_only=True)
class SummaryQualityAssessment:
    """Versioned aggregate assessment for one structured summary."""

    passed: bool
    overall_score: float
    thresholds: Mapping[str, float]
    verifier: str
    verifier_version: str
    prompt_version: str
    paragraph_assessments: tuple[ParagraphQualityAssessment, ...] = ()
    claim_assessments: tuple[ClaimSupportAssessment, ...] = ()
    schema_version: int = SUMMARY_QUALITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        """Validate version, aggregate fields, and unique assessment IDs."""
        if self.schema_version != SUMMARY_QUALITY_SCHEMA_VERSION:
            raise ValueError("Summary quality schema version is unsupported.")
        if not isinstance(self.passed, bool):
            raise TypeError("passed must be boolean.")
        object.__setattr__(
            self,
            "overall_score",
            _score(self.overall_score, field_name="overall_score"),
        )
        object.__setattr__(self, "thresholds", _threshold_mapping(self.thresholds))
        for field_name in ("verifier", "verifier_version", "prompt_version"):
            object.__setattr__(
                self,
                field_name,
                _bounded_text(
                    getattr(self, field_name),
                    field_name=field_name,
                    limit=256,
                ),
            )
        object.__setattr__(
            self,
            "paragraph_assessments",
            _typed_tuple(
                self.paragraph_assessments,
                field_name="paragraph_assessments",
                item_type=ParagraphQualityAssessment,
            ),
        )
        object.__setattr__(
            self,
            "claim_assessments",
            _typed_tuple(
                self.claim_assessments,
                field_name="claim_assessments",
                item_type=ClaimSupportAssessment,
            ),
        )
        paragraph_ids = tuple(
            item.paragraph_id for item in self.paragraph_assessments
        )
        claim_ids = tuple(item.claim_id for item in self.claim_assessments)
        if len(paragraph_ids) != len(set(paragraph_ids)):
            raise ValueError("Paragraph assessments must have unique IDs.")
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("Claim assessments must have unique IDs.")

    def as_dict(self) -> dict[str, Any]:
        """Return a detached versioned JSON assessment."""
        return {
            "schema_version": self.schema_version,
            "passed": self.passed,
            "overall_score": self.overall_score,
            "thresholds": dict(self.thresholds),
            "paragraph_assessments": [
                item.as_dict() for item in self.paragraph_assessments
            ],
            "claim_assessments": [
                item.as_dict() for item in self.claim_assessments
            ],
            "verifier": self.verifier,
            "verifier_version": self.verifier_version,
            "prompt_version": self.prompt_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SummaryQualityAssessment:
        """Restore one aggregate summary quality assessment from JSON."""
        raw = _require_mapping(value, field_name="Summary quality assessment")
        return cls(
            schema_version=_required(raw, field_name="schema_version"),
            passed=_required(raw, field_name="passed"),
            overall_score=_required(raw, field_name="overall_score"),
            thresholds=raw.get("thresholds", {}),
            paragraph_assessments=_restore_sequence(
                raw.get("paragraph_assessments", ()),
                field_name="paragraph_assessments",
                item_type=ParagraphQualityAssessment,
            ),
            claim_assessments=_restore_sequence(
                raw.get("claim_assessments", ()),
                field_name="claim_assessments",
                item_type=ClaimSupportAssessment,
            ),
            verifier=raw.get("verifier", ""),
            verifier_version=raw.get("verifier_version", ""),
            prompt_version=raw.get("prompt_version", ""),
        )


__all__ = [
    "CLAIM_VERDICTS",
    "CONFIDENCE_LEVELS",
    "PARAGRAPH_TYPES",
    "SUMMARY_DOCUMENT_SCHEMA_VERSION",
    "SUMMARY_QUALITY_SCHEMA_VERSION",
    "ClaimSupportAssessment",
    "ParagraphQualityAssessment",
    "StructuredSummaryDocument",
    "SummaryParagraph",
    "SummaryQualityAssessment",
    "stable_paragraph_id",
]
