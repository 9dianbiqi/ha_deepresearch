"""Search dispatch helpers leveraging HelloAgents SearchTool."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Protocol, Tuple

from config import Configuration, SearchAPI
from research.adapters import HelloAgentsSearchAdapter
from research.operations import OperationRejectedError, OperationScope, OperationSpec
from research.session import CancellationRequestedError, DeadlineExceededError
from utils import (
    deduplicate_and_format_sources,
    format_sources,
    get_config_value,
    sanitize_reference_url,
    sanitize_text_urls,
)

logger = logging.getLogger(__name__)

MAX_TOKENS_PER_SOURCE = 2000
SEARCH_NOTICE_CODE = "search_backend_notice"
SEARCH_NOTICE_MESSAGE = "Search backend returned a notice."
SEARCH_UNAVAILABLE_CODE = "search_backend_unavailable"
SEARCH_UNAVAILABLE_MESSAGE = "搜索服务暂时不可用，请稍后重试。"
SEARCH_NOTICE_MESSAGES = {
    SEARCH_NOTICE_CODE: SEARCH_NOTICE_MESSAGE,
    SEARCH_UNAVAILABLE_CODE: SEARCH_UNAVAILABLE_MESSAGE,
}
# --- 搜索重试与降级配置 ---
SEARCH_MAX_RETRIES = 2          # 单后端最大重试次数
SEARCH_BACKOFF_BASE = 1.5       # 指数退避基数（秒）
CACHE_TTL = 86400               # 搜索缓存有效期（秒），24小时
CACHE_SCHEMA_VERSION = 1
CACHE_CONTENT_CHAR_LIMIT = 2000
CACHE_MAX_FILE_BYTES = 256 * 1024
CACHE_SWEEP_MAX_ENTRIES = 512
CACHE_SWEEP_TIME_BUDGET_SECONDS = 1.0
MAX_SAFE_SEARCH_RESULTS = 5
MAX_SEARCH_TITLE_CHARS = 300
MAX_SEARCH_URL_CHARS = 8192
_CACHE_FILE_PATTERN = re.compile(r"[0-9a-f]{32}\.json\Z")
_CACHE_TEMP_PATTERN = re.compile(
    r"\.[0-9a-f]{32}\.[a-z0-9_]{8}\.tmp\Z"
)
_SAFE_BACKEND_LABELS = frozenset(
    {backend.value for backend in SearchAPI} | {"hybrid", "none"}
)


class CancellationWaiter(Protocol):
    """Minimal cooperative cancellation interface for retry waits."""

    def wait(self, timeout: float) -> bool:
        """Return whether cancellation happened before ``timeout``."""

    def raise_if_cancelled(self) -> None:
        """Raise the caller's cancellation exception when requested."""


class SearchRunner(Protocol):
    """Typed public SearchTool-compatible boundary used by the dispatcher."""

    def run(self, parameters: dict[str, Any]) -> str | dict[str, Any]:
        """Run one physical backend attempt."""


