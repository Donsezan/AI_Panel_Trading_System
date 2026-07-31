"""RSS and Atom feeds — the only news sourcing this system does (PLAN §3.3).

The prototype scraped Cointelegraph's HTML. Cointelegraph publishes RSS, so scraping bought ToS
and copyright exposure for nothing; this replaces it. Adding a source is a URL in config, not
code, which is the cheap way to broaden coverage.

`feedparser` does the parsing because real feeds are malformed in ways a hand-rolled parser gets
wrong: unclosed tags, three different date formats in one feed, RSS elements inside an Atom
document. It never touches the network here — the `FeedFetcher` owns I/O, so politeness,
conditional GET and `robots.txt` are enforced in one place and the parser is fed a string.

`published_at` is the publisher's claim and is *not* trusted for ordering: when it is missing or
unparseable it falls back to `observed_at`, and the point-in-time filter uses `observed_at`
regardless (DESIGN [L12]).

Failure semantics: a feed that will not parse into a single entry raises `VenueError`; the hub
records the coverage gap and the cycle proceeds without that source. An unchanged feed (`304`)
yields no items, which is not a failure.
"""

from __future__ import annotations

import calendar
from datetime import UTC, datetime
from typing import Any, Final

import feedparser

from tradebot.core.clock import Clock
from tradebot.core.errors import ConfigError, VenueError
from tradebot.core.logging import get_logger
from tradebot.interfaces.news import RawNewsItem
from tradebot.news.http import FeedFetcher
from tradebot.news.normalize import DEFAULT_EXCERPT_CHARS, excerpt, strip_html

logger = get_logger(__name__)

#: Built-in feeds, selectable by id from a basket's config. All free, all publisher-provided.
#: No paid APIs in v1 (PLAN scope): NewsAPI's free tier delays articles 24 h and forbids
#: commercial use, which makes it worthless as a trading signal.
FEEDS: Final[dict[str, str]] = {
    "cointelegraph": "https://cointelegraph.com/rss",
    "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "sec_press": "https://www.sec.gov/news/pressreleases.rss",
}

#: Entries taken from one fetch. Feeds publish 20–100; more than this is history, not news.
DEFAULT_MAX_ENTRIES: Final = 50

#: Date fields in order of preference. `published` is the publication claim; `updated` is a
#: revision, used only when there is nothing better.
_DATE_FIELDS: Final = ("published_parsed", "updated_parsed")


def _entry_datetime(entry: Any, fallback: datetime) -> datetime:
    """A UTC instant from whichever date field parsed, else the fallback."""
    for field in _DATE_FIELDS:
        parsed = entry.get(field)
        if parsed is not None:
            return datetime.fromtimestamp(calendar.timegm(parsed), UTC)
    return fallback


def _entry_body(entry: Any) -> str:
    """The longest text the entry offers, which is what an excerpt is taken from."""
    contents = [item.get("value", "") for item in (entry.get("content") or [])]
    candidates = [entry.get("summary", ""), entry.get("description", ""), *contents]
    return max((strip_html(text) for text in candidates if text), key=len, default="")


class RssNewsSource:
    """One RSS or Atom feed."""

    def __init__(
        self,
        source_id: str,
        url: str,
        fetcher: FeedFetcher,
        clock: Clock,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        excerpt_chars: int = DEFAULT_EXCERPT_CHARS,
    ) -> None:
        if not url.startswith(("http://", "https://")):
            raise ConfigError(f"feed url for {source_id!r} must be http(s): {url!r}")
        self.source_id = source_id
        self.url = url
        self._fetcher = fetcher
        self._clock = clock
        self._max_entries = max_entries
        self._excerpt_chars = excerpt_chars

    async def fetch_latest(self) -> tuple[RawNewsItem, ...]:
        body = await self._fetcher.fetch(self.url)
        if body is None:
            return ()
        return self._parse(body, self._clock.now())

    def _parse(self, body: str, observed_at: datetime) -> tuple[RawNewsItem, ...]:
        parsed = feedparser.parse(body)
        entries = parsed.get("entries") or []
        if not entries:
            raise VenueError(
                f"{self.source_id} returned no entries "
                f"(bozo={parsed.get('bozo')}: {parsed.get('bozo_exception')})"
            )
        items = [
            RawNewsItem(
                source_id=self.source_id,
                title=strip_html(entry.get("title", "")),
                body=excerpt(_entry_body(entry), self._excerpt_chars),
                url=link,
                published_at=_entry_datetime(entry, observed_at),
                observed_at=observed_at,
            )
            for entry in entries[: self._max_entries]
            if (link := entry.get("link", "").strip()) and entry.get("title")
        ]
        if not items:
            raise VenueError(f"{self.source_id} returned {len(entries)} entries, none usable")
        return tuple(items)


def build_sources(
    source_ids: tuple[str, ...],
    fetcher: FeedFetcher,
    clock: Clock,
    *,
    extra: dict[str, str] | None = None,
) -> tuple[RssNewsSource, ...]:
    """Resolve source ids against the built-in feeds plus any config-supplied URLs."""
    catalogue = {**FEEDS, **(extra or {})}
    unknown = [source_id for source_id in source_ids if source_id not in catalogue]
    if unknown:
        raise ConfigError(
            f"unknown news source(s) {', '.join(sorted(unknown))}; known: {sorted(catalogue)}"
        )
    return tuple(
        RssNewsSource(source_id, catalogue[source_id], fetcher, clock) for source_id in source_ids
    )
