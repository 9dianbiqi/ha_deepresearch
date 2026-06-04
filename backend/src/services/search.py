"""Search dispatch helpers leveraging HelloAgents SearchTool."""

from __future__ import annotations

import hashlib
import json
import logging
import random
import time
from pathlib import Path
from typing import Any, Optional, Tuple

from hello_agents.tools import SearchTool

from config import Configuration, SearchAPI
from utils import (
    deduplicate_and_format_sources,
    format_sources,
    get_config_value,
)

logger = logging.getLogger(__name__)

MAX_TOKENS_PER_SOURCE = 2000
_GLOBAL_SEARCH_TOOL = SearchTool(backend="hybrid")

# --- 搜索重试与降级配置 ---
SEARCH_MAX_RETRIES = 2          # 单后端最大重试次数
SEARCH_BACKOFF_BASE = 1.5       # 指数退避基数（秒）
CACHE_TTL = 86400               # 搜索缓存有效期（秒），24小时


def _cache_dir(config: Configuration) -> Path:
    path = Path(config.notes_workspace).parent / "cache" / "search"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_key(query: str, config: Configuration) -> str:
    content = f"{query}_{get_config_value(config.search_api)}_{config.fetch_full_page}"
    return hashlib.md5(content.encode()).hexdigest()


def _load_from_cache(query: str, config: Configuration) -> dict[str, Any] | None:
    cache_file = _cache_dir(config) / f"{_cache_key(query, config)}.json"
    if not cache_file.exists():
        return None
    # 检查缓存是否过期
    if time.time() - cache_file.stat().st_mtime > CACHE_TTL:
        cache_file.unlink(missing_ok=True)
        return None
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


def _try_single_search(
    query: str,
    search_api: str,
    config: Configuration,
    loop_count: int,
) -> dict[str, Any]:
    """Execute a single search call against one backend.  No retry — the caller loops."""

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

    if isinstance(raw_response, str):
        logger.warning("Search backend %s returned text notice: %s", search_api, raw_response)
        return {
            "results": [],
            "backend": search_api,
            "answer": None,
            "notices": [raw_response],
        }
    elif isinstance(raw_response, dict):
        return raw_response
    else:
        logger.error(
            "Search backend %s returned unexpected type: %s", search_api, type(raw_response),
        )
        return {
            "results": [],
            "backend": search_api,
            "answer": None,
            "notices": [f"Search returned unexpected type: {type(raw_response).__name__}"],
        }


def dispatch_search(
    query: str,
    config: Configuration,
    loop_count: int,
    use_cache: bool = True,
) -> Tuple[dict[str, Any] | None, list[str], Optional[str], str]:
    """Execute configured search backend with retry + fallback + cache."""

    primary_api = get_config_value(config.search_api)

    # 构建降级链：主后端 → DuckDuckGo（免费，无需 API key）
    backends: list[str] = [primary_api]
    ddg_value = SearchAPI.DUCKDUCKGO.value
    if primary_api != ddg_value:
        backends.append(ddg_value)

    # 缓存只在首次搜索时生效
    if use_cache and loop_count == 0:
        cached = _load_from_cache(query, config)
        if cached is not None:
            notices = list(cached.get("notices") or [])
            return cached, notices, cached.get("answer"), str(cached.get("backend") or primary_api)

    last_error: Optional[Exception] = None

    for backend in backends:
        for attempt in range(SEARCH_MAX_RETRIES + 1):
            try:
                payload = _try_single_search(query, backend, config, loop_count)
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Search attempt %d/%d backend=%s failed: %s",
                    attempt + 1, SEARCH_MAX_RETRIES + 1, backend, exc,
                )
                if attempt < SEARCH_MAX_RETRIES:
                    delay = SEARCH_BACKOFF_BASE ** attempt + random.uniform(0, 1)
                    time.sleep(delay)
                continue

            # 成功 — 记录并返回
            notices = list(payload.get("notices") or [])
            backend_label = str(payload.get("backend") or backend)
            answer_text = payload.get("answer")

            if use_cache and loop_count == 0 and payload.get("results"):
                _save_to_cache(query, config, payload)

            if notices:
                for notice in notices:
                    logger.info("Search notice (%s): %s", backend_label, notice)

            logger.info(
                "Search backend=%s resolved_backend=%s answer=%s results=%s",
                backend, backend_label, bool(answer_text),
                len(payload.get("results", [])),
            )
            return payload, notices, answer_text, backend_label

        logger.warning("All retries exhausted for backend=%s, trying next fallback", backend)

    # 所有后端都失败 — 降级返回空结果
    logger.error("All search backends failed (last error: %s)", last_error)
    fallback_payload: dict[str, Any] = {
        "results": [],
        "backend": "none",
        "answer": None,
        "notices": ["搜索服务暂时不可用，请稍后重试。"],
    }
    return fallback_payload, fallback_payload["notices"], None, "none"


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
