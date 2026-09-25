"""Security contracts for search-result projection and caching."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from config import Configuration, SearchAPI
from services import search as search_service


class _MaliciousSearchRunner:
    """Return useful fields mixed with provider-controlled secret material."""

    def run(self, parameters: dict[str, Any]) -> dict[str, Any]:
        assert parameters["backend"] == SearchAPI.DUCKDUCKGO.value
        return {
            "results": [
                {
                    "title": "Stable result",
                    "url": (
                        "https://url-user:url-password@example.test/reference/path"
                        "?token=TOKEN_SENTINEL#SIGNATURE_SENTINEL"
                    ),
                    "content": (
                        "Useful snippet from "
                        "https://content-user:content-password@cdn.example.test/article"
                        "?signature=SIGNATURE_SENTINEL#TOKEN_SENTINEL"
                        + (" A" * 2500)
                        + " TRUNCATED_CONTENT_SENTINEL"
                    ),
                    "raw_content": (
                        "Full page from https://raw-user:raw-password@raw.example.test/body"
                        "?token=RAW_CONTENT_SENTINEL&signature=SIGNATURE_SENTINEL"
                    ),
                    "headers": {"Authorization": "ARBITRARY_PAYLOAD_SENTINEL"},
                }
            ],
            "backend": SearchAPI.DUCKDUCKGO.value,
            "answer": (
                "Direct answer at https://answer-user:answer-password@example.test/answer"
                "?token=TOKEN_SENTINEL#SIGNATURE_SENTINEL"
            ),
            "provider_payload": {"secret": "ARBITRARY_PAYLOAD_SENTINEL"},
        }


class _ForbiddenSearchRunner:
    """Fail if a cache hit invokes the provider."""

    def run(self, parameters: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError(f"cache hit invoked provider: {parameters!r}")


def _config(tmp_path: Path) -> Configuration:
    return Configuration.from_env(
        overrides={
            "search_api": SearchAPI.DUCKDUCKGO,
            "notes_workspace": str(tmp_path / "notes"),
            "fetch_full_page": True,
            "enable_notes": False,
            "enable_github_research": False,
        }
    )


def _sweep_search_cache(config: Configuration) -> None:
    """Call the production sweep after a useful pre-implementation assertion."""
    assert hasattr(search_service, "sweep_search_cache"), (
        "search cache directory sweep has not been implemented"
    )
    search_service.sweep_search_cache(config)


def test_search_projection_keeps_useful_fields_without_persisting_secrets(
    tmp_path: Path,
) -> None:
    """Provider payloads must become a bounded, URL-safe runtime/cache shape."""
    config = _config(tmp_path)
    query = "security projection"

    live, notices, answer, backend = search_service.dispatch_search(
        query,
        config,
        0,
        use_cache=True,
        search_adapter=_MaliciousSearchRunner(),
    )
    assert live is not None
    sources, context = search_service.prepare_research_context(live, answer, config)

    cache_file = (
        search_service._cache_dir(config)
        / f"{search_service._cache_key(query, config)}.json"
    )
    cached_wire = json.loads(cache_file.read_text(encoding="utf-8"))
    cached, cached_notices, cached_answer, cached_backend = (
        search_service.dispatch_search(
            query,
            config,
            0,
            use_cache=True,
            search_adapter=_ForbiddenSearchRunner(),
        )
    )

    serialized = json.dumps(
        {
            "live": live,
            "cache": cached_wire,
            "cached": cached,
            "sources": sources,
            "context": context,
        },
        ensure_ascii=False,
    )
    for sentinel in (
        "url-user",
        "url-password",
        "content-user",
        "content-password",
        "raw-user",
        "raw-password",
        "answer-user",
        "answer-password",
        "TOKEN_SENTINEL",
        "SIGNATURE_SENTINEL",
        "RAW_CONTENT_SENTINEL",
        "ARBITRARY_PAYLOAD_SENTINEL",
        "provider_payload",
        "headers",
    ):
        assert sentinel not in serialized

    assert live["results"][0]["title"] == "Stable result"
    assert live["results"][0]["url"] == "https://example.test/reference/path"
    assert "Useful snippet" in live["results"][0]["content"]
    assert "TRUNCATED_CONTENT_SENTINEL" in live["results"][0]["content"]
    assert "Full page" in live["results"][0]["raw_content"]
    assert sources == "* Stable result : https://example.test/reference/path"
    assert "https://cdn.example.test/article" in context
    assert "https://raw.example.test/body" in context
    assert backend == cached_backend == SearchAPI.DUCKDUCKGO.value
    assert notices == cached_notices == []
    assert answer is not None and "Direct answer" in answer
    assert cached_answer is None

    assert set(cached_wire) == {
        "schema_version",
        "results",
        "backend",
        "notices",
        "notice_codes",
    }
    assert set(cached_wire["results"][0]) == {"title", "url", "content"}
    assert cached_wire["results"][0]["title"] == "Stable result"
    assert "Useful snippet" in cached_wire["results"][0]["content"]
    assert len(cached_wire["results"][0]["content"]) <= 2000
    assert "TRUNCATED_CONTENT_SENTINEL" not in json.dumps(cached_wire)
    assert "raw_content" not in cached_wire["results"][0]
    assert "answer" not in cached_wire


def test_search_projection_keeps_distinct_safe_query_identities(
    tmp_path: Path,
) -> None:
    """Results that differ by ordinary query parameters must not deduplicate."""

    class _IdentitySearchRunner:
        def run(self, parameters: dict[str, Any]) -> dict[str, Any]:
            assert parameters["backend"] == SearchAPI.DUCKDUCKGO.value
            return {
                "results": [
                    {
                        "title": "English page",
                        "url": (
                            "https://example.test/view?id=41&lang=en"
                            "&token=discard-first"
                        ),
                        "content": "First page",
                    },
                    {
                        "title": "Chinese page",
                        "url": (
                            "https://example.test/view?id=42&lang=zh"
                            "&signature=discard-second"
                        ),
                        "content": "Second page",
                    },
                ],
                "backend": SearchAPI.DUCKDUCKGO.value,
            }

    config = _config(tmp_path)
    live, _notices, answer, _backend = search_service.dispatch_search(
        "query identity",
        config,
        0,
        search_adapter=_IdentitySearchRunner(),
    )
    assert live is not None
    sources, context = search_service.prepare_research_context(live, answer, config)

    expected_urls = [
        "https://example.test/view?id=41&lang=en",
        "https://example.test/view?id=42&lang=zh",
    ]
    assert [item["url"] for item in live["results"]] == expected_urls
    assert all(url in sources for url in expected_urls)
    assert all(url in context for url in expected_urls)
    assert "discard-first" not in f"{sources}\n{context}"
    assert "discard-second" not in f"{sources}\n{context}"


def test_search_cache_is_enabled_by_default(tmp_path: Path) -> None:
    """Ordinary searches use the configured 24-hour cache by default."""
    config = _config(tmp_path)
    query = "default cache behavior"

    result, *_rest = search_service.dispatch_search(
        query,
        config,
        0,
        search_adapter=_MaliciousSearchRunner(),
    )

    assert result and result["results"][0]["title"] == "Stable result"
    cache_dir = Path(config.notes_workspace).parent / "cache" / "search"
    cache_file = (
        cache_dir / f"{search_service._cache_key(query, config)}.json"
    )
    assert cache_dir.exists()
    assert cache_file.exists()

    cached, *_rest = search_service.dispatch_search(
        query,
        config,
        7,
        search_adapter=_ForbiddenSearchRunner(),
    )
    assert cached is not None
    assert cached["cache_hit"] is True
    assert cached["original_query"] == query


def test_freshness_sensitive_query_bypasses_default_cache(tmp_path: Path) -> None:
    """Current-information intent bypasses both default cache reads and writes."""
    config = _config(tmp_path)
    query = "Redis latest version"

    result, *_rest = search_service.dispatch_search(
        query,
        config,
        0,
        search_adapter=_MaliciousSearchRunner(),
    )

    assert result is not None
    assert result["cache_hit"] is False
    cache_file = (
        Path(config.notes_workspace).parent
        / "cache"
        / "search"
        / f"{search_service._cache_key(query, config)}.json"
    )
    assert not cache_file.exists()


def test_cache_read_deletes_legacy_provider_payload_without_reusing_it(
    tmp_path: Path,
) -> None:
    """Accessing an old unsafe cache entry must remove it instead of reusing it."""
    config = _config(tmp_path)
    query = "legacy cache"
    cache_file = (
        search_service._cache_dir(config)
        / f"{search_service._cache_key(query, config)}.json"
    )
    cache_file.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "title": "Legacy result",
                        "url": (
                            "https://legacy-user:legacy-password@example.test/path"
                            "?token=LEGACY_TOKEN#LEGACY_SIGNATURE"
                        ),
                        "content": "Useful legacy snippet",
                        "raw_content": "LEGACY_RAW_CONTENT_SENTINEL",
                    }
                ],
                "backend": "duckduckgo",
                "answer": "LEGACY_ANSWER_SENTINEL",
                "arbitrary": "LEGACY_PAYLOAD_SENTINEL",
            }
        ),
        encoding="utf-8",
    )

    assert search_service._load_from_cache(query, config) is None
    assert not cache_file.exists()


def test_cache_projection_bounds_result_count_and_title_length(tmp_path: Path) -> None:
    """Durable search entries must remain small even for oversized providers."""
    config = _config(tmp_path)
    query = "bounded cache"
    payload = {
        "results": [
            {
                "title": f"result-{index}-" + ("T" * 1000),
                "url": f"https://example.test/{index}?token=discarded",
                "content": "useful snippet",
            }
            for index in range(8)
        ],
        "backend": "duckduckgo",
    }

    search_service._save_to_cache(query, config, payload)
    cache_file = (
        search_service._cache_dir(config)
        / f"{search_service._cache_key(query, config)}.json"
    )
    wire = json.loads(cache_file.read_text(encoding="utf-8"))

    assert len(wire["results"]) == 5
    assert all(len(item["title"]) <= 300 for item in wire["results"])
    assert "discarded" not in json.dumps(wire)


def test_search_cache_sweep_does_not_create_an_absent_directory(
    tmp_path: Path,
) -> None:
    """Startup migration must stay read-only when no cache exists."""
    config = _config(tmp_path)
    cache_dir = Path(config.notes_workspace).parent / "cache" / "search"

    _sweep_search_cache(config)

    assert not cache_dir.exists()


def test_search_cache_sweep_cleans_only_invalid_expired_and_app_temp_entries(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Sweep a mixed legacy directory without traversing or deleting unrelated data."""
    config = _config(tmp_path)
    cache_dir = Path(config.notes_workspace).parent / "cache" / "search"
    cache_dir.mkdir(parents=True)
    safe_payload = {
        "schema_version": search_service.CACHE_SCHEMA_VERSION,
        "results": [
            {
                "title": "Safe result",
                "url": "https://example.test/reference",
                "content": "Bounded public snippet",
            }
        ],
        "backend": SearchAPI.DUCKDUCKGO.value,
        "notices": [],
        "notice_codes": [],
    }

    retained = cache_dir / f"{'a' * 32}.json"
    retained.write_text(json.dumps(safe_payload), encoding="utf-8")
    expired = cache_dir / f"{'b' * 32}.json"
    expired.write_text(json.dumps(safe_payload), encoding="utf-8")
    os.utime(
        expired,
        (time.time() - search_service.CACHE_TTL - 5,) * 2,
    )
    legacy = cache_dir / f"{'c' * 32}.json"
    legacy.write_text(
        json.dumps({"results": [], "token": "LEGACY_SECRET_SENTINEL"}),
        encoding="utf-8",
    )
    unknown_schema = cache_dir / f"{'d' * 32}.json"
    unknown_schema.write_text(
        json.dumps({**safe_payload, "schema_version": 99}),
        encoding="utf-8",
    )
    non_exact = cache_dir / f"{'e' * 32}.json"
    non_exact.write_text(
        json.dumps({**safe_payload, "provider_payload": "SECRET"}),
        encoding="utf-8",
    )
    malformed = cache_dir / f"{'f' * 32}.json"
    malformed.write_text("{not-json", encoding="utf-8")
    app_temp = cache_dir / f".{('1' * 32)}.abcdefgh.tmp"
    app_temp.write_text("TEMP_SECRET_SENTINEL", encoding="utf-8")
    unrelated = cache_dir / "keep-me.txt"
    unrelated.write_text("unrelated", encoding="utf-8")
    unrelated_temp = cache_dir / ".keep-me.abcdefgh.tmp"
    unrelated_temp.write_text("unrelated", encoding="utf-8")
    nested = cache_dir / "nested"
    nested.mkdir()
    nested_secret = nested / f"{'2' * 32}.json"
    nested_secret.write_text("NESTED_SECRET_SENTINEL", encoding="utf-8")

    outside = tmp_path / "outside.json"
    outside.write_text("OUTSIDE_SECRET_SENTINEL", encoding="utf-8")
    symlink = cache_dir / f"{'3' * 32}.json"
    symlink_created = False
    try:
        symlink.symlink_to(outside)
        symlink_created = True
    except OSError:
        pass

    chmod_calls: list[tuple[Path, int]] = []
    monkeypatch.setattr(
        search_service.os,
        "chmod",
        lambda path, mode: chmod_calls.append((Path(path), mode)),
    )

    _sweep_search_cache(config)

    assert retained.exists()
    assert chmod_calls == [(retained, 0o600)]
    for removed in (
        expired,
        legacy,
        unknown_schema,
        non_exact,
        malformed,
        app_temp,
    ):
        assert not removed.exists()
    assert unrelated.read_text(encoding="utf-8") == "unrelated"
    assert unrelated_temp.read_text(encoding="utf-8") == "unrelated"
    assert nested_secret.read_text(encoding="utf-8") == "NESTED_SECRET_SENTINEL"
    assert outside.read_text(encoding="utf-8") == "OUTSIDE_SECRET_SENTINEL"
    if symlink_created:
        assert not symlink.exists()
        assert not symlink.is_symlink()


