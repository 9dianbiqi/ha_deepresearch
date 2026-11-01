"""Replay fixed, annotated plans against the deterministic Plan Gate offline.

Reference labels are supplied annotations, never inferred semantic judgments.
This evaluator does not call a planner, search provider, embedding or judge.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models import TodoItem
from research.plan_quality import PlanValidator, estimate_complexity
from research.plan_schema import ResearchPlanTask

_DEFAULT_DATASET = (
    Path(__file__).resolve().parents[2] / "evaluation" / "planner_v1.json"
)


class AnnotationModel(BaseModel):
    """Reject unknown fields and coercion in evaluation annotations."""

    model_config = ConfigDict(extra="forbid", strict=True)


class Dimension(AnnotationModel):
    """One required answer dimension defined by the topic's explicit scope."""

    id: str = Field(min_length=1)
    description: str = Field(min_length=1)


class AnnotatedTask(AnnotationModel):
    """One plan task plus reviewable relevance, coverage and grain labels."""

    id: int = Field(gt=0)
    title: str = Field(min_length=1)
    intent: str = Field(min_length=1)
    query: str = Field(min_length=1)
    relevance: Literal[0, 1, 2, "uncertain"]
    granularity: Literal["too_coarse", "appropriate", "too_fine", "uncertain"]
    covered_dimensions: list[str] | Literal["uncertain"]
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_task(self) -> AnnotatedTask:
        """Enforce the production task boundary and unique dimension bindings."""
        if isinstance(self.relevance, bool):
            raise ValueError("Relevance must be an integer score, not a boolean.")
        ResearchPlanTask(title=self.title, intent=self.intent, query=self.query)
        if isinstance(self.covered_dimensions, list) and len(
            set(self.covered_dimensions)
        ) != len(self.covered_dimensions):
            raise ValueError("Task dimension bindings must be unique.")
        if self.relevance == 0 and self.covered_dimensions:
            raise ValueError("Irrelevant tasks cannot cover required dimensions.")
        return self


class RedundantPair(AnnotationModel):
    """A semantically redundant task pair and the reviewer's reason."""

    task_ids: list[int] = Field(min_length=2, max_length=2)
    rationale: str = Field(min_length=1)


class PairRelation(AnnotationModel):
    """Record an explicit pair relation, including evidence insufficiency."""

    task_ids: list[int] = Field(min_length=2, max_length=2)
    label: Literal["redundant", "complementary", "uncertain"]
    reason: str = Field(min_length=1)


class PlanCandidate(AnnotationModel):
    """A fixed plan candidate with explicit per-task and pair annotations."""

    id: str = Field(min_length=1)
    kind: Literal[
        "acceptable",
        "missing_dimension",
        "redundant",
        "irrelevant",
        "too_coarse",
        "too_fine",
        "boundary",
    ]
    tasks: list[AnnotatedTask] = Field(max_length=16)
    redundant_pairs: list[RedundantPair]
    pair_relations: list[PairRelation] = Field(default_factory=list)
    evaluation_set: Literal["core", "boundary"] = "core"

    @model_validator(mode="after")
    def route_uncertainty(self) -> PlanCandidate:
        """Derive the evaluation set rather than trusting a core declaration."""
        uncertain = any(
            task.relevance == "uncertain"
            or task.granularity == "uncertain"
            or task.covered_dimensions == "uncertain"
            for task in self.tasks
        ) or any(pair.label == "uncertain" for pair in self.pair_relations)
        self.evaluation_set = "boundary" if uncertain else "core"
        if uncertain:
            self.kind = "boundary"
        return self


