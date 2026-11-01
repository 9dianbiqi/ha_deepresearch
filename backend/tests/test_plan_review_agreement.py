"""Verify agreement denominators and explicit, traceable human adjudication."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from research.plan_review_agreement import (
    adjudicate,
    adjudication_template,
    analyze,
    categorical_agreement,
    prepare,
)
from research.plan_sampling import export_review, review_labels, seed_collection

DATASET = Path(__file__).resolve().parents[1] / "evaluation" / "planner_v1.json"


def _reviews(tmp_path):
    source = seed_collection(DATASET)
    source["samples"] = [source["samples"][0]]
    left = export_review(source, tmp_path / "left.json")
    left["annotation_status"] = "human_reviewed"
    for item in left["items"]:
        item.update(
            reviewer="Fixture A",
            reviewed_at="2026-10-08",
            review_notes="Test annotations",
        )
        item["redundant_pairs"] = []
        for task in item["tasks"]:
            task.update(
                relevance=2,
                granularity="appropriate",
                covered_dimensions=[
                    {1: "share", 2: "pricing", 3: "policy", 4: "growth"}[task["id"]]
                ],
                rationale="Fixture scope",
            )
    right = copy.deepcopy(left)
    for item in right["items"]:
        item["reviewer"] = "Fixture B"
    return source, left, right


def test_kappa_known_counts_and_degenerate_cases():
    result = categorical_agreement([(0, 0), (0, 1), (1, 1), (1, 1)])
    assert result["matched_count"] == 3
    assert result["agreement"] == 0.75
    assert result["kappa"] == pytest.approx(0.5)
    assert result["confusion_matrix"] == [[1, 1], [0, 2]]
    assert categorical_agreement([])["agreement"] is None
    assert categorical_agreement([(2, 2)])["kappa"] is None


def test_agreement_does_not_invent_perfect_redundancy_from_all_negative_pairs(tmp_path):
    source, left, right = _reviews(tmp_path)
    result = analyze(source, left, right)
    assert result["metrics"]["relevance"]["unit_count"] == 4
    assert result["metrics"]["coverage_binding"]["unit_count"] == 16
    assert result["metrics"]["missing_dimension"]["unit_count"] == 4
    assert result["metrics"]["redundancy"]["unit_count"] == 6
    assert result["redundancy_positive_jaccard"] is None
    assert result["metrics"]["redundancy"]["kappa"] is None
    assert not result["needs_adjudication"]


def test_four_axis_disagreements_are_named_and_not_averaged_away(tmp_path):
    source, left, right = _reviews(tmp_path)
    task = right["items"][0]["tasks"][0]
    task.update(relevance=1, granularity="too_coarse", covered_dimensions=[])
    right["items"][0]["redundant_pairs"] = [
        {"task_ids": [2, 1], "rationale": "Fixture overlap"}
    ]
    result = analyze(source, left, right)
    assert {item["axis"] for item in result["disagreements"]} == {
        "relevance",
        "granularity",
        "coverage",
        "redundancy",
    }
    assert result["metrics"]["relevance"]["agreement"] == 0.75
    assert result["metrics"]["redundancy"]["agreement"] == pytest.approx(5 / 6)
    assert result["redundancy_positive_jaccard"] == 0
    assert result["needs_adjudication"]


def test_pair_direction_and_dimension_order_do_not_create_false_disagreements(tmp_path):
    source, left, right = _reviews(tmp_path)
    left["items"][0]["redundant_pairs"] = [
        {"task_ids": [1, 2], "rationale": "A reason"}
    ]
    right["items"][0]["redundant_pairs"] = [
        {"task_ids": [2, 1], "rationale": "Another reason"}
    ]
    for packet in (left, right):
        packet["items"][0]["tasks"][0]["covered_dimensions"] = ["share", "pricing"]
    right["items"][0]["tasks"][0]["covered_dimensions"].reverse()
    assert not analyze(source, left, right)["needs_adjudication"]


def test_same_reviewer_and_ai_author_cannot_pass_as_two_humans(tmp_path):
    source, left, right = _reviews(tmp_path)
    right["items"][0]["reviewer"] = "  fixture a "
    with pytest.raises(ValueError, match="distinct"):
        analyze(source, left, right)
    left["annotation_author_type"] = "ai"
    with pytest.raises(ValueError, match="AI review drafts"):
        review_labels(source, left)


def test_adjudication_requires_every_decision_and_records_parent_labels(tmp_path):
    source, left, right = _reviews(tmp_path)
    right["items"][0]["tasks"][0]["relevance"] = 1
    template = adjudication_template(analyze(source, left, right))
    assert template["decisions"][0]["value"] is None
    with pytest.raises(ValueError, match="not complete"):
        adjudicate(source, left, right, template)
    template.update(
        annotation_status="human_reviewed",
        adjudicator="Fixture adjudicator",
        adjudicated_at="2026-10-08",
    )
    with pytest.raises(ValueError, match="value"):
        adjudicate(source, left, right, template)
    template["decisions"][0].update(
        value=2, rationale="Directly answers requested share dimension"
    )
    result = adjudicate(source, left, right, template)
    assert result["audit"]["resolved_disagreement_count"] == 1
    assert result["review_packet"]["items"][0]["reviewer"] == "Fixture adjudicator"
    assert result["review_packet"]["items"][0]["tasks"][0]["relevance"] == 2
    changed = copy.deepcopy(right)
    changed["items"][0]["review_notes"] = "Changed after decision"
    with pytest.raises(ValueError, match="changed source"):
        adjudicate(source, left, changed, template)


def test_invalid_decision_is_rejected_by_original_annotation_schema(tmp_path):
    source, left, right = _reviews(tmp_path)
    right["items"][0]["tasks"][0]["relevance"] = 1
    decisions = adjudication_template(analyze(source, left, right))
    decisions.update(
        annotation_status="human_reviewed",
        adjudicator="Fixture C",
        adjudicated_at="2026-10-08",
    )
    decisions["decisions"][0].update(value=99, rationale="Out-of-range fixture")
    with pytest.raises(ValueError):
        adjudicate(source, left, right, decisions)


def test_prepare_keeps_blank_labels_and_preserves_existing_work(tmp_path):
    directory = tmp_path / "packets"
    prepare(seed_collection(DATASET), directory)
    assert (directory / "reviewer_a.json").exists()
    assert (directory / "reviewer_b.md").exists()
    assert '"relevance": null' in (directory / "reviewer_a.json").read_text(
        encoding="utf-8"
    )
    with pytest.raises(ValueError, match="preserve"):
        prepare(seed_collection(DATASET), directory)
