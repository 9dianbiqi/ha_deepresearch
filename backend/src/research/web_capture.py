"""Bounded Web page capture with paragraph-level, artifact-ready output."""

from __future__ import annotations

import hashlib
import ipaddress
import socket
from codecs import getincrementalencoder
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Final, Protocol, cast
from urllib.parse import (
    parse_qsl,
    quote,
    unquote,
    urlencode,
    urljoin,
    urlsplit,
    urlunsplit,
)

import certifi
from urllib3 import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.response import BaseHTTPResponse
from urllib3.util import Timeout

from .paragraphs import (
    ExtractedWebDocument,
    extract_html_document,
    extract_text_document,
    normalize_paragraph_text,
    paragraph_content_hash,
    stable_paragraph_id,
)

MAX_PAGE_BYTES: Final = 2 * 1024 * 1024
MAX_PAGE_CHARS: Final = 2 * 1024 * 1024
MAX_PARAGRAPH_CHARS: Final = 2_000
MAX_PARAGRAPHS: Final = 80
_REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})
_SUPPORTED_CONTENT_TYPES: Final = frozenset(
    {"text/html", "application/xhtml+xml", "text/plain", "text/markdown"}
)
_TRACKING_QUERY_KEYS: Final = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "vero_id",
    }
)
_CONTENT_ORIGINS: Final = frozenset(
    {"provided_content", "system_fetch", "upstream_provider", "search_metadata"}
)
_NOTICE_MESSAGES: Final = {
    "web_capture_invalid_url": "Web page URL is invalid.",
    "web_capture_unsafe_url": "Web page URL is not public.",
    "web_capture_fetch_failed": "Web page could not be fetched.",
    "web_capture_http_error": "Web page returned an unsuccessful response.",
    "web_capture_too_large": "Web page exceeded the capture size limit.",
    "web_capture_unsupported_content_type": "Web page content type is unsupported.",
    "web_capture_empty_content": "Web page returned no usable content.",
    "web_capture_no_paragraphs": "Web page contained no usable paragraphs.",
    "web_capture_metadata_fallback": "Search metadata was retained because full text was unavailable.",
}


class WebCaptureError(RuntimeError):
    """Represent one stable, non-sensitive page capture failure."""

    def __init__(self, code: str) -> None:
        """Store one allowlisted failure code and its safe public message."""
        if code not in _NOTICE_MESSAGES:
            raise ValueError("Web capture error code is unsupported.")
        self.code = code
        super().__init__(_NOTICE_MESSAGES[code])


@dataclass(frozen=True, kw_only=True)
class FetchedWebPage:
    """Bounded HTTP response detached from the requests client."""

    final_url: str
    body: bytes
    content_type: str
    encoding: str | None = None


class WebPageFetcher(Protocol):
    """Injectable network boundary used by ``WebCaptureService``."""

    def fetch(self, url: str, *, max_bytes: int) -> FetchedWebPage:
        """Fetch one bounded public page or raise ``WebCaptureError``."""
        raise NotImplementedError


class HostResolver(Protocol):
    """Resolve one logical host without making an HTTP connection."""

    def __call__(self, hostname: str, port: int) -> Sequence[str]:
        """Return every A/AAAA address visible for the host."""
        raise NotImplementedError


class PinnedWebResponse(Protocol):
    """Streaming HTTP response created from an IP-pinned connection."""

    status_code: int
    headers: Mapping[str, str]

    def iter_content(self, *, chunk_size: int) -> Iterator[bytes]:
        """Yield decoded response bytes without buffering the full response."""
        raise NotImplementedError

    def close(self) -> None:
        """Release the response and its connection pool."""
        raise NotImplementedError


class PinnedWebTransport(Protocol):
    """Connect to an already-validated IP for one logical HTTP(S) URL."""

    def request(
        self,
        url: str,
        *,
        connect_ip: str,
        timeout: tuple[float, float],
    ) -> PinnedWebResponse:
        """Return one response without resolving the logical hostname again."""
        raise NotImplementedError


