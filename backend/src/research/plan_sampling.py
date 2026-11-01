"""Collect production Planner plans and export blank, blinded review packets."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from datetime import date
from pathlib import Path
from time import monotonic
from typing import Any, cast

from dotenv import load_dotenv
from hello_agents import SimpleAgent

from config import Configuration
from models import ResearchState
from prompts import todo_planner_system_prompt
from research.plan_evaluation import (
    AnnotatedTask,
    EvaluationTopic,
    PairRelation,
    PlanCandidate,
    RedundantPair,
    load_dataset,
    reference_quality,
)
from research.plan_quality import PlanValidator
from research.plan_schema import parse_plan_payload
from research.telemetry import TelemetryHelloAgentsLLM, llm_telemetry_scope
from services.planner import PlanningService
from utils import strip_thinking_tokens

_BACKEND = Path(__file__).resolve().parents[2]
_DATASET = _BACKEND / "evaluation" / "planner_v1.json"


def _digest(value: object) -> str:
    """Return a stable content identity across JSON serialization layouts."""
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _write(path: Path, value: object) -> None:
    """Atomically persist an incremental local sampling artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


class _CaptureAgent:
    """Capture the public agent prompt and response without inspecting internals."""

    def __init__(
        self, delegate: SimpleAgent, request_options: dict[str, Any] | None = None
    ) -> None:
        """Bind a real planner agent and empty observation slots."""
        self.delegate = delegate
        self.request_options = request_options or {}
        self.prompt = ""
        self.response = ""

    def run(self, prompt: str) -> str:
        """Capture one invocation using the same SimpleAgent as production."""
        self.prompt = prompt
        self.response = ""
        self.response = self.delegate.run(prompt, **self.request_options)
        return self.response

    def clear_history(self) -> None:
        """Clear delegate history after every planning call."""
        self.delegate.clear_history()


class _UsageSink:
    """Receive safe provider usage records through the existing telemetry scope."""

    def __init__(self) -> None:
        """Create a per-attempt telemetry buffer."""
        self.records: list[dict[str, object]] = []

    def record_llm_telemetry(self, record: dict[str, object]) -> None:
        """Retain emitted usage observations without request credentials."""
        self.records.append(record)


def build_planner(
    config: Configuration, *, thinking: str = "provider_default"
) -> tuple[PlanningService, _CaptureAgent]:
    """Construct the production planner service with the configured provider."""
    provider = (config.llm_provider or "").strip()
    base_url = config.llm_base_url
    key = config.llm_api_key
    if provider == "ollama":
        base_url = config.sanitized_ollama_url()
        key = key or "ollama"
    elif provider == "lmstudio":
        base_url = config.lmstudio_base_url
    llm = TelemetryHelloAgentsLLM(
        model=config.llm_model_id or config.local_llm,
        provider=provider or None,
        api_key=key,
        base_url=base_url,
        temperature=0.0,
        timeout=config.llm_timeout,
        max_tokens=config.llm_max_tokens,
    )
    agent = SimpleAgent(
        name="PlannerEvaluation",
        llm=llm,
        system_prompt=todo_planner_system_prompt.strip(),
        enable_tool_calling=False,
    )
    options = (
        {"extra_body": {"thinking": {"type": "disabled"}}}
        if thinking == "disabled"
        else {}
    )
    capture = _CaptureAgent(agent, options)
    return PlanningService(cast(SimpleAgent, capture), config), capture


