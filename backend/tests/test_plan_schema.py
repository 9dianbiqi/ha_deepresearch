"""Unit tests for the strict research plan schema boundary."""

from __future__ import annotations

import json

import pytest

from research.plan_schema import (
    PlanSchemaError,
    ResearchPlan,
    ResearchPlanTask,
    parse_plan_payload,
)


class TestResearchPlanTask:
    """Boundary validation for one planner task payload."""

    def test_rejects_missing_required_fields(self) -> None:
        with pytest.raises(PlanSchemaError):
            ResearchPlanTask.from_payload({"title": "市场规模"})

    def test_rejects_empty_query(self) -> None:
        with pytest.raises(PlanSchemaError):
            ResearchPlanTask.from_payload(
                {"title": "t", "intent": "i", "query": "   "}
            )

    def test_rejects_non_text_fields(self) -> None:
        with pytest.raises(PlanSchemaError):
            ResearchPlanTask.from_payload(
                {"title": "t", "intent": "i", "query": 42}
            )

    def test_rejects_overlong_title(self) -> None:
        with pytest.raises(PlanSchemaError):
            ResearchPlanTask.from_payload(
                {"title": "超" * 200, "intent": "i", "query": "q"}
            )

    def test_rejects_overlong_intent_and_query(self) -> None:
        with pytest.raises(PlanSchemaError):
            ResearchPlanTask.from_payload(
                {"title": "t", "intent": "意" * 600, "query": "q"}
            )
        with pytest.raises(PlanSchemaError):
            ResearchPlanTask.from_payload(
                {"title": "t", "intent": "i", "query": "查" * 600}
            )

    def test_rejects_non_mapping_payload(self) -> None:
        for bad in ("纯字符串", 42, None, ["嵌套列表"]):
            with pytest.raises(PlanSchemaError):
                ResearchPlanTask.from_payload(bad)

    def test_accepts_unknown_fields(self) -> None:
        task = ResearchPlanTask.from_payload(
            {
                "title": "市场规模",
                "intent": "调研市场规模",
                "query": "AI Coding Agent 市场规模 2026",
                "extra_field": "ignored",
            }
        )
        assert task.title == "市场规模"
        assert task.query == "AI Coding Agent 市场规模 2026"


