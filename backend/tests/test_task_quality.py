"""Action-oriented ordinary-task quality controller tests."""

from __future__ import annotations

from collections.abc import Sequence

from research.task_quality import (
    AtomicTaskClaim,
    ClaimJudgment,
    ClaimVerdict,
    JsonBatchClaimEvidenceJudge,
    QualityAction,
    QualityMode,
    SupportSpan,
    TaskEvidence,
    TaskQualityBudget,
    TaskQualityController,
    TaskQualityInput,
    TaskSummaryDocument,
    document_from_markdown,
)


class FixedRanker:
    """Return one deterministic retrieval-relevance score."""

    def __init__(self, score: float) -> None:
        self.score_value = score

    def score(
        self,
        query: str,
        intent: str,
        evidence: Sequence[TaskEvidence],
    ) -> float:
        del query, intent, evidence
        return self.score_value


class RecordingJudge:
    """Return configured verdicts while recording the bounded batch."""

    def __init__(self, verdict: ClaimVerdict) -> None:
        self.verdict = verdict
        self.calls: list[tuple[int, int]] = []

    def judge(
        self,
        claims: Sequence[AtomicTaskClaim],
        evidence: Sequence[TaskEvidence],
        budget: TaskQualityBudget,
    ) -> tuple[ClaimJudgment, ...]:
        del budget
        self.calls.append((len(claims), len(evidence)))
        support_id = evidence[0].evidence_id if evidence else ""
        return tuple(
            ClaimJudgment(
                claim_id=claim.claim_id,
                verdict=self.verdict,
                supporting_evidence_ids=(support_id,)
                if self.verdict is ClaimVerdict.SUPPORTED and support_id
                else (),
                conflicting_evidence_ids=(support_id,)
                if self.verdict is ClaimVerdict.CONFLICTING and support_id
                else (),
                support_spans=(
                    (
                        SupportSpan(
                            evidence_id=support_id,
                            exact_text="Redis uses 16384 slots",
                        ),
                    )
                    if self.verdict is ClaimVerdict.SUPPORTED and support_id
                    else ()
                ),
                reason_code=f"judge_{self.verdict.value}",
            )
            for claim in claims
        )


def evidence() -> tuple[TaskEvidence, ...]:
    """Return stable primary evidence."""
    return (
        TaskEvidence(
            evidence_id="task-1-ev-1",
            title="Redis Cluster specification",
            url="https://redis.io/docs/latest/operate/oss_and_stack/reference/cluster-spec/",
            excerpt="Redis uses 16384 slots to shard keys across a cluster.",
            provider="duckduckgo",
            original_query="Redis cluster slots",
            retrieved_at="2026-09-07T00:00:00+00:00",
        ),
    )


def request(
    *,
    markdown: str = "### 任务总结\n- Redis uses 16384 slots [task-1-ev-1]",
    mode: QualityMode = QualityMode.EVIDENCE,
    budget: TaskQualityBudget | None = None,
) -> TaskQualityInput:
    """Build one task-quality request through the public projection helper."""
    records = evidence()
    return TaskQualityInput(
        task_id=1,
        task_intent="Redis cluster slots",
        current_query="Redis cluster slots",
        document=document_from_markdown(
            markdown,
            task_id=1,
            known_evidence_ids=tuple(item.evidence_id for item in records),
        ),
        evidence=records,
        mode=mode,
        budget=budget or TaskQualityBudget(),
    )


def test_citation_integrity_does_not_claim_factual_support() -> None:
    """A valid evidence ID cannot turn an unsupported statement into a fact."""
    judge = RecordingJudge(ClaimVerdict.UNSUPPORTED)
    result = TaskQualityController(ranker=FixedRanker(1.0), judge=judge).evaluate(
        request(markdown="### 任务总结\n- Redis uses 32768 slots [task-1-ev-1]")
    )

    assert result.assessment.citation_integrity == 1.0
    assert result.assessment.claim_support == 0.0
    assert result.action is QualityAction.REGENERATE_SUMMARY


def test_missing_citation_is_repaired_without_spending_judge_call() -> None:
    """Deterministic citation failure short-circuits the external judge."""
    judge = RecordingJudge(ClaimVerdict.SUPPORTED)
    result = TaskQualityController(ranker=FixedRanker(1.0), judge=judge).evaluate(
        request(markdown="### 任务总结\n- Redis uses 16384 slots")
    )

    assert result.action is QualityAction.REPAIR_CITATIONS
    assert result.reason_codes == ("claim_missing_citation",)
    assert judge.calls == []


def test_irrelevant_retrieval_produces_concrete_gap_without_judging() -> None:
    """Retrieval relevance remains separate from claim support."""
    judge = RecordingJudge(ClaimVerdict.SUPPORTED)
    result = TaskQualityController(ranker=FixedRanker(0.1), judge=judge).evaluate(
        request()
    )

    assert result.action is QualityAction.RETRIEVE_GAPS
    assert result.retrieval_gaps[0].gap_type == "retrieval_not_relevant"
    assert judge.calls == []


