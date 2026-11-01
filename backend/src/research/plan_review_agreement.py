"""Compare independent review labels and require explicit human adjudication."""

from __future__ import annotations

import argparse
import copy
import json
import math
import sys
from collections import Counter
from datetime import date
from itertools import combinations
from pathlib import Path
from typing import Any

from research.plan_sampling import (
    _digest,
    _write,
    export_review,
    review_labels,
    review_markdown,
)


def categorical_agreement(pairs: list[tuple[Any, Any]]) -> dict[str, Any]:
    """Compute exact agreement and unweighted Cohen kappa with explicit counts."""
    count = len(pairs)
    labels = sorted({value for pair in pairs for value in pair}, key=str)
    matrix = [
        [sum(a == left and b == right for a, b in pairs) for right in labels]
        for left in labels
    ]
    if not count:
        return {
            "unit_count": 0,
            "matched_count": 0,
            "agreement": None,
            "kappa": None,
            "labels": [],
            "confusion_matrix": [],
        }
    matches = sum(a == b for a, b in pairs)
    left_counts = Counter(a for a, _ in pairs)
    right_counts = Counter(b for _, b in pairs)
    observed = matches / count
    expected = (
        sum(left_counts[label] * right_counts[label] for label in labels) / count**2
    )
    return {
        "unit_count": count,
        "matched_count": matches,
        "agreement": observed,
        "kappa": None
        if math.isclose(expected, 1.0)
        else (observed - expected) / (1 - expected),
        "labels": labels,
        "confusion_matrix": matrix,
    }


def _pairs(plan: dict[str, Any]) -> set[tuple[int, int]]:
    """Normalize semantic redundancy labels independently of pair ordering."""
    return {tuple(sorted(pair["task_ids"])) for pair in plan["redundant_pairs"]} | {
        tuple(sorted(pair["task_ids"]))
        for pair in plan.get("pair_relations", [])
        if pair["label"] == "redundant"
    }


