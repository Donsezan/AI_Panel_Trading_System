"""Reading one publisher's archive: the listing pages, and the article pages behind them.

Structure verified against the live site on 2026-09-16, not inferred:

* An article page carries `application/ld+json` with a `NewsArticle`, whose `datePublished` is an
  instant precise to the millisecond. **That is the only acceptable source of publication time**
  (§6.7): the URL path and the listing column both carry a bare date, they disagree with each other
  on about 1% of rows, and a date plus a lag hands a whole day's headlines to that day's first
  cycle — up to twenty-four hours of intra-day look-ahead wearing a well-formed timestamp.
* The body lives in `<div class="document-body …">`, and there is **more than one** — an ad slot or
  a premium component splits it. They are joined in document order; taking the longest would drop
  the lede. The boilerplate disclosure is the block carrying `font-metadata`, and it is the only
  one excluded.

Failure semantics: an article with no `datePublished` raises `ConfigError` naming the URL and is
never stored. Fail closed — a row that cannot be placed in time cannot be filtered point-in-time,
and admitting it with a guessed timestamp is the look-ahead bug this whole design exists to avoid.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from html import unescape
from html.parser import HTMLParser
from typing import Any, Final

from decision_lab.archive.store import RawArticle
from tradebot.core.errors import ConfigError
from tradebot.core.schema import DomainModel

#: `<script type="application/ld+json">…</script>`, the publisher's structured record.
_LD_JSON: Final = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I
)
_WHITESPACE: Final = re.compile(r"\s+")

#: The class marking the boilerplate disclosure rather than the article body.
BOILERPLATE_MARKER: Final = "font-metadata"
BODY_MARKER: Final = "document-body"


class _BodyExtractor(HTMLParser):
    """Character data inside every `document-body` div that is not the disclosure block."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self._depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._depth:
            if tag == "div":
                self._depth += 1
            return
        if tag != "div":
            return
        classes = unescape(dict(attrs).get("class") or "")
        if BODY_MARKER in classes and BOILERPLATE_MARKER not in classes:
            self._depth = 1
            self._parts = []

    def handle_endtag(self, tag: str) -> None:
        if self._depth and tag == "div":
            self._depth -= 1
            if self._depth == 0:
                self.blocks.append("".join(self._parts))

    def handle_data(self, data: str) -> None:
        if self._depth:
            self._parts.append(data)


#: An article link: `/<section>/YYYY/MM/DD/<slug>` on the publisher's own host. The date in the
#: path is *not* trusted as a timestamp (§6.7) — it is only what tells an article link apart from
#: the sign-in and navigation links sharing the same list markup.
ARTICLE_HREF: Final = re.compile(
    r"^https://www\.coindesk\.com/[a-z0-9\-]+/\d{4}/\d{2}/\d{2}/[^/?#]+$"
)
_LISTED_DATE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ListingEntry(DomainModel):
    """One row of an archive listing page.

    `listed_on` is the date the listing is ordered by, which is what page-walking needs to know
    when it has gone far enough back. It is deliberately **not** a publication time: it disagrees
    with the article's own instant, and with the date in the URL, on about 1% of rows.
    """

    url: str
    title: str
    listed_on: date


class _ListingExtractor(HTMLParser):
    """Rows of an archive listing: an article `<a>` wrapping a title span and a date span."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[ListingEntry] = []
        self._href: str | None = None
        self._spans: list[str] = []
        self._in_span = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            href = unescape(dict(attrs).get("href") or "")
            self._href = href if ARTICLE_HREF.match(href) else None
            self._spans = []
        elif tag == "span" and self._href:
            self._in_span = True
            self._spans.append("")

    def handle_endtag(self, tag: str) -> None:
        if tag == "span":
            self._in_span = False
        elif tag == "a" and self._href:
            self._close(self._href)
            self._href = None

    def handle_data(self, data: str) -> None:
        if self._in_span and self._spans:
            self._spans[-1] += data

    def _close(self, href: str) -> None:
        texts = [_WHITESPACE.sub(" ", s).strip() for s in self._spans]
        dates = [t for t in texts if _LISTED_DATE.match(t)]
        titles = [t for t in texts if t and not _LISTED_DATE.match(t)]
        if not dates or not titles:
            return
        self.rows.append(
            ListingEntry(url=href, title=titles[0], listed_on=date.fromisoformat(dates[-1]))
        )


def parse_listing(page: str) -> tuple[ListingEntry, ...]:
    """Every article row on one archive listing page, in the page's own order."""
    extractor = _ListingExtractor()
    extractor.feed(page)
    extractor.close()
    return tuple(extractor.rows)


def _news_article(page: str) -> dict[str, Any]:
    """The `NewsArticle` record, or an empty mapping when the page carries none."""
    for block in _LD_JSON.findall(page):
        try:
            loaded = json.loads(block)
        except ValueError:
            continue
        for entry in loaded if isinstance(loaded, list) else [loaded]:
            if isinstance(entry, dict) and entry.get("@type") == "NewsArticle":
                return entry
    return {}


def _instant(value: str, *, url: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError(f"{url}: datePublished {value!r} is not an instant: {exc}") from exc
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def body_of(page: str) -> str:
    """Article text, joined across the split body blocks in document order."""
    extractor = _BodyExtractor()
    extractor.feed(page)
    extractor.close()
    joined = " ".join(block for block in extractor.blocks if block.strip())
    return _WHITESPACE.sub(" ", joined).strip()


def parse_article(page: str, *, url: str, fetched_at: datetime) -> RawArticle:
    """One article page to one raw row, refusing anything that cannot be placed in time."""
    record = _news_article(page)
    published = record.get("datePublished")
    if not published:
        raise ConfigError(
            f"{url}: no datePublished in the page's ld+json, so the article cannot be placed in "
            "time. Storing it with a date-derived timestamp would grant it to earlier cycles than "
            "it was knowable in (spec §6.7)."
        )
    modified = record.get("dateModified")
    return RawArticle(
        source_id="coindesk",
        url=url,
        title=_text(record.get("headline")),
        body=body_of(page),
        abstract=_text(record.get("abstract")),
        keywords=tuple(str(k) for k in record.get("keywords") or ()),
        author=_first_author(record.get("author")),
        section=_text(record.get("articleSection")),
        published_at=_instant(str(published), url=url),
        modified_at=_instant(str(modified), url=url) if modified else None,
        fetched_at=fetched_at,
    )


def _text(value: object) -> str:
    return _WHITESPACE.sub(" ", str(value or "")).strip()


def _first_author(value: object) -> str:
    """The byline. A list because a piece can be co-written; the record keeps the lead name."""
    entries = value if isinstance(value, list) else [value]
    for entry in entries:
        if isinstance(entry, dict) and entry.get("name"):
            return _text(entry["name"])
        if isinstance(entry, str) and entry.strip():
            return _text(entry)
    return ""