class EvaluationTopic(AnnotationModel):
    """A topic rubric shared by all candidates to avoid candidate-specific truth."""

    id: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    language: Literal["zh", "en"]
    split: Literal["development", "holdout"]
    topic: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    required_dimensions: list[Dimension] = Field(min_length=1)
    task_count_range: list[int] = Field(min_length=2, max_length=2)
    candidates: list[PlanCandidate] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_references(self) -> EvaluationTopic:
        """Reject duplicate IDs, dangling labels and incompatible grain bounds."""
        dimensions = {item.id for item in self.required_dimensions}
        if len(dimensions) != len(self.required_dimensions):
            raise ValueError("Dimension IDs must be unique within a topic.")
        low, high = self.task_count_range
        if not 1 <= low <= high <= 16:
            raise ValueError("Task count bounds must satisfy 1 <= low <= high <= 16.")
        if len({item.id for item in self.candidates}) != len(self.candidates):
            raise ValueError("Candidate IDs must be unique within a topic.")
        for candidate in self.candidates:
            task_ids = {item.id for item in candidate.tasks}
            if len(task_ids) != len(candidate.tasks):
                raise ValueError("Task IDs must be unique within a candidate.")
            for task in candidate.tasks:
                if (
                    isinstance(task.covered_dimensions, list)
                    and not set(task.covered_dimensions) <= dimensions
                ):
                    raise ValueError("Unknown task dimension binding.")
            seen: set[tuple[int, ...]] = set()
            for pair in candidate.redundant_pairs:
                key = tuple(sorted(pair.task_ids))
                if len(set(key)) != 2 or not set(key) <= task_ids or key in seen:
                    raise ValueError(
                        "Redundant pairs must be unique, distinct existing tasks."
                    )
                seen.add(key)
            relation_keys: set[tuple[int, ...]] = set()
            for relation in candidate.pair_relations:
                key = tuple(sorted(relation.task_ids))
                if (
                    len(set(key)) != 2
                    or not set(key) <= task_ids
                    or key in relation_keys
                ):
                    raise ValueError(
                        "Pair relations must identify distinct existing tasks exactly once."
                    )
                if not relation.reason.strip():
                    raise ValueError("Pair relation reasons must not be blank.")
                if key in seen and relation.label != "redundant":
                    raise ValueError(
                        "Pair relation conflicts with a declared redundant pair."
                    )
                relation_keys.add(key)
            if candidate.evaluation_set == "boundary":
                continue
            if candidate.kind == "boundary":
                raise ValueError(
                    "Boundary candidates require explicit uncertain labels."
                )
            issues = reference_quality(self, candidate, task_ids)["issues"]
            if (candidate.kind == "acceptable") != (not issues):
                raise ValueError("Candidate kind conflicts with reference annotations.")
            expected_issue = {
                "missing_dimension": "coverage",
                "redundant": "redundancy",
                "irrelevant": "relevance",
                "too_coarse": "granularity",
                "too_fine": "granularity",
            }.get(candidate.kind)
            if expected_issue is not None and expected_issue not in issues:
                raise ValueError("Candidate kind must have its named reference issue.")
        return self


class PlanEvaluationDataset(AnnotationModel):
    """Versioned synthetic seed corpus with honest annotation provenance."""

    schema_version: Literal[1]
    dataset_version: str = Field(min_length=1)
    provenance: Literal["synthetic_seed"]
    annotation_status: Literal["ai_draft", "human_reviewed"]
    annotation_author: str = Field(min_length=1)
    reviewers: list[str]
    topics: list[EvaluationTopic] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_dataset(self) -> PlanEvaluationDataset:
        """Keep topic groups unique and require provenance for reviewed labels."""
        if len({item.id for item in self.topics}) != len(self.topics):
            raise ValueError("Topic IDs must be unique.")
        if self.annotation_status == "human_reviewed" and not any(
            name.strip() for name in self.reviewers
        ):
            raise ValueError("Human-reviewed datasets require named reviewers.")
        return self