def _sanitize_search_payload(
    payload: object,
    *,
    include_raw_content: bool = False,
    cache_projection: bool = False,
    trusted_backend: str | None = None,
) -> dict[str, Any]:
    """Project provider data into the single bounded, URL-safe search shape."""
    source = payload if isinstance(payload, dict) else {}
    safe_results: list[dict[str, str]] = []
    raw_results = source.get("results")
    if isinstance(raw_results, (list, tuple)):
        for raw_result in raw_results:
            if len(safe_results) >= MAX_SAFE_SEARCH_RESULTS:
                break
            if not isinstance(raw_result, dict):
                continue
            safe_url = sanitize_reference_url(raw_result.get("url"))
            if not safe_url or len(safe_url) > MAX_SEARCH_URL_CHARS:
                continue
            content = sanitize_text_urls(raw_result.get("content"))
            if cache_projection:
                content = content[:CACHE_CONTENT_CHAR_LIMIT]
            safe_result = {
                "title": (
                    sanitize_text_urls(raw_result.get("title")) or safe_url
                )[:MAX_SEARCH_TITLE_CHARS],
                "url": safe_url,
                "content": content,
            }
            if include_raw_content and not cache_projection:
                raw_content = sanitize_text_urls(raw_result.get("raw_content"))
                if raw_content:
                    safe_result["raw_content"] = raw_content
            safe_results.append(safe_result)

    requested_codes = source.get("notice_codes")
    codes: list[str] = []
    if isinstance(requested_codes, (list, tuple)):
        codes = [
            code
            for code in requested_codes
            if isinstance(code, str) and code in SEARCH_NOTICE_MESSAGES
        ]
    if not codes and source.get("notices"):
        codes = [SEARCH_NOTICE_CODE]
    codes = list(dict.fromkeys(codes))

    requested_backend = (
        trusted_backend
        if trusted_backend in _SAFE_BACKEND_LABELS
        else source.get("backend")
    )
    backend = (
        requested_backend
        if isinstance(requested_backend, str)
        and requested_backend in _SAFE_BACKEND_LABELS
        else "none"
    )
    sanitized: dict[str, Any] = {
        "results": safe_results,
        "backend": backend,
        "notices": [SEARCH_NOTICE_MESSAGES[code] for code in codes],
        "notice_codes": codes,
    }
    if cache_projection:
        sanitized["schema_version"] = CACHE_SCHEMA_VERSION
    else:
        raw_answer = source.get("answer")
        sanitized["answer"] = (
            sanitize_text_urls(raw_answer) if isinstance(raw_answer, str) else None
        )
    return sanitized


def _cache_path(config: Configuration) -> Path:
    """Return the exact search-cache path without creating it."""
    return Path(config.notes_workspace).parent / "cache" / "search"


def _is_link_like(status: os.stat_result) -> bool:
    """Recognize symlinks and Windows reparse-point directories."""
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(status, "st_file_attributes", 0)
    return (
        stat.S_ISLNK(status.st_mode)
        or bool(getattr(status, "st_reparse_tag", 0))
        or bool(file_attributes & reparse_flag)
    )


def _validate_cache_directory(
    config: Configuration,
    *,
    require_existing: bool,
) -> tuple[Path, tuple[int, int]] | None:
    """Return a cache directory confined below the resolved configured root."""
    configured_root = Path(config.notes_workspace).expanduser().parent
    try:
        if not require_existing:
            configured_root.mkdir(parents=True, exist_ok=True)
        resolved_root = configured_root.resolve(strict=True)
        cache_parent = resolved_root / "cache"
        cache_path = cache_parent / "search"
        if not require_existing:
            cache_parent.mkdir(exist_ok=True)
        parent_status = cache_parent.lstat()
        resolved_parent = cache_parent.resolve(strict=True)
        resolved_parent.relative_to(resolved_root)
        if _is_link_like(parent_status) or not stat.S_ISDIR(
            parent_status.st_mode
        ):
            return None
        if not require_existing:
            cache_path.mkdir(exist_ok=True)
        cache_status = cache_path.lstat()
        resolved_cache = cache_path.resolve(strict=True)
        resolved_cache.relative_to(resolved_root)
    except (FileNotFoundError, NotADirectoryError, OSError, ValueError):
        return None
    if (
        _is_link_like(cache_status)
        or not stat.S_ISDIR(cache_status.st_mode)
    ):
        return None
    return cache_path, (cache_status.st_dev, cache_status.st_ino)


def _read_bounded_cache_payload(
    entry: Path,
    expected_status: os.stat_result,
) -> object:
    """Read one unchanged regular cache file without unbounded allocation."""
    if expected_status.st_size > CACHE_MAX_FILE_BYTES:
        raise ValueError("Oversized search cache entry")
    with open(entry, "rb") as handle:
        opened_status = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(opened_status.st_mode)
            or not os.path.samestat(expected_status, opened_status)
        ):
            raise ValueError("Search cache entry changed during read")
        encoded = handle.read(CACHE_MAX_FILE_BYTES + 1)
    if len(encoded) > CACHE_MAX_FILE_BYTES:
        raise ValueError("Oversized search cache entry")
    return json.loads(encoded.decode("utf-8"))