def collect(
    service: PlanningService,
    capture: _CaptureAgent,
    config: Configuration,
    *,
    dataset_path: Path,
    output: Path,
    iterations: int = 2,
    topic_limit: int | None = None,
) -> dict[str, Any]:
    """Collect bounded development samples and gate-triggered feedback attempts."""
    if not 1 <= iterations <= 5:
        raise ValueError("Iterations must be between 1 and 5.")
    if topic_limit is not None and topic_limit < 1:
        raise ValueError("Topic limit must be positive.")
    if output.exists():
        raise ValueError("Sampling output already exists; use a new run path.")
    dataset = load_dataset(dataset_path)
    topics = [topic for topic in dataset.topics if topic.split == "development"]
    if topic_limit is not None:
        topics = topics[:topic_limit]
    if not topics:
        raise ValueError("No development topics available for sampling.")
    validator = PlanValidator(
        overlap_threshold=config.plan_overlap_threshold,
        coverage_threshold=config.plan_coverage_threshold,
    )
    result: dict[str, Any] = {
        "schema_version": 1,
        "provenance": "llm_samples",
        "dataset_version": dataset.dataset_version,
        "rubric_sha256": _digest(
            [topic.model_dump(exclude={"candidates"}) for topic in topics]
        ),
        "rubrics": [topic.model_dump(exclude={"candidates"}) for topic in topics],
        "provider": config.llm_provider,
        "model": config.llm_model_id or config.local_llm,
        "temperature": 0.0,
        "request_options": capture.request_options,
        "max_output_tokens": config.llm_max_tokens,
        "timeout_seconds": config.llm_timeout,
        "system_prompt": todo_planner_system_prompt.strip(),
        "system_prompt_sha256": _digest(todo_planner_system_prompt.strip()),
        "planner_implementation_sha256": hashlib.sha256(
            (_BACKEND / "src" / "services" / "planner.py")
            .read_bytes()
            .replace(b"\r\n", b"\n")
            + Path(__file__)
            .with_name("plan_schema.py")
            .read_bytes()
            .replace(b"\r\n", b"\n")
        ).hexdigest(),
        "gate_configuration": {
            "overlap_threshold": config.plan_overlap_threshold,
            "coverage_threshold": config.plan_coverage_threshold,
            "max_retries": config.plan_validation_retries,
        },
        "sampling_scope": "planner_only_no_search_no_profile_trim_no_fallback",
        "iterations": iterations,
        "samples": [],
    }
    _write(output, result)
    for topic in topics:
        for iteration in range(1, iterations + 1):
            sample: dict[str, Any] = {
                "id": f"{topic.id}:{iteration}",
                "topic_id": topic.id,
                "iteration": iteration,
                "attempts": [],
                "status": "in_progress",
            }
            result["samples"].append(sample)
            feedback: tuple[str, ...] | None = None
            for ordinal in range(config.plan_validation_retries + 1):
                sink = _UsageSink()
                started = monotonic()
                attempt: dict[str, Any] = {"ordinal": ordinal}
                try:
                    with llm_telemetry_scope(sink, role="planner", retry_count=ordinal):
                        tasks = service.plan_todo_list(
                            ResearchState(research_topic=topic.topic),
                            validation_feedback=feedback,
                        )
                    plan_text = capture.response
                    if config.strip_thinking_tokens:
                        plan_text = strip_thinking_tokens(plan_text)
                    parsed = parse_plan_payload(plan_text)
                    gate = validator.validate(topic.topic, tasks)
                    attempt.update(
                        {
                            "tasks": [
                                {
                                    "id": task.id,
                                    "title": task.title,
                                    "intent": task.intent,
                                    "query": task.query,
                                }
                                for task in tasks
                            ],
                            "retained_tasks": [
                                {
                                    "id": task.id,
                                    "title": task.title,
                                    "intent": task.intent,
                                    "query": task.query,
                                }
                                for task in gate.tasks
                            ],
                            "parse_strategy": parsed.parse_strategy,
                            "rejected_task_count": parsed.rejected_task_count,
                            "truncated_task_count": parsed.truncated_task_count,
                            "gate_verdict": gate.verdict.value,
                            "gate_metrics": gate.metrics.as_dict(),
                        }
                    )
                    feedback = gate.retry_feedback
                except Exception as exc:
                    attempt["error_type"] = type(exc).__name__
                    sample["status"] = "error"
                attempt.update(
                    {
                        "elapsed_ms": round((monotonic() - started) * 1000, 2),
                        "prompt": capture.prompt,
                        "prompt_sha256": _digest(capture.prompt),
                        "raw_response": capture.response,
                        "usage": sink.records,
                    }
                )
                sample["attempts"].append(attempt)
                _write(output, result)
                if sample["status"] == "error" or not feedback:
                    break
            if sample["status"] != "error":
                sample["status"] = "collected"
            _write(output, result)
            if sample["status"] == "error":
                result["stop_reason"] = "provider_error"
                _write(output, result)
                return result
    return result


