"""Run an offline, frozen-prompt control and semantic-feedback experiment."""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path
from time import monotonic
from typing import Any

from dotenv import load_dotenv

from config import Configuration
from models import TodoItem
from prompts import todo_planner_system_prompt
from research.plan_quality import PlanValidator
from research.plan_sampling import (
    _CaptureAgent,
    _digest,
    _UsageSink,
    _write,
    build_planner,
)
from research.plan_schema import parse_plan_payload
from research.telemetry import llm_telemetry_scope
from services.planner import PlanningService
from utils import strip_thinking_tokens

_FEEDBACK = (
    "上一版任务2只明确研究价格战和定价策略，没有明确研究盈利压力。请在计划整体中补充价格竞争对利润、毛利、成本及盈利能力的影响，并让intent和query明确指向这些研究内容。",
    "上一版任务4主要研究2026年的技术、智能化和出口等当前增长动力。请在计划整体中补充未来增长情景、需求预测、下行风险及不确定性，不能只列增长驱动；技术例子是待验证假设，不是已证实的贡献。",
)


def prepare_experiment(
    source: dict[str, Any], *, feedback_repetitions: int = 2
) -> dict[str, Any]:
    """Freeze common context and criteria before issuing any model request."""
    if not 1 <= feedback_repetitions <= 3:
        raise ValueError("Feedback repetitions must be between 1 and 3.")
    sample = source["samples"][0]
    attempt = sample["attempts"][0]
    if sample["status"] != "collected" or not attempt.get("tasks"):
        raise ValueError("The historical sample must contain a valid non-empty plan.")
    if source["system_prompt"] != todo_planner_system_prompt.strip():
        raise ValueError("System prompt changed; this is not a matched experiment.")
    common = (
        "## 上一版任务草案（仅作规划参考，不是已验证行业事实）\n"
        + json.dumps({"tasks": attempt["tasks"]}, ensure_ascii=False, indent=2)
        + "\n\n"
    )
    control = common + attempt["prompt"]
    feedback_block = PlanningService._format_validation_feedback(_FEEDBACK).replace(
        "## 计划校验反馈（上一次规划未通过确定性校验，请修正以下问题）",
        "## AI离线评审反馈（原计划已通过词法门禁，请补足以下语义覆盖缺口）",
    )
    treatment = common + feedback_block + "\n\n" + attempt["prompt"]
    rubric = next(
        item for item in source["rubrics"] if item["id"] == sample["topic_id"]
    )
    return {
        "schema_version": 1,
        "experiment": "semantic_feedback_single_topic_control",
        "historical_source_sha256": _digest(source),
        "historical_sample_id": sample["id"],
        "model": source["model"],
        "provider": source["provider"],
        "temperature": source["temperature"],
        "max_output_tokens": source["max_output_tokens"],
        "timeout_seconds": source["timeout_seconds"],
        "request_options": source["request_options"],
        "system_prompt_sha256": source["system_prompt_sha256"],
        "frozen_base_prompt_sha256": _digest(attempt["prompt"]),
        "rubric": rubric,
        "feedback": list(_FEEDBACK),
        "criteria": {
            "share": ["主要品牌市场份额与竞争位置"],
            "pricing": ["价格竞争或定价变化", "利润、毛利或成本压力"],
            "policy": ["政策或监管对竞争/市场的影响"],
            "growth": ["未来增长驱动", "未来预测、情景或不确定风险"],
            "coverage_scope": "whole plan; complementary tasks may jointly satisfy a dimension",
            "other_axes": ["task relevance", "semantic redundancy", "task granularity"],
        },
        "design": {
            "control": "old plan context plus frozen base prompt, without semantic feedback",
            "treatment": "same context and base prompt, plus the two feedback items",
            "historical_baseline": "descriptive only; lacked the common old-plan context",
            "condition_order": "control then feedback repeats; not randomized",
            "evaluation": "subsequent Codex AI review; not blind or human gold",
            "scope": "planner only; no web search, report, automatic gate retry or budget trim",
        },
        "conditions": [
            {
                "id": "control_1",
                "arm": "control",
                "prompt": control,
                "status": "pending",
            },
            *[
                {
                    "id": f"feedback_{i}",
                    "arm": "feedback",
                    "prompt": treatment,
                    "status": "pending",
                }
                for i in range(1, feedback_repetitions + 1)
            ],
        ],
    }