def _cache_directory_is_unchanged(
    cache_path: Path,
    expected_identity: tuple[int, int],
) -> bool:
    """Check that a validated directory was not replaced during a sweep."""
    try:
        status = cache_path.lstat()
    except (FileNotFoundError, NotADirectoryError, OSError):
        return False
    return (
        not _is_link_like(status)
        and stat.S_ISDIR(status.st_mode)
        and (status.st_dev, status.st_ino) == expected_identity
    )


def _cache_dir(config: Configuration) -> Path:
    validated = _validate_cache_directory(config, require_existing=False)
    if validated is None:
        raise OSError("Unsafe search cache directory")
    return validated[0]


def _remove_cache_entry(
    path: Path,
    *,
    cache_path: Path,
    cache_identity: tuple[int, int],
) -> None:
    """Remove one already-classified cache entry without following symlinks."""
    if not _cache_directory_is_unchanged(cache_path, cache_identity):
        return
    path.unlink(missing_ok=True)


def _is_exact_cache_payload(value: object) -> bool:
    """Return whether a cache object is the canonical schema-v1 projection."""
    if not isinstance(value, dict):
        return False
    schema_version = value.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != CACHE_SCHEMA_VERSION
    ):
        return False
    return _sanitize_search_payload(value, cache_projection=True) == value


def _sweep_search_cache_entry(
    entry: Path,
    *,
    now: float,
    cache_path: Path,
    cache_identity: tuple[int, int],
) -> None:
    """Validate or remove one direct child of the cache directory."""
    if not _cache_directory_is_unchanged(cache_path, cache_identity):
        return
    try:
        entry_status = entry.lstat()
    except FileNotFoundError:
        return

    name = entry.name
    is_cache_file = _CACHE_FILE_PATTERN.fullmatch(name) is not None
    is_app_temp = _CACHE_TEMP_PATTERN.fullmatch(name) is not None
    if _is_link_like(entry_status):
        if is_cache_file or is_app_temp:
            _remove_cache_entry(
                entry,
                cache_path=cache_path,
                cache_identity=cache_identity,
            )
        return
    if not stat.S_ISREG(entry_status.st_mode):
        return
    if is_app_temp:
        _remove_cache_entry(
            entry,
            cache_path=cache_path,
            cache_identity=cache_identity,
        )
        return
    if not is_cache_file:
        return
    if now - entry_status.st_mtime > CACHE_TTL:
        _remove_cache_entry(
            entry,
            cache_path=cache_path,
            cache_identity=cache_identity,
        )
        return
    if entry_status.st_size > CACHE_MAX_FILE_BYTES:
        _remove_cache_entry(
            entry,
            cache_path=cache_path,
            cache_identity=cache_identity,
        )
        return

    try:
        payload = _read_bounded_cache_payload(entry, entry_status)
    except (json.JSONDecodeError, UnicodeError, OSError, ValueError, RecursionError):
        _remove_cache_entry(
            entry,
            cache_path=cache_path,
            cache_identity=cache_identity,
        )
        return
    if not _is_exact_cache_payload(payload):
        _remove_cache_entry(
            entry,
            cache_path=cache_path,
            cache_identity=cache_identity,
        )
        return
    if not _cache_directory_is_unchanged(cache_path, cache_identity):
        return
    try:
        current_status = entry.lstat()
    except (FileNotFoundError, NotADirectoryError, OSError):
        return
    if _is_link_like(current_status) or not os.path.samestat(
        entry_status,
        current_status,
    ):
        return
    try:
        os.chmod(entry, 0o600)
    except OSError:
        pass


