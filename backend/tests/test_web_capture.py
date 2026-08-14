"""Web full-text capture and paragraph evidence tests."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from research.paragraphs import extract_text_document
from research.profiles import ResearchMode, RetrievalBudget
from research.providers.web import WebSourceProvider
from research.sources import ProviderContext, RetrievalBudgetTracker
from research.web_capture import (
    FetchedWebPage,
    WebCaptureError,
    WebCaptureService,
    canonicalize_web_url,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "web_capture_page.html"
CAPTURE_TIME = datetime(2026, 8, 14, 1, 2, 3, tzinfo=timezone.utc)


class NeverCancelled:
    """Minimal cancellation checkpoint for provider tests."""

    def raise_if_cancelled(self) -> None:
        return None


class StaticFetcher:
    """Return one injected response without network access."""

    def __init__(self, page: FetchedWebPage) -> None:
        self.page = page

    def fetch(self, url: str, *, max_bytes: int) -> FetchedWebPage:
        del url, max_bytes
        return self.page


class BrokenFetcher:
    """Raise one stable capture failure without leaking dynamic details."""

    def fetch(self, url: str, *, max_bytes: int) -> FetchedWebPage:
        del url, max_bytes
        raise WebCaptureError("web_capture_fetch_failed")


def capture_service(**kwargs: object) -> WebCaptureService:
    return WebCaptureService(clock=lambda: CAPTURE_TIME, **kwargs)


def provider_context() -> ProviderContext:
    return ProviderContext(
        run_id="run-web-capture",
        profile_id="web.evidence.v1",
        mode=ResearchMode.WEB,
        operation_scope=None,
        cancellation=NeverCancelled(),
        budget=RetrievalBudgetTracker(
            RetrievalBudget(max_requests=5, max_results=10, max_evidence=10)
        ),
    )


def test_canonicalization_removes_fragments_tracking_and_default_ports() -> None:
    assert canonicalize_web_url(
        "HTTPS://Example.COM:443/a%20b/?z=2&utm_source=x&a=1&fbclid=y#part"
    ) == "https://example.com/a%20b/?a=1&z=2"


def test_bilingual_html_becomes_heading_aware_deduplicated_paragraphs() -> None:
    html = FIXTURE_PATH.read_text(encoding="utf-8")
    result = capture_service().capture_content(
        "https://example.test/research?utm_medium=email&a=1",
        html,
        content_type="text/html",
    )

    assert result.status == "complete"
    assert result.canonical_url == "https://example.test/research?a=1"
    assert result.page_title == "段落证据示例"
    assert len(result.paragraphs) == 2
    assert result.paragraphs[0].section_path == ("研究概览",)
    assert result.paragraphs[1].section_path == ("研究概览", "Evidence quality")
    combined = " ".join(item.exact_excerpt for item in result.paragraphs)
    assert "navigation" not in combined
    assert "cookies" not in combined
    assert "footer" not in combined


def test_paragraph_identity_ignores_capture_time_but_tracks_text_changes() -> None:
    first = capture_service().capture_content(
        "https://example.test/page",
        "# Heading\n\nThis paragraph contains a stable factual statement for evidence.",
    )
    later_service = WebCaptureService(
        clock=lambda: datetime(2026, 8, 15, tzinfo=timezone.utc)
    )
    second = later_service.capture_content(
        "https://example.test/page",
        "# Heading\n\nThis paragraph contains a stable factual statement for evidence.",
    )
    changed = capture_service().capture_content(
        "https://example.test/page",
        "# Heading\n\nThis paragraph contains a changed factual statement for evidence.",
    )

    assert first.paragraphs[0].paragraph_id == second.paragraphs[0].paragraph_id
    assert first.paragraphs[0].captured_at != second.paragraphs[0].captured_at
    assert first.paragraphs[0].content_hash != changed.paragraphs[0].content_hash
    assert first.snapshot is not None and changed.snapshot is not None
    assert first.snapshot.content_hash != changed.snapshot.content_hash


def test_oversized_content_degrades_to_metadata_without_false_full_text() -> None:
    service = capture_service(max_page_bytes=64)
    result = service.capture_content(
        "https://example.test/large",
        "x" * 65,
        fallback_title="Large result",
        fallback_snippet="A bounded search result summary remains available.",
    )

    assert result.status == "partial"
    assert result.evidence_level == "metadata"
    assert result.notice_codes == (
        "web_capture_too_large",
        "web_capture_metadata_fallback",
    )
    records = result.as_records()
    assert records[0]["evidence_level"] == "metadata"
    assert records[0]["evidence_type"] == "search_result"


def test_empty_document_degrades_with_stable_notice_code() -> None:
    result = capture_service().capture_content(
        "https://example.test/empty",
        "<html><body><nav><p>Only navigation content exists here.</p></nav></body></html>",
        content_type="text/html",
        fallback_snippet="Search metadata is retained for this otherwise empty result.",
    )

    assert result.status == "partial"
    assert result.notice_codes[0] == "web_capture_no_paragraphs"
    assert result.paragraphs == ()
    assert result.snapshot is None


def test_network_failure_never_promotes_search_snippet_to_full_text() -> None:
    result = capture_service(fetcher=BrokenFetcher()).capture(
        "https://example.test/unavailable",
        fallback_title="Unavailable page",
        fallback_snippet="This text came from a search result snippet.",
    )

    assert result.status == "partial"
    assert result.evidence_level == "metadata"
    assert result.as_records()[0]["locator_type"] == "search_result"
    assert result.notice_codes[0] == "web_capture_fetch_failed"


def test_paragraph_count_and_length_are_bounded_at_safe_breaks() -> None:
    document = extract_text_document(
        (
            "First sentence has enough detail to be useful. "
            "Second sentence also contains evidence. "
            "Third sentence should exceed the paragraph count."
        ),
        fallback_title="Bounded",
        min_chars=1,
        max_chars=55,
        max_paragraphs=2,
    )

    assert len(document.blocks) == 2
    assert all(len(item.text) <= 55 for item in document.blocks)
    assert document.blocks[0].text.endswith(".")


def test_snapshot_descriptor_excludes_content_and_matches_checksum() -> None:
    result = capture_service().capture_content(
        "https://example.test/snapshot",
        "A sufficiently long paragraph is normalized into a snapshot artifact payload.",
    )

    assert result.snapshot is not None
    descriptor = result.snapshot.descriptor()
    assert "content" not in descriptor
    assert descriptor["checksum"] == result.snapshot.content_hash
    assert descriptor["size_bytes"] == len(result.snapshot.content.encode("utf-8"))


def test_injected_fetched_page_uses_final_url_and_content_type() -> None:
    page = FetchedWebPage(
        final_url="https://example.test/final?utm_campaign=x",
        body=b"A fetched plain text paragraph contains enough content for evidence.",
        content_type="text/plain",
        encoding="utf-8",
    )
    result = capture_service(fetcher=StaticFetcher(page)).capture(
        "https://example.test/start"
    )

    assert result.status == "complete"
    assert result.canonical_url == "https://example.test/final"


def test_provider_converts_existing_raw_content_without_extra_request() -> None:
    provider = WebSourceProvider(capture_service=capture_service())
    context = provider_context()
    collection = provider.collect_search_result(
        {
            "url": "https://example.test/article",
            "title": "Article",
            "content": "Search-only summary.",
            "raw_content": (
                "# Details\n\nThe fetched source paragraph is long enough to become full-text evidence."
            ),
        },
        context,
        dimension="architecture",
    )

    assert collection.collection_status == "complete"
    assert collection.records[0]["evidence_level"] == "full_text"
    assert collection.records[0]["dimension"] == "architecture"
    assert collection.records[0]["locator"]["paragraph"].startswith("webp_")
    assert context.budget.snapshot()["requests"] == 0
    assert context.budget.snapshot()["evidence"] == 1


def test_canonical_link_cannot_replace_source_with_another_site() -> None:
    result = capture_service().capture_content(
        "https://example.test/source",
        (
            '<link rel="canonical" href="https://attacker.test/other">'
            "<h1>Title</h1><p>This paragraph remains attached to the requested source site.</p>"
        ),
        content_type="text/html",
    )

    assert result.canonical_url == "https://example.test/source"
