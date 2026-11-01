"""Verify fixed plan evaluation, annotation integrity and misleading repairs."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from research.plan_evaluation import (
    PlanEvaluationDataset,
    evaluate_dataset,
    load_dataset,
)

DATASET = Path(__file__).resolve().parents[1] / "evaluation" / "planner_v1.json"
BASELINE = DATASET.with_name("planner_v1_baseline.json")


def _payload():
    return json.loads(DATASET.read_text(encoding="utf-8"))


def test_seed_is_explicitly_draft_and_split_by_topic():
    dataset = load_dataset(DATASET)
    assert dataset.annotation_status == "ai_draft"
    assert not dataset.reviewers
    assert len(dataset.topics) == 8
    assert sum(len(topic.candidates) for topic in dataset.topics) == 48
    assert {topic.language for topic in dataset.topics} == {"zh", "en"}
    assert {topic.split for topic in dataset.topics} == {"development", "holdout"}


@pytest.mark.parametrize(
    "defect",
    [
        "unknown_dimension",
        "duplicate_id",
        "dangling_pair",
        "kind_conflict",
        "boolean_relevance",
        "unreviewed_gold",
    ],
)
def test_invalid_annotations_cannot_silently_enter_metrics(defect):
    payload = _payload()
    candidate = payload["topics"][0]["candidates"][0]
    if defect == "unknown_dimension":
        candidate["tasks"][0]["covered_dimensions"] = ["undefined"]
    elif defect == "duplicate_id":
        candidate["tasks"][1]["id"] = candidate["tasks"][0]["id"]
    elif defect == "dangling_pair":
        candidate["redundant_pairs"] = [
            {"task_ids": [1, 99], "rationale": "Bad reference"}
        ]
    elif defect == "kind_conflict":
        candidate["tasks"][0]["granularity"] = "too_coarse"
    elif defect == "boolean_relevance":
        candidate["tasks"][0]["relevance"] = True
    else:
        payload["annotation_status"] = "human_reviewed"
    with pytest.raises(ValidationError):
        PlanEvaluationDataset.model_validate(payload)


def test_default_replay_excludes_holdout_and_matches_checked_in_baseline():
    result = evaluate_dataset(load_dataset(DATASET))
    assert {row["split"] for row in result["candidates"]} == {"development"}
    assert result == json.loads(BASELINE.read_text(encoding="utf-8"))


def test_paraphrases_and_missing_dimensions_expose_lexical_blind_spots():
    result = evaluate_dataset(load_dataset(DATASET))
    rows = {(row["topic_id"], row["candidate_id"]): row for row in result["candidates"]}
    redundant = rows[("ev-market", "paraphrase-duplicate")]
    assert redundant["gate_verdict"] == "pass"
    assert redundant["reference_after"]["evaluation_set"] == "boundary"
    assert redundant["reference_after"]["acceptable"] is None
    assert redundant["reference_after"]["uncertain_pairs"] == [[1, 5]]
    omitted = rows[("ev-market", "omit-last-dimension")]
    assert omitted["gate_verdict"] == "pass"
    assert omitted["reference_after"]["missing_dimensions"] == ["growth"]


def test_positive_controls_distinguish_repair_from_retry_without_claiming_llm_success():
    result = evaluate_dataset(load_dataset(DATASET))
    for row in result["candidates"]:
        if row["candidate_id"] == "exact-duplicate-control":
            assert row["gate_metrics"]["dropped_duplicate_count"] == 1
            assert row["reference_after"]["acceptable"]
        elif row["candidate_id"] == "unrelated-plan-control":
            assert row["gate_verdict"] == "retry"
            assert not row["reference_after"]["acceptable"]
    assert result["evaluation_scope"] == "single_gate_pass_no_llm_retry_no_profile_trim"


def test_query_dedup_can_lose_a_distinct_dimension_and_is_reported():
    payload = _payload()
    topic = copy.deepcopy(payload["topics"][0])
    topic["topic"] = "alpha beta"
    topic["required_dimensions"] = [
        {"id": "alpha", "description": "Alpha aspect"},
        {"id": "beta", "description": "Beta aspect"},
    ]
    topic["task_count_range"] = [1, 4]
    candidate = copy.deepcopy(topic["candidates"][0])
    candidate["tasks"] = candidate["tasks"][:2]
    for task, dimension in zip(candidate["tasks"], ("alpha", "beta")):
        task.update(
            title=dimension,
            intent=dimension,
            query="shared search",
            covered_dimensions=[dimension],
        )
    topic["candidates"] = [candidate]
    payload["topics"] = [topic]
    result = evaluate_dataset(PlanEvaluationDataset.model_validate(payload))
    assert result["summary"]["false_alerts"] == 1
    assert result["summary"]["repairs_losing_dimensions"] == 1
    assert result["candidates"][0]["reference_after"]["missing_dimensions"] == ["beta"]


def test_empty_selected_split_fails_instead_of_reporting_zero_quality():
    payload = _payload()
    payload["topics"] = [
        topic for topic in payload["topics"] if topic["split"] == "development"
    ]
    with pytest.raises(ValueError, match="no candidates"):
        evaluate_dataset(PlanEvaluationDataset.model_validate(payload), split="holdout")


def test_cli_creates_valid_utf8_report_with_dataset_identity(tmp_path):
    output = tmp_path / "nested" / "results.json"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "research.plan_evaluation",
            "--dataset",
            str(DATASET),
            "--output",
            str(output),
        ],
        cwd=DATASET.parents[1],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["dataset_version"] == "planner-seed-v1.1.0"
    assert len(report["dataset_sha256"]) == 64
    assert report["summary"]["candidate_count"] == 36
