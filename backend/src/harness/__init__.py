"""Harness layer for orchestrating, recording, and evaluating research runs."""

from .compressor import ContextCompressor
from .context_manager import ContextManager
from .event_bus import InMemoryEventBus
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
from .runner import HarnessRunner
from .scenarios import HarnessScenario, build_default_scenarios

__all__ = [
    "ContextCompressor",
    "ContextManager",
    "EvaluationFinding",
    "EvaluationResult",
    "HarnessEvent",
    "HarnessPolicy",
    "HarnessRunRecord",
    "HarnessRunRequest",
    "HarnessRunResult",
    "HarnessRunner",
    "HarnessScenario",
    "InMemoryEventBus",
    "JsonlRunRecorder",
    "PolicyDecision",
    "RuleBasedEvaluator",
    "RunContext",
    "build_default_scenarios",
]
