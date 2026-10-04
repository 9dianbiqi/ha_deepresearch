"""Regression coverage for task-first, shared Web paragraph evidence."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Lock

import pytest

from research.pipeline import ResearchKernel
from research.profiles import ResearchMode
from research.providers.web import WebSourceProvider
from research.sources import SourceProviderRegistry
from research.web_capture import WebCaptureResult, WebCaptureService
from research.web_evidence import RunWebEvidence, task_evidence_from_collections


class CapturingProvider(WebSourceProvider):
    def __init__(self, *, fallback=False):
        super().__init__()
        self.calls = 0
        self.lock = Lock()
        self.fallback = fallback

    def capture_search_result(self, result, context):
        context.cancellation.raise_if_cancelled()
        with self.lock:
            self.calls += 1
        if self.fallback:
            return WebCaptureResult(
                status="partial",
                source_id="web:fallback",
                canonical_url=result["url"],
                page_title="Unavailable page",
                captured_at="2026-09-12T00:00:00Z",
                metadata_excerpt="Hopper demand may explain growth.",
                notice_codes=("web_capture_fetch_failed",),
            )
        html = "<html><title>Annual report</title><article>"
        html += "".join(
            f"<p>Unrelated corporate background paragraph {i}.</p>" for i in range(20)
        )
        html += "<p>Hopper demand explains data center revenue growth.</p>"
        html += "<p>This explanation applies to fiscal 2025 only.</p></article></html>"
        return WebCaptureService().capture_content(
            result["url"], html, content_type="text/html"
        )


def setup_reader(provider):
    kernel = ResearchKernel(provider_registry=SourceProviderRegistry((provider,)))
    prepared = kernel.prepare(
        "Hopper demand", mode=ResearchMode.WEB, profile_id="web.evidence.v1"
    )
    return kernel, prepared


def read(
    prepared,
    provider,
    url="https://example.com/report",
    intent="Hopper demand revenue growth",
):
    return prepared.web_evidence.read(
        provider,
        ({"url": url},),
        prepared.provider_context,
        intent=intent,
        dimension="overview",
        max_excerpt_chars=1200,
    )


def test_late_paragraph_and_neighbor_share_exact_report_identity():
    provider = CapturingProvider()
    kernel, prepared = setup_reader(provider)
    collections = read(prepared, provider)
    task = task_evidence_from_collections(collections, query="Hopper demand")
    assert any("Hopper demand" in item.excerpt for item in task)
    assert any("fiscal 2025 only" in item.excerpt for item in task)
    assert len(task) == 3
    supporting = next(item for item in task if "Hopper demand" in item.excerpt)
    summary = f"Hopper demand explains revenue growth. [{supporting.evidence_id}]"
    bundle = kernel.finalize(
        prepared,
        collections=prepared.web_evidence.collections(),
        task_results=({"summary": summary, "dimension": "overview"},),
    )
    canonical = {item.evidence_id: item for item in bundle.evidence}
    for item in task:
        assert canonical[item.evidence_id].excerpt == item.excerpt
        assert canonical[item.evidence_id].locator.as_dict() == dict(item.locator)
    assert bundle.claims[0].evidence_ids == (supporting.evidence_id,)
    assert provider.calls == 1


def test_concurrent_tasks_reuse_capture_and_budget_across_tracking_urls():
    provider = CapturingProvider()
    _, prepared = setup_reader(provider)
    urls = (
        "https://example.com/report?utm_source=a",
        "https://example.com/report#part",
    ) * 4
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda url: read(prepared, provider, url), urls))
    assert provider.calls == 1
    assert prepared.provider_context.budget.snapshot()["evidence"] == 3
    assert len(prepared.web_evidence.collections()) == 1
    assert all(len(result[0].records) == 3 for result in results)
    # A later intent can select more passages without downloading the page again.
    read(prepared, provider, intent="corporate background paragraph 3")
    assert provider.calls == 1


def test_failed_capture_preserves_metadata_and_reason_without_refetch():
    provider = CapturingProvider(fallback=True)
    _, prepared = setup_reader(provider)
    first = read(prepared, provider)
    second = read(prepared, provider)
    task = task_evidence_from_collections(first, query="Hopper demand")
    assert task[0].evidence_level == "metadata"
    assert "web_capture_fetch_failed" in second[0].notice_codes
    assert provider.calls == 1


def test_exhausted_evidence_budget_keeps_snapshot_without_overflow():
    provider = CapturingProvider()
    _, prepared = setup_reader(provider)
    budget = prepared.provider_context.budget
    budget.reserve(evidence=budget.remaining()["evidence"])
    collections = read(prepared, provider)
    assert collections[0].records == ()
    assert collections[0].provider_payload.snapshot is not None
    assert budget.remaining()["evidence"] == 0


def test_cancelled_reader_does_not_fetch():
    class Cancelled:
        def raise_if_cancelled(self):
            raise RuntimeError("cancelled")

    provider = CapturingProvider()
    _, prepared = setup_reader(provider)
    prepared = replace(
        prepared,
        provider_context=replace(prepared.provider_context, cancellation=Cancelled()),
    )
    with pytest.raises(RuntimeError, match="cancelled"):
        read(prepared, provider)
    assert provider.calls == 0


def test_captures_are_isolated_between_runs():
    provider = CapturingProvider()
    _, prepared = setup_reader(provider)
    read(prepared, provider)
    read(replace(prepared, web_evidence=RunWebEvidence()), provider)
    assert provider.calls == 2


def test_long_paragraph_keeps_relevant_tail_as_exact_snapshot_span():
    class LongParagraph(CapturingProvider):
        def capture_search_result(self, result, context):
            return WebCaptureService().capture_content(
                result["url"],
                "<article><p>"
                + "Background detail. " * 75
                + "Hopper demand explains revenue growth.</p></article>",
                content_type="text/html",
                fallback_title="Report",
            )

    provider = LongParagraph()
    _, prepared = setup_reader(provider)
    collection = read(prepared, provider)[0]
    record = collection.records[0]
    assert "Hopper demand" in record["excerpt"]
    assert len(record["excerpt"]) <= 1200
    assert record["excerpt"] in collection.provider_payload.snapshot.content
