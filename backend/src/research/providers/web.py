"""Web SourceProvider adapter over the existing search dispatcher."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from typing import Any

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

_NOTICE_CODE = "web_provider_unavailable"
_NOTICE_MESSAGE = "Web source provider unavailable."
_ALLOWED_NOTICE_CODES = frozenset(
    {"web_provider_unavailable", *SEARCH_NOTICE_MESSAGES}
)


class WebSourceProvider:
    """Adapt the existing retry/cache search dispatcher to typed results."""

    provider_id = "web"
    supported_modes = frozenset({ResearchMode.WEB, ResearchMode.GITHUB, ResearchMode.PAPER})

    def __init__(
        self,
        *,
        dispatcher: Callable[..., Any] = dispatch_search,
        search_adapter: object | None = None,
    ) -> None:
        """Store the dispatcher and optional existing SearchTool adapter."""
        self._dispatcher = dispatcher
        self._search_adapter = search_adapter

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
            payload, notices, answer, backend = self._dispatcher(
                request.query,
                request.config,
                request.loop_count,
                use_cache=request.use_cache,
                cancellation=context.cancellation,
                operation_scope=context.operation_scope,
                search_adapter=self._search_adapter,
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