def seed_collection(dataset_path: Path, *, topic_limit: int = 1) -> dict[str, Any]:
    """Prepare synthetic seed plans for label review without claiming model output."""
    if topic_limit < 1:
        raise ValueError("Topic limit must be positive.")
    dataset = load_dataset(dataset_path)
    topics = [topic for topic in dataset.topics if topic.split == "development"][
        :topic_limit
    ]
    samples = []
    for topic in topics:
        for candidate in topic.candidates:
            tasks = [
                {
                    "id": task.id,
                    "title": task.title,
                    "intent": task.intent,
                    "query": task.query,
                }
                for task in candidate.tasks
            ]
            samples.append(
                {
                    "id": f"{topic.id}:{candidate.id}",
                    "topic_id": topic.id,
                    "status": "collected",
                    "attempts": [{"tasks": tasks, "retained_tasks": tasks}],
                }
            )
    return {
        "schema_version": 1,
        "provenance": "synthetic_seed",
        "dataset_version": dataset.dataset_version,
        "sampling_scope": "seed_annotation_review_no_model_no_gate",
        "rubrics": [topic.model_dump(exclude={"candidates"}) for topic in topics],
        "samples": samples,
    }


def export_review(
    collection: dict[str, Any], output: Path, *, seed: int = 42
) -> dict[str, Any]:
    """Export unlabeled, shuffled plans without model, phase or gate results."""
    if output.exists():
        raise ValueError("Review output already exists; use a new path.")
    source_hash = _digest(collection)
    items = []
    mapping = []
    for sample in collection["samples"]:
        if sample["status"] != "collected":
            continue
        initial = sample["attempts"][0]["tasks"]
        final = sample["attempts"][-1]["retained_tasks"]
        phases: tuple[tuple[str, Any], ...] = (
            (("initial", initial),)
            if collection["provenance"] == "synthetic_seed"
            else (("initial", initial), ("final", final))
        )
        for phase, tasks in phases:
            review_id = _digest([source_hash, sample["id"], phase])[:16]
            items.append(
                {
                    "review_id": review_id,
                    "topic_id": sample["topic_id"],
                    "tasks": [
                        {
                            **task,
                            "relevance": None,
                            "granularity": None,
                            "covered_dimensions": None,
                            "rationale": None,
                        }
                        for task in tasks
                    ],
                    "redundant_pairs": None,
                    "pair_relations": [],
                    "reviewer": None,
                    "reviewed_at": None,
                    "review_notes": None,
                }
            )
            mapping.append(
                {"review_id": review_id, "sample_id": sample["id"], "phase": phase}
            )
    random.Random(seed).shuffle(items)
    if not items:
        raise ValueError("No completed samples are available for review.")
    packet = {
        "schema_version": 1,
        "source_sha256": source_hash,
        "annotation_status": "unreviewed",
        "rubrics": collection["rubrics"],
        "items": items,
    }
    _write(output, packet)
    _write(
        output.with_suffix(".mapping.json"),
        {"source_sha256": source_hash, "items": mapping},
    )
    return packet


