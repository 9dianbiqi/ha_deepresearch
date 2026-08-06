"""Deprecated compatibility facades for canonical research execution."""

from .compressor import ContextCompressor
from .evaluator import EvaluationResult, RuleBasedEvaluator
from .models import (
    EvaluationFinding,
    HarnessEvent,
    HarnessRunRecord,
    HarnessRunRequest,
    HarnessRunResult,
    RunContext,
)
from .policy import HarnessPolicy, PolicyDecision
from .recorder import JsonlRunRecorder


def __getattr__(name: str):
    """Load heavyweight harness exports only when explicitly requested."""
    if name == "HarnessRunner":
        from .runner import HarnessRunner

        return HarnessRunner
    if name in {"HarnessScenario", "build_default_scenarios"}:
        from .scenarios import HarnessScenario, build_default_scenarios

        return {
            "HarnessScenario": HarnessScenario,
            "build_default_scenarios": build_default_scenarios,
        }[name]
    raise AttributeError(name)

__all__ = [
    "ContextCompressor",
    "EvaluationFinding",
    "EvaluationResult",
    "HarnessEvent",
    "HarnessPolicy",
    "HarnessRunRecord",
    "HarnessRunRequest",
    "HarnessRunResult",
    "HarnessRunner",
    "HarnessScenario",
    "JsonlRunRecorder",
    "PolicyDecision",
    "RuleBasedEvaluator",
    "RunContext",
    "build_default_scenarios",
]
