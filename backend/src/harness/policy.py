"""Lightweight policy controls for harness-managed research runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from loguru import logger

from config import SearchAPI
from services.github_research import parse_github_repository

from .models import HarnessRunRequest


@dataclass(frozen=True, kw_only=True)
class PolicyDecision:
    """Result of evaluating one capability against the current request."""

    capability: str
    outcome: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        """Serialize the decision into a simple dictionary."""
        return {
            "capability": self.capability,
            "outcome": self.outcome,
            "reason": self.reason,
        }


class HarnessPolicy:
    """Evaluate a small set of capabilities for a research run."""

    def required_capabilities(self, request: HarnessRunRequest) -> list[str]:
        """Infer the capability set required by the current request."""
        capabilities = [
            "research:run",
            "llm:invoke",
            "search:web",
            "report:export",
        ]

        if request.config.search_api == SearchAPI.PERPLEXITY:
            capabilities.append("search:premium")
        if (
            getattr(request.config, "enable_github_research", True)
            and parse_github_repository(request.topic)
        ):
            capabilities.append("github:read")
        if request.config.enable_notes:
            capabilities.extend(["notes:read", "notes:write"])

        return capabilities

    def evaluate(self, request: HarnessRunRequest) -> list[PolicyDecision]:
        """Return policy decisions for the requested run."""
        decisions: list[PolicyDecision] = []
        for capability in self.required_capabilities(request):
            decisions.append(self.evaluate_capability(capability, request))
        return decisions

    def assert_executable(self, decisions: Iterable[PolicyDecision]) -> None:
        """Raise when one or more policy decisions block execution."""
        blocked = [item for item in decisions if item.outcome != "allow"]
        if blocked:
            reasons = "; ".join(
                f"{item.capability}: {item.reason}" for item in blocked
            )
            logger.warning("Policy blocked execution: {}", reasons)
            raise PermissionError(reasons)
        logger.debug("Policy check passed: all capabilities allowed")

    def evaluate_capability(
        self,
        capability: str,
        request: HarnessRunRequest,
    ) -> PolicyDecision:
        """Evaluate one capability for preflight or a dynamic operation."""
        if capability == "research:run":
            return PolicyDecision(
                capability=capability,
                outcome="allow",
                reason="Standard research execution is enabled.",
            )

        if capability == "search:web":
            return PolicyDecision(
                capability=capability,
                outcome="allow",
                reason="Configured web search backend is permitted.",
            )

        if capability == "llm:invoke":
            return PolicyDecision(
                capability=capability,
                outcome="allow",
                reason="Configured language-model invocation is permitted.",
            )

        if capability == "search:premium":
            outcome = "ask" if request.permission_mode == "strict" else "allow"
            return PolicyDecision(
                capability=capability,
                outcome=outcome,
                reason="Premium search requires explicit approval in strict mode.",
            )

        if capability == "github:read":
            return PolicyDecision(
                capability=capability,
                outcome="allow",
                reason="Read-only GitHub repository metadata is permitted.",
            )

        if capability in {"notes:read", "notes:write"}:
            outcome = "allow" if request.config.enable_notes else "deny"
            return PolicyDecision(
                capability=capability,
                outcome=outcome,
                reason="Note capabilities depend on ENABLE_NOTES.",
            )

        if capability == "report:export":
            outcome = "allow" if request.caller_mode in {"public", "internal"} else "deny"
            return PolicyDecision(
                capability=capability,
                outcome=outcome,
                reason="Report export is enabled for supported caller modes.",
            )

        return PolicyDecision(
            capability=capability,
            outcome="deny",
            reason="Unknown capability is not permitted.",
        )

    def _evaluate_capability(
        self,
        capability: str,
        request: HarnessRunRequest,
    ) -> PolicyDecision:
        """Retain the historical private helper as a compatibility alias."""
        return self.evaluate_capability(capability, request)
