"""Deterministic quality control for planner-generated research plans.

The plan gate validates one planner output against the original topic using
only deterministic checks: pairwise query overlap (explicit duplication) and
lexical coverage of significant topic terms.  Repairs are deterministic
(dropping verbatim-duplicate tasks, keeping the first of each overlapping
pair) and the boundary fails open: a plan that cannot be repaired is still
returned so the surrounding research run continues, unlike the evidence
layer which fails closed.

An LLM span-binding coverage scorer can replace the lexical scorer through
the ``PlanCoverageScorer`` protocol without changing the gate itself.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from models import TodoItem

from .history import _ASCII_TOKEN_RE, _CJK_RUN_RE, _normalize_text, _tokens

_PLAN_STOPWORDS = frozenset(
    {
        # Chinese function words and particles.
        "的", "了", "和", "与", "及", "或", "是", "有", "在", "对", "从",
        "被", "把", "请", "给", "为", "以", "个", "这", "那", "并", "等",
        "中", "上", "下", "呢", "吧", "吗", "一个", "一些", "以及", "还有",
        # Generic instruction and question words.
        "分析", "调研", "研究", "介绍", "了解", "总结", "梳理", "帮我",
        "看看", "告诉", "解释", "什么", "为什么", "怎么", "如何", "哪些",
        "哪个", "一下", "关于", "请问", "描述", "说明",
        # English generic words.
        "the", "a", "an", "of", "for", "and", "or", "to", "in", "on",
        "about", "with", "what", "why", "how", "which", "compare",
        "analysis", "research", "overview", "introduction", "tell",
        "me", "please", "give",
    }
)
_TERM_DISPLAY_LIMIT = 16


class PlanValidationVerdict(str, Enum):
    """Final planning-layer validation outcome."""

    PASS = "pass"
    PASS_WITH_REPAIRS = "pass_with_repairs"
    RETRY = "retry"


class PlanCoverageScorer(Protocol):
    """Score how well a planned task set covers the original research topic."""

    def coverage(self, topic: str, task_texts: Sequence[str]) -> float:
        """Return a finite coverage score between zero and one."""
        ...

    def uncovered_terms(self, topic: str, task_texts: Sequence[str]) -> tuple[str, ...]:
        """Return the significant topic terms no task text covers."""
        ...


class LexicalCoverageScorer:
    """Deterministic keyword and CJK-bigram coverage scorer."""

    def coverage(self, topic: str, task_texts: Sequence[str]) -> float:
        """Return the covered fraction of significant topic terms."""
        expected = _significant_tokens(topic)
        if not expected:
            return 1.0
        if not task_texts:
            return 0.0
        observed = _observed_tokens(task_texts)
        return len(expected & observed) / len(expected)

    def uncovered_terms(self, topic: str, task_texts: Sequence[str]) -> tuple[str, ...]:
        """Return bounded readable uncovered topic fragments in source order.

        Adjacent uncovered CJK bigrams are merged into one continuous
        fragment (``竞争`` + ``争者`` becomes ``竞争者``) so retry feedback
        shows readable concept spans instead of raw overlapping bigrams.
        """
        observed = _observed_tokens(task_texts)
        normalized = _normalize_text(topic)
        uncovered: list[str] = []
        for run in _CJK_RUN_RE.findall(normalized):
            segment = ""
            for index in range(len(run) - 1):
                bigram = run[index : index + 2]
                if bigram in observed or bigram in _PLAN_STOPWORDS:
                    if segment:
                        uncovered.append(segment)
                        segment = ""
                    continue
                if not segment:
                    segment = bigram
                elif segment[-1] == bigram[0]:
                    segment += bigram[1]
                else:
                    uncovered.append(segment)
                    segment = bigram
            if segment:
                uncovered.append(segment)
        for token in _ASCII_TOKEN_RE.findall(normalized):
            if (
                len(token) >= 2
                and token not in _PLAN_STOPWORDS
                and token not in observed
            ):
                uncovered.append(token)
        return tuple(dict.fromkeys(uncovered))[:_TERM_DISPLAY_LIMIT]


@dataclass(frozen=True, kw_only=True, slots=True)
class PlanQualityMetrics:
    """Bounded deterministic observations for one repaired plan.

    ``max_pairwise_overlap`` describes the repaired plan: it is measured
    only across retained tasks, so it never exceeds ``overlap_threshold``
    even when near-duplicate tasks were dropped.
    """

    task_count: int
    dropped_duplicate_count: int
    max_pairwise_overlap: float
    coverage_ratio: float

    def as_dict(self) -> dict[str, float | int]:
        """Return a JSON-serializable metrics projection."""
        return {
            "task_count": self.task_count,
            "dropped_duplicate_count": self.dropped_duplicate_count,
            "max_pairwise_overlap": round(self.max_pairwise_overlap, 4),
            "coverage_ratio": round(self.coverage_ratio, 4),
        }


@dataclass(frozen=True, kw_only=True, slots=True)
class PlanValidationResult:
    """One validated (possibly repaired) plan plus deterministic diagnostics."""

    verdict: PlanValidationVerdict
    tasks: tuple[TodoItem, ...]
    reason_codes: tuple[str, ...]
    metrics: PlanQualityMetrics
    retry_feedback: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate the verdict-repair consistency."""
        if self.verdict is PlanValidationVerdict.RETRY and not self.retry_feedback:
            raise ValueError("A retry verdict requires bounded feedback.")
        if self.verdict is not PlanValidationVerdict.RETRY and self.retry_feedback:
            raise ValueError("Feedback is only allowed on a retry verdict.")


