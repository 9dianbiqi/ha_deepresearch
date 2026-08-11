"""Tests for HarnessPolicy capability evaluation."""

from __future__ import annotations

import unittest

import conftest  # noqa: F401 — ensure path setup runs

from config import Configuration, SearchAPI
from harness.models import HarnessRunRequest
from harness.policy import HarnessPolicy, PolicyDecision


def _make_request(**overrides) -> HarnessRunRequest:
    """Build a minimal request for policy testing."""
    config = Configuration.from_env(overrides=overrides.pop("config_overrides", None))
    topic = overrides.pop("topic", "test topic")
    return HarnessRunRequest(
        topic=topic,
        config=config,
        **overrides,
    )


class TestRequiredCapabilities(unittest.TestCase):
    """Verify the inferred capability set for different configurations."""

    def test_default_capabilities_include_core_set(self) -> None:
        policy = HarnessPolicy()
        request = _make_request(config_overrides={"enable_notes": True})
        caps = policy.required_capabilities(request)

        self.assertIn("research:run", caps)
        self.assertIn("llm:invoke", caps)
        self.assertIn("search:web", caps)
        self.assertIn("report:export", caps)
        self.assertIn("notes:read", caps)
        self.assertIn("notes:write", caps)

    def test_perplexity_adds_premium_search(self) -> None:
        policy = HarnessPolicy()
        request = _make_request(config_overrides={"search_api": SearchAPI.PERPLEXITY})
        caps = policy.required_capabilities(request)

        self.assertIn("search:premium", caps)

    def test_disabled_notes_excludes_note_capabilities(self) -> None:
        policy = HarnessPolicy()
        request = _make_request(config_overrides={"enable_notes": False})
        caps = policy.required_capabilities(request)

        self.assertNotIn("notes:read", caps)
        self.assertNotIn("notes:write", caps)

    def test_github_repository_topic_adds_github_read(self) -> None:
        policy = HarnessPolicy()
        request = _make_request(topic="https://github.com/bytedance/deer-flow")
        caps = policy.required_capabilities(request)

        self.assertIn("github:read", caps)


class TestPolicyEvaluate(unittest.TestCase):
    """Verify individual capability decisions."""

    def test_all_allowed_in_default_mode(self) -> None:
        policy = HarnessPolicy()
        request = _make_request(config_overrides={"enable_notes": True})
        decisions = policy.evaluate(request)

        for decision in decisions:
            self.assertEqual(
                decision.outcome,
                "allow",
                f"{decision.capability} was not allowed",
            )

    def test_premium_search_asks_in_strict_mode(self) -> None:
        policy = HarnessPolicy()
        request = _make_request(
            config_overrides={"search_api": SearchAPI.PERPLEXITY},
            permission_mode="strict",
        )
        decisions = policy.evaluate(request)
        premium = [d for d in decisions if d.capability == "search:premium"]

        self.assertEqual(len(premium), 1)
        self.assertEqual(premium[0].outcome, "ask")

    def test_notes_denied_when_disabled(self) -> None:
        policy = HarnessPolicy()
        request = _make_request(config_overrides={"enable_notes": False})
        # Manually add notes capabilities to test denial
        decisions = [
            policy._evaluate_capability("notes:read", request),
            policy._evaluate_capability("notes:write", request),
        ]

        for decision in decisions:
            self.assertEqual(decision.outcome, "deny")

    def test_unknown_capability_is_denied(self) -> None:
        policy = HarnessPolicy()
        request = _make_request()
        decision = policy._evaluate_capability("unknown:cap", request)

        self.assertEqual(decision.outcome, "deny")

    def test_github_read_is_allowed(self) -> None:
        policy = HarnessPolicy()
        request = _make_request(topic="bytedance/deer-flow")
        decision = policy._evaluate_capability("github:read", request)

        self.assertEqual(decision.outcome, "allow")

    def test_public_capability_authorizer_supports_operation_checks(self) -> None:
        policy = HarnessPolicy()
        request = _make_request()

        decision = policy.evaluate_capability("llm:invoke", request)

        self.assertEqual(decision.capability, "llm:invoke")
        self.assertEqual(decision.outcome, "allow")


class TestAssertExecutable(unittest.TestCase):
    """Verify that blocked decisions raise PermissionError."""

    def test_all_allowed_passes(self) -> None:
        policy = HarnessPolicy()
        decisions = [
            PolicyDecision(capability="research:run", outcome="allow", reason="ok"),
        ]
        # Should not raise
        policy.assert_executable(decisions)

    def test_deny_raises(self) -> None:
        policy = HarnessPolicy()
        decisions = [
            PolicyDecision(capability="notes:write", outcome="deny", reason="disabled"),
        ]
        with self.assertRaises(PermissionError):
            policy.assert_executable(decisions)

    def test_ask_raises(self) -> None:
        policy = HarnessPolicy()
        decisions = [
            PolicyDecision(capability="search:premium", outcome="ask", reason="strict"),
        ]
        with self.assertRaises(PermissionError):
            policy.assert_executable(decisions)

    def test_unknown_outcome_fails_closed(self) -> None:
        policy = HarnessPolicy()
        decisions = [
            PolicyDecision(
                capability="llm:invoke",
                outcome="unexpected",
                reason="invalid policy result",
            ),
        ]
        with self.assertRaises(PermissionError):
            policy.assert_executable(decisions)


if __name__ == "__main__":
    unittest.main()
