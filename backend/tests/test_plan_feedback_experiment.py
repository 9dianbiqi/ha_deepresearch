"""Verify feedback isolation, fresh histories and preserved failed controls."""

from __future__ import annotations

import copy
import json

import pytest

from config import Configuration
from prompts import todo_planner_system_prompt
from research.plan_feedback_experiment import (
    prepare_experiment,
    prepare_retry,
    run_experiment,
)
from research.plan_sampling import _CaptureAgent


def _source():
    return {
        "samples": [
            {
                "id": "ev-market:1",
                "topic_id": "ev-market",
                "status": "collected",
                "attempts": [
                    {
                        "tasks": [
                            {
                                "id": 1,
                                "title": "Market",
                                "intent": "Collect market share",
                                "query": "alpha beta",
                            }
                        ],
                        "prompt": "Frozen topic prompt: alpha beta",
                    }
                ],
            }
        ],
        "rubrics": [{"id": "ev-market", "topic": "alpha beta"}],
        "model": "fixture",
        "provider": "custom",
        "temperature": 0.0,
        "max_output_tokens": 2000,
        "timeout_seconds": 45.0,
        "system_prompt": todo_planner_system_prompt.strip(),
        "system_prompt_sha256": "fixture-hash",
        "request_options": {"extra_body": {"thinking": {"type": "disabled"}}},
    }


class _Agent:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.histories = []
        self.history = []
        self.clear_count = 0

    def run(self, prompt, **kwargs):
        self.histories.append(list(self.history))
        self.history.append(prompt)
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result

    def clear_history(self):
        self.history.clear()
        self.clear_count += 1


def _plan():
    return '{"tasks":[{"title":"Alpha","intent":"Research alpha beta","query":"alpha beta"}]}'


def test_common_old_plan_context_and_frozen_prompt_are_identical_between_arms():
    source = _source()
    original = copy.deepcopy(source)
    experiment = prepare_experiment(source)
    control = experiment["conditions"][0]["prompt"]
    feedback = experiment["conditions"][1]["prompt"]
    assert control.endswith(source["samples"][0]["attempts"][0]["prompt"])
    assert feedback.endswith(source["samples"][0]["attempts"][0]["prompt"])
    assert control.split("\n\n")[0] == feedback.split("\n\n")[0]
    assert "盈利压力" not in control
    assert "盈利压力" in feedback and "下行风险" in feedback
    assert "原计划已通过词法门禁" in feedback
    assert "上一次规划未通过确定性校验" not in feedback
    assert (
        experiment["conditions"][1]["prompt"] == experiment["conditions"][2]["prompt"]
    )
    assert source == original


def test_each_condition_gets_empty_history_and_records_real_parse_result(tmp_path):
    experiment = prepare_experiment(_source())
    agent = _Agent([_plan(), "unparseable response", _plan()])
    capture = _CaptureAgent(agent, experiment["request_options"])
    result = run_experiment(
        experiment, capture, Configuration.from_env(), tmp_path / "result.json"
    )
    assert agent.histories == [[], [], []]
    assert agent.clear_count == 3
    assert [condition["status"] for condition in result["conditions"]] == [
        "collected",
        "invalid_plan",
        "collected",
    ]
    assert result["conditions"][1]["tasks"] == []
    assert result["conditions"][1]["usage"] == []


def test_provider_error_stops_and_pending_treatments_are_not_fake_results(tmp_path):
    class _RateError(RuntimeError):
        status_code = 429
        body = {"error": {"code": "1305", "message": "SECRET-SENTINEL"}}

    experiment = prepare_experiment(_source())
    capture = _CaptureAgent(
        _Agent([_RateError("SECRET-SENTINEL")]), experiment["request_options"]
    )
    result = run_experiment(
        experiment, capture, Configuration.from_env(), tmp_path / "result.json"
    )
    assert result["conditions"][0]["provider_error_code"] == "1305"
    assert result["conditions"][1]["status"] == "pending"
    assert "tasks" not in result["conditions"][1]
    assert "SECRET-SENTINEL" not in json.dumps(result)


def test_changed_request_settings_fail_before_any_network_or_artifact_write(tmp_path):
    experiment = prepare_experiment(_source())
    output = tmp_path / "result.json"
    with pytest.raises(ValueError, match="settings"):
        run_experiment(
            experiment, _CaptureAgent(_Agent([])), Configuration.from_env(), output
        )
    assert not output.exists()


def test_changed_system_prompt_and_invalid_baseline_are_rejected():
    source = _source()
    source["system_prompt"] = "Changed instructions"
    with pytest.raises(ValueError, match="System prompt changed"):
        prepare_experiment(source)
    source = _source()
    source["samples"][0]["attempts"][0]["tasks"] = []
    with pytest.raises(ValueError, match="non-empty"):
        prepare_experiment(source)


def test_retry_preserves_failed_attempt_and_does_not_recall_successful_conditions(
    tmp_path,
):
    experiment = prepare_experiment(_source())
    experiment["conditions"][0].update(status="collected", tasks=[{"id": 1}], usage=[])
    experiment["conditions"][1].update(status="collected", tasks=[{"id": 2}], usage=[])
    experiment["conditions"][2].update(
        status="provider_error", provider_error_code="1305"
    )
    original = copy.deepcopy(experiment)
    child = prepare_retry(experiment, "feedback_2")
    assert experiment == original
    assert child["request_failure_history"][0]["provider_error_code"] == "1305"
    agent = _Agent([_plan()])
    result = run_experiment(
        child,
        _CaptureAgent(agent, child["request_options"]),
        Configuration.from_env(),
        tmp_path / "retry.json",
    )
    assert len(agent.histories) == 1
    assert result["conditions"][0] == original["conditions"][0]
    assert result["conditions"][2]["status"] == "collected"
    with pytest.raises(ValueError, match="Only"):
        prepare_retry(result, "feedback_2")
