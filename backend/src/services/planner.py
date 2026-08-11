"""Service responsible for converting the research topic into actionable tasks."""

from __future__ import annotations

import json
import logging
from threading import Lock
from typing import Any, List

from hello_agents import SimpleAgent

from config import Configuration
from models import SummaryState, TodoItem
from prompts import get_current_date, todo_planner_instructions
from research.operations import OperationScope
from utils import strip_thinking_tokens

logger = logging.getLogger(__name__)


class PlanningService:
    """Wraps the planner agent to produce structured TODO items."""

    def __init__(self, planner_agent: SimpleAgent, config: Configuration) -> None:
        """Initialize the service with its planner agent and configuration."""
        self._agent = planner_agent
        self._config = config
        self._agent_lock = Lock()

    def plan_todo_list(
        self,
        state: SummaryState,
        prior_context: dict[str, Any] | None = None,
        related_history: dict[str, Any] | None = None,
        user_memories: dict[str, Any] | None = None,
        *,
        operation_scope: OperationScope | None = None,
    ) -> List[TodoItem]:
        """Ask the planner agent to break the topic into actionable tasks."""
        prompt = todo_planner_instructions.format(
            current_date=get_current_date(),
            research_topic=state.research_topic,
        )

        if prior_context:
            prior_block = self._format_prior_context(prior_context)
            prompt = prior_block + "\n\n" + prompt
            logger.info("Planner prompt augmented with prior research context")

        if related_history:
            history_block = self._format_related_history(related_history)
            if history_block:
                prompt = history_block + "\n\n" + prompt
                logger.info("Planner prompt augmented with related history context")

        if user_memories:
            memory_block = self._format_user_memories(user_memories)
            if memory_block:
                prompt = memory_block + "\n\n" + prompt
                logger.info("Planner prompt augmented with confirmed user memory")

        with self._agent_lock:
            try:
                if operation_scope is None:
                    response = self._agent.run(prompt)
                else:
                    response = self._agent.run(
                        prompt,
                        _research_operation_scope=operation_scope,
                    )
            finally:
                self._agent.clear_history()

        logger.info("Planner response received")

        tasks_payload = self._extract_tasks(response)
        todo_items: List[TodoItem] = []

        for idx, item in enumerate(tasks_payload, start=1):
            title = str(item.get("title") or f"任务{idx}").strip()
            intent = str(item.get("intent") or "聚焦主题的关键问题").strip()
            query = str(item.get("query") or state.research_topic).strip()

            if not query:
                query = state.research_topic or ""

            task = TodoItem(
                id=idx,
                title=title,
                intent=intent,
                query=query,
            )
            todo_items.append(task)

        logger.info("Planner produced %d tasks", len(todo_items))
        return todo_items

    @staticmethod
    def create_fallback_task(state: SummaryState) -> TodoItem:
        """Create a minimal fallback task when planning failed."""
        return TodoItem(
            id=1,
            title="基础背景梳理",
            intent="收集主题的核心背景与最新动态",
            query=f"{state.research_topic} 最新进展" if state.research_topic else "基础背景梳理",
        )

    @staticmethod
    def _format_prior_context(prior: dict[str, Any]) -> str:
        """Build a context block summarising the previous research run."""
        parts: list[str] = [
            "## 上一轮研究发现（请勿重复研究以下已完成的主题）",
            "",
        ]

        key_findings = prior.get("key_findings") or []
        if key_findings:
            parts.append("### 已发现的关键结论")
            for finding in key_findings[:5]:
                parts.append(f"- {finding}")
            parts.append("")

        open_questions = prior.get("open_questions") or []
        if open_questions:
            parts.append("### 待深入探究的问题（请让新任务聚焦于此）")
            for question in open_questions:
                parts.append(f"- {question}")
            parts.append("")

        key_sources = prior.get("key_sources") or []
        if key_sources:
            parts.append("### 上一轮关键来源（可复用）")
            for source in key_sources[:3]:
                parts.append(f"- {source}")
            parts.append("")

        parts.append("请基于以上历史上下文规划新任务，避免重复已完成的调研。")
        return "\n".join(parts)

    @staticmethod
    def _format_related_history(history: dict[str, Any]) -> str:
        """Format bounded historical clues as untrusted leads for the planner."""
        matches = history.get("matches")
        if not isinstance(matches, (list, tuple)):
            return ""
        parts: list[str] = [
            "## 相关历史研究线索（仅作线索，必须重新验证，不可直接当作事实）",
            "",
        ]
        added = 0
        for match in matches[:3]:
            if not isinstance(match, dict):
                continue
            topic = match.get("topic")
            score = match.get("score")
            if not isinstance(topic, str) or not topic.strip():
                continue
            parts.append(f"### 历史主题：{topic[:180]}")
            completed_at = match.get("completed_at")
            if isinstance(completed_at, str) and completed_at.strip():
                parts.append(f"完成时间：{completed_at[:32]}")
            if isinstance(score, (int, float)) and not isinstance(score, bool):
                parts.append(f"相关度：{float(score):.2f}")
            findings = match.get("key_findings")
            if isinstance(findings, (list, tuple)) and findings:
                parts.append("- 可复核结论：" + "；".join(str(item)[:180] for item in findings[:2]))
            sources = match.get("key_sources")
            if isinstance(sources, (list, tuple)) and sources:
                parts.append("- 可复核来源：" + "；".join(str(item)[:180] for item in sources[:1]))
            questions = match.get("open_questions")
            if isinstance(questions, (list, tuple)) and questions:
                parts.append("- 未决问题：" + "；".join(str(item)[:180] for item in questions[:2]))
            parts.append("")
            added += 1
        if not added:
            return ""
        parts.append("请把这些历史内容当作检索提示，优先验证时间、来源和结论，不要复述未经验证的断言。")
        return "\n".join(parts)

    @staticmethod
    def _format_user_memories(memories: dict[str, Any]) -> str:
        """Format confirmed preferences as constraints, never as research evidence."""
        values = memories.get("memories")
        if not isinstance(values, (list, tuple)):
            return ""
        parts: list[str] = [
            "## Confirmed user preferences and facts (planning constraints only; not external evidence)",
            "",
        ]
        added = 0
        for item in values[:20]:
            if not isinstance(item, dict):
                continue
            kind = item.get("kind")
            text = item.get("text")
            if not isinstance(kind, str) or not isinstance(text, str) or not text.strip():
                continue
            label = "preference" if kind == "preference" else "fact"
            parts.append(f"- [{label}] {text[:220]}")
            added += 1
        if not added:
            return ""
        parts.append(
            "Treat these as user-provided working constraints. If they conflict with current verifiable material, explain the conflict and re-validate; do not cite them as sources."
        )
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------
    def _extract_tasks(self, raw_response: str) -> List[dict[str, Any]]:
        """Parse planner output into a list of task dictionaries."""
        text = raw_response.strip()
        if self._config.strip_thinking_tokens:
            text = strip_thinking_tokens(text)

        json_payload = self._extract_json_payload(text)
        tasks: List[dict[str, Any]] = []

        if isinstance(json_payload, dict):
            candidate = json_payload.get("tasks")
            if isinstance(candidate, list):
                for item in candidate:
                    if isinstance(item, dict):
                        tasks.append(item)
        elif isinstance(json_payload, list):
            for item in json_payload:
                if isinstance(item, dict):
                    tasks.append(item)

        return tasks

    def _extract_json_payload(self, text: str) -> dict[str, Any] | list | None:
        """Try to locate and parse a JSON object or array from the text."""
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = text[start : end + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass

        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end > start:
            candidate = text[start : end + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                return None

        return None
