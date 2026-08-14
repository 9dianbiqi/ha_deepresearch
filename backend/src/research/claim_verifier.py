"""Strict, injectable claim-support scoring boundaries."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .intelligence import ClaimRecord, EvidenceRecord
from .report_document import CLAIM_VERDICTS


class VerifierResponseError(ValueError):
    """Reject malformed or internally inconsistent verifier output."""


class VerifierUnavailableError(RuntimeError):
    """Signal that no factual verifier is available for this evaluation."""


class SemanticScorerUnavailableError(RuntimeError):
    """Signal that no semantic scorer is available for this evaluation."""


def _bounded_score(value: object, *, field_name: str) -> float:
    """Return a finite unit-interval score from an untrusted boundary."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise VerifierResponseError(f"{field_name} must be numeric.")
    score = float(value)
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        raise VerifierResponseError(f"{field_name} must be between 0 and 1.")
    return score


def _required_string(value: object, *, field_name: str, limit: int = 4096) -> str:
    """Return normalized non-empty bounded text."""
    if not isinstance(value, str) or not value.strip():
        raise VerifierResponseError(f"{field_name} must not be empty.")
    normalized = value.strip()
    if len(normalized) > limit:
        raise VerifierResponseError(f"{field_name} exceeds its length bound.")
    return normalized


def _string_tuple(value: object, *, field_name: str) -> tuple[str, ...]:
    """Restore a unique tuple of strings from a strict JSON array."""
    if not isinstance(value, (list, tuple)):
        raise VerifierResponseError(f"{field_name} must be an array.")
    normalized = tuple(
        _required_string(item, field_name=f"{field_name} item") for item in value
    )
    if len(normalized) != len(set(normalized)):
        raise VerifierResponseError(f"{field_name} must not contain duplicates.")
    return normalized


@dataclass(frozen=True, kw_only=True)
class SupportSpan:
    """One exact verifier-selected span from a known Evidence excerpt."""

    evidence_id: str
    exact_text: str

    def __post_init__(self) -> None:
        """Validate the evidence identity and bounded exact text."""
        object.__setattr__(
            self,
            "evidence_id",
            _required_string(self.evidence_id, field_name="span evidence_id", limit=512),
        )
        object.__setattr__(
            self,
            "exact_text",
            _required_string(self.exact_text, field_name="span exact_text", limit=12_000),
        )