def canonicalize_web_url(url: str) -> str:
    """Return a stable HTTP(S) URL without fragments or tracking parameters."""
    if not isinstance(url, str) or not url.strip():
        raise WebCaptureError("web_capture_invalid_url")
    try:
        parsed = urlsplit(url.strip())
        scheme = parsed.scheme.casefold()
        if scheme not in {"http", "https"} or parsed.username or parsed.password:
            raise WebCaptureError("web_capture_invalid_url")
        hostname = parsed.hostname
        if not hostname:
            raise WebCaptureError("web_capture_invalid_url")
        ascii_hostname = hostname.rstrip(".").encode("idna").decode("ascii").casefold()
        port = parsed.port
    except (UnicodeError, ValueError) as exc:
        raise WebCaptureError("web_capture_invalid_url") from exc
    if not ascii_hostname:
        raise WebCaptureError("web_capture_invalid_url")
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    host_display = f"[{ascii_hostname}]" if ":" in ascii_hostname else ascii_hostname
    netloc = host_display if port is None or default_port else f"{host_display}:{port}"
    decoded_path = unquote(parsed.path or "/")
    normalized_path = str(PurePosixPath(decoded_path))
    if not normalized_path.startswith("/"):
        normalized_path = f"/{normalized_path}"
    if decoded_path.endswith("/") and normalized_path != "/":
        normalized_path = f"{normalized_path}/"
    safe_path = quote(normalized_path, safe="/:@!$&'()*+,;=-._~")
    query_items = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_")
        and key.casefold() not in _TRACKING_QUERY_KEYS
    ]
    query = urlencode(sorted(query_items), doseq=True)
    return urlunsplit((scheme, netloc, safe_path, query, ""))


def stable_web_source_id(canonical_url: str) -> str:
    """Return a provider source identity independent of capture time."""
    digest = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:24]
    return f"web:{digest}"


def _socket_resolve(hostname: str, port: int) -> tuple[str, ...]:
    """Return socket addresses without applying the public-address policy."""
    try:
        return tuple(
            cast(str, item[4][0])
            for item in socket.getaddrinfo(
                hostname,
                port,
                type=socket.SOCK_STREAM,
            )
        )
    except OSError as exc:
        raise WebCaptureError("web_capture_fetch_failed") from exc


def _resolve_public_addresses(
    url: str,
    resolver: HostResolver,
) -> tuple[str, ...]:
    """Resolve and reject the whole destination when any address is unsafe."""
    parsed = urlsplit(url)
    hostname = parsed.hostname
    if not hostname:
        raise WebCaptureError("web_capture_invalid_url")
    lowered = hostname.casefold().rstrip(".")
    if lowered == "localhost" or lowered.endswith((".localhost", ".local", ".internal")):
        raise WebCaptureError("web_capture_unsafe_url")
    port = parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)
    raw_addresses: Sequence[str]
    try:
        literal = ipaddress.ip_address(lowered)
        raw_addresses = (str(literal),)
    except ValueError:
        try:
            raw_addresses = resolver(lowered, port)
        except WebCaptureError:
            raise
        except Exception as exc:
            raise WebCaptureError("web_capture_fetch_failed") from exc
    try:
        addresses = tuple({ipaddress.ip_address(item) for item in raw_addresses})
    except ValueError as exc:
        raise WebCaptureError("web_capture_fetch_failed") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise WebCaptureError("web_capture_unsafe_url")
    ordered = sorted(addresses, key=lambda item: (item.version, int(item)))
    return tuple(str(item) for item in ordered)


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    """Read a header from arbitrary case-sensitive or insensitive mappings."""
    target = name.casefold()
    return next(
        (
            value
            for key, value in headers.items()
            if isinstance(key, str)
            and key.casefold() == target
            and isinstance(value, str)
        ),
        None,
    )


def _response_encoding(content_type: str) -> str | None:
    """Extract a bounded charset token from a Content-Type header."""
    for parameter in content_type.split(";")[1:]:
        key, separator, value = parameter.partition("=")
        if separator and key.strip().casefold() == "charset":
            charset = value.strip().strip('"\'')[:128]
            return charset or None
    return None


