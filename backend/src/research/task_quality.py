"""Action-oriented quality control for ordinary research task summaries."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, cast

from .intelligence import EvidenceRecord

_CITATION_RE = re.compile(r"\[([A-Za-z0-9][A-Za-z0-9._:-]{0,127})\]")
_NUMBER_RE = re.compile(r"(?:\d+(?:\.\d+)?%?|\d{4}年?|\$\s*\d+)")
_HIGH_RISK_TERMS = (
    "首次",
    "最高",
    "最低",
    "唯一",
    "性能",
    "成本",
    "价格",
    "安全",
    "法律",
    "政策",
    "first",
    "highest",
    "only",
    "performance",
    "cost",
    "price",
    "security",
    "legal",
    "policy",
)


class QualityMode(str, Enum):
    """Validation strength and terminal failure policy."""

    BASIC = "basic"
    EVIDENCE = "evidence"
    STRICT = "strict"


class QualityAction(str, Enum):
    """One bounded next action selected from an explainable assessment."""

    ACCEPT = "accept"
    REPAIR_CITATIONS = "repair_citations"
    REGENERATE_SUMMARY = "regenerate_summary"
    RETRIEVE_GAPS = "retrieve_gaps"
    FLAG_CONFLICT = "flag_conflict"
    DEGRADE = "degrade"
    BLOCK = "block"


class ClaimVerdict(str, Enum):
    """Discrete claim-evidence conclusion returned by a judge."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    CONFLICTING = "conflicting"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, kw_only=True)
