"""Unit tests for the deterministic plan quality gate."""

from __future__ import annotations

import pytest

from agent import DeepResearchAgent
from config import Configuration
from models import ResearchState, TodoItem
from research.contracts import ResearchCommand
from research.operations import GovernedOperations, OperationScope
from research.plan_quality import (
    LexicalCoverageScorer,
    PlanComplexityTier,
    PlanQualityMetrics,
    PlanValidationResult,
    PlanValidationVerdict,
    PlanValidator,
    estimate_complexity,
    tier_task_bounds,
)
from research.session import RunSession


def _task(task_id: int, title: str, intent: str, query: str) -> TodoItem:
    """Build one planner-shaped task for gate tests."""
    return TodoItem(id=task_id, title=title, intent=intent, query=query)


class TestPlanValidatorConstruction:
    """Threshold boundary validation for the deterministic gate."""

    def test_rejects_non_positive_overlap_threshold(self) -> None:
        with pytest.raises(ValueError):
            PlanValidator(overlap_threshold=0.0)

    def test_rejects_overlap_threshold_above_one(self) -> None:
        with pytest.raises(ValueError):
            PlanValidator(overlap_threshold=1.5)

    def test_rejects_coverage_threshold_out_of_bounds(self) -> None:
        with pytest.raises(ValueError):
            PlanValidator(coverage_threshold=-0.1)
        with pytest.raises(ValueError):
            PlanValidator(coverage_threshold=1.1)

    def test_accepts_boundary_coverage_thresholds(self) -> None:
        PlanValidator(coverage_threshold=0.0)
        PlanValidator(coverage_threshold=1.0)


class TestOverlapDedup:
    """Near-verbatim query duplication is repaired deterministically."""

    def test_identical_queries_drop_later_task(self) -> None:
        tasks = [
            _task(1, "市场规模", "调研市场规模", "alpha beta gamma"),
            _task(2, "重复任务", "重复的市场调研", "alpha beta gamma"),
        ]
        result = PlanValidator().validate("alpha beta gamma topic", tasks)
        assert result.verdict is PlanValidationVerdict.PASS_WITH_REPAIRS
        assert [task.id for task in result.tasks] == [1]
        assert result.metrics.dropped_duplicate_count == 1
        assert "plan_duplicate_tasks_dropped" in result.reason_codes

    def test_high_query_overlap_drops_later_task(self) -> None:
        tasks = [
            _task(1, "市场", "调研", "AI Coding Agent 市场规模 2026"),
            _task(2, "市场2", "调研", "AI Coding Agent 市场规模"),
        ]
        result = PlanValidator(overlap_threshold=0.75).validate("市场调研主题", tasks)
        assert [task.id for task in result.tasks] == [1]
        assert result.metrics.dropped_duplicate_count == 1

    def test_distinct_queries_are_all_kept(self) -> None:
        tasks = [
            _task(1, "市场", "调研", "AI Coding Agent 市场规模 2026"),
            _task(2, "OpenAI", "调研", "OpenAI 产品矩阵与战略动向"),
            _task(3, "Cursor", "调研", "Cursor 商业化模式 定价"),
        ]
        result = PlanValidator().validate("AI Coding Agent 市场竞争", tasks)
        assert result.verdict is PlanValidationVerdict.PASS
        assert len(result.tasks) == 3
        assert result.metrics.dropped_duplicate_count == 0
        assert result.metrics.max_pairwise_overlap < 0.75

    def test_numbered_variant_queries_are_not_duplicates(self) -> None:
        tasks = [
            _task(1, "Verify 1", "Collect overview", "Alpha evidence research 1"),
            _task(2, "Verify 2", "Collect overview", "Alpha evidence research 2"),
        ]
        result = PlanValidator().validate("Alpha evidence research", tasks)
        assert result.verdict is PlanValidationVerdict.PASS
        assert len(result.tasks) == 2
        assert result.metrics.max_pairwise_overlap == pytest.approx(0.6)

    def test_third_duplicate_chains_to_first_kept_task(self) -> None:
        tasks = [
            _task(1, "A", "调研", "alpha beta gamma"),
            _task(2, "B", "调研", "gamma beta alpha"),
            _task(3, "C", "调研", "alpha beta gamma delta"),
        ]
        result = PlanValidator(overlap_threshold=0.6).validate("alpha beta", tasks)
        assert [task.id for task in result.tasks] == [1]
        assert result.metrics.dropped_duplicate_count == 2

    def test_non_todo_items_are_filtered_silently(self) -> None:
        tasks: list[object] = [
            _task(1, "A", "调研", "alpha beta"),
            "字符串",
            None,
            42,
        ]
        result = PlanValidator().validate("alpha", tasks)
        assert [task.id for task in result.tasks] == [1]
        assert result.metrics.task_count == 1