def review_markdown(packet: dict[str, Any]) -> str:
    """Render a readable blank worksheet without source labels."""

    def cell(value: str) -> str:
        return value.replace("|", "\\|").replace("\n", " ").replace("\r", " ")

    rubrics = {topic["id"]: topic for topic in packet["rubrics"]}
    lines = [
        "# Planner 四轴标注工作表",
        "",
        "这是未标注材料。请填写对应 JSON 的 reviewer、任务标签、redundant_pairs 和 review_notes。",
        "相关性：0 偏题 / 1 部分相关 / 2 直接相关；粒度：too_coarse / appropriate / too_fine。",
        "允许显式uncertain；null仍表示未标注。pair_relations可记录redundant/complementary/uncertain及reason。",
        "不确定计划进入Boundary Set，不计入Core Set主要指标；没有确定冗余须填redundant_pairs=[]。",
        "请先复核题目范围与维度；修改 rubric 时应创建新版本材料。",
        "",
    ]
    for item in packet["items"]:
        topic = rubrics[item["topic_id"]]
        lines.extend(
            [
                f"## 计划 {item['review_id']}",
                "",
                topic["topic"],
                "",
                f"范围：{topic['scope']}",
                "",
                "必需维度："
                + "；".join(
                    f"{d['id']}：{d['description']}"
                    for d in topic["required_dimensions"]
                ),
                f"任务数量建议：{topic['task_count_range'][0]}–{topic['task_count_range'][1]}",
                "",
                "| ID | 标题 | 意图 | 检索词 | 相关性 | 粒度 | 覆盖维度 | 理由 |",
                "| --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for task in item["tasks"]:
            lines.append(
                f"| {task['id']} | {cell(task['title'])} | {cell(task['intent'])} | {cell(task['query'])} | 待标 | 待标 | 待标 | 待标 |"
            )
        if not item["tasks"]:
            lines.append("| — | 空计划 | — | — | — | — | — | 请记录全部遗漏维度 |")
        lines.extend(
            ["", "冗余任务对及理由：待标。", "", "标注者 / 日期 / 复核说明：待填。", ""]
        )
    return "\n".join(lines)


def review_labels(collection: dict[str, Any], packet: dict[str, Any]) -> dict[str, Any]:
    """Import complete annotations while rejecting source and rubric drift."""
    if packet.get("source_sha256") != _digest(collection):
        raise ValueError("Review packet belongs to a different collection.")
    if packet.get("annotation_status") != "human_reviewed":
        raise ValueError("Review is incomplete; semantic comparison is unavailable.")
    if packet.get("annotation_author_type") == "ai":
        raise ValueError(
            "AI review drafts require actual human verification before import."
        )
    if packet.get("rubrics") != collection["rubrics"]:
        raise ValueError("Review cannot alter the frozen topic rubric.")
    rubrics = {topic["id"]: topic for topic in collection["rubrics"]}
    expected = {}
    for sample in collection["samples"]:
        if sample["status"] != "collected":
            continue
        phases: tuple[tuple[str, Any], ...] = (
            ("initial", sample["attempts"][0]["tasks"]),
            ("final", sample["attempts"][-1]["retained_tasks"]),
        )
        if collection["provenance"] == "synthetic_seed":
            phases = phases[:1]
        for phase, tasks in phases:
            key = _digest([_digest(collection), sample["id"], phase])[:16]
            expected[key] = (sample, phase, tasks)
    items = packet.get("items", [])
    if not expected:
        raise ValueError("No completed paired samples are available.")
    if len(items) != len(expected) or {item["review_id"] for item in items} != set(
        expected
    ):
        raise ValueError("Every paired plan must be reviewed exactly once.")
    plans = []
    for item in items:
        if not isinstance(item.get("reviewer"), str) or not item["reviewer"].strip():
            raise ValueError("Every plan requires a named reviewer.")
        if not isinstance(item.get("reviewed_at"), str):
            raise ValueError("Every plan requires a review date.")
        date.fromisoformat(item["reviewed_at"])
        if (
            not isinstance(item.get("review_notes"), str)
            or not item["review_notes"].strip()
        ):
            raise ValueError("Every plan requires review notes.")
        sample, phase, original = expected[item["review_id"]]
        if item["topic_id"] != sample["topic_id"]:
            raise ValueError("Reviewed topic identity was changed.")
        tasks = [AnnotatedTask.model_validate(task) for task in item["tasks"]]
        if [
            {
                "id": task.id,
                "title": task.title,
                "intent": task.intent,
                "query": task.query,
            }
            for task in tasks
        ] != original:
            raise ValueError("Review cannot alter captured task text or order.")
        pairs = [RedundantPair.model_validate(pair) for pair in item["redundant_pairs"]]
        relations = [
            PairRelation.model_validate(pair) for pair in item.get("pair_relations", [])
        ]
        if any(not task.rationale.strip() for task in tasks) or any(
            not pair.rationale.strip() for pair in pairs
        ):
            raise ValueError("Annotation rationales must not be blank.")
        rubric = rubrics[sample["topic_id"]]
        covered = {
            dimension
            for task in tasks
            if isinstance(task.covered_dimensions, list)
            for dimension in task.covered_dimensions
        }
        if set(d["id"] for d in rubric["required_dimensions"]) - covered:
            kind = "missing_dimension"
        elif any(
            isinstance(task.relevance, int) and task.relevance < 2 for task in tasks
        ):
            kind = "irrelevant"
        elif pairs or any(relation.label == "redundant" for relation in relations):
            kind = "redundant"
        elif any(task.granularity == "too_fine" for task in tasks):
            kind = "too_fine"
        elif (
            any(task.granularity == "too_coarse" for task in tasks)
            or not rubric["task_count_range"][0]
            <= len(tasks)
            <= rubric["task_count_range"][1]
        ):
            kind = "too_coarse"
        else:
            kind = "acceptable"
        candidate = PlanCandidate.model_validate(
            {
                "id": item["review_id"],
                "kind": kind,
                "tasks": [task.model_dump() for task in tasks],
                "redundant_pairs": [pair.model_dump() for pair in pairs],
                "pair_relations": [pair.model_dump() for pair in relations],
            }
        )
        topic = EvaluationTopic.model_validate(
            {**rubric, "candidates": [candidate.model_dump()]}
        )
        plans.append(
            {
                "sample_id": sample["id"],
                "phase": phase,
                "topic_id": sample["topic_id"],
                "candidate": candidate.model_dump(),
                "quality": reference_quality(
                    topic, candidate, {task.id for task in tasks}
                ),
                "reviewer": item["reviewer"],
                "reviewed_at": item["reviewed_at"],
                "review_notes": item["review_notes"],
            }
        )
    return {
        "source_sha256": _digest(collection),
        "annotation_status": "human_reviewed",
        "provenance": collection["provenance"],
        "plans": plans,
    }


def compare_review(
    collection: dict[str, Any], packet: dict[str, Any]
) -> dict[str, Any]:
    """Compare paired real plans only after complete reviewer annotation."""
    if collection["provenance"] != "llm_samples":
        raise ValueError("Paired model comparison requires real LLM samples.")
    labels = review_labels(collection, packet)
    qualities = {
        f"{plan['sample_id']}:{plan['phase']}": plan["quality"]
        for plan in labels["plans"]
    }
    rows = []
    boundary_rows = []
    for sample in collection["samples"]:
        if sample["status"] != "collected":
            continue
        before, after = (
            qualities[f"{sample['id']}:{phase}"] for phase in ("initial", "final")
        )
        if (
            before["evaluation_set"] == "boundary"
            or after["evaluation_set"] == "boundary"
        ):
            boundary_rows.append(
                {"sample_id": sample["id"], "before": before, "after": after}
            )
            continue
        rows.append(
            {
                "sample_id": sample["id"],
                "before": before,
                "after": after,
                "improved_to_acceptable": not before["acceptable"]
                and after["acceptable"],
                "regressed_from_acceptable": before["acceptable"]
                and not after["acceptable"],
                "axis_changes": {
                    axis: {
                        "resolved": axis in before["issues"]
                        and axis not in after["issues"],
                        "introduced": axis not in before["issues"]
                        and axis in after["issues"],
                    }
                    for axis in ("relevance", "redundancy", "coverage", "granularity")
                },
                "elapsed_ms": sum(
                    attempt["elapsed_ms"] for attempt in sample["attempts"]
                ),
                "total_tokens": sum(
                    record["total_tokens"]
                    for attempt in sample["attempts"]
                    for record in attempt["usage"]
                )
                if all(
                    attempt["usage"]
                    and all(
                        isinstance(record.get("total_tokens"), int)
                        for record in attempt["usage"]
                    )
                    for attempt in sample["attempts"]
                )
                else None,
            }
        )
    return {
        "source_sha256": _digest(collection),
        "provenance": collection["provenance"],
        "annotation_status": "human_reviewed",
        "paired_sample_count": len(rows),
        "boundary_paired_sample_count": len(boundary_rows),
        "total_paired_sample_count": len(rows) + len(boundary_rows),
        "uncertain_sample_ratio": len(boundary_rows) / (len(rows) + len(boundary_rows)),
        "boundary_samples": boundary_rows,
        "excluded_error_samples": sum(
            sample["status"] != "collected" for sample in collection["samples"]
        ),
        "improved_to_acceptable": sum(row["improved_to_acceptable"] for row in rows),
        "regressed_from_acceptable": sum(
            row["regressed_from_acceptable"] for row in rows
        ),
        "samples": rows,
    }


def main() -> None:
    """Collect real samples, export review packets or compare completed labels."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    sampler = commands.add_parser("collect")
    sampler.add_argument("--dataset", type=Path, default=_DATASET)
    sampler.add_argument("--output", type=Path, required=True)
    sampler.add_argument("--iterations", type=int, default=2)
    sampler.add_argument("--topic-limit", type=int)
    sampler.add_argument(
        "--thinking",
        choices=("provider_default", "disabled"),
        default="provider_default",
    )
    sampler.add_argument("--max-output-tokens", type=int, default=None)
    seed_parser = commands.add_parser("seed-review")
    seed_parser.add_argument("--dataset", type=Path, default=_DATASET)
    seed_parser.add_argument("--topic-limit", type=int, default=1)
    seed_parser.add_argument("--collection", type=Path, required=True)
    seed_parser.add_argument("--output", type=Path, required=True)
    reviewer = commands.add_parser("export-review")
    reviewer.add_argument("--collection", type=Path, required=True)
    reviewer.add_argument("--output", type=Path, required=True)
    comparison = commands.add_parser("compare")
    comparison.add_argument("--collection", type=Path, required=True)
    comparison.add_argument("--review", type=Path, required=True)
    comparison.add_argument("--output", type=Path, required=True)
    importer = commands.add_parser("import-review")
    importer.add_argument("--collection", type=Path, required=True)
    importer.add_argument("--review", type=Path, required=True)
    importer.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "collect":
            load_dotenv(_BACKEND / ".env")
            overrides: dict[str, Any] = {"llm_timeout": 45.0}
            if args.max_output_tokens is not None:
                if not 1 <= args.max_output_tokens <= 8192:
                    raise ValueError("Output token limit must be between 1 and 8192.")
                overrides["llm_max_tokens"] = args.max_output_tokens
            config = Configuration.from_env(overrides=overrides)
            service, capture = build_planner(config, thinking=args.thinking)
            result = collect(
                service,
                capture,
                config,
                dataset_path=args.dataset,
                output=args.output,
                iterations=args.iterations,
                topic_limit=args.topic_limit,
            )
            sys.stdout.write(
                json.dumps(
                    {
                        "samples": len(result["samples"]),
                        "errors": sum(
                            sample["status"] == "error" for sample in result["samples"]
                        ),
                    }
                )
            )
            if any(sample["status"] == "error" for sample in result["samples"]):
                raise SystemExit(2)
        elif args.command == "seed-review":
            if args.collection.exists() or args.output.exists():
                raise ValueError("Use new collection and review paths.")
            collection = seed_collection(args.dataset, topic_limit=args.topic_limit)
            _write(args.collection, collection)
            packet = export_review(collection, args.output)
            args.output.with_suffix(".md").write_text(
                review_markdown(packet), encoding="utf-8"
            )
            sys.stdout.write(json.dumps({"review_items": len(packet["items"])}))
        else:
            collection = json.loads(args.collection.read_text(encoding="utf-8"))
            if args.command == "export-review":
                packet = export_review(collection, args.output)
                args.output.with_suffix(".md").write_text(
                    review_markdown(packet), encoding="utf-8"
                )
                sys.stdout.write(json.dumps({"review_items": len(packet["items"])}))
            elif args.command == "import-review":
                labels = review_labels(
                    collection, json.loads(args.review.read_text(encoding="utf-8"))
                )
                _write(args.output, labels)
                sys.stdout.write(json.dumps({"reviewed_plans": len(labels["plans"])}))
            else:
                report = compare_review(
                    collection, json.loads(args.review.read_text(encoding="utf-8"))
                )
                _write(args.output, report)
                sys.stdout.write(
                    json.dumps({"paired_samples": report["paired_sample_count"]})
                )
    except Exception as exc:
        sys.stderr.write(f"Planner evaluation failed ({type(exc).__name__}).\n")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
