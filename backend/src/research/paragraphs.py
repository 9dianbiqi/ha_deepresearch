"""Deterministic heading-aware paragraph extraction for captured Web pages."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Final
from urllib.parse import urljoin

_SPACE_RE: Final = re.compile(r"\s+")
_MARKDOWN_HEADING_RE: Final = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_BLOCK_TAGS: Final = frozenset({"p", "li", "blockquote", "pre", "dd", "dt", "figcaption"})
_VOID_TAGS: Final = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
)
_SKIP_TAGS: Final = frozenset(
    {"script", "style", "noscript", "svg", "canvas", "form", "nav", "footer", "aside"}
)
_BOILERPLATE_TOKENS: Final = (
    "accept all cookies",
    "accept cookies",
    "cookie preferences",
    "manage cookies",
    "privacy choices",
    "enable javascript",
    "all rights reserved",
    "接受所有 cookie",
    "接受全部 cookie",
    "cookie 设置",
    "隐私设置",
)
_SKIP_ATTRIBUTE_TOKENS: Final = (
    "cookie",
    "consent",
    "footer",
    "navigation",
    "navbar",
    "sidebar",
    "subscribe",
    "newsletter",
)


def normalize_paragraph_text(value: str) -> str:
    """Collapse markup whitespace without changing the textual wording."""
    return _SPACE_RE.sub(" ", value).strip()


def paragraph_content_hash(excerpt: str) -> str:
    """Return a full SHA-256 digest for one normalized paragraph."""
    normalized = normalize_paragraph_text(excerpt)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def stable_paragraph_id(
    *,
    canonical_url: str,
    section_path: tuple[str, ...],
    excerpt: str,
) -> str:
    """Build an ID stable across capture time and unrelated page edits."""
    payload = "\x1f".join(
        (
            canonical_url.strip(),
            *(normalize_paragraph_text(item).casefold() for item in section_path),
            normalize_paragraph_text(excerpt).casefold(),
        )
    )
    return f"webp_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]}"


@dataclass(frozen=True, kw_only=True)
class ExtractedTextBlock:
    """One paragraph-like block associated with the active heading path."""

    text: str
    section_path: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class ExtractedWebDocument:
    """Bounded parsing output before capture metadata is attached."""

    title: str
    blocks: tuple[ExtractedTextBlock, ...]
    canonical_hint: str | None = None


class _DocumentHTMLParser(HTMLParser):
    """Extract meaningful blocks using only the standard library parser."""

    def __init__(self, *, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.blocks: list[ExtractedTextBlock] = []
        self.title_parts: list[str] = []
        self.og_title: str | None = None
        self.first_heading: str | None = None
        self.canonical_hint: str | None = None
        self._skip_depth = 0
        self._title_depth = 0
        self._active_tag: str | None = None
        self._active_parts: list[str] = []
        self._active_heading_level: int | None = None
        self._headings: list[str] = []

    @staticmethod
    def _attributes(values: list[tuple[str, str | None]]) -> dict[str, str]:
        return {key.casefold(): value or "" for key, value in values}

    @staticmethod
    def _skip_from_attributes(attributes: dict[str, str]) -> bool:
        if attributes.get("aria-hidden", "").casefold() == "true":
            return True
        role = attributes.get("role", "").casefold()
        if role in {"navigation", "banner", "contentinfo", "dialog"}:
            return True
        combined = f"{attributes.get('id', '')} {attributes.get('class', '')}".casefold()
        return any(token in combined for token in _SKIP_ATTRIBUTE_TOKENS)

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized_tag = tag.casefold()
        attributes = self._attributes(attrs)
        if self._skip_depth:
            if normalized_tag not in _VOID_TAGS:
                self._skip_depth += 1
            return
        if normalized_tag in _SKIP_TAGS or self._skip_from_attributes(attributes):
            if normalized_tag not in _VOID_TAGS:
                self._skip_depth = 1
            return
        if normalized_tag == "title":
            self._title_depth = 1
            return
        if normalized_tag == "meta":
            property_name = attributes.get("property", "").casefold()
            name = attributes.get("name", "").casefold()
            if property_name == "og:title" or name == "twitter:title":
                candidate = normalize_paragraph_text(attributes.get("content", ""))
                if candidate:
                    self.og_title = candidate
            return
        if normalized_tag == "link" and "canonical" in attributes.get("rel", "").casefold().split():
            href = attributes.get("href", "").strip()
            if href:
                self.canonical_hint = urljoin(self.base_url, href)
            return
        if normalized_tag == "br" and self._active_tag is not None:
            self._active_parts.append(" ")
            return
        is_heading = len(normalized_tag) == 2 and normalized_tag[0] == "h" and normalized_tag[1].isdigit()
        if self._active_tag is None and (normalized_tag in _BLOCK_TAGS or is_heading):
            self._active_tag = normalized_tag
            self._active_parts = []
            self._active_heading_level = int(normalized_tag[1]) if is_heading else None

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        was_skipping = bool(self._skip_depth)
        previous_depth = self._skip_depth
        self.handle_starttag(tag, attrs)
        if self._skip_depth > previous_depth:
            self.handle_endtag(tag)
            return
        if was_skipping:
            return
        if tag.casefold() not in {"meta", "link", "br", "img", "input", "hr"}:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.casefold()
        if self._skip_depth:
            if normalized_tag not in _VOID_TAGS:
                self._skip_depth -= 1
            return
        if normalized_tag == "title" and self._title_depth:
            self._title_depth = 0
            return
        if self._active_tag != normalized_tag:
            return
        text = normalize_paragraph_text("".join(self._active_parts))
        heading_level = self._active_heading_level
        self._active_tag = None
        self._active_parts = []
        self._active_heading_level = None
        if not text:
            return
        if heading_level is not None:
            if self.first_heading is None:
                self.first_heading = text
            self._headings = self._headings[: heading_level - 1]
            self._headings.append(text)
            return
        self.blocks.append(
            ExtractedTextBlock(text=text, section_path=tuple(self._headings))
        )

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._title_depth:
            self.title_parts.append(data)
        if self._active_tag is not None:
            self._active_parts.append(data)


def _is_boilerplate(text: str) -> bool:
    normalized = normalize_paragraph_text(text).casefold()
    return any(token in normalized for token in _BOILERPLATE_TOKENS)


def _bounded_chunks(text: str, *, max_chars: int) -> tuple[str, ...]:
    """Split oversized blocks at sentence, clause, or word boundaries."""
    normalized = normalize_paragraph_text(text)
    if len(normalized) <= max_chars:
        return (normalized,)
    chunks: list[str] = []
    remaining = normalized
    preferred = frozenset("。！？!?；;.")
    secondary = frozenset("，,:：、 ")
    while len(remaining) > max_chars:
        window = remaining[: max_chars + 1]
        boundary = max((index for index, char in enumerate(window) if char in preferred), default=-1)
        if boundary < max_chars // 3:
            boundary = max((index for index, char in enumerate(window) if char in secondary), default=-1)
        end = boundary + 1 if boundary >= max_chars // 3 else max_chars
        chunk = remaining[:end].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[end:].strip()
    if remaining:
        chunks.append(remaining)
    return tuple(chunks)


def _clean_blocks(
    blocks: tuple[ExtractedTextBlock, ...],
    *,
    min_chars: int,
    max_chars: int,
    max_paragraphs: int,
) -> tuple[ExtractedTextBlock, ...]:
    """Bound, de-duplicate, and filter extracted paragraph blocks."""
    selected: list[ExtractedTextBlock] = []
    seen: set[str] = set()
    for block in blocks:
        for chunk in _bounded_chunks(block.text, max_chars=max_chars):
            normalized_key = normalize_paragraph_text(chunk).casefold()
            if len(chunk) < min_chars or normalized_key in seen or _is_boilerplate(chunk):
                continue
            seen.add(normalized_key)
            selected.append(
                ExtractedTextBlock(
                    text=chunk,
                    section_path=tuple(
                        normalize_paragraph_text(item)
                        for item in block.section_path
                        if normalize_paragraph_text(item)
                    ),
                )
            )
            if len(selected) >= max_paragraphs:
                return tuple(selected)
    return tuple(selected)


def extract_html_document(
    html: str,
    *,
    base_url: str,
    fallback_title: str = "",
    min_chars: int = 20,
    max_chars: int = 2_000,
    max_paragraphs: int = 80,
) -> ExtractedWebDocument:
    """Extract a bounded heading-aware document from HTML markup."""
    parser = _DocumentHTMLParser(base_url=base_url)
    parser.feed(html)
    parser.close()
    title = (
        normalize_paragraph_text("".join(parser.title_parts))
        or parser.og_title
        or parser.first_heading
        or normalize_paragraph_text(fallback_title)
        or base_url
    )
    return ExtractedWebDocument(
        title=title[:512],
        canonical_hint=parser.canonical_hint,
        blocks=_clean_blocks(
            tuple(parser.blocks),
            min_chars=min_chars,
            max_chars=max_chars,
            max_paragraphs=max_paragraphs,
        ),
    )


def extract_text_document(
    text: str,
    *,
    fallback_title: str,
    min_chars: int = 20,
    max_chars: int = 2_000,
    max_paragraphs: int = 80,
) -> ExtractedWebDocument:
    """Extract paragraphs and Markdown heading paths from plain text."""
    headings: list[str] = []
    blocks: list[ExtractedTextBlock] = []
    pending: list[str] = []

    def flush() -> None:
        if not pending:
            return
        blocks.append(
            ExtractedTextBlock(
                text=normalize_paragraph_text(" ".join(pending)),
                section_path=tuple(headings),
            )
        )
        pending.clear()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        heading = _MARKDOWN_HEADING_RE.match(line)
        if heading:
            flush()
            level = len(heading.group(1))
            headings = headings[: level - 1]
            headings.append(normalize_paragraph_text(heading.group(2)))
        elif not line:
            flush()
        else:
            pending.append(line)
    flush()
    title = headings[0] if headings else normalize_paragraph_text(fallback_title)
    return ExtractedWebDocument(
        title=(title or "Captured Web page")[:512],
        blocks=_clean_blocks(
            tuple(blocks),
            min_chars=min_chars,
            max_chars=max_chars,
            max_paragraphs=max_paragraphs,
        ),
    )


__all__ = [
    "ExtractedTextBlock",
    "ExtractedWebDocument",
    "extract_html_document",
    "extract_text_document",
    "normalize_paragraph_text",
    "paragraph_content_hash",
    "stable_paragraph_id",
]