class TestCoverage:
    """Lexical coverage over significant topic terms decides retry feedback."""

    def test_uncovered_topic_terms_trigger_retry_feedback(self) -> None:
        tasks = [_task(1, "经典计算", "概述", "经典计算 基础知识 概览")]
        result = PlanValidator(coverage_threshold=0.4).validate(
            "量子计算 超导量子比特 退相干", tasks
        )
        assert result.verdict is PlanValidationVerdict.RETRY
        assert result.retry_feedback
        assert "plan_coverage_below_threshold" in result.reason_codes
        assert result.metrics.coverage_ratio < 0.4

    def test_covering_plan_passes(self) -> None:
        tasks = [
            _task(1, "市场规模", "调研市场规模", "AI Coding Agent 市场规模与增长趋势 2026"),
            _task(2, "OpenAI", "调研", "OpenAI 产品矩阵与战略动向"),
            _task(3, "竞争格局", "对比", "OpenAI Anthropic Cursor 竞争格局对比"),
        ]
        result = PlanValidator(coverage_threshold=0.4).validate(
            "分析 2026 年 AI Coding Agent 市场及主要竞争者", tasks
        )
        assert result.verdict is PlanValidationVerdict.PASS
        assert result.metrics.coverage_ratio >= 0.4
        assert not result.retry_feedback

    def test_empty_topic_passes_vacuously(self) -> None:
        tasks = [_task(1, "任意", "任意", "whatever query")]
        result = PlanValidator().validate("分析 调研", tasks)
        assert result.metrics.coverage_ratio == 1.0
        assert result.verdict is PlanValidationVerdict.PASS

    def test_empty_task_list_with_topic_passes_for_fallback(self) -> None:
        result = PlanValidator().validate("量子计算 退相干", [])
        assert result.verdict is PlanValidationVerdict.PASS
        assert result.tasks == ()
        assert result.metrics.task_count == 0

    def test_duplicate_drop_and_low_coverage_combine_into_retry(self) -> None:
        tasks = [
            _task(1, "市场", "调研", "alpha beta gamma delta"),
            _task(2, "市场重复", "调研", "alpha beta gamma epsilon"),
        ]
        result = PlanValidator(overlap_threshold=0.5, coverage_threshold=0.9).validate(
            "完全无关的 zeta eta theta 主题", tasks
        )
        assert result.verdict is PlanValidationVerdict.RETRY
        assert result.metrics.dropped_duplicate_count == 1
        feedback_text = "\n".join(result.retry_feedback)
        assert "重复" in feedback_text
        assert "覆盖" in feedback_text