class TaskEvidence:
    """One bounded piece of task evidence with freshness provenance."""

    evidence_id: str
    title: str
    url: str
    excerpt: str
    provider: str
    original_query: str
    retrieved_at: str
    cache_age_seconds: float = 0.0
    content_hash: str = ""
    source_type: str = "web"
    evidence_level: str = "unknown"
    locator: Mapping[str, object] = field(default_factory=dict)
    # Canonical records are already bounded once at ID creation time.  Legacy
    # search-result projections keep their historical prompt/judge limits.
    canonical: bool = False
    source_provenance: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate bounds and derive a stable content hash."""
        for name in (
            "evidence_id",
            "title",
            "url",
            "provider",
            "original_query",
            "retrieved_at",
            "source_type",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty text.")
        if not isinstance(self.excerpt, str):
            raise TypeError("Evidence excerpt must be text.")
        if not isinstance(self.canonical, bool):
            raise TypeError("canonical must be boolean.")
        if self.canonical and len(self.excerpt) > 2000:
            raise ValueError("Canonical evidence excerpt exceeds the judge bound.")
        if not self.canonical:
            object.__setattr__(self, "excerpt", self.excerpt[:4000])
        if not isinstance(self.locator, Mapping):
            raise TypeError("locator must be a mapping.")
        if not isinstance(self.source_provenance, Mapping):
            raise TypeError("source_provenance must be a mapping.")
        object.__setattr__(self, "locator", dict(self.locator))
        object.__setattr__(self, "source_provenance", dict(self.source_provenance))
        if (
            isinstance(self.cache_age_seconds, bool)
            or not isinstance(self.cache_age_seconds, (int, float))
            or not math.isfinite(float(self.cache_age_seconds))
            or self.cache_age_seconds < 0
        ):
            raise ValueError("cache_age_seconds must be finite and non-negative.")
        if not self.content_hash:
            digest = hashlib.sha256(self.excerpt.encode("utf-8")).hexdigest()
            object.__setattr__(self, "content_hash", digest)

    @classmethod
    def from_record(
        cls,
        record: EvidenceRecord,
        query: str,
        *,
        canonical: bool = True,
    ) -> TaskEvidence:
        """Project one canonical record without changing its ID or excerpt."""
        if not isinstance(record, EvidenceRecord):
            raise TypeError("TaskEvidence.from_record requires an EvidenceRecord.")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Task evidence query must not be empty.")
        source = record.source
        provenance = source.as_dict()
        return cls(
            evidence_id=record.evidence_id,
            title=record.title,
            url=record.locator.url,
            excerpt=record.excerpt,
            provider=source.provider_id,
            original_query=query,
            retrieved_at=source.captured_at,
            content_hash=source.content_hash,
            source_type=source.source_kind,
            evidence_level=record.evidence_level,
            locator=record.locator.as_dict(),
            canonical=canonical,
            source_provenance=provenance,
        )


@dataclass(frozen=True, kw_only=True)
class AtomicTaskClaim:
    """A summary claim classified without another model call."""

    claim_id: str
    text: str
    claim_type: str = "general"
    importance: str = "supporting"
    risk_level: str = "low"
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class TaskSummaryDocument:
    """Structured projection of one ordinary task summary."""

    markdown: str
    claims: tuple[AtomicTaskClaim, ...]
    citation_ids: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class SupportSpan:
    """Exact supporting text copied from one evidence excerpt."""

    evidence_id: str
    exact_text: str


@dataclass(frozen=True, kw_only=True)
class ClaimJudgment:
    """Validated discrete output from the claim-evidence judge."""

    claim_id: str
    verdict: ClaimVerdict
    supporting_evidence_ids: tuple[str, ...] = ()
    conflicting_evidence_ids: tuple[str, ...] = ()
    support_spans: tuple[SupportSpan, ...] = ()
    reason_code: str = ""


@dataclass(frozen=True, kw_only=True)
class RetrievalGap:
    """A concrete missing-evidence request used to refine a query."""

    claim_id: str | None
    gap_type: str
    topic: str
    preferred_source_types: tuple[str, ...] = ()
    time_constraint: str | None = None


@dataclass(frozen=True, kw_only=True)
class TaskQualityBudget:
    """Hard per-task judge and context limits."""

    max_claims: int = 3
    max_evidence_per_claim: int = 3
    max_evidence_chars: int = 2000
    max_batch_tokens: int = 6000
    max_judge_calls: int = 1
    judge_calls_used: int = 0
    retrieval_attempts_used: int = 0
    max_retrieval_attempts: int = 2

    @property
    def judge_exhausted(self) -> bool:
        """Return whether another judge call would exceed the budget."""
        return self.judge_calls_used >= self.max_judge_calls

    @property
    def retrieval_exhausted(self) -> bool:
        """Return whether another gap retrieval would exceed the budget."""
        return self.retrieval_attempts_used >= self.max_retrieval_attempts


@dataclass(frozen=True, kw_only=True)
class TaskQualityInput:
    """The complete immutable input to the quality-controller interface."""

    task_id: int
    task_intent: str
    current_query: str
    document: TaskSummaryDocument
    evidence: tuple[TaskEvidence, ...]
    budget: TaskQualityBudget = field(default_factory=TaskQualityBudget)
    mode: QualityMode = QualityMode.EVIDENCE


@dataclass(frozen=True, kw_only=True)
class TaskQualityAssessment:
    """Independent dimensions; none is an alias for another."""

    retrieval_relevance: float
    claim_support: float
    citation_integrity: float
    judgments: tuple[ClaimJudgment, ...] = ()
    checked_claim_ids: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class TaskQualityResult:
    """One assessment plus the single recommended runtime action."""

    action: QualityAction
    reason_codes: tuple[str, ...]
    assessment: TaskQualityAssessment
    retrieval_gaps: tuple[RetrievalGap, ...] = ()
    repair_instructions: tuple[str, ...] = ()

    def as_event_payload(self) -> dict[str, object]:
        """Return bounded metadata suitable for events and snapshots."""
        return {
            "action": self.action.value,
            "reason_codes": list(self.reason_codes),
            "retrieval_relevance": self.assessment.retrieval_relevance,
            "claim_support": self.assessment.claim_support,
            "citation_integrity": self.assessment.citation_integrity,
            "checked_claim_ids": list(self.assessment.checked_claim_ids),
            "retrieval_gaps": [
                {
                    "claim_id": gap.claim_id,
                    "gap_type": gap.gap_type,
                    "topic": gap.topic[:300],
                    "preferred_source_types": list(gap.preferred_source_types),
                    "time_constraint": gap.time_constraint,
                }
                for gap in self.retrieval_gaps[:3]
            ],
        }


class RetrievalRelevanceRanker(Protocol):
    """Replaceable adapter for query-to-evidence relevance."""

    def score(self, query: str, intent: str, evidence: Sequence[TaskEvidence]) -> float:
        """Return a finite score in the unit interval."""


class ClaimEvidenceJudge(Protocol):
    """Replaceable batch adapter for bounded claim-evidence reasoning."""

    def judge(
        self,
        claims: Sequence[AtomicTaskClaim],
        evidence: Sequence[TaskEvidence],
        budget: TaskQualityBudget,
    ) -> tuple[ClaimJudgment, ...]:
        """Return one discrete judgment per selected claim."""


class LexicalRelevanceRanker:
    """Deterministic local baseline; production may inject a reranker adapter."""

    def score(self, query: str, intent: str, evidence: Sequence[TaskEvidence]) -> float:
        """Measure bounded lexical overlap, including CJK bigrams."""
        expected = _tokens(f"{query} {intent}")
        if not expected or not evidence:
            return 0.0
        observed = _tokens(" ".join(f"{item.title} {item.excerpt}" for item in evidence))
        return min(1.0, len(expected & observed) / max(1, min(len(expected), 12)))


class UnavailableClaimEvidenceJudge:
    """Fail-closed local adapter used when no external judge is configured."""

    def judge(
        self,
        claims: Sequence[AtomicTaskClaim],
        evidence: Sequence[TaskEvidence],
        budget: TaskQualityBudget,
    ) -> tuple[ClaimJudgment, ...]:
        """Return explicit uncertainty rather than inventing support."""
        del evidence, budget
        return tuple(
            ClaimJudgment(
                claim_id=claim.claim_id,
                verdict=ClaimVerdict.UNCERTAIN,
                reason_code="judge_unavailable",
            )
            for claim in claims
        )


class JsonBatchClaimEvidenceJudge:
    """Strict JSON adapter for one bounded batch LLM invocation per task."""

    def __init__(self, invoker: Callable[[str], str]) -> None:
        """Bind a run-scoped external invoker."""
        self._invoker = invoker

    def judge(
        self,
        claims: Sequence[AtomicTaskClaim],
        evidence: Sequence[TaskEvidence],
        budget: TaskQualityBudget,
    ) -> tuple[ClaimJudgment, ...]:
        """Invoke once and validate discrete verdicts and exact support spans."""
        payload = {
            "claims": [
                {
                    "claim_id": item.claim_id,
                    "text": item.text,
                    "claim_type": item.claim_type,
                    "importance": item.importance,
                    "risk_level": item.risk_level,
                    "candidate_evidence_ids": list(item.evidence_ids),
                }
                for item in claims[: budget.max_claims]
            ],
            "evidence": [
                {
                    "evidence_id": item.evidence_id,
                    "source_type": item.source_type,
                    "title": item.title,
                    "excerpt": (
                        item.excerpt
                        if item.canonical
                        else item.excerpt[: budget.max_evidence_chars]
                    ),
                    "evidence_level": item.evidence_level,
                    "locator": dict(item.locator),
                    "source_provenance": dict(item.source_provenance),
                }
                for item in evidence
            ],
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) // 4 > budget.max_batch_tokens:
            raise ValueError("Claim-evidence judge batch exceeds its token budget.")
        prompt = (
            "Judge each claim only against the supplied evidence. Return exactly one "
            "JSON object with a judgments array. Each item must contain claim_id, "
            "verdict (supported|unsupported|conflicting|uncertain), "
            "supporting_evidence_ids, conflicting_evidence_ids, support_spans "
            "([{evidence_id, exact_text}]), and reason_code. Copy exact_text verbatim "
            "from an evidence excerpt. Do not return numeric scores.\n"
            "Metadata is search discovery context, not verified page content. "
            "For strong factual claims backed only by metadata, return uncertain.\n"
            + encoded
        )
        raw = self._invoker(prompt)
        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("Judge returned malformed JSON.") from exc
        if not isinstance(decoded, Mapping) or not isinstance(decoded.get("judgments"), list):
            raise ValueError("Judge response must contain a judgments array.")
        return tuple(self._parse_item(item) for item in decoded["judgments"])

    @staticmethod
    def _parse_item(value: object) -> ClaimJudgment:
        if not isinstance(value, Mapping):
            raise ValueError("Judge judgment must be an object.")
        try:
            verdict = ClaimVerdict(str(value.get("verdict", "")))
        except ValueError as exc:
            raise ValueError("Judge returned an unsupported verdict.") from exc
        raw_spans = value.get("support_spans", [])
        if not isinstance(raw_spans, list):
            raise ValueError("Judge support_spans must be a list.")
        spans: list[SupportSpan] = []
        for raw_span in raw_spans:
            if not isinstance(raw_span, Mapping):
                raise ValueError("Judge support span must be an object.")
            evidence_id = raw_span.get("evidence_id")
            exact_text = raw_span.get("exact_text")
            if not isinstance(evidence_id, str) or not isinstance(exact_text, str):
                raise ValueError("Judge support span fields must be text.")
            spans.append(SupportSpan(evidence_id=evidence_id, exact_text=exact_text))
        return ClaimJudgment(
            claim_id=_required_text(value.get("claim_id"), "claim_id"),
            verdict=verdict,
            supporting_evidence_ids=_text_tuple(value.get("supporting_evidence_ids")),
            conflicting_evidence_ids=_text_tuple(value.get("conflicting_evidence_ids")),
            support_spans=tuple(spans),
            reason_code=_required_text(value.get("reason_code"), "reason_code"),
        )


class TaskQualityController:
    """Deep module that hides checks, selection, batching, and action priority."""

    def __init__(
        self,
        *,
        ranker: RetrievalRelevanceRanker | None = None,
        judge: ClaimEvidenceJudge | None = None,
        relevance_threshold: float = 0.25,
    ) -> None:
        """Bind replaceable external adapters at the quality seam."""
        self._ranker = ranker or LexicalRelevanceRanker()
        self._judge = judge or UnavailableClaimEvidenceJudge()
        self._relevance_threshold = relevance_threshold

    def evaluate(self, request: TaskQualityInput) -> TaskQualityResult:
        """Evaluate once and select one bounded, reason-coded action."""
        relevance = _unit_score(
            self._ranker.score(
                request.current_query,
                request.task_intent,
                request.evidence,
            )
        )
        citation_score, citation_reasons = _citation_integrity(
            request.document,
            request.evidence,
        )
        content_reasons = _content_reasons(request.document.markdown)
        selected = _select_claims(request.document.claims, request.budget.max_claims)
        judgments: tuple[ClaimJudgment, ...] = ()
        should_judge = (
            request.mode is not QualityMode.BASIC
            and bool(selected)
            and not request.budget.judge_exhausted
            and not content_reasons
            and not citation_reasons
            and relevance >= self._relevance_threshold
        )
        if should_judge:
            evidence = _bounded_evidence(selected, request.evidence, request.budget)
            try:
                judgments = self._judge.judge(selected, evidence, request.budget)
                _validate_judgments(judgments, selected, evidence)
            except (TimeoutError, RuntimeError, TypeError, ValueError):
                judgments = tuple(
                    ClaimJudgment(
                        claim_id=claim.claim_id,
                        verdict=ClaimVerdict.UNCERTAIN,
                        reason_code="judge_invalid_or_unavailable",
                    )
                    for claim in selected
                )
        claim_support = _claim_support_score(judgments)
        reasons = tuple(
            dict.fromkeys(
                (
                    *content_reasons,
                    *citation_reasons,
                    *(item.reason_code for item in judgments if item.reason_code),
                )
            )
        )
        assessment = TaskQualityAssessment(
            retrieval_relevance=relevance,
            claim_support=claim_support,
            citation_integrity=citation_score,
            judgments=judgments,
            checked_claim_ids=tuple(item.claim_id for item in selected),
            reason_codes=reasons,
        )
        return self._decide(request, assessment, content_reasons, citation_reasons)

    def _decide(
        self,
        request: TaskQualityInput,
        assessment: TaskQualityAssessment,
        content_reasons: tuple[str, ...],
        citation_reasons: tuple[str, ...],
    ) -> TaskQualityResult:
        judgments = assessment.judgments
        conflicts = tuple(
            item for item in judgments if item.verdict is ClaimVerdict.CONFLICTING
        )
        unsupported = tuple(
            item for item in judgments if item.verdict is ClaimVerdict.UNSUPPORTED
        )
        uncertain = tuple(
            item for item in judgments if item.verdict is ClaimVerdict.UNCERTAIN
        )
        if content_reasons:
            return _result(
                QualityAction.REGENERATE_SUMMARY,
                assessment,
                content_reasons,
                repair=("Rewrite the task summary with 3-5 complete factual findings.",),
            )
        if assessment.retrieval_relevance < self._relevance_threshold:
            gaps = (
                RetrievalGap(
                    claim_id=None,
                    gap_type="retrieval_not_relevant",
                    topic=request.task_intent,
                    preferred_source_types=("primary_source",),
                ),
            )
            return self._retrieve_or_finish(request, assessment, gaps)
        if citation_reasons:
            return _result(
                QualityAction.REPAIR_CITATIONS,
                assessment,
                citation_reasons,
                repair=(
                    "Bind every factual finding to existing evidence IDs; do not invent IDs.",
                ),
            )
        if request.mode is QualityMode.BASIC:
            return _result(QualityAction.ACCEPT, assessment, ())
        if conflicts:
            return _result(
                QualityAction.FLAG_CONFLICT,
                assessment,
                tuple(item.reason_code or "evidence_conflict" for item in conflicts),
                repair=("Preserve and explain the unresolved source conflict.",),
            )
        if unsupported:
            return _result(
                QualityAction.REGENERATE_SUMMARY,
                assessment,
                tuple(item.reason_code or "claim_unsupported" for item in unsupported),
                repair=("Remove or rewrite claims that the supplied evidence does not support.",),
            )
        if uncertain or not judgments:
            retrieval_gaps: tuple[RetrievalGap, ...] = tuple(
                RetrievalGap(
                    claim_id=item.claim_id,
                    gap_type="missing_primary_source",
                    topic=next(
                        claim.text
                        for claim in request.document.claims
                        if claim.claim_id == item.claim_id
                    ),
                    preferred_source_types=("official_documentation", "primary_source"),
                )
                for item in uncertain
            ) or (
                RetrievalGap(
                    claim_id=None,
                    gap_type="claim_support_unverified",
                    topic=request.task_intent,
                    preferred_source_types=("primary_source",),
                ),
            )
            return self._retrieve_or_finish(request, assessment, retrieval_gaps)
        return _result(QualityAction.ACCEPT, assessment, ())

    @staticmethod
    def _retrieve_or_finish(
        request: TaskQualityInput,
        assessment: TaskQualityAssessment,
        gaps: tuple[RetrievalGap, ...],
    ) -> TaskQualityResult:
        reasons = tuple(dict.fromkeys(gap.gap_type for gap in gaps))
        if not request.budget.retrieval_exhausted:
            return _result(
                QualityAction.RETRIEVE_GAPS,
                assessment,
                reasons,
                gaps=gaps,
            )
        action = (
            QualityAction.BLOCK
            if request.mode is QualityMode.STRICT
            else QualityAction.DEGRADE
        )
        return _result(action, assessment, (*reasons, "retrieval_budget_exhausted"))


def document_from_markdown(
    markdown: str,
    *,
    task_id: int,
    known_evidence_ids: Sequence[str],
) -> TaskSummaryDocument:
    """Create a bounded deterministic claim projection from Markdown lines."""
    known = set(known_evidence_ids)
    claims: list[AtomicTaskClaim] = []
    all_citations = tuple(_CITATION_RE.findall(markdown))
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        text = re.sub(r"^(?:[-*+]\s+|\d+[.)]\s+)", "", line).strip()
        text = _CITATION_RE.sub("", text).strip()
        if not text:
            continue
        citations = tuple(
            dict.fromkeys(item for item in _CITATION_RE.findall(line) if item in known)
        )
        is_metric = bool(_NUMBER_RE.search(text))
        is_high_risk = is_metric or any(term in text.casefold() for term in _HIGH_RISK_TERMS)
        claims.append(
            AtomicTaskClaim(
                claim_id=f"task-{task_id}-claim-{len(claims) + 1}",
                text=text[:1200],
                claim_type="metric" if is_metric else "general",
                importance="core" if len(claims) < 3 else "supporting",
                risk_level="high" if is_high_risk else ("medium" if len(claims) < 3 else "low"),
                evidence_ids=citations,
            )
        )
        if len(claims) >= 12:
            break
    return TaskSummaryDocument(
        markdown=markdown,
        claims=tuple(claims),
        citation_ids=all_citations,
    )


def evidence_from_search_result(
    result: Mapping[str, object],
    *,
    task_id: int,
    query: str,
    backend: str,
) -> tuple[TaskEvidence, ...]:
    """Project a sanitized search result into bounded task evidence."""
    raw_results = result.get("results")
    if not isinstance(raw_results, list):
        return ()
    retrieved_at = str(result.get("retrieved_at") or "unknown")
    raw_age = result.get("cache_age_seconds", 0.0)
    age = float(raw_age) if isinstance(raw_age, (int, float)) and not isinstance(raw_age, bool) else 0.0
    evidence: list[TaskEvidence] = []
    for raw in raw_results[:5]:
        if not isinstance(raw, Mapping):
            continue
        title = raw.get("title")
        url = raw.get("url")
        excerpt = raw.get("raw_content") or raw.get("content")
        if not all(isinstance(item, str) and item.strip() for item in (title, url, excerpt)):
            continue
        evidence.append(
            TaskEvidence(
                evidence_id=f"task-{task_id}-ev-{len(evidence) + 1}",
                title=cast(str, title),
                url=cast(str, url),
                excerpt=cast(str, excerpt),
                provider=backend or "unknown",
                original_query=query,
                retrieved_at=retrieved_at,
                cache_age_seconds=max(0.0, age),
            )
        )
    return tuple(evidence)


def _content_reasons(markdown: str) -> tuple[str, ...]:
    reasons: list[str] = []
    text = markdown.strip()
    if not text or text == "暂无可用信息":
        reasons.append("empty_or_fallback")
    if len(text) < 30:
        reasons.append("too_short")
    if not any(marker in markdown for marker in ("###", "- ", "* ", "1. ", "2. ")):
        reasons.append("no_structure")
    return tuple(reasons)


def _citation_integrity(
    document: TaskSummaryDocument,
    evidence: Sequence[TaskEvidence],
) -> tuple[float, tuple[str, ...]]:
    known = {item.evidence_id for item in evidence}
    reasons: list[str] = []
    unknown = tuple(item for item in document.citation_ids if item not in known)
    if unknown:
        reasons.append("unknown_evidence_id")
    if len(document.citation_ids) != len(set(document.citation_ids)):
        reasons.append("duplicate_citation")
    factual_claims = tuple(document.claims)
    missing = tuple(claim for claim in factual_claims if not claim.evidence_ids)
    if missing:
        reasons.append("claim_missing_citation")
    bound = {item for claim in factual_claims for item in claim.evidence_ids}
    if any(item not in bound for item in document.citation_ids):
        reasons.append("orphan_citation")
    if not factual_claims:
        return 0.0, tuple(dict.fromkeys((*reasons, "no_claims")))
    coverage = (len(factual_claims) - len(missing)) / len(factual_claims)
    if unknown or any(item not in bound for item in document.citation_ids):
        coverage = 0.0
    return coverage, tuple(dict.fromkeys(reasons))


def _select_claims(
    claims: Sequence[AtomicTaskClaim],
    maximum: int,
) -> tuple[AtomicTaskClaim, ...]:
    priority = {"high": 0, "medium": 1, "low": 2}
    ordered = sorted(
        claims,
        key=lambda item: (
            priority.get(item.risk_level, 3),
            0 if item.importance == "core" else 1,
            item.claim_id,
        ),
    )
    return tuple(ordered[: max(0, maximum)])


def _bounded_evidence(
    claims: Sequence[AtomicTaskClaim],
    evidence: Sequence[TaskEvidence],
    budget: TaskQualityBudget,
) -> tuple[TaskEvidence, ...]:
    by_id = {item.evidence_id: item for item in evidence}
    selected: list[TaskEvidence] = []
    seen: set[str] = set()
    for claim in claims:
        candidates = [by_id[item] for item in claim.evidence_ids if item in by_id]
        if not candidates:
            candidates = list(evidence)
        for item in candidates[: budget.max_evidence_per_claim]:
            if item.evidence_id in seen:
                continue
            seen.add(item.evidence_id)
            selected.append(
                TaskEvidence(
                    evidence_id=item.evidence_id,
                    title=item.title,
                    url=item.url,
                    excerpt=(
                        item.excerpt
                        if item.canonical
                        else item.excerpt[: budget.max_evidence_chars]
                    ),
                    provider=item.provider,
                    original_query=item.original_query,
                    retrieved_at=item.retrieved_at,
                    cache_age_seconds=item.cache_age_seconds,
                    content_hash=item.content_hash,
                    source_type=item.source_type,
                    evidence_level=item.evidence_level,
                    locator=item.locator,
                    canonical=item.canonical,
                    source_provenance=item.source_provenance,
                )
            )
    return tuple(selected)


def _validate_judgments(
    judgments: Sequence[ClaimJudgment],
    claims: Sequence[AtomicTaskClaim],
    evidence: Sequence[TaskEvidence],
) -> None:
    expected = {item.claim_id for item in claims}
    known = {item.evidence_id: item for item in evidence}
    seen: set[str] = set()
    for item in judgments:
        if item.claim_id not in expected or item.claim_id in seen:
            raise ValueError("Judge returned an unknown or duplicate claim ID.")
        seen.add(item.claim_id)
        for evidence_id in (*item.supporting_evidence_ids, *item.conflicting_evidence_ids):
            if evidence_id not in known:
                raise ValueError("Judge returned an unknown evidence ID.")
        for span in item.support_spans:
            record = known.get(span.evidence_id)
            if record is None or span.exact_text not in record.excerpt:
                raise ValueError("Judge support span is not an exact evidence excerpt.")
    if seen != expected:
        raise ValueError("Judge did not return one result per selected claim.")


def _claim_support_score(judgments: Sequence[ClaimJudgment]) -> float:
    if not judgments:
        return 0.0
    values = {
        ClaimVerdict.SUPPORTED: 1.0,
        ClaimVerdict.UNCERTAIN: 0.4,
        ClaimVerdict.CONFLICTING: 0.25,
        ClaimVerdict.UNSUPPORTED: 0.0,
    }
    return sum(values[item.verdict] for item in judgments) / len(judgments)


def _result(
    action: QualityAction,
    assessment: TaskQualityAssessment,
    reasons: Sequence[str],
    *,
    gaps: tuple[RetrievalGap, ...] = (),
    repair: tuple[str, ...] = (),
) -> TaskQualityResult:
    return TaskQualityResult(
        action=action,
        reason_codes=tuple(dict.fromkeys(reasons)),
        assessment=assessment,
        retrieval_gaps=gaps,
        repair_instructions=repair,
    )


def _unit_score(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        return 0.0
    return max(0.0, min(1.0, float(value)))


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Judge {field_name} must be non-empty text.")
    return value.strip()


def _text_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError("Judge evidence ID fields must be text lists.")
    return tuple(dict.fromkeys(item.strip() for item in value))


def _tokens(text: str) -> set[str]:
    lowered = text.casefold()
    words = set(re.findall(r"[a-z0-9][a-z0-9_.+-]{1,}", lowered))
    cjk = "".join(re.findall(r"[\u3400-\u9fff]", lowered))
    words.update(cjk[index : index + 2] for index in range(max(0, len(cjk) - 1)))
    return {item for item in words if item}


__all__ = [
    "AtomicTaskClaim",
    "ClaimEvidenceJudge",
    "ClaimJudgment",
    "ClaimVerdict",
    "LexicalRelevanceRanker",
    "JsonBatchClaimEvidenceJudge",
    "QualityAction",
    "QualityMode",
    "RetrievalGap",
    "RetrievalRelevanceRanker",
    "SupportSpan",
    "TaskEvidence",
    "TaskQualityAssessment",
    "TaskQualityBudget",
    "TaskQualityController",
    "TaskQualityInput",
    "TaskQualityResult",
    "TaskSummaryDocument",
    "UnavailableClaimEvidenceJudge",
    "document_from_markdown",
    "evidence_from_search_result",
]
