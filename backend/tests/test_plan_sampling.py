"""Verify planner collection and blind review integrity without network access."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from config import Configuration
from research.plan_sampling import (
    _CaptureAgent,
    collect,
    compare_review,
    export_review,
    review_labels,
    review_markdown,
    seed_collection,
)
from services.planner import PlanningService

DATASET = Path(__file__).resolve().parents[1] / "evaluation" / "planner_v1.json"


class _ScriptedAgent:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.prompts = []
        self.clears = 0

    def run(self, prompt):
        self.prompts.append(prompt)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response

    def clear_history(self):
        self.clears += 1


def _collection():
    result = seed_collection(DATASET)
    result["provenance"] = "llm_samples"
    result["samples"] = [result["samples"][0]]
    attempt = result["samples"][0]["attempts"][0]
    attempt.update(elapsed_ms=123.0, usage=[])
    attempt["retained_tasks"] = attempt["tasks"][:-1]
    return result


def _annotate(packet):
    result = copy.deepcopy(packet)
    result["annotation_status"] = "human_reviewed"
    for item in result["items"]:
        item.update(
            reviewer="Unit test reviewer",
            reviewed_at="2026-10-08",
            review_notes="Fixture labels only",
        )
        item["redundant_pairs"] = []
        for task in item["tasks"]:
            task.update(
                relevance=2,
                granularity="appropriate",
                covered_dimensions=[
                    {1: "share", 2: "pricing", 3: "policy", 4: "growth"}[task["id"]]
                ],
                rationale="Fixture dimension binding",
            )
    return result


def test_seed_review_hides_labels_and_has_single_plan_per_candidate(tmp_path):
    source = seed_collection(DATASET)
    packet = export_review(source, tmp_path / "review.json")
    assert len(packet["items"]) == 6
    assert packet["annotation_status"] == "unreviewed"
    assert "kind" not in json.dumps(packet)
    assert "provenance" not in packet
    assert "phase" not in json.dumps(packet)
    assert all(
        task["relevance"] is None for item in packet["items"] for task in item["tasks"]
    )
    worksheet = review_markdown(packet)
    assert "待标" in worksheet
    assert "exact-duplicate-control" not in worksheet
    assert (tmp_path / "review.mapping.json").exists()


def test_paired_review_reports_real_regression_without_filling_unknown_usage(tmp_path):
    source = _collection()
    packet = _annotate(export_review(source, tmp_path / "review.json"))
    result = compare_review(source, packet)
    assert result["paired_sample_count"] == 1
    assert result["regressed_from_acceptable"] == 1
    assert result["samples"][0]["after"]["missing_dimensions"] == ["growth"]
    assert result["samples"][0]["axis_changes"]["coverage"]["introduced"]
    assert result["samples"][0]["total_tokens"] is None


@pytest.mark.parametrize(
    "defect",
    [
        "unreviewed",
        "changed_text",
        "changed_rubric",
        "missing_plan",
        "missing_label",
        "missing_reviewer",
        "missing_date",
        "dangling_pair",
    ],
)
def test_incomplete_or_changed_review_cannot_generate_comparison(tmp_path, defect):
    source = _collection()
    packet = _annotate(export_review(source, tmp_path / "review.json"))
    item = packet["items"][0]
    if defect == "unreviewed":
        packet["annotation_status"] = "unreviewed"
    elif defect == "changed_text":
        item["tasks"][0]["query"] = "changed query"
    elif defect == "changed_rubric":
        packet["rubrics"] = copy.deepcopy(packet["rubrics"])
        packet["rubrics"][0]["scope"] = "Changed scope"
    elif defect == "missing_plan":
        packet["items"].pop()
    elif defect == "missing_label":
        item["tasks"][0]["relevance"] = None
    elif defect == "missing_reviewer":
        item["reviewer"] = " "
    elif defect == "missing_date":
        item["reviewed_at"] = None
    else:
        item["redundant_pairs"] = [{"task_ids": [1, 99], "rationale": "Dangling task"}]
    with pytest.raises((ValueError, ValidationError)):
        compare_review(source, packet)


def test_empty_generated_plan_is_reviewable_and_counts_all_missing_dimensions(tmp_path):
    source = _collection()
    source["samples"][0]["attempts"][0]["retained_tasks"] = []
    result = compare_review(
        source, _annotate(export_review(source, tmp_path / "review.json"))
    )
    assert len(result["samples"][0]["after"]["missing_dimensions"]) == 4


def test_seed_labels_can_be_imported_but_cannot_claim_model_improvement(tmp_path):
    source = seed_collection(DATASET)
    source["samples"] = [source["samples"][0]]
    packet = _annotate(export_review(source, tmp_path / "review.json"))
    assert len(review_labels(source, packet)["plans"]) == 1
    with pytest.raises(ValueError, match="real LLM"):
        compare_review(source, packet)


def test_collector_uses_production_feedback_and_bounded_retry(tmp_path):
    source = seed_collection(DATASET)
    config = Configuration.from_env(
        overrides={"plan_validation_retries": 1, "llm_api_key": "SECRET-SENTINEL"}
    )
    fake = _ScriptedAgent(
        [
            '{"tasks": [{"title":"Bread", "intent":"Bake", "query":"bread recipe"}]}',
            json.dumps({"tasks": source["samples"][0]["attempts"][0]["tasks"]}),
        ]
    )
    capture = _CaptureAgent(fake)
    result = collect(
        PlanningService(capture, config),
        capture,
        config,
        dataset_path=DATASET,
        output=tmp_path / "capture.json",
        iterations=1,
        topic_limit=1,
    )
    sample = result["samples"][0]
    assert sample["status"] == "collected"
    assert len(sample["attempts"]) == 2
    assert sample["attempts"][0]["gate_verdict"] == "retry"
    assert "计划校验反馈" in fake.prompts[1]
    assert fake.clears == 2
    assert "SECRET-SENTINEL" not in json.dumps(result)


def test_provider_error_stops_collection_and_is_not_a_semantic_plan(tmp_path):
    config = Configuration.from_env()
    fake = _ScriptedAgent([RuntimeError("SECRET-IN-ERROR")])
    capture = _CaptureAgent(fake)
    result = collect(
        PlanningService(capture, config),
        capture,
        config,
        dataset_path=DATASET,
        output=tmp_path / "capture.json",
        iterations=2,
    )
    assert result["stop_reason"] == "provider_error"
    assert len(result["samples"]) == 1
    assert result["samples"][0]["status"] == "error"
    assert "SECRET-IN-ERROR" not in json.dumps(result)
    with pytest.raises(ValueError, match="No completed"):
        export_review(result, tmp_path / "review.json")