def reference_quality(
    topic: EvaluationTopic,
    candidate: PlanCandidate,
    retained_ids: set[int],
) -> dict[str, Any]:
    """Project supplied annotations onto the retained plan without judging text."""
    tasks = [task for task in candidate.tasks if task.id in retained_ids]
    coverage_unknown = any(task.covered_dimensions == "uncertain" for task in tasks)
    covered = {
        dimension
        for task in tasks
        if isinstance(task.covered_dimensions, list)
        for dimension in task.covered_dimensions
    }
    absent = [item.id for item in topic.required_dimensions if item.id not in covered]
    missing = [] if coverage_unknown else absent
    redundant = [
        pair.task_ids
        for pair in candidate.redundant_pairs
        if set(pair.task_ids) <= retained_ids
    ]
    pair_keys = {tuple(sorted(pair)) for pair in redundant}
    for relation in candidate.pair_relations:
        key = tuple(sorted(relation.task_ids))
        if (
            relation.label == "redundant"
            and set(key) <= retained_ids
            and key not in pair_keys
        ):
            redundant.append(list(key))
            pair_keys.add(key)
    unknown_pairs = [
        relation.task_ids
        for relation in candidate.pair_relations
        if relation.label == "uncertain" and set(relation.task_ids) <= retained_ids
    ]
    relevance_unknown = any(task.relevance == "uncertain" for task in tasks)
    relevance_bad = [
        task.id
        for task in tasks
        if isinstance(task.relevance, int) and task.relevance < 2
    ]
    grain_bad = [
        task.id
        for task in tasks
        if task.granularity not in {"appropriate", "uncertain"}
    ]
    low, high = topic.task_count_range
    count_bad = not low <= len(tasks) <= high
    issues = []
    if relevance_bad:
        issues.append("relevance")
    if redundant:
        issues.append("redundancy")
    if missing:
        issues.append("coverage")
    if grain_bad or count_bad:
        issues.append("granularity")
    return {
        "acceptable": None if candidate.evaluation_set == "boundary" else not issues,
        "evaluation_set": candidate.evaluation_set,
        "uncertain_pairs": unknown_pairs,
        "issues": issues,
        "relevance_mean": sum(
            task.relevance for task in tasks if isinstance(task.relevance, int)
        )
        / len(tasks)
        if tasks and not relevance_unknown
        else None,
        "relevance_problem_task_ids": relevance_bad,
        "redundant_pairs": redundant,
        "dimension_coverage": None
        if coverage_unknown
        else (len(topic.required_dimensions) - len(missing))
        / len(topic.required_dimensions),
        "unresolved_dimensions": absent if coverage_unknown else [],
        "missing_dimensions": missing,
        "granularity_problem_task_ids": grain_bad,
        "task_count_out_of_range": count_bad,
    }


def load_dataset(path: Path) -> PlanEvaluationDataset:
    """Load and strictly validate an annotated JSON corpus."""
    return PlanEvaluationDataset.model_validate_json(path.read_text(encoding="utf-8"))