def sweep_search_cache(config: Configuration) -> None:
    """Remove legacy, invalid, expired, and abandoned cache entries safely."""
    try:
        validated = _validate_cache_directory(config, require_existing=True)
    except Exception:
        logger.warning("Search cache sweep failed")
        return
    if validated is None:
        return
    cache_path, cache_identity = validated
    now = time.time()
    deadline = time.monotonic() + CACHE_SWEEP_TIME_BUDGET_SECONDS
    scanned = 0
    try:
        with os.scandir(cache_path) as entries:
            while (
                scanned < CACHE_SWEEP_MAX_ENTRIES
                and time.monotonic() < deadline
            ):
                try:
                    raw_entry = next(entries)
                except StopIteration:
                    break
                scanned += 1
                if not _cache_directory_is_unchanged(
                    cache_path,
                    cache_identity,
                ):
                    break
                try:
                    _sweep_search_cache_entry(
                        cache_path / raw_entry.name,
                        now=now,
                        cache_path=cache_path,
                        cache_identity=cache_identity,
                    )
                except Exception:
                    logger.warning("Search cache sweep entry cleanup failed")
    except Exception:
        logger.warning("Search cache sweep failed")


def _cache_key(query: str, config: Configuration) -> str:
    content = f"{query}_{get_config_value(config.search_api)}_{config.fetch_full_page}"
    return hashlib.md5(content.encode()).hexdigest()


def _query_hash(query: str) -> str:
    """Return a safe stable identifier for query diagnostics and audit data."""
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def _load_from_cache(query: str, config: Configuration) -> dict[str, Any] | None:
    cache_file: Path | None = None
    cache_path: Path | None = None
    cache_identity: tuple[int, int] | None = None
    try:
        cache_path = _cache_dir(config)
        cache_status = cache_path.lstat()
        cache_identity = (cache_status.st_dev, cache_status.st_ino)
        cache_file = cache_path / f"{_cache_key(query, config)}.json"
        try:
            entry_status = cache_file.lstat()
        except FileNotFoundError:
            return None
        if _is_link_like(entry_status) or not stat.S_ISREG(entry_status.st_mode):
            _remove_cache_entry(
                cache_file,
                cache_path=cache_path,
                cache_identity=cache_identity,
            )
            return None
        if (
            time.time() - entry_status.st_mtime > CACHE_TTL
            or entry_status.st_size > CACHE_MAX_FILE_BYTES
        ):
            _remove_cache_entry(
                cache_file,
                cache_path=cache_path,
                cache_identity=cache_identity,
            )
            return None
        data = _read_bounded_cache_payload(cache_file, entry_status)
        if not isinstance(data, dict):
            raise ValueError("Search cache payload must be an object.")
        schema_version = data.get("schema_version")
        if (
            not isinstance(schema_version, int)
            or isinstance(schema_version, bool)
            or schema_version != CACHE_SCHEMA_VERSION
        ):
            _remove_cache_entry(
                cache_file,
                cache_path=cache_path,
                cache_identity=cache_identity,
            )
            return None
        projected = _sanitize_search_payload(data, cache_projection=True)
        if projected != data:
            _remove_cache_entry(
                cache_file,
                cache_path=cache_path,
                cache_identity=cache_identity,
            )
            return None
        logger.info("Search cache hit: query_hash=%s", _query_hash(query))
        return projected
    except (json.JSONDecodeError, UnicodeError, OSError, ValueError, RecursionError):
        logger.warning("Search cache read failed")
        if (
            cache_file is not None
            and cache_path is not None
            and cache_identity is not None
        ):
            try:
                _remove_cache_entry(
                    cache_file,
                    cache_path=cache_path,
                    cache_identity=cache_identity,
                )
            except OSError:
                logger.warning("Search cache cleanup failed")
    return None