class _Urllib3PinnedResponse:
    """Adapt an urllib3 response while owning its one-shot connection pool."""

    status_code: int
    headers: Mapping[str, str]

    def __init__(
        self,
        response: BaseHTTPResponse,
        pool: HTTPConnectionPool,
    ) -> None:
        self.status_code = response.status
        self.headers = dict(response.headers.items())
        self._response = response
        self._pool = pool

    def iter_content(self, *, chunk_size: int) -> Iterator[bytes]:
        """Yield decompressed bytes under the caller's decoded-size bound."""
        yield from self._response.stream(chunk_size, decode_content=True)

    def close(self) -> None:
        """Release both response and pool even after a bounded-read failure."""
        self._response.release_conn()
        self._response.close()
        self._pool.close()


class Urllib3PinnedWebTransport:
    """Connect directly to verified IPs while preserving Host and TLS identity."""

    def request(
        self,
        url: str,
        *,
        connect_ip: str,
        timeout: tuple[float, float],
    ) -> PinnedWebResponse:
        """Make one IP-pinned request with certificate verification enabled."""
        parsed = urlsplit(url)
        hostname = parsed.hostname
        if not hostname:
            raise WebCaptureError("web_capture_invalid_url")
        scheme = parsed.scheme.casefold()
        port = parsed.port or (443 if scheme == "https" else 80)
        host_display = f"[{hostname}]" if ":" in hostname else hostname
        default_port = (scheme == "http" and port == 80) or (
            scheme == "https" and port == 443
        )
        host_header = host_display if default_port else f"{host_display}:{port}"
        request_target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        pool_timeout = Timeout(connect=timeout[0], read=timeout[1])
        if scheme == "https":
            pool: HTTPConnectionPool = HTTPSConnectionPool(
                host=connect_ip,
                port=port,
                timeout=pool_timeout,
                retries=False,
                cert_reqs="CERT_REQUIRED",
                ca_certs=certifi.where(),
                assert_hostname=hostname,
                server_hostname=hostname,
            )
        elif scheme == "http":
            pool = HTTPConnectionPool(
                host=connect_ip,
                port=port,
                timeout=pool_timeout,
                retries=False,
            )
        else:
            raise WebCaptureError("web_capture_invalid_url")
        try:
            response = pool.request(
                "GET",
                request_target,
                preload_content=False,
                decode_content=False,
                redirect=False,
                retries=False,
                headers={
                    "Host": host_header,
                    "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8",
                    "Accept-Encoding": "gzip, deflate",
                    "User-Agent": (
                        "helloagents-deepresearch/1.1 web-evidence-capture"
                    ),
                },
            )
        except Exception:
            pool.close()
            raise
        return _Urllib3PinnedResponse(response, pool)


