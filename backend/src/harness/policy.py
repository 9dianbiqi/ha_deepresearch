"""Lightweight policy controls for harness-managed research runs."""

from __future__ import annotations

from dataclasses import dataclass

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

    def evaluate(self, request: HarnessRunRequest) -> list[PolicyDecision]:
        """Return policy decisions for the requested run."""
        decisions: list[PolicyDecision] = [
            PolicyDecision(
                capability="research:run",
                outcome="allow",
                reason="Standard research execution is enabled.",
            ),
            PolicyDecision(
                capability="notes:write",
                outcome="allow" if request.config.enable_notes else "deny",
                reason="Note persistence follows the ENABLE_NOTES configuration.",
            ),
        ]

        if request.config.search_api == SearchAPI.PERPLEXITY:
            decisions.append(
                PolicyDecision(
                    capability="network:premium_search",
                    outcome="ask" if request.permission_mode == "strict" else "allow",
                    reason="Perplexity may incur external API usage and should be explicit in strict mode.",
                )
            )
        else:
            decisions.append(
                PolicyDecision(
                    capability="network:search",
                    outcome="allow",
                    reason="Configured search backend is permitted for standard research runs.",
                )
            )

        return decisions

    def assert_allowed(self, decisions: list[PolicyDecision]) -> None:
        """Raise when one or more policy decisions reject the run."""
        denied = [item for item in decisions if item.outcome == "deny"]
        if denied:
            reasons = "; ".join(
                f"{item.capability}: {item.reason}" for item in denied
            )
            raise PermissionError(reasons)