def _save_to_cache(query: str, config: Configuration, payload: dict[str, Any]) -> None:
    cache_payload = _sanitize_search_payload(payload, cache_projection=True)
    encoded_payload = json.dumps(
        cache_payload,
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    if len(encoded_payload) > CACHE_MAX_FILE_BYTES:
        logger.warning("Search cache write skipped oversized payload")
        return
    cache_file: Path | None = None
    temp_path: Path | None = None
    try:
        cache_file = _cache_dir(config) / f"{_cache_key(query, config)}.json"
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=cache_file.parent,
            prefix=f".{cache_file.stem}.",
            suffix=".tmp",
            delete=False,
        ) as f:
            temp_path = Path(f.name)
            f.write(encoded_payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, cache_file)
        temp_path = None
        try:
            os.chmod(cache_file, 0o600)
        except OSError:
            pass
    except OSError:
        logger.warning("Search cache write failed")
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Search cache temporary cleanup failed")


def _try_single_search(
    query: str,
    search_api: str,
    config: Configuration,
    loop_count: int,
    search_adapter: SearchRunner,
) -> dict[str, Any]:
    """Execute a single search call against one backend.  No retry — the caller loops."""
    raw_response = search_adapter.run(
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
        logger.warning("Search backend %s returned a text notice", search_api)
        return _sanitize_search_payload(
            {
                "results": [],
                "answer": None,
                "notices": [True],
            },
            trusted_backend=search_api,
        )
    elif isinstance(raw_response, dict):
        return _sanitize_search_payload(
            raw_response,
            include_raw_content=config.fetch_full_page,
            trusted_backend=search_api,
        )
    else:
        logger.error(
            "Search backend %s returned unexpected type: %s", search_api, type(raw_response),
        )
        return _sanitize_search_payload(
            {
                "results": [],
                "answer": None,
                "notices": [True],
            },
            trusted_backend=search_api,
        )


def dispatch_search(
    query: str,
    config: Configuration,
    loop_count: int,
    use_cache: bool = False,
    cancellation: CancellationWaiter | None = None,
    *,
    operation_scope: OperationScope | None = None,
    search_adapter: SearchRunner | None = None,
) -> Tuple[dict[str, Any] | None, list[str], str | None, str]:
    """Execute configured search backend with retry + fallback + cache."""
    if cancellation is not None:
        cancellation.raise_if_cancelled()

    primary_api = get_config_value(config.search_api)
    runner = search_adapter or HelloAgentsSearchAdapter()
    query_hash = _query_hash(query)
    configured_capabilities = _search_capabilities(primary_api)

    # 构建降级链：主后端 → DuckDuckGo（免费，无需 API key）
    backends: list[str] = [primary_api]
    ddg_value = SearchAPI.DUCKDUCKGO.value
    if primary_api != ddg_value:
        backends.append(ddg_value)

    # 缓存只在首次搜索时生效
    if use_cache and loop_count == 0:
        if operation_scope is None:
            cached = _load_from_cache(query, config)
        else:
            cache_spec = operation_scope.spec(
                operation_name="search.cache_read",
                capabilities=configured_capabilities,
                resource={"query_hash": query_hash, "backend": primary_api},
            )
            cached = operation_scope.operations.call(
                cache_spec,
                lambda: _load_from_cache(query, config),
            )
        if cached is not None:
            cached = _sanitize_search_payload(
                cached,
                trusted_backend=primary_api,
            )
            notices = list(cached.get("notices") or [])
            return cached, notices, cached.get("answer"), str(cached.get("backend") or primary_api)

    logical_operation_id: str | None = None
    if operation_scope is not None:
        logical_operation_id = operation_scope.spec(
            operation_name="search.execute",
            capabilities=configured_capabilities,
            resource={"query_hash": query_hash, "backend": primary_api},
        ).operation_id

    for fallback_index, backend in enumerate(backends, start=1):
        for attempt in range(SEARCH_MAX_RETRIES + 1):
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            try:
                if operation_scope is None:
                    payload = _try_single_search(
                        query,
                        backend,
                        config,
                        loop_count,
                        runner,
                    )
                else:
                    spec = OperationSpec(
                        operation_name="search.execute",
                        capabilities=_search_capabilities(backend),
                        resource={"query_hash": query_hash, "backend": backend},
                        operation_id=logical_operation_id or "",
                        task_id=operation_scope.task_id,
                        task_attempt=operation_scope.task_attempt,
                        operation_attempt=attempt + 1,
                        fallback_index=fallback_index,
                    )
                    payload = operation_scope.operations.call(
                        spec,
                        lambda: _try_single_search(
                            query,
                            backend,
                            config,
                            loop_count,
                            runner,
                        ),
                    )
            except (
                OperationRejectedError,
                CancellationRequestedError,
                DeadlineExceededError,
            ):
                raise
            except Exception:
                logger.warning(
                    "Search attempt %d/%d backend=%s failed",
                    attempt + 1, SEARCH_MAX_RETRIES + 1, backend,
                )
                if attempt < SEARCH_MAX_RETRIES:
                    delay = SEARCH_BACKOFF_BASE ** attempt + random.uniform(0, 1)
                    if cancellation is None:
                        time.sleep(delay)
                    elif cancellation.wait(delay):
                        cancellation.raise_if_cancelled()
                continue

            # 成功 — 记录并返回
            payload = _sanitize_search_payload(
                payload,
                include_raw_content=config.fetch_full_page,
                trusted_backend=backend,
            )
            notices = list(payload.get("notices") or [])
            backend_label = str(payload.get("backend") or backend)
            answer_text = payload.get("answer")

            if use_cache and loop_count == 0 and payload.get("results"):
                if operation_scope is None:
                    _save_to_cache(query, config, payload)
                else:
                    cache_spec = operation_scope.spec(
                        operation_name="search.cache_write",
                        capabilities=configured_capabilities,
                        resource={"query_hash": query_hash, "backend": backend},
                    )
                    operation_scope.operations.call(
                        cache_spec,
                        lambda: _save_to_cache(query, config, payload),
                    )

            if notices:
                logger.info("Search backend=%s returned %d notice(s)", backend_label, len(notices))

            logger.info(
                "Search backend=%s resolved_backend=%s answer=%s results=%s",
                backend, backend_label, bool(answer_text),
                len(payload.get("results", [])),
            )
            return payload, notices, answer_text, backend_label

        logger.warning("All retries exhausted for backend=%s, trying next fallback", backend)

    # 所有后端都失败 — 降级返回空结果
    logger.error("All search backends failed")
    fallback_payload: dict[str, Any] = {
        "results": [],
        "backend": "none",
        "answer": None,
        "notices": [SEARCH_UNAVAILABLE_MESSAGE],
        "notice_codes": [SEARCH_UNAVAILABLE_CODE],
    }
    return fallback_payload, fallback_payload["notices"], None, "none"


def _search_capabilities(backend: str) -> tuple[str, ...]:
    """Return the complete atomic capability set for one backend."""
    capabilities = ["search:web"]
    if backend == SearchAPI.PERPLEXITY.value:
        capabilities.append("search:premium")
    return tuple(capabilities)


def prepare_research_context(
    search_result: dict[str, Any] | None,
    answer_text: str | None,
    config: Configuration,
) -> tuple[str, str]:
    """Build structured context and source summary for downstream agents."""
    safe_result = _sanitize_search_payload(
        search_result or {"results": []},
        include_raw_content=config.fetch_full_page,
        trusted_backend=get_config_value(config.search_api),
    )
    sources_summary = format_sources(safe_result)
    context = deduplicate_and_format_sources(
        safe_result,
        max_tokens_per_source=MAX_TOKENS_PER_SOURCE,
        fetch_full_page=config.fetch_full_page,
    )

    safe_answer = sanitize_text_urls(answer_text)
    if safe_answer:
        context = f"AI直接答案：\n{safe_answer}\n\n{context}"

    return sources_summary, context
