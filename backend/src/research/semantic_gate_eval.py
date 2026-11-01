"""Evaluate twelve obvious development fixtures with one offline model."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from time import sleep
from typing import Any

from dotenv import load_dotenv

from research.semantic_gate import Input, evaluate, validate_input


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Report abstentions and failures separately from fixture disagreement."""
    stats = {
        "attempted": len(rows),
        "correct_full_vector": 0,
        "uncertain": 0,
        "technical_failure": 0,
        "false_rejection": 0,
        "missed_failure": 0,
        "other_mismatch": 0,
    }
    for row in rows:
        result = row["result"]
        if result["status"] == "technical_failure":
            stats["technical_failure"] += 1
            continue
        judgment = result["judgment"]
        if judgment["overall"] == "uncertain":
            stats["uncertain"] += 1
        actual = {key: judgment[key]["status"] for key in row["expected"]}
        if actual == row["expected"]:
            stats["correct_full_vector"] += 1
        elif (
            all(value == "pass" for value in row["expected"].values())
            and judgment["overall"] == "fail"
        ):
            stats["false_rejection"] += 1
        elif "fail" in row["expected"].values() and judgment["overall"] == "pass":
            stats["missed_failure"] += 1
        elif judgment["overall"] != "uncertain":
            stats["other_mismatch"] += 1
    return stats


def main() -> None:
    """Run a bounded, incremental offline smoke benchmark."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path(__file__).resolve().parents[2]
        / "evaluation"
        / "semantic_gate_lite_dev12.json",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--interval-seconds", type=float, default=20)
    args = parser.parse_args()
    if args.output.exists() or not 0 <= args.interval_seconds <= 60:
        raise ValueError(
            "Use a new output path and an interval between 0 and 60 seconds."
        )
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    for case in dataset["cases"]:
        validate_input(
            Input.model_validate({"topic": case["topic"], "tasks": case["tasks"]})
        )
    report = {
        "dataset_version": dataset["version"],
        "label_provenance": dataset["label_provenance"],
        "human_gold": False,
        "mode": "offline; no production blocking or automatic planner revision",
        "repetitions": args.repetitions,
        "dataset_sha256": hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
        "observations": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for repetition in range(1, args.repetitions + 1):
        for case in dataset["cases"]:
            if report["observations"]:
                sleep(args.interval_seconds)
            result = evaluate(
                Input.model_validate({"topic": case["topic"], "tasks": case["tasks"]})
            )
            report["observations"].append(
                {
                    "case_id": case["id"],
                    "repetition": repetition,
                    "expected": case["expected"],
                    "input": {"topic": case["topic"], "tasks": case["tasks"]},
                    "result": result,
                }
            )
            report["summary"] = summarize(report["observations"])
            args.output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            sys.stdout.write(
                json.dumps(
                    {
                        "case": case["id"],
                        "status": result["status"],
                        "overall": result["overall"],
                    }
                )
                + "\n"
            )
            sys.stdout.flush()


if __name__ == "__main__":
    main()