def test_conflicting_evidence_is_preserved_as_an_explicit_action() -> None:
    """Conflict is surfaced instead of repeatedly searching for a preferred answer."""
    result = TaskQualityController(
        ranker=FixedRanker(1.0),
        judge=RecordingJudge(ClaimVerdict.CONFLICTING),
    ).evaluate(request())

    assert result.action is QualityAction.FLAG_CONFLICT
    assert result.assessment.judgments[0].verdict is ClaimVerdict.CONFLICTING


def test_strict_mode_blocks_when_retrieval_budget_is_exhausted() -> None:
    """Strict mode changes failure policy, not quality-dimension meanings."""
    result = TaskQualityController(
        ranker=FixedRanker(1.0),
        judge=RecordingJudge(ClaimVerdict.UNCERTAIN),
    ).evaluate(
        request(
            mode=QualityMode.STRICT,
            budget=TaskQualityBudget(
                retrieval_attempts_used=2,
                max_retrieval_attempts=2,
            ),
        )
    )

    assert result.action is QualityAction.BLOCK
    assert "retrieval_budget_exhausted" in result.reason_codes


def test_basic_mode_does_not_call_claim_judge() -> None:
    """Basic mode keeps deterministic dimensions only."""
    judge = RecordingJudge(ClaimVerdict.UNSUPPORTED)
    result = TaskQualityController(ranker=FixedRanker(1.0), judge=judge).evaluate(
        request(mode=QualityMode.BASIC)
    )

    assert result.action is QualityAction.ACCEPT
    assert judge.calls == []


def test_selector_and_batch_context_obey_hard_limits() -> None:
    """One large task cannot create an unbounded judge request."""
    records = evidence()
    claims = tuple(
        AtomicTaskClaim(
            claim_id=f"claim-{index}",
            text=f"metric {index}",
            claim_type="metric",
            importance="core",
            risk_level="high",
            evidence_ids=("task-1-ev-1",),
        )
        for index in range(8)
    )
    document = TaskSummaryDocument(
        markdown="### Summary\n- enough detailed findings [task-1-ev-1]",
        claims=claims,
        citation_ids=("task-1-ev-1",),
    )
    judge = RecordingJudge(ClaimVerdict.SUPPORTED)
    result = TaskQualityController(ranker=FixedRanker(1.0), judge=judge).evaluate(
        TaskQualityInput(
            task_id=1,
            task_intent="metrics",
            current_query="metrics",
            document=document,
            evidence=records,
            budget=TaskQualityBudget(max_claims=3, max_evidence_per_claim=1),
        )
    )

    assert result.action is QualityAction.ACCEPT
    assert judge.calls == [(3, 1)]
    assert len(result.assessment.checked_claim_ids) == 3


def test_judge_support_span_must_be_exact() -> None:
    """Hallucinated evidence spans fail closed into a retrieval decision."""

    class InvalidSpanJudge(RecordingJudge):
        def judge(
            self,
            claims: Sequence[AtomicTaskClaim],
            evidence: Sequence[TaskEvidence],
            budget: TaskQualityBudget,
        ) -> tuple[ClaimJudgment, ...]:
            del budget
            return (
                ClaimJudgment(
                    claim_id=claims[0].claim_id,
                    verdict=ClaimVerdict.SUPPORTED,
                    supporting_evidence_ids=(evidence[0].evidence_id,),
                    support_spans=(
                        SupportSpan(
                            evidence_id=evidence[0].evidence_id,
                            exact_text="hallucinated span",
                        ),
                    ),
                    reason_code="direct_primary_support",
                ),
            )

    result = TaskQualityController(
        ranker=FixedRanker(1.0),
        judge=InvalidSpanJudge(ClaimVerdict.SUPPORTED),
    ).evaluate(request())

    assert result.action is QualityAction.RETRIEVE_GAPS
    assert result.assessment.judgments[0].verdict is ClaimVerdict.UNCERTAIN
    assert "judge_invalid_or_unavailable" in result.assessment.reason_codes


def test_json_batch_judge_uses_verdicts_and_exact_spans_not_scores() -> None:
    """The external adapter exposes discrete evidence reasoning only."""
    prompts: list[str] = []

    def invoke(prompt: str) -> str:
        prompts.append(prompt)
        return """{
          "judgments": [{
            "claim_id": "task-1-claim-1",
            "verdict": "supported",
            "supporting_evidence_ids": ["task-1-ev-1"],
            "conflicting_evidence_ids": [],
            "support_spans": [{
              "evidence_id": "task-1-ev-1",
              "exact_text": "Redis uses 16384 slots"
            }],
            "reason_code": "direct_primary_support"
          }]
        }"""

    claims = request().document.claims
    judgments = JsonBatchClaimEvidenceJudge(invoke).judge(
        claims,
        evidence(),
        TaskQualityBudget(),
    )

    assert judgments[0].verdict is ClaimVerdict.SUPPORTED
    assert judgments[0].support_spans[0].exact_text == "Redis uses 16384 slots"
    assert "Do not return numeric scores" in prompts[0]