class RequestsWebPageFetcher:
    """Fetch public pages through DNS-validated, IP-pinned connections."""

    def __init__(
        self,
        *,
        resolver: HostResolver = _socket_resolve,
        transport: PinnedWebTransport | None = None,
        timeout: tuple[float, float] = (5.0, 15.0),
        max_redirects: int = 5,
    ) -> None:
        """Store injectable DNS and pinned-transport boundaries."""
        self._resolver = resolver
        self._transport = transport or Urllib3PinnedWebTransport()
        self._timeout = timeout
        self._max_redirects = max_redirects

    def fetch(self, url: str, *, max_bytes: int) -> FetchedWebPage:
        """Fetch one page while validating every redirect destination."""
        current = canonicalize_web_url(url)
        for redirect_index in range(self._max_redirects + 1):
            addresses = _resolve_public_addresses(current, self._resolver)
            response: PinnedWebResponse | None = None
            last_error: Exception | None = None
            for connect_ip in addresses:
                try:
                    response = self._transport.request(
                        current,
                        connect_ip=connect_ip,
                        timeout=self._timeout,
                    )
                    break
                except WebCaptureError:
                    raise
                except Exception as exc:
                    last_error = exc
            if response is None:
                raise WebCaptureError("web_capture_fetch_failed") from last_error
            try:
                if response.status_code in _REDIRECT_STATUSES:
                    location = _header_value(response.headers, "Location")
                    if not location or redirect_index >= self._max_redirects:
                        raise WebCaptureError("web_capture_http_error")
                    current = canonicalize_web_url(urljoin(current, location))
                    continue
                if response.status_code < 200 or response.status_code >= 300:
                    raise WebCaptureError("web_capture_http_error")
                raw_content_type = _header_value(
                    response.headers, "Content-Type"
                ) or "text/html"
                content_type = raw_content_type.partition(";")[0].strip().casefold()
                if content_type not in _SUPPORTED_CONTENT_TYPES:
                    raise WebCaptureError("web_capture_unsupported_content_type")
                content_length = _header_value(response.headers, "Content-Length")
                if content_length:
                    try:
                        if int(content_length) > max_bytes:
                            raise WebCaptureError("web_capture_too_large")
                    except ValueError:
                        pass
                collected = bytearray()
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    collected.extend(chunk)
                    if len(collected) > max_bytes:
                        raise WebCaptureError("web_capture_too_large")
                if not collected:
                    raise WebCaptureError("web_capture_empty_content")
                return FetchedWebPage(
                    final_url=current,
                    body=bytes(collected),
                    content_type=content_type,
                    encoding=_response_encoding(raw_content_type),
                )
            finally:
                response.close()
        raise WebCaptureError("web_capture_http_error")


@dataclass(frozen=True, kw_only=True)
class WebParagraph:
    """One source-addressable paragraph suitable for Evidence normalization."""

    paragraph_id: str
    source_id: str
    canonical_url: str
    page_title: str
    section_path: tuple[str, ...]
    paragraph_index: int
    exact_excerpt: str
    captured_at: str
    content_hash: str
    content_origin: str = "provided_content"
    evidence_level: str = "full_text"

    def as_record(self, *, dimension: str = "overview") -> Mapping[str, object]:
        """Project the paragraph into the existing provider-record shape."""
        section = " > ".join(self.section_path)
        locator: dict[str, object] = {
            "locator_type": "web_paragraph",
            "url": self.canonical_url,
            "paragraph": self.paragraph_id,
            "fragment": self.exact_excerpt[:1_024],
        }
        if section:
            locator["section"] = section[:1_024]
        return {
            "source_id": self.source_id,
            "canonical_url": self.canonical_url,
            "dimension": dimension,
            "evidence_type": "web_paragraph",
            "evidence_level": self.evidence_level,
            "title": self.page_title,
            "excerpt": self.exact_excerpt,
            "url": self.canonical_url,
            "locator_type": "web_paragraph",
            "locator": locator,
            "section_path": list(self.section_path),
            "paragraph_id": self.paragraph_id,
            "paragraph_index": self.paragraph_index,
            "captured_at": self.captured_at,
            "content_hash": self.content_hash,
            "content_origin": self.content_origin,
        }


@dataclass(frozen=True, kw_only=True)
class WebSnapshotPayload:
    """Normalized page snapshot ready for an integration-owned ArtifactStore."""

    source_id: str
    canonical_url: str
    page_title: str
    captured_at: str
    content: str
    content_hash: str
    suggested_filename: str
    content_origin: str = "provided_content"
    mime_type: str = "text/plain; charset=utf-8"

    def descriptor(self) -> Mapping[str, object]:
        """Return metadata without embedding snapshot content."""
        encoded = self.content.encode("utf-8")
        return {
            "artifact_type": "web_page_snapshot",
            "mime_type": self.mime_type,
            "suggested_filename": self.suggested_filename,
            "title": self.page_title,
            "source_ids": [self.source_id],
            "canonical_url": self.canonical_url,
            "captured_at": self.captured_at,
            "size_bytes": len(encoded),
            "checksum": self.content_hash,
            "content_origin": self.content_origin,
        }


