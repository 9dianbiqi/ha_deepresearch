"""Focused retrieval-budget contract tests."""

from __future__ import annotations

import pytest

from research.profiles import RetrievalBudget
from research.sources import BudgetExceededError, RetrievalBudgetTracker


def test_budget_tracker_accounts_all_dimensions_and_rejects_atomic_overflow() -> None:
    """A failed reservation changes no counter in any dimension."""
    tracker = RetrievalBudgetTracker(
        RetrievalBudget(
            max_requests=2,
            max_results=3,
            max_evidence=4,
            max_enrich_passes=1,
        )
    )
    tracker.reserve(requests=1, results=2, evidence=3, enrich_passes=1)

    with pytest.raises(BudgetExceededError):
        tracker.reserve(requests=2, results=1, evidence=1)

    assert tracker.snapshot() == {
        "requests": 1,
        "results": 2,
        "evidence": 3,
        "enrich_passes": 1,
    }


def test_budget_tracker_rejects_invalid_increments() -> None:
    """Reservations must be non-negative integer increments."""
    tracker = RetrievalBudgetTracker(RetrievalBudget())

    with pytest.raises(ValueError):
        tracker.reserve(requests=-1)
    with pytest.raises(ValueError):
        tracker.reserve(results=True)  # type: ignore[arg-type]