def test_search_cache_sweep_logs_no_path_or_exception_details(
    tmp_path: Path,
    monkeypatch: Any,
    caplog: Any,
) -> None:
    """Unexpected per-entry failures stay isolated behind a stable log message."""
    config = _config(tmp_path)
    cache_dir = Path(config.notes_workspace).parent / "cache" / "search"
    cache_dir.mkdir(parents=True)
    sentinel_name = f"{'4' * 32}.json"
    cache_file = cache_dir / sentinel_name
    cache_file.write_text("{}", encoding="utf-8")

    def explode(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("EXCEPTION_SECRET_SENTINEL")

    monkeypatch.setattr(search_service, "_read_bounded_cache_payload", explode)

    _sweep_search_cache(config)

    rendered = caplog.text
    assert "Search cache sweep entry cleanup failed" in rendered
    assert sentinel_name not in rendered
    assert str(cache_dir) not in rendered
    assert "EXCEPTION_SECRET_SENTINEL" not in rendered


def test_search_cache_sweep_refuses_an_intermediate_directory_symlink(
    tmp_path: Path,
) -> None:
    """A configured-root cache link must never redirect cleanup elsewhere."""
    trusted_root = tmp_path / "trusted"
    outside_cache = tmp_path / "outside-cache"
    outside_search = outside_cache / "search"
    trusted_root.mkdir()
    outside_search.mkdir(parents=True)
    outside_entry = outside_search / f"{'5' * 32}.json"
    outside_entry.write_text("{legacy-json", encoding="utf-8")
    try:
        (trusted_root / "cache").symlink_to(
            outside_cache,
            target_is_directory=True,
        )
    except OSError:
        pytest.skip("Directory symlinks are unavailable on this platform")
    config = Configuration.from_env(
        overrides={
            "notes_workspace": str(trusted_root / "notes"),
            "enable_notes": False,
        }
    )

    _sweep_search_cache(config)

    assert outside_entry.read_text(encoding="utf-8") == "{legacy-json"


def test_search_cache_link_guard_recognizes_windows_reparse_points() -> None:
    """Junction-like reparse metadata is rejected without needing OS privileges."""

    class FakeStatus:
        st_mode = 0
        st_reparse_tag = 1
        st_file_attributes = 0

    assert search_service._is_link_like(FakeStatus())  # type: ignore[arg-type]


def test_search_cache_creation_refuses_an_intermediate_directory_symlink(
    tmp_path: Path,
) -> None:
    """Cache setup must validate a parent before creating children through it."""
    trusted_root = tmp_path / "trusted-create"
    outside_cache = tmp_path / "outside-create"
    trusted_root.mkdir()
    outside_cache.mkdir()
    try:
        (trusted_root / "cache").symlink_to(
            outside_cache,
            target_is_directory=True,
        )
    except OSError:
        pytest.skip("Directory symlinks are unavailable on this platform")
    config = Configuration.from_env(
        overrides={
            "notes_workspace": str(trusted_root / "notes"),
            "enable_notes": False,
        }
    )

    with pytest.raises(OSError, match="Unsafe search cache directory"):
        search_service._cache_dir(config)

    assert not (outside_cache / "search").exists()


def test_search_cache_sweep_discards_oversized_entries_without_parsing(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Untrusted legacy files must be size-bounded before JSON decoding."""
    config = _config(tmp_path)
    cache_dir = Path(config.notes_workspace).parent / "cache" / "search"
    cache_dir.mkdir(parents=True)
    size_limit = getattr(search_service, "CACHE_MAX_FILE_BYTES", 64 * 1024)
    oversized = cache_dir / f"{'6' * 32}.json"
    oversized.write_bytes(b"{" + (b"x" * (size_limit + 1)))
    load_calls: list[object] = []

    def forbidden_load(handle: object) -> object:
        load_calls.append(handle)
        raise AssertionError("oversized cache entry was parsed")

    monkeypatch.setattr(search_service.json, "load", forbidden_load)

    _sweep_search_cache(config)

    assert load_calls == []
    assert not oversized.exists()


def test_search_cache_read_discards_oversized_entries_without_parsing(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Opt-in cache reads share the migration's hard allocation boundary."""
    config = _config(tmp_path)
    query = "oversized direct read"
    cache_file = (
        search_service._cache_dir(config)
        / f"{search_service._cache_key(query, config)}.json"
    )
    cache_file.write_bytes(
        b"{" + (b"x" * (search_service.CACHE_MAX_FILE_BYTES + 1))
    )
    load_calls: list[object] = []

    def forbidden_load(handle: object) -> object:
        load_calls.append(handle)
        raise AssertionError("oversized cache entry was parsed")

    monkeypatch.setattr(search_service.json, "load", forbidden_load)

    assert search_service._load_from_cache(query, config) is None
    assert load_calls == []
    assert not cache_file.exists()


def test_search_projection_drops_urls_above_the_cache_safety_bound() -> None:
    """Provider URLs cannot make the bounded cache projection arbitrarily large."""
    payload = {
        "results": [
            {
                "title": "oversized URL",
                "url": "https://example.test/" + ("a" * (1024 * 1024)),
                "content": "public snippet",
            }
        ],
        "backend": "duckduckgo",
    }

    projected = search_service._sanitize_search_payload(
        payload,
        cache_projection=True,
    )

    assert projected["results"] == []


def test_search_cache_sweep_honors_entry_and_time_budgets(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Startup migration work remains bounded for very large directories."""
    config = _config(tmp_path)
    cache_dir = Path(config.notes_workspace).parent / "cache" / "search"
    cache_dir.mkdir(parents=True)
    for index in range(5):
        (cache_dir / f"entry-{index}.txt").write_text("ignored", encoding="utf-8")

    visited: list[Path] = []
    monkeypatch.setattr(
        search_service,
        "_sweep_search_cache_entry",
        lambda entry, **_kwargs: visited.append(entry),
    )
    monkeypatch.setattr(
        search_service,
        "CACHE_SWEEP_MAX_ENTRIES",
        2,
        raising=False,
    )
    monkeypatch.setattr(
        search_service,
        "CACHE_SWEEP_TIME_BUDGET_SECONDS",
        60.0,
        raising=False,
    )

    _sweep_search_cache(config)

    assert len(visited) == 2

    visited.clear()
    ticks = iter((0.0, 0.0, 1.0))
    monkeypatch.setattr(search_service, "CACHE_SWEEP_MAX_ENTRIES", 100)
    monkeypatch.setattr(search_service, "CACHE_SWEEP_TIME_BUDGET_SECONDS", 0.5)
    monkeypatch.setattr(search_service.time, "monotonic", lambda: next(ticks))

    _sweep_search_cache(config)

    assert len(visited) == 1
