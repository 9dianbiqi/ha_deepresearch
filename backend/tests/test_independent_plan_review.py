"""Verify model separation, blind inputs and exhaustive uncertain judgments."""

from __future__ import annotations

import copy
import json

import pytest
from pydantic import SecretStr

from prompts import todo_planner_system_prompt
from research import independent_plan_review as review


def _role(role, model):
    return review.RoleSettings(
        role=role,
        model_id=model,
        base_url="https://fixture.invalid/v1",
        api_key=SecretStr("SECRET-SENTINEL"),
    )


def _rubric():
    return {
        "id": "ev-market",
        "topic": "Alpha beta",
        "scope": "two dimensions",
        "required_dimensions": [
            {"id": "a", "description": "Alpha"},
            {"id": "b", "description": "Beta"},
        ],
        "task_count_range": [1, 3],
    }


def _tasks():
    return [
        {"id": 1, "title": "Alpha", "intent": "Research alpha", "query": "alpha"},
        {"id": 2, "title": "Beta", "intent": "Research beta", "query": "beta"},
    ]


def _packet():
    packets, _ = review.blind_packets(
        _rubric(),
        [
            {
                "id": "control_1",
                "arm": "control",
                "model": "GENERATOR",
                "feedback": "SECRET-FEEDBACK",
                "tasks": _tasks(),
            }
        ],
    )
    return packets[0]


def _judgment():
    return {
        "task_labels": [
            {
                "task_id": i,
                "relevance": 2,
                "granularity": "appropriate",
                "reason": "Scope matches",
            }
            for i in (1, 2)
        ],
        "dimension_labels": [
            {
                "dimension_id": d,
                "status": "covered",
                "task_ids": [i],
                "reason": "Explicit target",
            }
            for d, i in [("a", 1), ("b", 2)]
        ],
        "pair_relations": [
            {
                "task_ids": [1, 2],
                "label": "complementary",
                "reason": "Different conclusions",
            }
        ],
    }


def _source():
    return {
        "samples": [
            {
                "id": "ev-market:1",
                "topic_id": "ev-market",
                "status": "collected",
                "attempts": [{"tasks": _tasks(), "prompt": "Frozen topic"}],
            }
        ],
        "rubrics": [_rubric()],
        "model": "planner-fixture",
        "provider": "custom",
        "temperature": 0.0,
        "max_output_tokens": 2000,
        "timeout_seconds": 45.0,
        "system_prompt": todo_planner_system_prompt.strip(),
        "system_prompt_sha256": review._digest(todo_planner_system_prompt.strip()),
        "request_options": {},
    }


@pytest.mark.parametrize(
    "a,b",
    [
        ("GLM-4.7-Flash", "glm_4.7_flash"),
        ("vendor/glm-4.7-flash", "glm-4.7-flash"),
        ("same", "SAME"),
    ],
)
def test_roles_and_provider_prefixes_do_not_make_identical_models_independent(a, b):
    with pytest.raises(ValueError, match="different models"):
        review.require_different_models(a, b)


