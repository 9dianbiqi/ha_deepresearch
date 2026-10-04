"""Web SourceProvider adapter over the existing search dispatcher."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from inspect import Parameter, signature
from typing import Any

from config import Configuration
from research.operations import OperationRejectedError
from research.profiles import ResearchMode
from research.session import CancellationRequestedError, DeadlineExceededError
from services.search import (
    SEARCH_NOTICE_MESSAGES,
    dispatch_search,
)

from ..sources import (
    BudgetExceededError,
    DetectionResult,
    EnrichmentRequest,
    ProviderContext,
    SourceCollection,
    SourceRequestSpec,
    SourceSearchRequest,
    SourceSearchResult,
    SourceTarget,
)
from ..web_capture import WebCaptureResult, WebCaptureService

_NOTICE_CODE = "web_provider_unavailable"
_NOTICE_MESSAGE = "Web source provider unavailable."
_ALLOWED_NOTICE_CODES = frozenset(
    {"web_provider_unavailable", *SEARCH_NOTICE_MESSAGES}
)


def _call_dispatcher(
    dispatcher: Callable[..., Any],
    query: str,
    config: Configuration,
    loop_count: int,
    kwargs: Mapping[str, object],
) -> Any:
    """Pass only supported keyword arguments to legacy test and production dispatchers."""
    try:
        parameters = tuple(signature(dispatcher).parameters.values())
    except (TypeError, ValueError):
        parameters = ()
    accepts_kwargs = any(item.kind is Parameter.VAR_KEYWORD for item in parameters)
    if accepts_kwargs:
        selected = dict(kwargs)
    else:
        allowed = {
            item.name
            for item in parameters
            if item.kind in {Parameter.POSITIONAL_OR_KEYWORD, Parameter.KEYWORD_ONLY}
        }
        selected = {key: value for key, value in kwargs.items() if key in allowed}
    return dispatcher(query, config, loop_count, **selected)


class WebSourceProvider:
    """Adapt the existing retry/cache search dispatcher to typed results."""

    provider_id = "web"
    supported_modes = frozenset({ResearchMode.WEB, ResearchMode.GITHUB})

    def __init__(
        self,
        *,
        dispatcher: Callable[..., Any] = dispatch_search,
        search_adapter: object | None = None,
        capture_service: WebCaptureService | None = None,
    ) -> None:
        """Store search and page-capture adapters behind injectable boundaries."""
        self._dispatcher = dispatcher
        self._search_adapter = search_adapter
        self._capture_service = capture_service or WebCaptureService()

    def detect_target(
        self,
        request: SourceRequestSpec,
        context: ProviderContext,
    ) -> DetectionResult:
        """Represent a generic topic as a stable Web query target."""
        del context
        if request.mode not in self.supported_modes:
            return DetectionResult(provider_id=self.provider_id, matched=False)
        digest = hashlib.sha256(request.topic.encode("utf-8")).hexdigest()[:20]
        target = SourceTarget(
            provider_id=self.provider_id,
            source_kind="web_query",
            source_id=f"query:{digest}",
            canonical_url=f"search://{digest}",
            metadata={"topic": request.topic},
        )
        return DetectionResult(
            provider_id=self.provider_id,
            matched=True,
            targets=(target,),
            confidence=0.5,
        )

    def search(
        self,
        request: SourceSearchRequest,
        context: ProviderContext,
    ) -> SourceSearchResult:
        """Run the existing dispatcher under the supplied scope and budget."""
        context.cancellation.raise_if_cancelled()
        context.budget.reserve(requests=1)
        if context.operation_scope is None:
            raise ValueError("Web provider requires an operation scope.")
        try:
            payload, notices, answer, backend = _call_dispatcher(
                self._dispatcher,
                request.query,
                request.config,
                request.loop_count,
                {
                    "use_cache": request.use_cache,
                    "cancellation": context.cancellation,
                    "operation_scope": context.operation_scope,
                    "search_adapter": self._search_adapter,
                },
            )
        except (
            OperationRejectedError,
            CancellationRequestedError,
            DeadlineExceededError,
            BudgetExceededError,
        ):
            raise
        except Exception:
            return SourceSearchResult(
                provider_id=self.provider_id,
                notices=(_NOTICE_MESSAGE,),
                notice_codes=(_NOTICE_CODE,),
            )
        raw_payload = payload if isinstance(payload, Mapping) else {}
        raw_results = raw_payload.get("results")
        results = tuple(
            dict(item)
            for item in raw_results
            if isinstance(item, Mapping)
        ) if isinstance(raw_results, (list, tuple)) else ()
        raw_codes = raw_payload.get("notice_codes")
        codes = tuple(
            code
            for code in (raw_codes if isinstance(raw_codes, (list, tuple)) else ())
            if isinstance(code, str) and code in _ALLOWED_NOTICE_CODES
        )
        safe_notices = tuple(
            SEARCH_NOTICE_MESSAGES.get(code, _NOTICE_MESSAGE)
            for code in codes
        )
        if not codes and isinstance(notices, (list, tuple)) and notices:
            codes = ("search_backend_notice",)
            safe_notices = (SEARCH_NOTICE_MESSAGES["search_backend_notice"],)
        return SourceSearchResult(
            provider_id=self.provider_id,
            results=results,
            answer=answer if isinstance(answer, str) else None,
            backend=backend if isinstance(backend, str) else "none",
            notices=safe_notices,
            notice_codes=codes,
        )

    def collect_search_result(
        self,
        result: Mapping[str, object],
        context: ProviderContext,
        *,
        dimension: str = "overview",
    ) -> SourceCollection:
        """Capture one search result as full-text paragraph or metadata records.

        Existing search backends may already return bounded ``raw_content``. That
        path is parsed locally. Otherwise the page fetch is run through the
        existing governed ``search:web`` operation boundary.
        """
        capture = self.capture_search_result(result, context)
        return self.collection_from_capture(capture, context, dimension=dimension)

    def capture_search_result(
        self,
        result: Mapping[str, object],
        context: ProviderContext,
    ) -> WebCaptureResult:
        """Read one page without consuming paragraph evidence budget."""
        context.cancellation.raise_if_cancelled()
        raw_url = result.get("url")
        if not isinstance(raw_url, str) or not raw_url.strip():
            raise ValueError("Web search result URL must not be empty.")
        raw_title = result.get("title")
        title = raw_title if isinstance(raw_title, str) else ""
        snippet = next(
            (
                value
                for value in (
                    result.get("content"),
                    result.get("snippet"),
                    result.get("summary"),
                )
                if isinstance(value, str) and value.strip()
            ),
            "",
        )
        raw_content = result.get("raw_content")
        if isinstance(raw_content, str) and raw_content:
            prefix = raw_content[:512].lstrip().casefold()
            content_type = (
                "text/html"
                if prefix.startswith("<!doctype html")
                or "<html" in prefix
                or "<article" in prefix
                else "text/plain"
            )
            capture = self._capture_service.capture_content(
                raw_url,
                raw_content,
                content_type=content_type,
                fallback_title=title,
                fallback_snippet=snippet,
                content_origin="upstream_provider",
                validate_public_url=True,
            )
        else:
            context.budget.reserve(requests=1)
            operation_scope = context.operation_scope
            if operation_scope is None:
                raise ValueError("Web page capture requires an operation scope.")
            url_hash = hashlib.sha256(raw_url.strip().encode("utf-8")).hexdigest()
            spec = operation_scope.spec(
                operation_name="search.fetch_page",
                capabilities=("search:web",),
                resource={"query_hash": url_hash, "backend": "web_capture"},
            )
            capture = operation_scope.operations.call(
                spec,
                lambda: self._capture_service.capture(
                    raw_url,
                    fallback_title=title,
                    fallback_snippet=snippet,
                ),
            )
        if not isinstance(capture, WebCaptureResult):
            raise TypeError("Web capture service returned an invalid result.")
        return capture

    @staticmethod
    def collection_from_capture(
        capture: WebCaptureResult,
        context: ProviderContext,
        *,
        dimension: str = "overview",
    ) -> SourceCollection:
        """Project a capture for callers using the legacy collection boundary."""
        records = capture.as_records(dimension=dimension)
        remaining = context.budget.remaining()["evidence"]
        # A profile budget is a hard upper bound.  Once it is exhausted, keep
        # the capture metadata for provenance but return no additional records;
        # attempting to reserve a synthetic record here would turn a normal
        # multi-result truncation into a coordinator-wide failure.
        bounded_records = records[:remaining] if remaining > 0 else ()
        if bounded_records:
            context.budget.reserve(evidence=len(bounded_records))
        target = SourceTarget(
            provider_id="web",
            source_kind="web_page",
            source_id=capture.source_id,
            canonical_url=capture.canonical_url,
            metadata={
                "title": capture.page_title,
                "evidence_level": capture.evidence_level,
                "content_origin": capture.content_origin,
            },
        )
        return SourceCollection(
            provider_id="web",
            source_kind=target.source_kind,
            target=target,
            collection_status=capture.status,
            provider_payload=capture,
            records=bounded_records,
            captured_at=capture.captured_at,
            notices=capture.notices,
            notice_codes=capture.notice_codes,
        )

    def collect(
        self,
        target: SourceTarget,
        context: ProviderContext,
    ) -> SourceCollection:
        """Return an explicit no-op collection for query targets."""
        del context
        return SourceCollection(
            provider_id=self.provider_id,
            source_kind=target.source_kind,
            target=target,
            collection_status="partial",
            notices=("Web search results are normalized directly from search.",),
            notice_codes=("web_search_result_collection",),
        )

    def enrich(
        self,
        request: EnrichmentRequest,
        context: ProviderContext,
    ) -> SourceCollection:
        """Reject enrichment without a search request at this adapter layer."""
        if not request.hints:
            raise ValueError("Web enrichment hints must not be empty.")
        return self.collect(request.target, context)


__all__ = ["WebSourceProvider"]
