"""Security-focused tests for shared formatting utilities."""

from __future__ import annotations

import hashlib
import logging

from utils import (
    deduplicate_and_format_sources,
    format_sources,
    sanitize_reference_url,
)


def test_reference_url_preserves_identity_query_and_filters_sensitive_keys() -> None:
    """Keep ordinary repeated/empty parameters while removing signing material."""
    url = (
        "https://url-user:url-password@example.test/view"
        "?id=41&id=42&lang=zh&page="
        "&ToKeN=token-secret&api-key=api-secret"
        "&client_signature=signature-secret&X-Amz-Date=amz-secret"
        "&x_goog_credential=goog-secret&AWS-Signature=aws-secret"
            "&apiKey=camel-api-secret&accessToken=camel-token-secret"
            "&clientSecret=camel-client-secret&token%5B%5D=array-token-secret"
            "&apikey=compact-api-secret"
            "&token%5B0%5D=indexed-token-secret"
            "&token%5Bvalue%5D=member-token-secret"
            "&filters%5Btoken%5D=nested-token-secret"
            "#private-fragment"
    )

    sanitized = sanitize_reference_url(url)

    assert sanitized == (
        "https://example.test/view?id=41&id=42&lang=zh&page="
    )
    for secret in (
        "url-user",
        "url-password",
        "token-secret",
        "api-secret",
        "signature-secret",
        "amz-secret",
        "goog-secret",
        "aws-secret",
        "camel-api-secret",
        "camel-token-secret",
        "camel-client-secret",
        "array-token-secret",
        "private-fragment",
    ):
        assert secret not in sanitized


def test_missing_raw_content_log_hashes_url_without_revealing_it(caplog) -> None:
    """Do not place signed URLs or their query secrets in debug logs."""
    url = "https://private.example/report?token=raw-secret&signature=private"
    expected_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()

    with caplog.at_level(logging.DEBUG, logger="utils"):
        deduplicate_and_format_sources(
            {"results": [{"title": "Result", "url": url, "content": "body"}]},
            max_tokens_per_source=100,
            fetch_full_page=True,
        )

    rendered = caplog.text
    assert expected_hash in rendered
    assert url not in rendered
    assert "raw-secret" not in rendered


def test_source_formatters_strip_url_credentials_queries_and_fragments() -> None:
    """Both prompt context and source summaries must expose reference-safe URLs."""
    payload = {
        "results": [
            {
                "title": "Reference",
                "url": (
                    "https://source-user:source-password@example.test/path"
                    "?token=SOURCE_TOKEN#SOURCE_FRAGMENT"
                ),
                "content": (
                    "Read https://content-user:content-password@cdn.example.test/body"
                    "?signature=CONTENT_SIGNATURE#CONTENT_FRAGMENT"
                ),
                "raw_content": (
                    "Full https://raw-user:raw-password@raw.example.test/page"
                    "?token=RAW_TOKEN#RAW_FRAGMENT"
                ),
            }
        ]
    }

    sources = format_sources(payload)
    context = deduplicate_and_format_sources(
        payload,
        max_tokens_per_source=100,
        fetch_full_page=True,
    )
    rendered = f"{sources}\n{context}"

    assert "https://example.test/path" in rendered
    assert "https://cdn.example.test/body" in rendered
    assert "https://raw.example.test/page" in rendered
    for sentinel in (
        "source-user",
        "source-password",
        "SOURCE_TOKEN",
        "SOURCE_FRAGMENT",
        "content-user",
        "content-password",
        "CONTENT_SIGNATURE",
        "CONTENT_FRAGMENT",
        "raw-user",
        "raw-password",
        "RAW_TOKEN",
        "RAW_FRAGMENT",
    ):
        assert sentinel not in rendered