class TestParsePlanPayload:
    """Three-strategy deterministic parsing with fail-open semantics."""

    def test_direct_json_object(self) -> None:
        raw = json.dumps(
            {"tasks": [{"title": "t", "intent": "i", "query": "q"}]},
            ensure_ascii=False,
        )
        plan = parse_plan_payload(raw)
        assert plan.parse_strategy == "direct"
        assert len(plan.tasks) == 1
        assert plan.tasks[0].query == "q"
        assert plan.rejected_task_count == 0
        assert plan.truncated_task_count == 0

    def test_direct_json_array(self) -> None:
        raw = json.dumps([{"title": "t", "intent": "i", "query": "q"}])
        plan = parse_plan_payload(raw)
        assert plan.parse_strategy == "direct"
        assert len(plan.tasks) == 1

    def test_fenced_block_with_prose(self) -> None:
        raw = (
            "规划如下：\n"
            "```json\n"
            '{"tasks": [{"title": "t", "intent": "i", "query": "q"}]}\n'
            "```\n"
            "以上规划供参考。"
        )
        plan = parse_plan_payload(raw)
        assert plan.parse_strategy == "fenced"
        assert len(plan.tasks) == 1

    def test_invalid_fenced_falls_through_to_brace(self) -> None:
        raw = (
            '正式计划：{"tasks": [{"title": "t", "intent": "i", "query": "q"}]}\n'
            "草稿（无效，请忽略）：```json\n"
            '{"tasks": [broken, 未闭合\n'
            "```"
        )
        plan = parse_plan_payload(raw)
        assert plan.parse_strategy == "brace"
        assert len(plan.tasks) == 1
        assert plan.tasks[0].title == "t"

    def test_brace_scrape_with_prose(self) -> None:
        raw = '说明文字 {"tasks": [{"title": "t", "intent": "i", "query": "q"}]} 结尾'
        plan = parse_plan_payload(raw)
        assert plan.parse_strategy == "brace"
        assert len(plan.tasks) == 1

    def test_explicit_empty_plan(self) -> None:
        plan = parse_plan_payload('{"tasks": []}')
        assert plan.tasks == ()
        assert plan.parse_strategy == "direct"
        assert plan.is_explicit_empty

    def test_explicit_empty_with_prose(self) -> None:
        plan = parse_plan_payload("主题信息不足，无法规划：{\"tasks\": []}")
        assert plan.tasks == ()
        assert plan.is_explicit_empty

    def test_all_tasks_rejected_is_not_explicit_empty(self) -> None:
        plan = parse_plan_payload(json.dumps({"tasks": [{"bogus": 1}, "字符串", 42]}))
        assert plan.tasks == ()
        assert plan.rejected_task_count == 3
        assert not plan.is_explicit_empty

    def test_unparsable_returns_unparsed(self) -> None:
        plan = parse_plan_payload("完全没有任何 JSON 结构的自由文本")
        assert plan.tasks == ()
        assert plan.parse_strategy == "unparsed"
        assert not plan.is_explicit_empty

    def test_blank_text_returns_empty_strategy(self) -> None:
        plan = parse_plan_payload("   \n  ")
        assert plan.parse_strategy == "empty"
        assert plan.tasks == ()

    def test_malformed_tasks_rejected_individually(self) -> None:
        raw = json.dumps(
            {
                "tasks": [
                    {"title": "t1", "intent": "i1", "query": "q1"},
                    {"title": "缺intent和query"},
                    "自由文本项",
                    {"title": "t2", "intent": "i2", "query": "q2"},
                ]
            },
            ensure_ascii=False,
        )
        plan = parse_plan_payload(raw)
        assert [task.title for task in plan.tasks] == ["t1", "t2"]
        assert plan.rejected_task_count == 2

    def test_non_tasks_dict_is_unparsed(self) -> None:
        plan = parse_plan_payload(json.dumps({"answer": 42}))
        assert plan.parse_strategy == "unparsed"

    def test_tasks_null_is_valid_empty(self) -> None:
        plan = parse_plan_payload('{"tasks": null}')
        assert plan.tasks == ()
        assert plan.parse_strategy == "direct"
        assert plan.is_explicit_empty

    def test_tasks_non_list_is_unparsed(self) -> None:
        plan = parse_plan_payload('{"tasks": "nope"}')
        assert plan.parse_strategy == "unparsed"

    def test_max_tasks_truncation_counted_separately(self) -> None:
        tasks = [
            {"title": f"t{i}", "intent": "i", "query": f"q{i}"} for i in range(20)
        ]
        plan = parse_plan_payload(json.dumps({"tasks": tasks}))
        assert len(plan.tasks) == 16
        assert plan.rejected_task_count == 0
        assert plan.truncated_task_count == 4
        assert not plan.is_explicit_empty

    def test_malformed_and_truncation_counted_separately(self) -> None:
        tasks = [
            {"title": f"good{i}", "intent": "i", "query": f"q{i}"} for i in range(18)
        ] + [{"title": "bad"}, {"title": "bad2"}]
        plan = parse_plan_payload(json.dumps({"tasks": tasks}))
        assert len(plan.tasks) == 16
        assert plan.rejected_task_count == 2
        assert plan.truncated_task_count == 2

    def test_chinese_fields_round_trip(self) -> None:
        raw = json.dumps(
            {
                "tasks": [
                    {
                        "title": "市场规模",
                        "intent": "调研 AI Coding Agent 市场规模",
                        "query": "AI Coding Agent 市场规模 2026",
                    }
                ]
            },
            ensure_ascii=False,
        )
        plan = parse_plan_payload(raw)
        assert plan.tasks[0].title == "市场规模"
        assert plan.tasks[0].query == "AI Coding Agent 市场规模 2026"


class TestResearchPlanInvariants:
    """Plan-level dataclass invariant checks."""

    def test_rejects_unknown_parse_strategy(self) -> None:
        with pytest.raises(PlanSchemaError):
            ResearchPlan(tasks=(), parse_strategy="bogus")

    def test_rejects_negative_counts(self) -> None:
        with pytest.raises(PlanSchemaError):
            ResearchPlan(tasks=(), parse_strategy="direct", rejected_task_count=-1)
        with pytest.raises(PlanSchemaError):
            ResearchPlan(tasks=(), parse_strategy="direct", truncated_task_count=-1)

    def test_rejects_over_task_bound(self) -> None:
        tasks = tuple(
            ResearchPlanTask(title=f"t{i}", intent="i", query=f"q{i}")
            for i in range(17)
        )
        with pytest.raises(PlanSchemaError):
            ResearchPlan(tasks=tasks, parse_strategy="direct")
