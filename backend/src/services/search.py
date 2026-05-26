"""Search dispatch helpers leveraging HelloAgents SearchTool."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Optional, Tuple

from hello_agents.tools import SearchTool

from config import Configuration
from utils import (
    deduplicate_and_format_sources,
    format_sources,
    get_config_value,
)

logger = logging.getLogger(__name__)

MAX_TOKENS_PER_SOURCE = 2000
_GLOBAL_SEARCH_TOOL = SearchTool(backend="hybrid")


def _cache_dir(config: Configuration) -> Path:
    path = Path(config.notes_workspace).parent / "cache" / "search"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_key(query: str, config: Configuration) -> str:
    content = f"{query}_{get_config_value(config.search_api)}_{config.fetch_full_page}"
    return hashlib.md5(content.encode()).hexdigest()


def _load_from_cache(query: str, config: Configuration) -> dict[str, Any] | None:
    cache_file = _cache_dir(config) / f"{_cache_key(query, config)}.json"
    if cache_file.exists():
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            logger.info("Search cache hit: query=%s", query[:60])
            return data
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Search cache read failed: %s", e)
    return None


def _save_to_cache(query: str, config: Configuration, payload: dict[str, Any]) -> None:
    cache_file = _cache_dir(config) / f"{_cache_key(query, config)}.json"
    try:
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.warning("Search cache write failed: %s", e)


def dispatch_search(
    query: str,
    config: Configuration,
    loop_count: int,
    use_cache: bool = True,
) -> Tuple[dict[str, Any] | None, list[str], Optional[str], str]:
    """Execute configured search backend and normalise response payload."""

    search_api = get_config_value(config.search_api)

    if use_cache and loop_count == 0:
        cached = _load_from_cache(query, config)
        if cached is not None:
            notices = list(cached.get("notices") or [])
            return cached, notices, cached.get("answer"), str(cached.get("backend") or search_api)

    try:
        raw_response = _GLOBAL_SEARCH_TOOL.run(
            {
                "input": query,
                "backend": search_api,
                "mode": "structured",
                "fetch_full_page": config.fetch_full_page,
                "max_results": 5,
                "max_tokens_per_source": MAX_TOKENS_PER_SOURCE,
                "loop_count": loop_count,
            }
        )
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.exception("Search backend %s failed: %s", search_api, exc)
        raise

    if isinstance(raw_response, str):
        notices = [raw_response]
        logger.warning("Search backend %s returned text notice: %s", search_api, raw_response)
        payload: dict[str, Any] = {
            "results": [],
            "backend": search_api,
            "answer": None,
            "notices": notices,
        }
    else:
        payload = raw_response
        notices = list(payload.get("notices") or [])

    backend_label = str(payload.get("backend") or search_api)
    answer_text = payload.get("answer")

    if use_cache and loop_count == 0 and payload.get("results"):
        _save_to_cache(query, config, payload)

    if notices:
        for notice in notices:
            logger.info("Search notice (%s): %s", backend_label, notice)

    logger.info(
        "Search backend=%s resolved_backend=%s answer=%s results=%s",
        search_api,
        backend_label,
        bool(answer_text),
        len(payload.get("results", [])),
    )

    return payload, notices, answer_text, backend_label


def prepare_research_context(
    search_result: dict[str, Any] | None,
    answer_text: Optional[str],
    config: Configuration,
) -> tuple[str, str]:
    """Build structured context and source summary for downstream agents."""

    sources_summary = format_sources(search_result)
    context = deduplicate_and_format_sources(
        search_result or {"results": []},
        max_tokens_per_source=MAX_TOKENS_PER_SOURCE,
        fetch_full_page=config.fetch_full_page,
    )

    if answer_text:
        context = f"AI直接答案：\n{answer_text}\n\n{context}"

    return sources_summary, context