@dataclass(frozen=True, kw_only=True)
class WebCaptureResult:
    """Full-text capture or an explicit metadata-only fallback."""

    status: str
    source_id: str
    canonical_url: str
    page_title: str
    captured_at: str
    paragraphs: tuple[WebParagraph, ...] = ()
    snapshot: WebSnapshotPayload | None = None
    metadata_excerpt: str = ""
    notices: tuple[str, ...] = ()
    notice_codes: tuple[str, ...] = ()
    content_origin: str = "search_metadata"

    @property
    def evidence_level(self) -> str:
        """Expose the strongest evidence level available in this result."""
        return "full_text" if self.paragraphs else "metadata"

    def as_records(self, *, dimension: str = "overview") -> tuple[Mapping[str, object], ...]:
        """Return full-text records, or one clearly labelled metadata record."""
        if self.paragraphs:
            return tuple(item.as_record(dimension=dimension) for item in self.paragraphs)
        excerpt = normalize_paragraph_text(self.metadata_excerpt)
        if not excerpt:
            return ()
        content_hash = paragraph_content_hash(excerpt)
        return (
            {
                "source_id": self.source_id,
                "canonical_url": self.canonical_url,
                "dimension": dimension,
                "evidence_type": "search_result",
                "evidence_level": "metadata",
                "title": self.page_title,
                "excerpt": excerpt[:MAX_PARAGRAPH_CHARS],
                "url": self.canonical_url,
                "locator_type": "search_result",
                "locator": {
                    "locator_type": "search_result",
                    "url": self.canonical_url,
                },
                "captured_at": self.captured_at,
                "content_hash": content_hash,
                "content_origin": "search_metadata",
                "capture_notice_codes": list(self.notice_codes),
            },
        )


def _require_bounded_text_encoding(
    value: str,
    *,
    encoding: str,
    max_chars: int,
    max_bytes: int,
) -> None:
    """Enforce character and encoded-byte limits without one large allocation."""
    if len(value) > max_chars:
        raise WebCaptureError("web_capture_too_large")
    try:
        encoder = getincrementalencoder(encoding)(errors="strict")
    except LookupError as exc:
        raise WebCaptureError("web_capture_empty_content") from exc
    encoded_size = 0
    for start in range(0, len(value), 16 * 1024):
        encoded_size += len(encoder.encode(value[start : start + 16 * 1024]))
        if encoded_size > max_bytes:
            raise WebCaptureError("web_capture_too_large")
    encoded_size += len(encoder.encode("", final=True))
    if encoded_size > max_bytes:
        raise WebCaptureError("web_capture_too_large")


def _same_canonical_site(requested_url: str, hinted_url: str) -> bool:
    requested = (urlsplit(requested_url).hostname or "").casefold().removeprefix("www.")
    hinted = (urlsplit(hinted_url).hostname or "").casefold().removeprefix("www.")
    return bool(requested and requested == hinted)


def _snapshot_text(document: ExtractedWebDocument) -> str:
    """Render a deterministic normalized page snapshot."""
    lines = [f"# {document.title}"]
    active_section: tuple[str, ...] = ()
    for block in document.blocks:
        if block.section_path != active_section:
            common = 0
            for left, right in zip(active_section, block.section_path):
                if left != right:
                    break
                common += 1
            for level, heading in enumerate(block.section_path[common:], start=common + 2):
                lines.extend(("", f"{'#' * min(level, 6)} {heading}"))
            active_section = block.section_path
        lines.extend(("", block.text))
    return "\n".join(lines).strip() + "\n"


