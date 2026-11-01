"""Verify simple fault localization and fail-open semantic observations."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from research import semantic_gate as gate
from research.semantic_gate_eval import summarize


def _input():
    return gate.Input(
        topic="研究市场份额和盈利能力。",
        tasks=[
            gate.Task(id=1, title="份额", intent="研究市场份额", query="市场份额"),
            gate.Task(id=2, title="副本", intent="研究市场份额", query="份额"),
        ],
    )


def _result():
    return {
        "coverage": {"status": "pass", "missing": []},
        "redundancy": {"status": "pass", "duplicate_pairs": []},
        "relevance": {"status": "pass", "irrelevant_tasks": []},
        "overall": "pass",
        "feedback": "",
        "evidence": [],
    }


def test_coverage_absence_uses_topic_quote_not_invented_task():
    r = _result()
    r.update(
        coverage={
            "status": "fail",
            "missing": ["盈利能力"],
            "checked_task_ids": [1, 2],
        },
        overall="fail",
        feedback="补充盈利能力任务",
        evidence=[
            {
                "check": "coverage",
                "task_id": None,
                "field": "topic",
                "quote": "盈利能力",
            }
        ],
    )
    gate.validate_judgment(gate.Judgment.model_validate(r), _input())


@pytest.mark.parametrize(
    "defect",
    [
        "unknown_task",
        "fabricated_quote",
        "wrong_field",
        "missing_peer",
        "overall_conflict",
        "empty_feedback",
    ],
)
def test_failure_requires_valid_locations_and_quotes(defect):
    r = _result()
    r.update(
        redundancy={"status": "fail", "duplicate_pairs": [[1, 2]]},
        overall="fail",
        feedback="合并1和2",
        evidence=[
            {
                "check": "redundancy",
                "task_id": i,
                "field": "intent",
                "quote": "研究市场份额",
            }
            for i in (1, 2)
        ],
    )
    if defect == "unknown_task":
        r["redundancy"]["duplicate_pairs"] = [[1, 99]]
    elif defect == "fabricated_quote":
        r["evidence"][0]["quote"] = "盈利分析"
    elif defect == "wrong_field":
        r["evidence"][0]["field"] = "query"
    elif defect == "missing_peer":
        r["evidence"].pop()
    elif defect == "overall_conflict":
        r["overall"] = "pass"
    else:
        r["feedback"] = " "
    with pytest.raises(ValueError):
        gate.validate_judgment(gate.Judgment.model_validate(r), _input())


def test_uncertain_is_not_failure():
    r = _result()
    r["redundancy"]["status"] = "uncertain"
    r["overall"] = "uncertain"
    assert (
        gate.parse_response("```json\n" + json.dumps(r) + "\n```", _input()).overall
        == "uncertain"
    )


def test_credentials_are_not_reused_for_different_endpoints(monkeypatch):
    monkeypatch.setenv("SEMANTIC_GATE_MODEL", "fixture")
    monkeypatch.setenv("SEMANTIC_GATE_BASE_URL", "https://other.invalid")
    monkeypatch.delenv("SEMANTIC_GATE_API_KEY", raising=False)
    monkeypatch.setenv("PLAN_FEEDBACK_BASE_URL", "https://feedback.invalid")
    monkeypatch.setenv("PLAN_FEEDBACK_API_KEY", "SECRET-SENTINEL")
    result = gate.evaluate(_input())
    assert result["status"] == "technical_failure" and result["overall"] == "uncertain"
    assert not result["production_blocked"] and not result["replan_recommended"]
    assert "SECRET-SENTINEL" not in json.dumps(result)


def test_bad_model_json_never_blocks_or_requests_replanning(monkeypatch):
    class Client:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=self)

        def create(self, **kwargs):
            return SimpleNamespace(
                model="fixture",
                usage=None,
                choices=[
                    SimpleNamespace(
                        finish_reason="stop",
                        message=SimpleNamespace(content="bad json"),
                    )
                ],
            )

    monkeypatch.setattr(gate, "OpenAI", Client)
    monkeypatch.setenv("SEMANTIC_GATE_API_KEY", "fake")
    result = gate.evaluate(_input())
    assert result["status"] == "technical_failure" and not result["replan_recommended"]


def test_twelve_cases_are_two_pass_and_two_fail_per_check():
    data = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "evaluation"
            / "semantic_gate_lite_dev12.json"
        ).read_text(encoding="utf-8")
    )
    assert not data["human_gold"] and len(data["cases"]) == 12
    for axis in ("coverage", "redundancy", "relevance"):
        cases = [c for c in data["cases"] if c["check"] == axis]
        assert sum(c["expected"][axis] == "pass" for c in cases) == 2
        assert sum(c["expected"][axis] == "fail" for c in cases) == 2


def test_metrics_separate_uncertain_and_technical_failure():
    expected = {k: "pass" for k in ("coverage", "redundancy", "relevance")}
    r = _result()
    r["coverage"]["status"] = "uncertain"
    r["overall"] = "uncertain"
    stats = summarize(
        [
            {"expected": expected, "result": {"status": "valid", "judgment": r}},
            {"expected": expected, "result": {"status": "technical_failure"}},
        ]
    )
    assert (
        stats["correct_full_vector"] == 0
        and stats["uncertain"] == 1
        and stats["technical_failure"] == 1
    )