class TestLexicalCoverageScorer:
    """Deterministic scorer boundary behavior."""

    def test_uncovered_terms_merge_into_readable_fragments(self) -> None:
        scorer = LexicalCoverageScorer()
        terms = scorer.uncovered_terms("量子计算 超导量子比特 退相干", ["经典计算 基础"])
        assert "超导量子比特" in terms
        assert "退相干" in terms
        assert "子计" not in terms
        assert "导量" not in terms
        assert all(len(term) > 2 for term in terms)

    def test_partially_covered_topic_reports_readable_span(self) -> None:
        scorer = LexicalCoverageScorer()
        terms = scorer.uncovered_terms(
            "分析 2026 年 AI Coding Agent 市场及主要竞争者与差异化定位策略",
            ["AI Coding Agent 市场规模 2026"],
        )
        joined = "、".join(terms)
        assert "主要竞争者" in joined
        assert all(len(term) > 2 for term in terms)

    def test_uncovered_terms_empty_for_covered_text(self) -> None:
        scorer = LexicalCoverageScorer()
        assert scorer.uncovered_terms("alpha beta", ["alpha beta gamma"]) == ()

    def test_uncovered_terms_empty_without_task_texts(self) -> None:
        scorer = LexicalCoverageScorer()
        assert scorer.uncovered_terms("alpha beta", []) == ("alpha", "beta")

    def test_uncovered_terms_skip_stopword_bigrams(self) -> None:
        scorer = LexicalCoverageScorer()
        terms = scorer.uncovered_terms("分析 量子计算", ["完全无关 other text"])
        assert "分析" not in terms
        assert "量子计算" in terms


class TestResultInvariants:
    """Result dataclass invariants."""

    def test_retry_verdict_requires_feedback(self) -> None:
        with pytest.raises(ValueError):
            PlanValidationResult(
                verdict=PlanValidationVerdict.RETRY,
                tasks=(),
                reason_codes=("plan_coverage_below_threshold",),
                metrics=PlanQualityMetrics(
                    task_count=0,
                    dropped_duplicate_count=0,
                    max_pairwise_overlap=0.0,
                    coverage_ratio=0.0,
                ),
                retry_feedback=(),
            )

    def test_pass_verdict_rejects_feedback(self) -> None:
        with pytest.raises(ValueError):
            PlanValidationResult(
                verdict=PlanValidationVerdict.PASS,
                tasks=(),
                reason_codes=(),
                metrics=PlanQualityMetrics(
                    task_count=0,
                    dropped_duplicate_count=0,
                    max_pairwise_overlap=0.0,
                    coverage_ratio=1.0,
                ),
                retry_feedback=("不应存在的反馈",),
            )

    def test_metrics_as_dict_projection(self) -> None:
        metrics = PlanQualityMetrics(
            task_count=3,
            dropped_duplicate_count=1,
            max_pairwise_overlap=0.91234,
            coverage_ratio=0.56789,
        )
        projection = metrics.as_dict()
        assert projection["task_count"] == 3
        assert projection["dropped_duplicate_count"] == 1
        assert projection["max_pairwise_overlap"] == 0.9123
        assert projection["coverage_ratio"] == 0.5679


class TestValidatorFromConfig:
    """Configuration to gate mapping used by the coordinator."""

    def test_disabled_validation_maps_to_none(self) -> None:
        config = Configuration.from_env(overrides={"plan_validation": False})
        assert DeepResearchAgent._plan_validator_for(config) is None

    def test_enabled_validation_binds_thresholds(self) -> None:
        config = Configuration.from_env(
            overrides={
                "plan_validation": True,
                "plan_overlap_threshold": 0.8,
                "plan_coverage_threshold": 0.5,
            }
        )
        validator = DeepResearchAgent._plan_validator_for(config)
        assert isinstance(validator, PlanValidator)


class _SequencedPlanner:
    """Return scripted plans while recording validation feedback calls."""

    def __init__(self, plans: list[list[TodoItem]]) -> None:
        self.plans = plans
        self.feedback_calls: list[tuple[str, ...] | None] = []

    def plan_todo_list(
        self,
        state: object,
        prior_context: dict[str, object] | None = None,
        validation_feedback: tuple[str, ...] | None = None,
        *,
        operation_scope: object = None,
    ) -> list[TodoItem]:
        self.feedback_calls.append(validation_feedback)
        index = min(len(self.feedback_calls) - 1, len(self.plans) - 1)
        return [TodoItem(**task.to_dict()) for task in self.plans[index]]

    @staticmethod
    def create_fallback_task(state: object) -> TodoItem:
        return TodoItem(id=1, title="Fallback", intent="F", query="fallback")


