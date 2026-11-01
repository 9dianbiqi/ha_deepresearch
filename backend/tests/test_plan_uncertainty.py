"""Lock uncertainty out of core metrics while preserving human boundary labels."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from research.plan_evaluation import (
    PlanEvaluationDataset,
    evaluate_dataset,
    load_dataset,
)
from research.plan_review_agreement import adjudicate, adjudication_template, analyze
from research.plan_sampling import (
    compare_review,
    export_review,
    review_labels,
    seed_collection,
)

DATASET = Path(__file__).resolve().parents[1] / "evaluation" / "planner_v1.json"


def _reviews(tmp_path):
    source = seed_collection(DATASET)
    source["samples"] = [source["samples"][0]]
    packet = export_review(source, tmp_path / "review.json")
    packet["annotation_status"] = "human_reviewed"
    for item in packet["items"]:
        item.update(
            reviewer="Fixture A", reviewed_at="2026-10-08", review_notes="Test only"
        )
        item["redundant_pairs"] = []
        for task in item["tasks"]:
            task.update(
                relevance=2,
                granularity="appropriate",
                covered_dimensions=[
                    {1: "share", 2: "pricing", 3: "policy", 4: "growth"}[task["id"]]
                ],
                rationale="Fixture label",
            )
    return source, packet


def test_named_boundary_is_excluded_from_bad_plan_and_alert_denominators():
    result = evaluate_dataset(load_dataset(DATASET))
    assert result["summary"]["candidate_count"] == 36
    assert result["summary"]["core_candidate_count"] == 35
    assert result["summary"]["boundary_candidate_count"] == 1
    assert result["summary"]["reference_bad_count"] == 29
    assert result["summary"]["missed_bad_plans"] == 16
    boundary = result["boundary_candidates"][0]
    assert boundary["candidate_id"] == "paraphrase-duplicate"
    assert boundary["reference_before"]["acceptable"] is None
    assert boundary["gate_verdict"] == "pass"


@pytest.mark.parametrize("field", ["relevance", "granularity", "covered_dimensions"])
def test_uncertain_task_axis_is_explicit_and_cannot_be_forced_into_core(field):
    payload = json.loads(DATASET.read_text(encoding="utf-8"))
    payload["topics"] = [payload["topics"][0]]
    candidate = payload["topics"][0]["candidates"][0]
    payload["topics"][0]["candidates"] = [candidate]
    candidate["tasks"][0][field] = "uncertain"
    candidate["evaluation_set"] = "core"
    result = evaluate_dataset(PlanEvaluationDataset.model_validate(payload))
    assert result["summary"]["core_candidate_count"] == 0
    assert result["summary"]["alert_recall"] is None
    assert result["summary"]["uncertain_plan_ratio"] == 1
    assert result["candidates"][0]["reference_before"]["acceptable"] is None


def test_uncertain_pair_is_accepted_but_missing_labels_remain_incomplete(tmp_path):
    source, packet = _reviews(tmp_path)
    packet["items"][0]["pair_relations"] = [
        {
            "task_ids": [1, 2],
            "label": "uncertain",
            "reason": "Insufficient task distinction",
        }
    ]
    result = review_labels(source, packet)
    assert result["plans"][0]["quality"]["evaluation_set"] == "boundary"
    packet["items"][0]["tasks"][0]["relevance"] = None
    with pytest.raises(ValueError):
        review_labels(source, packet)


def test_contradictory_or_dangling_relation_is_rejected(tmp_path):
    source, packet = _reviews(tmp_path)
    item = packet["items"][0]
    item["redundant_pairs"] = [{"task_ids": [1, 2], "rationale": "Same goal"}]
    item["pair_relations"] = [
        {"task_ids": [1, 2], "label": "uncertain", "reason": "Cannot decide"}
    ]
    with pytest.raises(ValueError, match="conflicts"):
        review_labels(source, packet)


def test_explicit_redundant_and_complementary_labels_are_core(tmp_path):
    source, packet = _reviews(tmp_path)
    item = packet["items"][0]
    item["pair_relations"] = [
        {"task_ids": [1, 2], "label": "redundant", "reason": "Same goal"}
    ]
    result = review_labels(source, packet)
    assert result["plans"][0]["quality"]["redundant_pairs"] == [[1, 2]]
    assert result["plans"][0]["quality"]["evaluation_set"] == "core"
    item["pair_relations"][0]["label"] = "complementary"
    assert review_labels(source, packet)["plans"][0]["quality"]["acceptable"]
    item["redundant_pairs"] = []
    item["pair_relations"][0]["task_ids"] = [1, 99]
    with pytest.raises(ValueError, match="existing tasks"):
        review_labels(source, packet)


def test_agreement_excludes_boundary_instead_of_scoring_a_third_truth(tmp_path):
    source, left = _reviews(tmp_path)
    right = copy.deepcopy(left)
    right["items"][0]["reviewer"] = "Fixture B"
    left["items"][0]["pair_relations"] = [
        {"task_ids": [1, 2], "label": "uncertain", "reason": "Insufficient detail"}
    ]
    report = analyze(source, left, right)
    assert report["core_plan_count"] == 0
    assert report["boundary_plan_count"] == 1
    assert report["metrics"]["redundancy"]["unit_count"] == 0
    assert report["metrics"]["redundancy"]["agreement"] is None
    assert report["needs_adjudication"]


def test_adjudication_may_keep_uncertain_without_forcing_binary_resolution(tmp_path):
    source, left = _reviews(tmp_path)
    right = copy.deepcopy(left)
    right["items"][0]["reviewer"] = "Fixture B"
    left["items"][0]["pair_relations"] = [
        {"task_ids": [1, 2], "label": "uncertain", "reason": "Missing distinction"}
    ]
    report = analyze(source, left, right)
    decisions = adjudication_template(report)
    decisions.update(
        annotation_status="human_reviewed",
        adjudicator="Fixture C",
        adjudicated_at="2026-10-08",
    )
    decisions["decisions"][0].update(
        value=[
            {
                "task_ids": [1, 2],
                "label": "uncertain",
                "reason": "Still insufficient detail",
            }
        ],
        rationale="Retain boundary",
    )
    result = adjudicate(source, left, right, decisions)
    assert result["audit"]["remaining_boundary_plan_count"] == 1
    assert (
        review_labels(source, result["review_packet"])["plans"][0]["quality"][
            "acceptable"
        ]
        is None
    )


def test_real_paired_comparison_keeps_uncertain_pairs_out_of_improvement_rates(
    tmp_path,
):
    source, packet = _reviews(tmp_path)
    source["provenance"] = "llm_samples"
    packet = export_review(source, tmp_path / "real-review.json")
    packet["annotation_status"] = "human_reviewed"
    for item in packet["items"]:
        item.update(
            reviewer="Fixture A",
            reviewed_at="2026-10-08",
            review_notes="Test labels",
            redundant_pairs=[],
            pair_relations=[
                {
                    "task_ids": [1, 2],
                    "label": "uncertain",
                    "reason": "Insufficient distinction",
                }
            ],
        )
        for task in item["tasks"]:
            task.update(
                relevance=2,
                granularity="appropriate",
                covered_dimensions=[
                    {1: "share", 2: "pricing", 3: "policy", 4: "growth"}[task["id"]]
                ],
                rationale="Test dimension",
            )
    result = compare_review(source, packet)
    assert result["paired_sample_count"] == 0
    assert result["boundary_paired_sample_count"] == 1
    assert result["improved_to_acceptable"] == 0