def run_experiment(
    experiment: dict[str, Any],
    capture: _CaptureAgent,
    config: Configuration,
    output: Path,
) -> dict[str, Any]:
    """Run each pre-registered condition once and preserve failed observations."""
    if output.exists():
        raise ValueError("Output exists; use a new run path.")
    if capture.request_options != experiment["request_options"]:
        raise ValueError("Thinking/request settings do not match the baseline.")
    if experiment["temperature"] != 0.0:
        raise ValueError("This runner only supports the production temperature 0.")
    validator = PlanValidator(
        overlap_threshold=config.plan_overlap_threshold,
        coverage_threshold=config.plan_coverage_threshold,
    )
    _write(output, experiment)
    for condition in experiment["conditions"]:
        if condition["status"] != "pending":
            continue
        sink = _UsageSink()
        started = monotonic()
        try:
            with llm_telemetry_scope(sink, role="planner"):
                response = capture.run(condition["prompt"])
            text = (
                strip_thinking_tokens(response)
                if config.strip_thinking_tokens
                else response
            )
            plan = parse_plan_payload(text)
            tasks = [
                {
                    "id": index,
                    "title": task.title,
                    "intent": task.intent,
                    "query": task.query,
                }
                for index, task in enumerate(plan.tasks, 1)
            ]
            gate = validator.validate(
                experiment["rubric"]["topic"],
                [
                    TodoItem(
                        id=index, title=task.title, intent=task.intent, query=task.query
                    )
                    for index, task in enumerate(plan.tasks, 1)
                ],
            )
            condition.update(
                {
                    "status": "collected" if tasks else "invalid_plan",
                    "raw_response": response,
                    "tasks": tasks,
                    "parse_strategy": plan.parse_strategy,
                    "rejected_task_count": plan.rejected_task_count,
                    "truncated_task_count": plan.truncated_task_count,
                    "gate_verdict": gate.verdict.value,
                    "gate_metrics": gate.metrics.as_dict(),
                    "retained_task_ids": [task.id for task in gate.tasks],
                }
            )
        except Exception as exc:
            condition.update(status="provider_error", error_type=type(exc).__name__)
            status = getattr(exc, "status_code", None)
            if isinstance(status, int):
                condition["status_code"] = status
            body = getattr(exc, "body", {})
            if isinstance(body, dict):
                error = body.get("error", body)
                code = str(error.get("code", "")) if isinstance(error, dict) else ""
                if re.fullmatch(r"[0-9]{1,12}", code):
                    condition["provider_error_code"] = code
        finally:
            capture.clear_history()
            condition["elapsed_ms"] = round((monotonic() - started) * 1000, 2)
            condition["usage"] = sink.records
            condition["prompt_sha256"] = _digest(condition["prompt"])
            _write(output, experiment)
        sys.stdout.write(
            json.dumps(
                {
                    "condition": condition["id"],
                    "status": condition["status"],
                    "task_count": len(condition.get("tasks", [])),
                }
            )
            + "\n"
        )
        sys.stdout.flush()
        if condition["status"] == "provider_error":
            experiment["stop_reason"] = (
                "provider_error; uncalled conditions remain pending"
            )
            _write(output, experiment)
            break
    return experiment


def prepare_retry(prior: dict[str, Any], condition_id: str) -> dict[str, Any]:
    """Preserve provider failures while retrying only one unfulfilled request."""
    result = copy.deepcopy(prior)
    condition = next(
        (row for row in result["conditions"] if row["id"] == condition_id), None
    )
    if condition is None or condition["status"] not in {"provider_error", "pending"}:
        raise ValueError("Only a provider-failed or uncalled condition can be retried.")
    history = result.setdefault("request_failure_history", [])
    if condition["status"] == "provider_error":
        history.append(copy.deepcopy(condition))
    replacement = {key: condition[key] for key in ("id", "arm", "prompt")}
    replacement["status"] = "pending"
    result["conditions"] = [
        replacement if row["id"] == condition_id else row
        for row in result["conditions"]
    ]
    result["parent_run_sha256"] = _digest(prior)
    result["retry_scope"] = (
        f"only {condition_id}; successful observations inherited, not recalled"
    )
    result.pop("stop_reason", None)
    return result


def main() -> None:
    """Run a matched semantic-feedback experiment from one recorded sample."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--feedback-repetitions", type=int, default=2)
    parser.add_argument("--retry-run", type=Path)
    parser.add_argument("--retry-condition")
    args = parser.parse_args()
    try:
        source = json.loads(args.baseline.read_text(encoding="utf-8"))
        if args.retry_run is not None:
            if not args.retry_condition:
                raise ValueError("Specify the exact condition to retry.")
            prior = json.loads(args.retry_run.read_text(encoding="utf-8"))
            if prior["historical_source_sha256"] != _digest(source):
                raise ValueError("Retry baseline does not match the original source.")
            if prior["system_prompt_sha256"] != _digest(
                todo_planner_system_prompt.strip()
            ):
                raise ValueError("System prompt changed since the original run.")
            experiment = prepare_retry(prior, args.retry_condition)
        else:
            experiment = prepare_experiment(
                source, feedback_repetitions=args.feedback_repetitions
            )
        load_dotenv(Path(__file__).resolve().parents[2] / ".env")
        config = Configuration.from_env(
            overrides={
                "llm_provider": source["provider"],
                "llm_model_id": source["model"],
                "llm_max_tokens": source["max_output_tokens"],
                "llm_timeout": source["timeout_seconds"],
            }
        )
        thinking = (
            "disabled"
            if source["request_options"]
            == {"extra_body": {"thinking": {"type": "disabled"}}}
            else "provider_default"
        )
        _, capture = build_planner(config, thinking=thinking)
        result = run_experiment(experiment, capture, config, args.output)
        if any(
            condition["status"] != "collected" for condition in result["conditions"]
        ):
            raise SystemExit(2)
    except Exception as exc:
        sys.stderr.write(f"Feedback experiment failed ({type(exc).__name__}).\n")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
