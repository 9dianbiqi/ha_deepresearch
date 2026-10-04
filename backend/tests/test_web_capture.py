"""Web full-text capture and paragraph evidence tests."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import research.web_capture as web_capture
from research.paragraphs import extract_text_document
from research.profiles import ResearchMode, RetrievalBudget
from research.providers.web import WebSourceProvider
from research.sources import ProviderContext, RetrievalBudgetTracker
from research.web_capture import (
    FetchedWebPage,
    RequestsWebPageFetcher,
    Urllib3PinnedWebTransport,
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


class StaticResolver:
    """Return configured DNS answers while recording every resolution."""

    def __init__(self, answers: dict[str, tuple[str, ...]]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, int]] = []

    def __call__(self, hostname: str, port: int) -> tuple[str, ...]:
        self.calls.append((hostname, port))
        return self.answers[hostname]


class FakePinnedResponse:
    """Minimal streaming response for pinned-transport boundary tests."""

    def __init__(
        self,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        chunks: tuple[bytes, ...] = (b"bounded public page content",),
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {"Content-Type": "text/plain; charset=utf-8"}
        self.chunks = chunks
        self.closed = False

    def iter_content(self, *, chunk_size: int):
        del chunk_size
        yield from self.chunks

    def close(self) -> None:
        self.closed = True


class RecordingPinnedTransport:
    """Return queued responses and expose the exact IP connection targets."""

    def __init__(self, responses: list[FakePinnedResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, tuple[float, float]]] = []

    def request(
        self,
        url: str,
        *,
        connect_ip: str,
        timeout: tuple[float, float],
    ) -> FakePinnedResponse:
        self.calls.append((url, connect_ip, timeout))
        return self.responses.pop(0)


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
    resolver = StaticResolver({"public.example": ("93.184.216.34",)})
    provider = WebSourceProvider(
        capture_service=capture_service(resolver=resolver)
    )
    context = provider_context()
    collection = provider.collect_search_result(
        {
            "url": "https://public.example/article",
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
    assert collection.records[0]["content_origin"] == "upstream_provider"
    assert collection.target.metadata["content_origin"] == "upstream_provider"
    assert context.budget.snapshot()["requests"] == 0
    assert context.budget.snapshot()["evidence"] == 1
    assert resolver.calls == [("public.example", 443)]


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


def test_fetcher_pins_verified_ip_without_second_dns_resolution() -> None:
    class RebindingResolver:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, hostname: str, port: int) -> tuple[str, ...]:
            del hostname, port
            self.calls += 1
            return ("93.184.216.34",) if self.calls == 1 else ("127.0.0.1",)

    resolver = RebindingResolver()
    response = FakePinnedResponse()
    transport = RecordingPinnedTransport([response])
    page = RequestsWebPageFetcher(
        resolver=resolver,
        transport=transport,
    ).fetch("https://public.example/article", max_bytes=128)

    assert page.body == b"bounded public page content"
    assert resolver.calls == 1
    assert transport.calls[0][:2] == (
        "https://public.example/article",
        "93.184.216.34",
    )
    assert response.closed is True


def test_mixed_public_and_private_dns_answers_fail_closed() -> None:
    resolver = StaticResolver(
        {"mixed.example": ("93.184.216.34", "::1")}
    )
    transport = RecordingPinnedTransport([])

    with pytest.raises(WebCaptureError) as raised:
        RequestsWebPageFetcher(
            resolver=resolver,
            transport=transport,
        ).fetch("https://mixed.example/", max_bytes=128)

    assert raised.value.code == "web_capture_unsafe_url"
    assert transport.calls == []


def test_each_redirect_is_resolved_and_pinned_independently() -> None:
    resolver = StaticResolver(
        {
            "first.example": ("93.184.216.34",),
            "second.example": ("1.1.1.1",),
        }
    )
    redirect = FakePinnedResponse(
        status_code=302,
        headers={"Location": "https://second.example/final"},
        chunks=(),
    )
    final = FakePinnedResponse(chunks=(b"final public response",))
    transport = RecordingPinnedTransport([redirect, final])

    page = RequestsWebPageFetcher(
        resolver=resolver,
        transport=transport,
    ).fetch("https://first.example/start", max_bytes=128)

    assert page.final_url == "https://second.example/final"
    assert resolver.calls == [("first.example", 443), ("second.example", 443)]
    assert [item[1] for item in transport.calls] == ["93.184.216.34", "1.1.1.1"]
    assert redirect.closed is True
    assert final.closed is True


def test_redirect_to_private_literal_is_rejected_before_connection() -> None:
    resolver = StaticResolver({"public.example": ("93.184.216.34",)})
    redirect = FakePinnedResponse(
        status_code=302,
        headers={"Location": "http://127.0.0.1/admin"},
        chunks=(),
    )
    transport = RecordingPinnedTransport([redirect])

    with pytest.raises(WebCaptureError) as raised:
        RequestsWebPageFetcher(
            resolver=resolver,
            transport=transport,
        ).fetch("https://public.example/start", max_bytes=128)

    assert raised.value.code == "web_capture_unsafe_url"
    assert len(transport.calls) == 1
    assert redirect.closed is True


def test_streaming_decoded_body_is_bounded() -> None:
    resolver = StaticResolver({"public.example": ("93.184.216.34",)})
    response = FakePinnedResponse(chunks=(b"1234", b"56"))
    transport = RecordingPinnedTransport([response])

    with pytest.raises(WebCaptureError) as raised:
        RequestsWebPageFetcher(
            resolver=resolver,
            transport=transport,
        ).fetch("https://public.example/large", max_bytes=5)

    assert raised.value.code == "web_capture_too_large"
    assert response.closed is True


def test_default_https_transport_pins_ip_and_preserves_tls_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeUrllibResponse:
        status = 200
        headers = {"Content-Type": "text/plain"}

        def stream(self, amount: int, decode_content: bool):
            del amount, decode_content
            yield b"ok"

        def release_conn(self) -> None:
            return None

        def close(self) -> None:
            return None

    class FakeHttpsPool:
        def __init__(self, **kwargs: Any) -> None:
            captured["pool"] = kwargs

        def request(self, method: str, target: str, **kwargs: Any):
            captured["request"] = (method, target, kwargs)
            return FakeUrllibResponse()

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(web_capture, "HTTPSConnectionPool", FakeHttpsPool)

    response = Urllib3PinnedWebTransport().request(
        "https://public.example:8443/path?q=1",
        connect_ip="93.184.216.34",
        timeout=(1.0, 2.0),
    )
    tuple(response.iter_content(chunk_size=16))
    response.close()

    pool = captured["pool"]
    assert pool["host"] == "93.184.216.34"
    assert pool["port"] == 8443
    assert pool["cert_reqs"] == "CERT_REQUIRED"
    assert pool["assert_hostname"] == "public.example"
    assert pool["server_hostname"] == "public.example"
    method, target, request_kwargs = captured["request"]
    assert (method, target) == ("GET", "/path?q=1")
    assert request_kwargs["headers"]["Host"] == "public.example:8443"
    assert captured["closed"] is True


def test_upstream_content_enforces_character_and_encoded_byte_limits() -> None:
    char_limited = capture_service(
        max_page_bytes=100,
        max_page_chars=4,
    ).capture_content(
        "https://example.test/characters",
        "12345",
        fallback_snippet="safe metadata remains",
    )
    byte_limited = capture_service(
        max_page_bytes=4,
        max_page_chars=10,
    ).capture_content(
        "https://example.test/bytes",
        "你好",
        fallback_snippet="safe metadata remains",
    )

    assert char_limited.notice_codes[0] == "web_capture_too_large"
    assert byte_limited.notice_codes[0] == "web_capture_too_large"
    assert char_limited.evidence_level == byte_limited.evidence_level == "metadata"


def test_upstream_raw_content_rejects_private_source_url() -> None:
    provider = WebSourceProvider(capture_service=capture_service())
    collection = provider.collect_search_result(
        {
            "url": "http://127.0.0.1/private",
            "title": "Untrusted",
            "content": "Only this bounded search metadata may remain.",
            "raw_content": "This provider body must not become paragraph evidence.",
        },
        provider_context(),
    )

    assert collection.collection_status == "partial"
    assert collection.records[0]["evidence_level"] == "metadata"
    assert collection.records[0]["content_origin"] == "search_metadata"
    assert collection.notice_codes[0] == "web_capture_unsafe_url"
