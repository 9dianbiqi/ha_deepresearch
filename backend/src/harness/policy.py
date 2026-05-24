"""Lightweight policy controls for harness-managed research runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from config import SearchAPI

from .models import HarnessRunRequest


@dataclass(kw_only=True)
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
        capabilities = ["research:run", "search:web", "report:export"]

        if request.config.search_api == SearchAPI.PERPLEXITY:
            capabilities.append("search:premium")
        if request.config.enable_notes:
            capabilities.extend(["notes:read", "notes:write"])

        return capabilities

    def evaluate(self, request: HarnessRunRequest) -> list[PolicyDecision]:
        """Return policy decisions for the requested run."""
        decisions: list[PolicyDecision] = []
        for capability in self.required_capabilities(request):
            decisions.append(self._evaluate_capability(capability, request))
        return decisions

    def assert_executable(self, decisions: Iterable[PolicyDecision]) -> None:
        """Raise when one or more policy decisions block execution."""
        blocked = [item for item in decisions if item.outcome in {"deny", "ask"}]
        if blocked:
            reasons = "; ".join(
                f"{item.capability}: {item.reason}" for item in blocked
            )
            raise PermissionError(reasons)

    def _evaluate_capability(
        self,
        capability: str,
        request: HarnessRunRequest,
    ) -> PolicyDecision:
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

        if capability == "search:premium":
            outcome = "ask" if request.permission_mode == "strict" else "allow"
            return PolicyDecision(
                capability=capability,
                outcome=outcome,
                reason="Premium search requires explicit approval in strict mode.",
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