class WebCaptureService:
    """Create bounded paragraph evidence and normalized snapshot payloads."""

    def __init__(
        self,
        *,
        fetcher: WebPageFetcher | None = None,
        resolver: HostResolver = _socket_resolve,
        max_page_bytes: int = MAX_PAGE_BYTES,
        max_page_chars: int = MAX_PAGE_CHARS,
        max_paragraph_chars: int = MAX_PARAGRAPH_CHARS,
        max_paragraphs: int = MAX_PARAGRAPHS,
        min_paragraph_chars: int = 20,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Store bounded extraction limits and injectable side-effect seams."""
        limits = {
            "max_page_bytes": max_page_bytes,
            "max_page_chars": max_page_chars,
            "max_paragraph_chars": max_paragraph_chars,
            "max_paragraphs": max_paragraphs,
            "min_paragraph_chars": min_paragraph_chars,
        }
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 1
            for value in limits.values()
        ):
            raise ValueError("Web capture limits must be positive integers.")
        if min_paragraph_chars > max_paragraph_chars:
            raise ValueError("Minimum paragraph length cannot exceed its maximum.")
        self._resolver = resolver
        self._fetcher = fetcher or RequestsWebPageFetcher(resolver=resolver)
        self._max_page_bytes = max_page_bytes
        self._max_page_chars = max_page_chars
        self._max_paragraph_chars = max_paragraph_chars
        self._max_paragraphs = max_paragraphs
        self._min_paragraph_chars = min_paragraph_chars
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def capture(
        self,
        url: str,
        *,
        fallback_title: str = "",
        fallback_snippet: str = "",
    ) -> WebCaptureResult:
        """Fetch and parse one page, retaining metadata on recoverable failure."""
        try:
            canonical_url = canonicalize_web_url(url)
            fetched = self._fetcher.fetch(canonical_url, max_bytes=self._max_page_bytes)
            return self.capture_content(
                canonical_url,
                fetched.body,
                final_url=fetched.final_url,
                content_type=fetched.content_type,
                encoding=fetched.encoding,
                fallback_title=fallback_title,
                fallback_snippet=fallback_snippet,
                content_origin="system_fetch",
            )
        except WebCaptureError as exc:
            return self._fallback(
                url=url,
                title=fallback_title,
                snippet=fallback_snippet,
                code=exc.code,
            )
        except Exception:
            return self._fallback(
                url=url,
                title=fallback_title,
                snippet=fallback_snippet,
                code="web_capture_fetch_failed",
            )

    def capture_content(
        self,
        url: str,
        content: str | bytes,
        *,
        final_url: str | None = None,
        content_type: str = "text/plain",
        encoding: str | None = None,
        fallback_title: str = "",
        fallback_snippet: str = "",
        content_origin: str = "provided_content",
        validate_public_url: bool = False,
    ) -> WebCaptureResult:
        """Normalize already-fetched content without performing network I/O."""
        try:
            if content_origin not in _CONTENT_ORIGINS - {"search_metadata"}:
                raise ValueError("Web content origin is unsupported.")
            requested_url = canonicalize_web_url(url)
            canonical_url = canonicalize_web_url(final_url or requested_url)
            if validate_public_url:
                _resolve_public_addresses(requested_url, self._resolver)
                if canonical_url != requested_url:
                    _resolve_public_addresses(canonical_url, self._resolver)
            if isinstance(content, str):
                if not content:
                    raise WebCaptureError("web_capture_empty_content")
                _require_bounded_text_encoding(
                    content,
                    encoding=encoding or "utf-8",
                    max_chars=self._max_page_chars,
                    max_bytes=self._max_page_bytes,
                )
            else:
                if not content:
                    raise WebCaptureError("web_capture_empty_content")
                if len(content) > self._max_page_bytes:
                    raise WebCaptureError("web_capture_too_large")
            normalized_type = content_type.partition(";")[0].strip().casefold()
            if normalized_type not in _SUPPORTED_CONTENT_TYPES:
                raise WebCaptureError("web_capture_unsupported_content_type")
            if isinstance(content, str):
                decoded = content
            else:
                try:
                    decoded = content.decode(encoding or "utf-8")
                except (LookupError, UnicodeDecodeError):
                    decoded = content.decode("utf-8", errors="replace")
            if normalized_type in {"text/html", "application/xhtml+xml"}:
                document = extract_html_document(
                    decoded,
                    base_url=canonical_url,
                    fallback_title=fallback_title,
                    min_chars=self._min_paragraph_chars,
                    max_chars=self._max_paragraph_chars,
                    max_paragraphs=self._max_paragraphs,
                )
                if document.canonical_hint:
                    hinted = canonicalize_web_url(document.canonical_hint)
                    if _same_canonical_site(canonical_url, hinted):
                        canonical_url = hinted
            else:
                document = extract_text_document(
                    decoded,
                    fallback_title=fallback_title or canonical_url,
                    min_chars=self._min_paragraph_chars,
                    max_chars=self._max_paragraph_chars,
                    max_paragraphs=self._max_paragraphs,
                )
            if not document.blocks:
                raise WebCaptureError("web_capture_no_paragraphs")
            captured_at = self._capture_time()
            source_id = stable_web_source_id(canonical_url)
            paragraphs = tuple(
                WebParagraph(
                    paragraph_id=stable_paragraph_id(
                        canonical_url=canonical_url,
                        section_path=block.section_path,
                        excerpt=block.text,
                    ),
                    source_id=source_id,
                    canonical_url=canonical_url,
                    page_title=document.title,
                    section_path=block.section_path,
                    paragraph_index=index,
                    exact_excerpt=block.text,
                    captured_at=captured_at,
                    content_hash=paragraph_content_hash(block.text),
                    content_origin=content_origin,
                )
                for index, block in enumerate(document.blocks, start=1)
            )
            snapshot_content = _snapshot_text(document)
            snapshot_hash = hashlib.sha256(snapshot_content.encode("utf-8")).hexdigest()
            snapshot = WebSnapshotPayload(
                source_id=source_id,
                canonical_url=canonical_url,
                page_title=document.title,
                captured_at=captured_at,
                content=snapshot_content,
                content_hash=snapshot_hash,
                suggested_filename=f"web-{source_id.partition(':')[2]}-{snapshot_hash[:12]}.txt",
                content_origin=content_origin,
            )
            return WebCaptureResult(
                status="complete",
                source_id=source_id,
                canonical_url=canonical_url,
                page_title=document.title,
                captured_at=captured_at,
                paragraphs=paragraphs,
                snapshot=snapshot,
                content_origin=content_origin,
            )
        except WebCaptureError as exc:
            return self._fallback(
                url=final_url or url,
                title=fallback_title,
                snippet=fallback_snippet,
                code=exc.code,
            )
        except Exception:
            return self._fallback(
                url=final_url or url,
                title=fallback_title,
                snippet=fallback_snippet,
                code="web_capture_empty_content",
            )

    def _capture_time(self) -> str:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()

    def _fallback(
        self,
        *,
        url: str,
        title: str,
        snippet: str,
        code: str,
    ) -> WebCaptureResult:
        try:
            canonical_url = canonicalize_web_url(url)
        except WebCaptureError:
            canonical_url = "https://invalid.example/"
        normalized_snippet = normalize_paragraph_text(snippet)[:MAX_PARAGRAPH_CHARS]
        codes = (code,) if not normalized_snippet else (code, "web_capture_metadata_fallback")
        return WebCaptureResult(
            status="partial" if normalized_snippet else "failed",
            source_id=stable_web_source_id(canonical_url),
            canonical_url=canonical_url,
            page_title=normalize_paragraph_text(title)[:512] or canonical_url,
            captured_at=self._capture_time(),
            metadata_excerpt=normalized_snippet,
            notices=tuple(_NOTICE_MESSAGES[item] for item in codes),
            notice_codes=codes,
            content_origin="search_metadata",
        )


__all__ = [
    "MAX_PAGE_BYTES",
    "MAX_PAGE_CHARS",
    "MAX_PARAGRAPH_CHARS",
    "MAX_PARAGRAPHS",
    "FetchedWebPage",
    "HostResolver",
    "PinnedWebResponse",
    "PinnedWebTransport",
    "RequestsWebPageFetcher",
    "Urllib3PinnedWebTransport",
    "WebCaptureError",
    "WebCaptureResult",
    "WebCaptureService",
    "WebPageFetcher",
    "WebParagraph",
    "WebSnapshotPayload",
    "canonicalize_web_url",
    "stable_web_source_id",
]