class _LegacyPlanner:
    """Mimic an injected fake whose signature predates validation feedback."""

    def __init__(self, tasks: list[TodoItem]) -> None:
        self.tasks = tasks
        self.calls = 0

    def plan_todo_list(
        self,
        state: object,
        prior_context: dict[str, object] | None = None,
        *,
        operation_scope: object = None,
    ) -> list[TodoItem]:
        self.calls += 1
        return [TodoItem(**task.to_dict()) for task in self.tasks]

    @staticmethod
    def create_fallback_task(state: object) -> TodoItem:
        return TodoItem(id=1, title="Fallback", intent="F", query="fallback")


def _retry_test_agent(planner: object, config: Configuration) -> DeepResearchAgent:
    """Build a fully-injected coordinator for plan-gate orchestration tests."""
    return DeepResearchAgent(
        config=config,
        planner=planner,
        search_adapter=lambda *args, **kwargs: ({"results": []}, [], None, "fake"),
        context_preparer=lambda *args, **kwargs: ("", ""),
        summarizer=object(),
        reporting=object(),
        note_agent=None,
    )


def _started_plan_session(topic: str, config: Configuration) -> RunSession:
    """Create one started session bound to a plan-gate test topic."""
    session = RunSession(
        command=ResearchCommand(topic=topic, config=config),
        state=ResearchState(research_topic=topic),
    )
    session.start()
    return session


def _plan_once(agent: DeepResearchAgent, session: RunSession) -> list[TodoItem]:
    """Invoke the coordinator plan path with one governed root scope."""
    scope = OperationScope(
        operations=GovernedOperations(session, agent._operation_authorizer),
    )
    return agent._plan_research_tasks(session, None, root_scope=scope)


class TestComplexityTier:
    """Deterministic query-complexity classification."""

    def test_simple_factual_question(self) -> None:
        assert (
            estimate_complexity("Python 3.14 有什么新特性？")
            is PlanComplexityTier.SIMPLE
        )

    def test_simple_english_lookup(self) -> None:
        assert estimate_complexity("topic") is PlanComplexityTier.SIMPLE

    def test_medium_comparison_question(self) -> None:
        assert (
            estimate_complexity("调研 LangChain 和 LlamaIndex 的区别")
            is PlanComplexityTier.MEDIUM
        )

    def test_complex_market_analysis_question(self) -> None:
        assert (
            estimate_complexity("分析 2026 年 AI Coding Agent 市场及主要竞争者")
            is PlanComplexityTier.COMPLEX
        )

    def test_complex_multi_aspect_question(self) -> None:
        assert (
            estimate_complexity("量子计算、超导材料、离子阱、光量子多条技术路线对比")
            is PlanComplexityTier.COMPLEX
        )

    def test_blank_topic_is_simple(self) -> None:
        assert estimate_complexity("") is PlanComplexityTier.SIMPLE
        assert estimate_complexity("   ") is PlanComplexityTier.SIMPLE

    def test_tier_task_bounds_are_ordered(self) -> None:
        for tier in PlanComplexityTier:
            low, high = tier_task_bounds(tier)
            assert 1 <= low <= high
        assert tier_task_bounds(PlanComplexityTier.SIMPLE) == (1, 2)
        assert tier_task_bounds(PlanComplexityTier.MEDIUM) == (3, 5)
        assert tier_task_bounds(PlanComplexityTier.COMPLEX) == (5, 8)


class TestPromptTierParameterization:
    """Planner prompt renders the query-aware task-count range."""

    def test_instructions_template_renders_tier_fields(self) -> None:
        from prompts import todo_planner_instructions

        rendered = todo_planner_instructions.format(
            current_date="October 06, 2026",
            research_topic="任意主题",
            complexity_tier="complex",
            task_count_range="5~8 个",
        )
        assert "complex" in rendered
        assert "5~8 个" in rendered
        assert "{complexity_tier}" not in rendered
        assert "{task_count_range}" not in rendered

    def test_planning_service_prompt_carries_tier(self) -> None:
        from services.planner import PlanningService

        captured: dict[str, object] = {}

        class AgentStub:
            def run(self, prompt: str, **kwargs: object) -> str:
                captured["prompt"] = prompt
                return '{"tasks": []}'

            def clear_history(self) -> None:
                return None

        service = PlanningService(
            AgentStub(),  # type: ignore[arg-type]
            Configuration.from_env(overrides={"enable_notes": False}),
        )
        tasks = service.plan_todo_list(
            ResearchState(research_topic="分析 2026 年 AI Coding Agent 市场及主要竞争者")
        )
        assert tasks == []
        prompt_text = str(captured["prompt"])
        assert "complex" in prompt_text
        assert "5~8 个" in prompt_text