def evaluate_dataset(
    dataset: PlanEvaluationDataset,
    *,
    overlap_threshold: float = 0.75,
    coverage_threshold: float = 0.4,
    split: Literal["development", "holdout", "all"] = "development",
) -> dict[str, Any]:
    """Measure gate signals and repairs against supplied reference annotations."""
    validator = PlanValidator(
        overlap_threshold=overlap_threshold,
        coverage_threshold=coverage_threshold,
    )
    rows: list[dict[str, Any]] = []
    for topic in dataset.topics:
        if split != "all" and topic.split != split:
            continue
        for candidate in topic.candidates:
            before = reference_quality(
                topic, candidate, {task.id for task in candidate.tasks}
            )
            result = validator.validate(
                topic.topic,
                [
                    TodoItem(
                        id=task.id,
                        title=task.title,
                        intent=task.intent,
                        query=task.query,
                    )
                    for task in candidate.tasks
                ],
            )
            after = reference_quality(
                topic, candidate, {task.id for task in result.tasks}
            )
            rows.append(
                {
                    "topic_id": topic.id,
                    "candidate_id": candidate.id,
                    "domain": topic.domain,
                    "split": topic.split,
                    "kind": candidate.kind,
                    "complexity_tier": estimate_complexity(topic.topic).value,
                    "gate_verdict": result.verdict.value,
                    "gate_action": result.verdict.value != "pass",
                    "gate_metrics": result.metrics.as_dict(),
                    "gate_reason_codes": list(result.reason_codes),
                    "retained_task_ids": [task.id for task in result.tasks],
                    "reference_before": before,
                    "reference_after": after,
                }
            )
    if not rows:
        raise ValueError("Selected split has no candidates.")
    core = [row for row in rows if row["reference_before"]["evaluation_set"] == "core"]
    boundary = [
        row for row in rows if row["reference_before"]["evaluation_set"] == "boundary"
    ]
    bad = [row for row in core if not row["reference_before"]["acceptable"]]
    good = [row for row in core if row["reference_before"]["acceptable"]]
    true_alerts = sum(row["gate_action"] for row in bad)
    false_alerts = sum(row["gate_action"] for row in good)
    summary = {
        "candidate_count": len(rows),
        "core_candidate_count": len(core),
        "boundary_candidate_count": len(boundary),
        "uncertain_plan_ratio": len(boundary) / len(rows),
        "reference_bad_count": len(bad),
        "reference_good_count": len(good),
        "true_alerts": true_alerts,
        "missed_bad_plans": len(bad) - true_alerts,
        "false_alerts": false_alerts,
        "correct_no_action": len(good) - false_alerts,
        "alert_precision": true_alerts / (true_alerts + false_alerts)
        if true_alerts + false_alerts
        else None,
        "alert_recall": true_alerts / len(bad) if bad else None,
        "bad_plan_no_action_rate": (len(bad) - true_alerts) / len(bad) if bad else None,
        "good_plan_intervention_rate": false_alerts / len(good) if good else None,
        "bad_plans_repaired_to_acceptable": sum(
            row["reference_after"]["acceptable"] for row in bad
        ),
        "reference_bad_retained_without_retry": sum(
            not row["reference_after"]["acceptable"] and row["gate_verdict"] != "retry"
            for row in core
        ),
        "repairs_losing_dimensions": sum(
            bool(
                set(row["reference_after"]["missing_dimensions"])
                - set(row["reference_before"]["missing_dimensions"])
            )
            for row in core
        ),
        "issues": {
            issue: {
                "reference_plan_count": sum(
                    issue in row["reference_before"]["issues"] for row in core
                ),
                "no_action_count": sum(
                    issue in row["reference_before"]["issues"]
                    and not row["gate_action"]
                    for row in core
                ),
                "retained_issue_count": sum(
                    issue in row["reference_after"]["issues"] for row in core
                ),
            }
            for issue in ("relevance", "redundancy", "coverage", "granularity")
        },
    }
    canonical = json.dumps(
        dataset.model_dump(), ensure_ascii=False, sort_keys=True
    ).encode("utf-8")
    implementation = b"".join(
        Path(__file__).with_name(name).read_bytes().replace(b"\r\n", b"\n")
        for name in ("plan_quality.py", "history.py")
    )
    return {
        "evaluator_version": 2,
        "dataset_version": dataset.dataset_version,
        "dataset_sha256": hashlib.sha256(canonical).hexdigest(),
        "gate_implementation_sha256": hashlib.sha256(implementation).hexdigest(),
        "annotation_status": dataset.annotation_status,
        "evaluation_scope": "single_gate_pass_no_llm_retry_no_profile_trim",
        "configuration": {
            "overlap_threshold": overlap_threshold,
            "coverage_threshold": coverage_threshold,
            "split": split,
        },
        "summary": summary,
        "boundary_candidates": boundary,
        "candidates": rows,
    }


def main() -> None:
    """Run the fixed Plan Gate baseline and write reproducible JSON results."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=_DEFAULT_DATASET)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--split", choices=("development", "holdout", "all"), default="development"
    )
    parser.add_argument("--overlap-threshold", type=float, default=0.75)
    parser.add_argument("--coverage-threshold", type=float, default=0.4)
    args = parser.parse_args()
    report = evaluate_dataset(
        load_dataset(args.dataset),
        overlap_threshold=args.overlap_threshold,
        coverage_threshold=args.coverage_threshold,
        split=args.split,
    )
    serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    else:
        sys.stdout.write(serialized)


if __name__ == "__main__":
    main()