def _significant_tokens(text: str) -> frozenset[str]:
    """Return bounded content tokens with generic instruction words removed."""
    return frozenset(
        token
        for token in _tokens(text)
        if len(token) >= 2 and token not in _PLAN_STOPWORDS
    )


def _observed_tokens(task_texts: Sequence[str]) -> frozenset[str]:
    """Return the union of tokens across all task text projections."""
    if not task_texts:
        return frozenset()
    return frozenset().union(*(frozenset(_tokens(text)) for text in task_texts))


def _task_text(task: TodoItem) -> str:
    """Return one task's searchable text projection."""
    return f"{task.title} {task.intent} {task.query}"


def _query_jaccard(left: TodoItem, right: TodoItem) -> float:
    """Return the query-token Jaccard overlap between two tasks."""
    left_tokens = frozenset(_tokens(left.query))
    right_tokens = frozenset(_tokens(right.query))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


class PlanValidator:
    """Deterministic plan gate with fail-open repairs.

    The gate drops only tasks whose queries are near-verbatim duplicates
    (strictly above ``overlap_threshold``), and asks for one bounded
    re-plan when significant topic terms remain uncovered.  Semantic
    near-duplicates are intentionally out of scope for this deterministic
    boundary and belong to an injected LLM coverage scorer.
    """

    def __init__(
        self,
        *,
        overlap_threshold: float = 0.75,
        coverage_threshold: float = 0.4,
        coverage_scorer: PlanCoverageScorer | None = None,
    ) -> None:
        """Bind thresholds and an optional coverage scorer replacement."""
        if not 0.0 < overlap_threshold <= 1.0:
            raise ValueError("overlap_threshold must be within (0, 1].")
        if not 0.0 <= coverage_threshold <= 1.0:
            raise ValueError("coverage_threshold must be within [0, 1].")
        self._overlap_threshold = overlap_threshold
        self._coverage_threshold = coverage_threshold
        self._coverage_scorer: PlanCoverageScorer = (
            coverage_scorer or LexicalCoverageScorer()
        )

    def validate(self, topic: str, tasks: Sequence[TodoItem]) -> PlanValidationResult:
        """Validate one plan, applying deterministic duplicate repairs."""
        task_list = [task for task in tasks if isinstance(task, TodoItem)]
        kept: list[TodoItem] = []
        dropped = 0
        max_overlap = 0.0
        overlap_pairs: list[str] = []
        for task in task_list:
            duplicate_of = next(
                (
                    kept_task
                    for kept_task in kept
                    if _query_jaccard(kept_task, task) > self._overlap_threshold
                ),
                None,
            )
            if duplicate_of is not None:
                dropped += 1
                overlap_pairs.append(
                    f"任务“{task.title}”与任务“{duplicate_of.title}”检索词高度重复"
                )
                continue
            for kept_task in kept:
                max_overlap = max(
                    max_overlap, _query_jaccard(kept_task, task)
                )
            kept.append(task)
        task_texts = [_task_text(task) for task in kept]
        coverage = self._coverage_scorer.coverage(topic, task_texts)
        uncovered = self._coverage_scorer.uncovered_terms(topic, task_texts)
        reason_codes: list[str] = []
        if dropped:
            reason_codes.append("plan_duplicate_tasks_dropped")
        retry_feedback: tuple[str, ...] = ()
        verdict: PlanValidationVerdict
        if coverage < self._coverage_threshold and kept:
            reason_codes.append("plan_coverage_below_threshold")
            feedback = [f"计划的检索任务未能覆盖原问题的关键概念：{'、'.join(uncovered) or '（无显式词）'}，请补充覆盖这些方面的任务"]
            feedback.extend(overlap_pairs)
            retry_feedback = tuple(feedback)
            verdict = PlanValidationVerdict.RETRY
        elif dropped:
            verdict = PlanValidationVerdict.PASS_WITH_REPAIRS
        else:
            verdict = PlanValidationVerdict.PASS
        metrics = PlanQualityMetrics(
            task_count=len(kept),
            dropped_duplicate_count=dropped,
            max_pairwise_overlap=round(max_overlap, 4),
            coverage_ratio=round(coverage, 4),
        )
        return PlanValidationResult(
            verdict=verdict,
            tasks=tuple(kept),
            reason_codes=tuple(reason_codes),
            metrics=metrics,
            retry_feedback=retry_feedback,
        )