@dataclass(frozen=True, kw_only=True)
class FactualVerification:
    """Strict structured result returned by a factual-support verifier."""

    verdict: str
    factual_score: float
    supporting_evidence_ids: tuple[str, ...] = ()
    conflicting_evidence_ids: tuple[str, ...] = ()
    support_spans: tuple[SupportSpan, ...] = ()
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate verdict, score, references, spans, and diagnostics."""
        verdict = _required_string(self.verdict, field_name="verdict", limit=32)
        if verdict not in CLAIM_VERDICTS:
            raise VerifierResponseError("verdict is unsupported.")
        object.__setattr__(self, "verdict", verdict)
        object.__setattr__(
            self,
            "factual_score",
            _bounded_score(self.factual_score, field_name="factual_score"),
        )
        for field_name in (
            "supporting_evidence_ids",
            "conflicting_evidence_ids",
            "reasons",
        ):
            object.__setattr__(
                self,
                field_name,
                _string_tuple(getattr(self, field_name), field_name=field_name),
            )
        if not isinstance(self.support_spans, (list, tuple)) or any(
            not isinstance(item, SupportSpan) for item in self.support_spans
        ):
            raise VerifierResponseError("support_spans contains an invalid item.")
        object.__setattr__(self, "support_spans", tuple(self.support_spans))
        if self.verdict == "unverified" and self.factual_score != 0.0:
            raise VerifierResponseError("unverified output must have factual_score 0.")
        if self.verdict in {"supported", "partial"} and not self.supporting_evidence_ids:
            raise VerifierResponseError("supported output requires supporting evidence.")
        if self.verdict == "contradicted" and not self.conflicting_evidence_ids:
            raise VerifierResponseError("contradicted output requires conflicting evidence.")
        if set(self.supporting_evidence_ids) & set(self.conflicting_evidence_ids):
            raise VerifierResponseError("Evidence cannot be both supporting and conflicting.")

    @classmethod
    def unverified(cls, reason: str) -> FactualVerification:
        """Return a safe result when verification cannot be completed."""
        return cls(
            verdict="unverified",
            factual_score=0.0,
            reasons=(reason,),
        )


def validate_factual_verification(
    result: FactualVerification,
    *,
    claim: ClaimRecord,
    evidence: Sequence[EvidenceRecord],
) -> None:
    """Require claim-bound IDs and exact excerpts for any verifier implementation."""
    evidence_by_id = {item.evidence_id: item for item in evidence}
    supporting = set(result.supporting_evidence_ids)
    conflicting = set(result.conflicting_evidence_ids)
    if not supporting.issubset(set(claim.evidence_ids)):
        raise VerifierResponseError("Verifier returned unbound supporting evidence.")
    if not conflicting.issubset(set(claim.conflicting_evidence_ids)):
        raise VerifierResponseError("Verifier returned unbound conflicting evidence.")
    if any(item not in evidence_by_id for item in supporting | conflicting):
        raise VerifierResponseError("Verifier returned unknown evidence.")
    span_ids: set[str] = set()
    for span in result.support_spans:
        record = evidence_by_id.get(span.evidence_id)
        if record is None or span.evidence_id not in supporting | conflicting:
            raise VerifierResponseError("Support span references invalid evidence.")
        if span.exact_text not in record.excerpt:
            raise VerifierResponseError("Support span is not exact Evidence text.")
        span_ids.add(span.evidence_id)
    required_span_ids: set[str] = set()
    if result.verdict == "supported":
        required_span_ids = supporting
    elif result.verdict == "partial":
        required_span_ids = supporting | conflicting
    elif result.verdict == "contradicted":
        required_span_ids = conflicting
    if required_span_ids - span_ids:
        raise VerifierResponseError("Every verifier-cited Evidence item requires an exact span.")


class SemanticSupportScorer(Protocol):
    """Score semantic relevance between one claim and its bound Evidence."""

    name: str
    version: str

    def score(
        self,
        claim: ClaimRecord,
        evidence: tuple[EvidenceRecord, ...],
    ) -> float:
        """Return a finite score between zero and one."""
        ...


class FactualSupportVerifier(Protocol):
    """Verify factual support with structured, source-addressable output."""

    name: str
    version: str
    prompt_version: str

    def verify(
        self,
        claim: ClaimRecord,
        evidence: tuple[EvidenceRecord, ...],
    ) -> FactualVerification:
        """Return one strict verification result."""
        ...


VerifierInvoker = Callable[[ClaimRecord, tuple[EvidenceRecord, ...]], str | Mapping[str, Any]]


class StructuredFactualSupportVerifier:
    """Validate JSON returned by an injected deterministic verifier invocation."""

    def __init__(
        self,
        invoker: VerifierInvoker,
        *,
        name: str,
        version: str,
        prompt_version: str,
    ) -> None:
        """Bind an invocation without depending on a concrete model client."""
        if not callable(invoker):
            raise TypeError("Verifier invoker must be callable.")
        self._invoker = invoker
        self.name = _required_string(name, field_name="verifier name", limit=256)
        self.version = _required_string(version, field_name="verifier version", limit=256)
        self.prompt_version = _required_string(
            prompt_version,
            field_name="prompt version",
            limit=256,
        )

    def verify(
        self,
        claim: ClaimRecord,
        evidence: tuple[EvidenceRecord, ...],
    ) -> FactualVerification:
        """Invoke and validate one strict structured response."""
        raw = self._invoker(claim, evidence)
        payload = self._parse_payload(raw)
        required_fields = {
            "verdict",
            "factual_score",
            "supporting_evidence_ids",
            "conflicting_evidence_ids",
            "support_spans",
            "reasons",
        }
        if set(payload) != required_fields:
            raise VerifierResponseError("Verifier response has missing or unknown fields.")
        raw_spans = payload["support_spans"]
        if not isinstance(raw_spans, (list, tuple)):
            raise VerifierResponseError("support_spans must be an array.")
        spans: list[SupportSpan] = []
        for raw_span in raw_spans:
            if not isinstance(raw_span, Mapping) or set(raw_span) != {
                "evidence_id",
                "exact_text",
            }:
                raise VerifierResponseError("support_spans entries are invalid.")
            spans.append(
                SupportSpan(
                    evidence_id=raw_span["evidence_id"],
                    exact_text=raw_span["exact_text"],
                )
            )
        result = FactualVerification(
            verdict=payload["verdict"],
            factual_score=payload["factual_score"],
            supporting_evidence_ids=payload["supporting_evidence_ids"],
            conflicting_evidence_ids=payload["conflicting_evidence_ids"],
            support_spans=tuple(spans),
            reasons=payload["reasons"],
        )
        validate_factual_verification(result, claim=claim, evidence=evidence)
        return result

    @staticmethod
    def _parse_payload(raw: str | Mapping[str, Any]) -> Mapping[str, Any]:
        """Parse a JSON object without accepting arrays or scalar values."""
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise VerifierResponseError("Verifier returned invalid JSON.") from exc
        elif isinstance(raw, Mapping):
            parsed = dict(raw)
        else:
            raise VerifierResponseError("Verifier response must be JSON text or an object.")
        if not isinstance(parsed, Mapping):
            raise VerifierResponseError("Verifier response must be a JSON object.")
        return parsed



class UnavailableSemanticSupportScorer:
    """Fail closed when semantic scoring has not been configured."""

    name = "unavailable"
    version = "0"

    def score(
        self,
        claim: ClaimRecord,
        evidence: tuple[EvidenceRecord, ...],
    ) -> float:
        """Signal the unavailable dependency without inventing a score."""
        del claim, evidence
        raise SemanticScorerUnavailableError("Semantic scorer is unavailable.")


class UnavailableFactualSupportVerifier:
    """Return an explicit unverified result when no verifier is configured."""

    name = "unavailable"
    version = "0"
    prompt_version = "0"

    def verify(
        self,
        claim: ClaimRecord,
        evidence: tuple[EvidenceRecord, ...],
    ) -> FactualVerification:
        """Return unverified rather than optimistic fallback output."""
        del claim, evidence
        return FactualVerification.unverified("factual_verifier_unavailable")


__all__ = [
    "FactualSupportVerifier",
    "FactualVerification",
    "SemanticScorerUnavailableError",
    "SemanticSupportScorer",
    "StructuredFactualSupportVerifier",
    "SupportSpan",
    "UnavailableFactualSupportVerifier",
    "UnavailableSemanticSupportScorer",
    "VerifierResponseError",
    "VerifierUnavailableError",
    "validate_factual_verification",
]
