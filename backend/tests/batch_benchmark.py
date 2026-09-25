"""Batch benchmark: compare quality-gate ON vs OFF across default scenarios.

Usage:
    cd backend
    uv run python tests/batch_benchmark.py                    # 1 iteration each
    uv run python tests/batch_benchmark.py --iterations 3     # 3 iterations each
    uv run python tests/batch_benchmark.py --output results.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

from dotenv import load_dotenv

from config import Configuration
from harness import HarnessRunner, build_default_scenarios
from harness.models import HarnessRunRequest, HarnessRunResult
from models import TodoItem
from research.evaluation import OfflineEvaluationService
from research.ports import RunRepository

BACKEND_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BACKEND_DIR / ".env")


class QualityCheck(TypedDict):
    """Static summary-quality result."""

    passed: bool
    reasons: list[str]


class TaskMetrics(TypedDict, total=False):
    """Per-run task metrics emitted in benchmark rows."""

    total_tasks: int
    completed_tasks: int
    tasks_with_summary: int
    tasks_passing_quality: int
    total_retries: int
    summary_validity_rate: float


BenchmarkRow = dict[str, Any]


def _console(message: str = "") -> None:
    """Write one progress line without relying on the print builtin."""
    sys.stdout.write(f"{message}\n")
    sys.stdout.flush()


def _evaluate_persisted_run(
    repository: RunRepository,
    run_id: str,
    evaluator: OfflineEvaluationService,
) -> tuple[float, list[str | None]]:
    """Assess a completed canonical snapshot or propagate infrastructure failure."""
    snapshot = repository.load(run_id)
    assessment = evaluator.evaluate(snapshot)
    return assessment.score, [finding.code for finding in assessment.findings]


def _check_summary_quality_static(summary: str) -> QualityCheck:
    """Legacy basic-summary metric retained for historical benchmark comparison."""
    passed = True
    reasons = []
    if not summary or summary.strip() == "暂无可用信息":
        passed = False
        reasons.append("empty_or_fallback")
    if len(summary.strip()) < 30:
        passed = False
        reasons.append("too_short")
    has_structure = any(
        marker in summary for marker in ("###", "- ", "* ", "1. ", "2. ")
    )
    if not has_structure:
        passed = False
        reasons.append("no_structure")
    return {"passed": passed, "reasons": reasons}


def extract_task_metrics(todo_items: Sequence[TodoItem]) -> TaskMetrics:
    """Derive per-task quality metrics from the final todo list."""
    total = len(todo_items)
    if total == 0:
        return {
            "total_tasks": 0,
            "completed_tasks": 0,
            "tasks_with_summary": 0,
            "tasks_passing_quality": 0,
            "total_retries": 0,
            "summary_validity_rate": 0.0,
        }
    completed = 0
    with_summary = 0
    passing_quality = 0
    total_retries = 0
    for t in todo_items:
        status = getattr(t, "status", None)
        summary = getattr(t, "summary", None) or ""
        retries = getattr(t, "retry_count", 0) or 0
        if status == "completed":
            completed += 1
        if summary.strip() and summary.strip() != "暂无可用信息":
            with_summary += 1
        if _check_summary_quality_static(summary)["passed"]:
            passing_quality += 1
        total_retries += retries
    return {
        "total_tasks": total,
        "completed_tasks": completed,
        "tasks_with_summary": with_summary,
        "tasks_passing_quality": passing_quality,
        "total_retries": total_retries,
        "summary_validity_rate": passing_quality / total,
    }


def format_elapsed(seconds: float) -> str:
    """Human-readable elapsed time."""
    m, s = divmod(int(seconds), 60)
    if m:
        return f"{m}m{s}s"
    return f"{s}s"


def run_benchmark(
    iterations: int = 1,
    output_path: str = "benchmark_results.json",
) -> dict[str, Any]:
    """Execute the A/B benchmark comparing quality-gate ON vs OFF."""

    base_config = Configuration.from_env()
    scenarios = build_default_scenarios()
    runner_output_base = BACKEND_DIR / "benchmark_runs"
    evaluator = OfflineEvaluationService()

    all_results: list[BenchmarkRow] = []
    start_ts = time.time()

    configs = [
        ("quality_gate_ON", True),
        ("quality_gate_OFF", False),
    ]

    total_runs = len(scenarios) * len(configs) * iterations
    current_run = 0

    _console("=" * 72)
    _console(
        f"Batch benchmark: {len(scenarios)} scenarios × "
        f"{len(configs)} configs × {iterations} iters"
    )
    _console(f"Total runs: {total_runs}")
    _console(f"Search API: {base_config.search_api.value}")
    _console(f"LLM: {base_config.llm_provider} / {base_config.resolved_model()}")
    _console(f"Output: {runner_output_base}")
    _console("=" * 72)

    for scenario in scenarios:
        for config_label, gate_enabled in configs:
            config = base_config.model_copy(update={"enable_quality_gate": gate_enabled})

            for i in range(iterations):
                current_run += 1
                run_tag = f"[{current_run}/{total_runs}] {scenario.name} | {config_label} | iter={i+1}"
                _console(f"\n{'─' * 60}\n▶ {run_tag}")

                runner = HarnessRunner.build_default(
                    base_path=runner_output_base / scenario.name / config_label / f"iter_{i+1}",
                )
                request = HarnessRunRequest(
                    topic=scenario.topic,
                    config=config,
                    metadata={
                        "scenario": scenario.name,
                        "quality_gate": str(gate_enabled),
                        "iteration": str(i + 1),
                    },
                    permission_mode="default",
                )

                run_start = time.time()
                result: HarnessRunResult | None = None
                try:
                    result = runner.run(request)
                    elapsed = time.time() - run_start
                    status = result.status
                except Exception:
                    elapsed = time.time() - run_start
                    status = "error"
                    _console(
                        f"  ✗ FAILED after {format_elapsed(elapsed)}: "
                        "research execution failed"
                    )

                task_metrics: TaskMetrics = {}
                if result and result.output:
                    task_metrics = extract_task_metrics(result.output.todo_items)

                eval_score: float | None = None
                findings_codes: list[str | None] = []
                assessment_status = "not_evaluated"
                if result is not None and result.status == "completed":
                    eval_score, findings_codes = _evaluate_persisted_run(
                        runner.repository,
                        result.run_id,
                        evaluator,
                    )
                    assessment_status = "completed"

                run_data: BenchmarkRow = {
                    "run_id": request.run_id,
                    "scenario": scenario.name,
                    "topic": scenario.topic,
                    "quality_gate": gate_enabled,
                    "iteration": i + 1,
                    "status": status,
                    "elapsed_seconds": round(elapsed, 1),
                    "elapsed_display": format_elapsed(elapsed),
                    "evaluation_score": eval_score,
                    "assessment_status": assessment_status,
                    "findings_codes": findings_codes,
                    **task_metrics,
                }

                all_results.append(run_data)

                # Print one-line summary
                vrate = task_metrics.get("summary_validity_rate", 0)
                score_label = f"{eval_score:.2f}" if eval_score is not None else "n/a"
                _console(
                    f"  ✓ score={score_label}  "
                    f"tasks={task_metrics.get('total_tasks', '?')}  "
                    f"valid_rate={vrate:.0%}  "
                    f"retries={task_metrics.get('total_retries', 0)}  "
                    f"elapsed={format_elapsed(elapsed)}",
                )

    total_elapsed = time.time() - start_ts

    # ------------------------------------------------------------------
    # Aggregate analysis
    # ------------------------------------------------------------------
    def _avg(key: str, items: list[BenchmarkRow]) -> float:
        values = [
            value
            for row in items
            if isinstance((value := row.get(key)), (int, float))
            and not isinstance(value, bool)
        ]
        return sum(float(value) for value in values) / len(values) if values else 0.0

    gate_on = [r for r in all_results if r["quality_gate"] is True]
    gate_off = [r for r in all_results if r["quality_gate"] is False]

    on_validity = _avg("summary_validity_rate", gate_on)
    off_validity = _avg("summary_validity_rate", gate_off)
    on_score = _avg("evaluation_score", gate_on)
    off_score = _avg("evaluation_score", gate_off)
    on_retries = _avg("total_retries", gate_on)
    off_retries = _avg("total_retries", gate_off)
    on_completed = _avg("completed_tasks", gate_on)
    off_completed = _avg("completed_tasks", gate_off)
    on_total = _avg("total_tasks", gate_on)
    off_total = _avg("total_tasks", gate_off)

    # Per-scenario breakdown
    per_scenario: dict[str, dict[str, Any]] = {}
    for scenario in scenarios:
        s_on = [r for r in gate_on if r["scenario"] == scenario.name]
        s_off = [r for r in gate_off if r["scenario"] == scenario.name]
        per_scenario[scenario.name] = {
            "ON": {
                "runs": len(s_on),
                "avg_validity_rate": _avg("summary_validity_rate", s_on),
                "avg_score": _avg("evaluation_score", s_on),
                "avg_retries": _avg("total_retries", s_on),
            },
            "OFF": {
                "runs": len(s_off),
                "avg_validity_rate": _avg("summary_validity_rate", s_off),
                "avg_score": _avg("evaluation_score", s_off),
                "avg_retries": _avg("total_retries", s_off),
            },
            "delta_validity": _avg("summary_validity_rate", s_on) - _avg("summary_validity_rate", s_off),
            "delta_score": _avg("evaluation_score", s_on) - _avg("evaluation_score", s_off),
        }

    summary = {
        "benchmark_ts": datetime.now(timezone.utc).isoformat(),
        "total_elapsed_seconds": round(total_elapsed, 1),
        "total_elapsed_display": format_elapsed(total_elapsed),
        "config": {
            "search_api": base_config.search_api.value,
            "llm_provider": base_config.llm_provider,
            "llm_model": base_config.resolved_model(),
            "iterations_per_config": iterations,
        },
        "aggregate": {
            "quality_gate_ON": {
                "runs": len(gate_on),
                "avg_validity_rate": round(on_validity, 4),
                "avg_evaluation_score": round(on_score, 4),
                "avg_retries": round(on_retries, 1),
                "avg_completed_tasks": round(on_completed, 1),
                "avg_total_tasks": round(on_total, 1),
            },
            "quality_gate_OFF": {
                "runs": len(gate_off),
                "avg_validity_rate": round(off_validity, 4),
                "avg_evaluation_score": round(off_score, 4),
                "avg_retries": round(off_retries, 1),
                "avg_completed_tasks": round(off_completed, 1),
                "avg_total_tasks": round(off_total, 1),
            },
            "delta": {
                "validity_rate_improvement": round(on_validity - off_validity, 4),
                "evaluation_score_improvement": round(on_score - off_score, 4),
                "extra_retries": round(on_retries - off_retries, 1),
            },
        },
        "per_scenario": per_scenario,
        "all_runs": all_results,
    }

    # ------------------------------------------------------------------
    # Write output
    # ------------------------------------------------------------------
    output_file = Path(output_path)
    if not output_file.is_absolute():
        output_file = BACKEND_DIR / output_path
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # ------------------------------------------------------------------
    # Console report
    # ------------------------------------------------------------------
    _console(f"\n{'=' * 72}")
    _console("AGGREGATE RESULTS")
    _console("=" * 72)
    _console(f"\n  {'Metric':<35} {'Gate OFF':>10} {'Gate ON':>10} {'Delta':>10}")
    _console(f"  {'─' * 35} {'─' * 10} {'─' * 10} {'─' * 10}")
    _console(
        f"  {'摘要有效率 (validity rate)':<30} {off_validity:>10.1%} "
        f"{on_validity:>10.1%} {on_validity - off_validity:>+10.1%}"
    )
    _console(
        f"  {'评估分数 (evaluation score)':<30} {off_score:>10.2f} "
        f"{on_score:>10.2f} {on_score - off_score:>+10.2f}"
    )
    _console(
        f"  {'平均重试次数':<30} {off_retries:>10.1f} "
        f"{on_retries:>10.1f} {on_retries - off_retries:>+10.1f}"
    )
    _console(
        f"  {'平均完成任务数':<30} {off_completed:>10.1f} "
        f"{on_completed:>10.1f} {on_completed - off_completed:>+10.1f}"
    )

    _console("\n  Per-scenario breakdown:")
    for name, data in per_scenario.items():
        _console(f"  ── {name} ──")
        _console(
            f"    Validity: {data['OFF']['avg_validity_rate']:.1%} → "
            f"{data['ON']['avg_validity_rate']:.1%}  "
            f"(Δ={data['delta_validity']:+.1%})"
        )
        _console(
            f"    Score:    {data['OFF']['avg_score']:.2f} → "
            f"{data['ON']['avg_score']:.2f}  "
            f"(Δ={data['delta_score']:+.2f})"
        )

    _console(f"\n  Full results → {output_file}")
    _console(f"  Total time: {format_elapsed(total_elapsed)}")
    _console("=" * 72)

    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Quality gate A/B benchmark")
    parser.add_argument(
        "--iterations", "-n",
        type=int,
        default=1,
        help="Runs per scenario per config (default: 1)",
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default="benchmark_results.json",
        help="Output JSON path (default: benchmark_results.json)",
    )
    args = parser.parse_args()
    run_benchmark(iterations=args.iterations, output_path=args.output)