def test_user_config_aliases_and_cross_endpoint_keys(monkeypatch):
    for name in tuple(review.os.environ):
        if (
            name.startswith(("PLAN_FEEDBACK_", "PLAN_JUDGE_", "FEEDBACK_", "JUDGE_"))
            or name == "PLANNER_MODEL"
        ):
            monkeypatch.delenv(name)
    monkeypatch.setenv("LLM_MODEL_ID", "glm-4.7-flash")
    monkeypatch.setenv("LLM_BASE_URL", "https://feedback.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "FEEDBACK-SECRET")
    monkeypatch.setenv("JUDGE_MODEL", "openai/gpt-oss-120b")
    monkeypatch.setenv("JUDGE_BASE_URL", "https://api.groq.com/openai/v1")
    with pytest.raises(ValueError, match="API_KEY"):
        review.load_roles()
    monkeypatch.setenv("JUDGE_API_KEY", "your_groq_api_key")
    with pytest.raises(ValueError, match="API_KEY"):
        review.load_roles()
    monkeypatch.setenv("JUDGE_API_KEY", "JUDGE-SECRET")
    feedback, judge = review.load_roles()
    assert feedback.api_key.get_secret_value() == "FEEDBACK-SECRET"
    assert judge.api_key.get_secret_value() == "JUDGE-SECRET"
    assert judge.model_id == "openai/gpt-oss-120b"
    assert "SECRET" not in json.dumps([feedback.model_dump(), judge.model_dump()])
    assert "SECRET" not in repr(judge)


def test_blind_packet_contains_neither_feedback_nor_generator_or_arm():
    packet = _packet()
    assert set(packet) == {
        "plan_id",
        "topic",
        "scope",
        "required_dimensions",
        "task_count_range",
        "tasks",
    }
    serialized = json.dumps(packet)
    assert (
        "SECRET-FEEDBACK" not in serialized
        and "GENERATOR" not in serialized
        and "control_1" not in serialized
    )


@pytest.mark.parametrize(
    "defect",
    [
        "missing_task",
        "missing_dimension",
        "duplicate_pair",
        "invented_task",
        "unsupported_coverage",
    ],
)
def test_incomplete_or_hallucinated_judge_references_cannot_be_accepted(defect):
    raw = _judgment()
    if defect == "missing_task":
        raw["task_labels"].pop()
    elif defect == "missing_dimension":
        raw["dimension_labels"].pop()
    elif defect == "duplicate_pair":
        raw["pair_relations"].append(copy.deepcopy(raw["pair_relations"][0]))
    elif defect == "invented_task":
        raw["dimension_labels"][0]["task_ids"] = [99]
    else:
        raw["dimension_labels"][0]["task_ids"] = []
    with pytest.raises(ValueError):
        review.validate_judgment(review.JudgeOutput.model_validate(raw), _packet())


def test_uncertain_projection_is_boundary_not_a_negative_or_zero_coverage():
    raw = _judgment()
    raw["dimension_labels"][0]["status"] = "uncertain"
    result = review.validate_judgment(review.JudgeOutput.model_validate(raw), _packet())
    assert result["evaluation_set"] == "boundary"
    assert result["acceptable"] is None and result["dimension_coverage"] is None
    assert result["confirmed_dimension_coverage_lower_bound"] == 0.5


def test_feedback_unknown_references_and_empty_replan_are_rejected():
    with pytest.raises(ValueError):
        review.FeedbackOutput.model_validate({"action": "replan", "findings": []})
    output = review.FeedbackOutput.model_validate(
        {
            "action": "replan",
            "findings": [
                {
                    "task_ids": [99],
                    "dimension_ids": ["a"],
                    "feedback": "Fix",
                    "reason": "Why",
                }
            ],
        }
    )
    with pytest.raises(ValueError, match="unknown"):
        review.validate_feedback(output, _tasks(), _rubric())


def test_provider_alias_returned_as_same_model_is_rejected_even_if_config_differs(
    tmp_path, monkeypatch
):
    calls = []

    def fake_request(role, system, payload):
        calls.append(role.role)
        result = {
            "status": "received",
            "finish_reason": "stop",
            "actual_model_id": "same-resolved-model",
        }
        result["raw_response"] = json.dumps(
            {"action": "keep", "findings": []}
            if role.role == "feedback"
            else _judgment()
        )
        return result

    monkeypatch.setattr(review, "request_model", fake_request)
    result = review.run_protocol(
        _source(),
        _role("feedback", "configured-A"),
        _role("judge", "configured-B"),
        tmp_path / "run",
    )
    assert calls == ["feedback", "judge"]
    assert result["status"] == "incomplete"
    assert not result.get("actual_model_separation_checked")


def test_feedback_and_judge_have_stateless_distinct_inputs_in_a_complete_protocol(
    tmp_path, monkeypatch
):
    seen = []

    def fake_request(role, system, payload):
        seen.append((role.role, copy.deepcopy(payload)))
        raw = (
            {"action": "keep", "findings": []}
            if role.role == "feedback"
            else _judgment()
        )
        return {
            "status": "received",
            "finish_reason": "stop",
            "actual_model_id": role.model_id,
            "raw_response": json.dumps(raw),
        }

    monkeypatch.setattr(review, "request_model", fake_request)
    result = review.run_protocol(
        _source(),
        _role("feedback", "feedback-model"),
        _role("judge", "judge-model"),
        tmp_path / "run",
    )
    assert result["status"] == "independent_model_review_complete"
    assert result["actual_model_separation_checked"]
    assert "rubric" in seen[0][1]
    assert set(seen[1][1]) == {
        "plan_id",
        "topic",
        "scope",
        "required_dimensions",
        "task_count_range",
        "tasks",
    }
    assert result["judge_requests"][0]["projection"]["acceptable"]


@pytest.mark.parametrize("planning_succeeds", [True, False])
def test_replan_path_hides_feedback_and_marks_missing_pairs(
    tmp_path, monkeypatch, planning_succeeds
):
    seen = []

    def fake_request(role, system, payload):
        seen.append((role.role, copy.deepcopy(payload)))
        raw = (
            {
                "action": "replan",
                "findings": [
                    {
                        "task_ids": [1],
                        "dimension_ids": ["a"],
                        "feedback": "SECRET-FEEDBACK",
                        "reason": "Scope gap",
                    }
                ],
            }
            if role.role == "feedback"
            else _judgment()
        )
        return {
            "status": "received",
            "finish_reason": "stop",
            "actual_model_id": role.model_id,
            "raw_response": json.dumps(raw),
        }

    def fake_plan(protocol, capture, config, output):
        assert "SECRET-FEEDBACK" not in protocol["conditions"][0]["prompt"]
        assert "SECRET-FEEDBACK" in protocol["conditions"][1]["prompt"]
        for i, condition in enumerate(protocol["conditions"]):
            condition["status"] = (
                "collected"
                if planning_succeeds
                else "provider_error"
                if i == 0
                else "pending"
            )
            if planning_succeeds:
                condition["tasks"] = _tasks()
        return protocol

    monkeypatch.setattr(review, "request_model", fake_request)
    monkeypatch.setattr(
        review, "build_planner", lambda *args, **kwargs: (None, object())
    )
    monkeypatch.setattr(review, "run_experiment", fake_plan)
    result = review.run_protocol(
        _source(),
        _role("feedback", "feedback-model"),
        _role("judge", "judge-model"),
        tmp_path / "run",
    )
    judge_inputs = [payload for role, payload in seen if role == "judge"]
    assert all("SECRET-FEEDBACK" not in json.dumps(payload) for payload in judge_inputs)
    assert len(judge_inputs) == (2 if planning_succeeds else 1)
    assert result["paired_comparison_available"] is planning_succeeds
    assert result["status"] == (
        "independent_model_review_complete"
        if planning_succeeds
        else "independent_review_complete_replan_incomplete"
    )


def test_keep_with_negative_judgment_is_a_disagreement_not_automatic_acceptance(
    tmp_path, monkeypatch
):
    def fake_request(role, system, payload):
        raw = (
            {"action": "keep", "findings": []}
            if role.role == "feedback"
            else _judgment()
        )
        if role.role == "judge":
            raw["task_labels"][1]["granularity"] = "too_coarse"
        return {
            "status": "received",
            "finish_reason": "stop",
            "actual_model_id": role.model_id,
            "raw_response": json.dumps(raw),
        }

    monkeypatch.setattr(review, "request_model", fake_request)
    result = review.run_protocol(
        _source(),
        _role("feedback", "feedback-model"),
        _role("judge", "judge-model"),
        tmp_path / "run",
    )
    assert result["model_decision_disagreement"]
    assert result["coordination_evaluation_set"] == "boundary"
    assert result["judge_requests"][0]["projection"]["evaluation_set"] == "core"
    assert not result["paired_comparison_available"]