class TestPlanRetryOrchestration:
    """Coordinator retry loop around the deterministic plan gate."""

    @staticmethod
    def _config(**overrides: object) -> Configuration:
        values: dict[str, object] = {
            "enable_notes": False,
            "enable_github_research": False,
            "enable_quality_gate": False,
        }
        values.update(overrides)
        return Configuration.from_env(overrides=values)

    def test_low_coverage_plan_retries_once_with_feedback(self) -> None:
        topic = "量子计算 超导量子比特 退相干"
        planner = _SequencedPlanner(
            [
                [_task(1, "经典", "概述", "经典计算 基础知识 概览")],
                [
                    _task(1, "原理", "调研", "量子计算 原理 量子比特"),
                    _task(2, "工程", "调研", "超导量子比特 工程"),
                    _task(3, "纠错", "调研", "量子退相干 纠错"),
                ],
            ]
        )
        agent = _retry_test_agent(planner, self._config())
        session = _started_plan_session(topic, agent.config)
        tasks = _plan_once(agent, session)
        assert len(planner.feedback_calls) == 2
        feedback = planner.feedback_calls[1]
        assert feedback is not None
        feedback_text = " ".join(feedback)
        assert "超导量子比特" in feedback_text
        assert "退相干" in feedback_text
        metrics = session.metrics["plan_quality"]
        assert metrics["validation_retries"] == 1
        assert metrics["validation_verdict"] == "pass"
        assert metrics["task_count"] == 3
        assert len(tasks) == 3

    def test_legacy_planner_signature_never_retries(self) -> None:
        planner = _LegacyPlanner([_task(1, "经典", "概述", "经典计算 基础 概览")])
        agent = _retry_test_agent(planner, self._config())
        session = _started_plan_session("量子计算 退相干", agent.config)
        tasks = _plan_once(agent, session)
        assert planner.calls == 1
        metrics = session.metrics["plan_quality"]
        assert metrics["validation_retries"] == 0
        assert metrics["validation_verdict"] == "retry"
        assert "plan_retry_unavailable" in metrics["reason_codes"]
        assert len(tasks) == 1

    def test_retry_exhaustion_fails_open(self) -> None:
        planner = _SequencedPlanner(
            [[_task(1, "经典", "概述", "经典计算 基础 概览")]],
        )
        agent = _retry_test_agent(
            planner, self._config(plan_validation_retries=1)
        )
        session = _started_plan_session("量子计算 退相干", agent.config)
        tasks = _plan_once(agent, session)
        assert len(planner.feedback_calls) == 2
        metrics = session.metrics["plan_quality"]
        assert metrics["validation_retries"] == 1
        assert metrics["validation_verdict"] == "retry"
        assert "plan_retry_exhausted" in metrics["reason_codes"]
        assert len(tasks) == 1


class TestValidationFeedbackBlock:
    """Bounded re-plan feedback formatting."""

    def test_feedback_block_lists_items_and_fix_instruction(self) -> None:
        from services.planner import PlanningService

        block = PlanningService._format_validation_feedback(("问题一", "问题二"))
        assert "计划校验反馈" in block
        assert "- 问题一" in block
        assert "重新规划" in block

    def test_feedback_block_truncates_at_eight_items(self) -> None:
        from services.planner import PlanningService

        block = PlanningService._format_validation_feedback(
            tuple(f"问题{i}" for i in range(10))
        )
        assert "- 问题7" in block
        assert "- 问题8" not in block
