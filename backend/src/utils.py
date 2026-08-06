"""Utility helpers shared across deep researcher services."""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Dict, List
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

CHARS_PER_TOKEN = 4

logger = logging.getLogger(__name__)

_HTTP_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "credential",
        "jwt",
        "key",
        "password",
        "secret",
        "session",
        "sig",
        "signature",
        "token",
    }
)
_SENSITIVE_QUERY_SUFFIXES = (
    "_api_key",
    "_credential",
    "_password",
    "_secret",
    "_signature",
    "_token",
)
_SIGNED_QUERY_PREFIXES = ("aws_", "x_amz_", "x_goog_")
_COMPACT_AWS_SIGNING_PREFIXES = (
    "awsaccess",
    "awscredential",
    "awssecurity",
    "awssession",
    "awssignature",
)


def _normalize_query_key(key: str) -> str:
    """Normalize common query-key casing and container syntax for matching."""
    separated = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", key.strip())
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", separated)
    return re.sub(r"[^a-z0-9]+", "_", separated.casefold()).strip("_")


def _is_sensitive_query_key(key: str) -> bool:
    raw_candidates = {
        candidate
        for candidate in (key.strip(), *re.split(r"[\[\].]+", key.strip()))
        if candidate
    }
    normalized_candidates = {
        normalized
        for candidate in raw_candidates
        for normalized in (
            _normalize_query_key(candidate),
            re.sub(
                r"[^a-z0-9]+",
                "_",
                candidate.casefold(),
            ).strip("_"),
        )
        if normalized
    }
    return any(
        normalized in _SENSITIVE_QUERY_KEYS
        or normalized.endswith(_SENSITIVE_QUERY_SUFFIXES)
        or normalized.startswith(_SIGNED_QUERY_PREFIXES)
        or normalized.startswith(_COMPACT_AWS_SIGNING_PREFIXES)
        for normalized in normalized_candidates
    )


def get_config_value(value: Any) -> str:
    """Return configuration value as plain string."""
    return value if isinstance(value, str) else value.value


def strip_thinking_tokens(text: str) -> str:
    """Remove ``<think>`` sections from model responses."""
    while "<think>" in text and "</think>" in text:
        start = text.find("<think>")
        end = text.find("</think>") + len("</think>")
        text = text[:start] + text[end:]
    return text


def sanitize_reference_url(value: object) -> str:
    """Return an HTTP(S) reference without credentials or secret URL parts."""
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    if not candidate or any(ord(character) < 32 for character in candidate):
        return ""
    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or not hostname:
        return ""
    if any(
        character.isspace() or character in {"/", "\\", "@", "?", "#"}
        for character in hostname
    ):
        return ""

    safe_host = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        safe_host = f"{safe_host}:{port}"
    safe_query = urlencode(
        [
            (key, item)
            for key, item in parse_qsl(
                parsed.query,
                keep_blank_values=True,
            )
            if not _is_sensitive_query_key(key)
        ],
        doseq=True,
    )
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            safe_host,
            parsed.path,
            safe_query,
            "",
        )
    )


def sanitize_text_urls(value: object) -> str:
    """Remove URL credentials and volatile components from untrusted text."""
    if not isinstance(value, str):
        return ""

    def replace(match: re.Match[str]) -> str:
        return sanitize_reference_url(match.group(0)) or "[redacted-url]"

    return _HTTP_URL_PATTERN.sub(replace, value)


def deduplicate_and_format_sources(
    search_response: Dict[str, Any] | List[Dict[str, Any]],
    max_tokens_per_source: int,
    *,
    fetch_full_page: bool = False,
) -> str:
    """Format and deduplicate search results for downstream prompting."""
    if isinstance(search_response, dict):
        sources_list = search_response.get("results", [])
    else:
        sources_list = search_response

    unique_sources: dict[str, Dict[str, Any]] = {}
    for source in sources_list:
        if not isinstance(source, dict):
            continue
        url = sanitize_reference_url(source.get("url"))
        if not url:
            continue
        if url not in unique_sources:
            source_url_hash = hashlib.sha256(
                str(source.get("url") or "").encode("utf-8")
            ).hexdigest()
            unique_sources[url] = {
                **source,
                "url": url,
                "_source_url_hash": source_url_hash,
            }

    formatted_parts: List[str] = []
    for source in unique_sources.values():
        title = sanitize_text_urls(source.get("title")) or source.get("url", "")
        content = sanitize_text_urls(source.get("content"))
        formatted_parts.append(f"信息来源: {title}\n\n")
        formatted_parts.append(f"URL: {source.get('url', '')}\n\n")
        formatted_parts.append(f"信息内容: {content}\n\n")

        if fetch_full_page:
            raw_content = source.get("raw_content")
            if raw_content is None:
                url_hash = str(source.get("_source_url_hash") or "")
                logger.debug("raw_content missing: url_hash=%s", url_hash)
                raw_content = ""
            raw_content = sanitize_text_urls(raw_content)
            char_limit = max_tokens_per_source * CHARS_PER_TOKEN
            if len(raw_content) > char_limit:
                raw_content = f"{raw_content[:char_limit]}... [truncated]"
            formatted_parts.append(
                f"详细信息内容限制为 {max_tokens_per_source} 个 token: {raw_content}\n\n"
            )

    return "".join(formatted_parts).strip()


def format_sources(search_results: Dict[str, Any] | None) -> str:
    """Return bullet list summarising search sources."""
    if not search_results:
        return ""

    results = search_results.get("results", [])
    formatted: list[str] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        url = sanitize_reference_url(item.get("url"))
        if not url:
            continue
        title = sanitize_text_urls(item.get("title")) or url
        formatted.append(f"* {title} : {url}")
    return "\n".join(formatted)