def _relation_labels(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Project explicit uncertainty without treating it as a negative pair."""
    positives = _pairs(plan)
    explicit = {
        tuple(sorted(pair["task_ids"])): pair for pair in plan.get("pair_relations", [])
    }
    return [
        {
            "task_ids": list(key),
            "label": explicit[key]["label"]
            if key in explicit
            else "redundant"
            if key in positives
            else "complementary",
        }
        for key in sorted(
            tuple(sorted(pair))
            for pair in combinations([task["id"] for task in plan["tasks"]], 2)
        )
    ]


def analyze(
    collection: dict[str, Any],
    left: dict[str, Any],
    right: dict[str, Any],
) -> dict[str, Any]:
    """Measure four-axis agreement between complete, distinct reviewer packets."""
    left_labels = review_labels(collection, left)
    right_labels = review_labels(collection, right)
    left_plans = {plan["candidate"]["id"]: plan for plan in left_labels["plans"]}
    right_plans = {plan["candidate"]["id"]: plan for plan in right_labels["plans"]}
    rubrics = {rubric["id"]: rubric for rubric in collection["rubrics"]}
    observations: dict[str, list[tuple[Any, Any]]] = {
        axis: []
        for axis in (
            "relevance",
            "granularity",
            "coverage_binding",
            "missing_dimension",
            "redundancy",
        )
    }
    disagreements: list[dict[str, Any]] = []
    positive_overlap = 0
    positive_union = 0
    boundary_ids = []

    def record(
        review_id: str,
        topic_id: str,
        axis: str,
        task_id: int | None,
        a: Any,
        b: Any,
        reasons: tuple[Any, Any],
    ) -> None:
        if a == b:
            return
        disagreements.append(
            {
                "id": _digest([review_id, axis, task_id])[:16],
                "review_id": review_id,
                "topic_id": topic_id,
                "axis": axis,
                "task_id": task_id,
                "left": a,
                "right": b,
                "left_rationale": reasons[0],
                "right_rationale": reasons[1],
            }
        )

    for review_id in sorted(left_plans):
        a, b = left_plans[review_id], right_plans[review_id]
        if a["reviewer"].strip().casefold() == b["reviewer"].strip().casefold():
            raise ValueError(
                "Independent packets require distinct reviewer identities for every plan."
            )
        topic_id = a["topic_id"]
        core = (
            a["quality"]["evaluation_set"] == b["quality"]["evaluation_set"] == "core"
        )
        if not core:
            boundary_ids.append(review_id)
        dimensions = [item["id"] for item in rubrics[topic_id]["required_dimensions"]]
        for task_a, task_b in zip(a["candidate"]["tasks"], b["candidate"]["tasks"]):
            for axis in ("relevance", "granularity"):
                if core:
                    observations[axis].append((task_a[axis], task_b[axis]))
                record(
                    review_id,
                    topic_id,
                    axis,
                    task_a["id"],
                    task_a[axis],
                    task_b[axis],
                    (task_a["rationale"], task_b["rationale"]),
                )
            coverage_a, coverage_b = (
                sorted(task_a["covered_dimensions"])
                if isinstance(task_a["covered_dimensions"], list)
                else "uncertain",
                sorted(task_b["covered_dimensions"])
                if isinstance(task_b["covered_dimensions"], list)
                else "uncertain",
            )
            for dimension in dimensions if core else []:
                observations["coverage_binding"].append(
                    (dimension in coverage_a, dimension in coverage_b)
                )
            record(
                review_id,
                topic_id,
                "coverage",
                task_a["id"],
                coverage_a,
                coverage_b,
                (task_a["rationale"], task_b["rationale"]),
            )
        for dimension in dimensions if core else []:
            observations["missing_dimension"].append(
                (
                    dimension in a["quality"]["missing_dimensions"],
                    dimension in b["quality"]["missing_dimensions"],
                )
            )
        pairs_a, pairs_b = _pairs(a["candidate"]), _pairs(b["candidate"])
        for pair in (
            combinations([task["id"] for task in a["candidate"]["tasks"]], 2)
            if core
            else []
        ):
            normalized = tuple(sorted(pair))
            observations["redundancy"].append(
                (normalized in pairs_a, normalized in pairs_b)
            )
        if core:
            positive_overlap += len(pairs_a & pairs_b)
            positive_union += len(pairs_a | pairs_b)
        record(
            review_id,
            topic_id,
            "redundancy",
            None,
            _relation_labels(a["candidate"]),
            _relation_labels(b["candidate"]),
            (
                a["candidate"]["pair_relations"] + a["candidate"]["redundant_pairs"],
                b["candidate"]["pair_relations"] + b["candidate"]["redundant_pairs"],
            ),
        )
    return {
        "source_sha256": _digest(collection),
        "left_sha256": _digest(left),
        "right_sha256": _digest(right),
        "provenance": collection["provenance"],
        "reviewed_plan_count": len(left_plans),
        "core_plan_count": len(left_plans) - len(boundary_ids),
        "boundary_plan_count": len(boundary_ids),
        "uncertain_plan_ratio": len(boundary_ids) / len(left_plans),
        "boundary_review_ids": boundary_ids,
        "metrics": {
            axis: categorical_agreement(pairs) for axis, pairs in observations.items()
        },
        "redundancy_positive_jaccard": positive_overlap / positive_union
        if positive_union
        else None,
        "redundancy_positive_union": positive_union,
        "needs_adjudication": bool(disagreements),
        "has_unresolved_boundaries": bool(boundary_ids),
        "disagreements": disagreements,
        "interpretation": "label_consistency_not_semantic_accuracy_or_verified_independence",
    }


def adjudication_template(report: dict[str, Any]) -> dict[str, Any]:
    """Prepare empty human decisions instead of resolving labels automatically."""
    return {
        "source_sha256": report["source_sha256"],
        "left_sha256": report["left_sha256"],
        "right_sha256": report["right_sha256"],
        "annotation_status": "unreviewed",
        "adjudicator": None,
        "adjudicated_at": None,
        "decisions": [
            {"id": item["id"], "value": None, "rationale": None}
            for item in report["disagreements"]
        ],
    }


def adjudicate(
    collection: dict[str, Any],
    left: dict[str, Any],
    right: dict[str, Any],
    decisions: dict[str, Any],
) -> dict[str, Any]:
    """Merge labels only after a named adjudicator resolves every disagreement."""
    report = analyze(collection, left, right)
    for key in ("source_sha256", "left_sha256", "right_sha256"):
        if decisions.get(key) != report[key]:
            raise ValueError("Adjudication refers to changed source annotations.")
    if decisions.get("annotation_status") != "human_reviewed":
        raise ValueError("Adjudication is not complete.")
    if (
        not isinstance(decisions.get("adjudicator"), str)
        or not decisions["adjudicator"].strip()
    ):
        raise ValueError("A named adjudicator is required.")
    if not isinstance(decisions.get("adjudicated_at"), str):
        raise ValueError("An adjudication date is required.")
    date.fromisoformat(decisions["adjudicated_at"])
    expected = {item["id"]: item for item in report["disagreements"]}
    entries = decisions.get("decisions", [])
    if len(entries) != len(expected) or {item["id"] for item in entries} != set(
        expected
    ):
        raise ValueError("Every disagreement needs exactly one decision.")
    merged = copy.deepcopy(left)
    items = {item["review_id"]: item for item in merged["items"]}
    for entry in entries:
        if (
            entry.get("value") is None
            or not isinstance(entry.get("rationale"), str)
            or not entry["rationale"].strip()
        ):
            raise ValueError("Every decision needs a value and non-empty rationale.")
        disagreement = expected[entry["id"]]
        item = items[disagreement["review_id"]]
        axis = disagreement["axis"]
        if axis == "redundancy":
            if any("label" in pair for pair in entry["value"]):
                item["pair_relations"] = entry["value"]
                item["redundant_pairs"] = []
            else:
                item["redundant_pairs"] = entry["value"]
                item["pair_relations"] = []
        else:
            task = next(
                task for task in item["tasks"] if task["id"] == disagreement["task_id"]
            )
            field = "covered_dimensions" if axis == "coverage" else axis
            task[field] = entry["value"]
            task["rationale"] += f"\nAdjudication ({axis}): {entry['rationale']}"
    for item in merged["items"]:
        item["reviewer"] = decisions["adjudicator"]
        item["reviewed_at"] = decisions["adjudicated_at"]
        item["review_notes"] += "\nAdjudicated after independent reviews."
    labels = review_labels(collection, merged)
    return {
        "review_packet": merged,
        "audit": {
            **decisions,
            "resolved_disagreement_count": len(entries),
            "remaining_boundary_plan_count": sum(
                plan["quality"]["evaluation_set"] == "boundary"
                for plan in labels["plans"]
            ),
            "evidence": report["disagreements"],
        },
    }


def prepare(collection: dict[str, Any], directory: Path) -> None:
    """Export two blank packets in distinct deterministic presentation orders."""
    if directory.exists():
        raise ValueError("Use a new directory to preserve existing reviewer work.")
    directory.mkdir(parents=True)
    for name, seed in (("reviewer_a", 42), ("reviewer_b", 73)):
        packet = export_review(collection, directory / f"{name}.json", seed=seed)
        (directory / f"{name}.md").write_text(review_markdown(packet), encoding="utf-8")


def main() -> None:
    """Prepare two-reviewer packets, measure agreement or import adjudication."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "agreement", "adjudicate"))
    parser.add_argument("--collection", type=Path, required=True)
    parser.add_argument("--left", type=Path)
    parser.add_argument("--right", type=Path)
    parser.add_argument("--decisions", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        collection = json.loads(args.collection.read_text(encoding="utf-8"))
        if args.command == "prepare":
            prepare(collection, args.output)
            sys.stdout.write("Two blank review packets prepared.\n")
            return
        if args.left is None or args.right is None:
            raise ValueError("Both completed review packets are required.")
        left = json.loads(args.left.read_text(encoding="utf-8"))
        right = json.loads(args.right.read_text(encoding="utf-8"))
        if args.output.exists():
            raise ValueError("Output exists; use a new path.")
        if args.command == "agreement":
            report = analyze(collection, left, right)
            _write(args.output, report)
            _write(
                args.output.with_suffix(".decisions.json"),
                adjudication_template(report),
            )
            sys.stdout.write(
                json.dumps({"disagreement_count": len(report["disagreements"])})
            )
        else:
            if args.decisions is None:
                raise ValueError("Completed adjudication decisions are required.")
            result = adjudicate(
                collection,
                left,
                right,
                json.loads(args.decisions.read_text(encoding="utf-8")),
            )
            _write(args.output, result["review_packet"])
            _write(args.output.with_suffix(".audit.json"), result["audit"])
            sys.stdout.write(
                json.dumps(
                    {
                        "resolved_disagreements": result["audit"][
                            "resolved_disagreement_count"
                        ]
                    }
                )
            )
    except Exception as exc:
        sys.stderr.write(f"Review workflow failed ({type(exc).__name__}).\n")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