class PlanComplexityTier(str, Enum):
    """Deterministic query-complexity tier for plan sizing."""

    SIMPLE = "simple"
    MEDIUM = "medium"
    COMPLEX = "complex"


_TIER_TASK_BOUNDS: dict[PlanComplexityTier, tuple[int, int]] = {
    PlanComplexityTier.SIMPLE: (1, 2),
    PlanComplexityTier.MEDIUM: (3, 5),
    PlanComplexityTier.COMPLEX: (5, 8),
}
_ASPECT_CONJUNCTION_RE = re.compile(
    r"[、，;；]|以及|还有|\bvs\.?\b|\bversus\b"
)
_COMPARISON_MARKERS = ("对比", "比较", "竞争", "差异", "优劣", "区别", "异同")
_DEPTH_MARKERS = ("分析", "综述", "评估", "预测", "趋势", "战略", "路线", "调研", "梳理")


def estimate_complexity(topic: str) -> PlanComplexityTier:
    """Estimate one deterministic complexity tier from bounded topic signals.

    The score counts ASCII keywords and CJK runs (robust to cross-word
    bigram noise), doubles aspect conjunction weight (``、``/``，``/``以及``/
    ``vs``), and adds comparison or depth markers.  Single CJK conjunctions
    (``和``/``与``/``及``) are deliberately ignored because without a word
    segmenter they would fire inside content words.  The tier is advisory:
    it sizes the suggested task count in the planner prompt while hard task
    bounds stay with the plan schema and the profile retrieval budget.
    """
    if not topic or not topic.strip():
        return PlanComplexityTier.SIMPLE
    normalized = _normalize_text(topic)
    ascii_terms = [
        token for token in _ASCII_TOKEN_RE.findall(normalized) if len(token) >= 2
    ]
    cjk_runs = _CJK_RUN_RE.findall(normalized)
    aspect_count = len(_ASPECT_CONJUNCTION_RE.findall(normalized))
    score = len(ascii_terms) + len(cjk_runs) + 2 * aspect_count
    if any(marker in topic for marker in _COMPARISON_MARKERS):
        score += 3
    if (
        len(ascii_terms) + len(cjk_runs) >= 3
        and any(marker in topic for marker in _DEPTH_MARKERS)
    ):
        score += 2
    if score >= 12:
        return PlanComplexityTier.COMPLEX
    if score >= 6:
        return PlanComplexityTier.MEDIUM
    return PlanComplexityTier.SIMPLE


def tier_task_bounds(tier: PlanComplexityTier) -> tuple[int, int]:
    """Return the advisory inclusive task-count bounds for one tier."""
    return _TIER_TASK_BOUNDS[tier]


__all__ = [
    "LexicalCoverageScorer",
    "PlanComplexityTier",
    "PlanCoverageScorer",
    "PlanQualityMetrics",
    "PlanValidationResult",
    "PlanValidationVerdict",
    "PlanValidator",
    "estimate_complexity",
    "tier_task_bounds",
]
