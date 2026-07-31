"""News ingestion, relevance, and the seam the ContextBuilder consumes.

Sourcing policy is legal, not merely technical: **RSS and official APIs, never scraping**.
Scraping raises ToS and copyright exposure for zero benefit when the publisher offers a feed.
Implementations respect `robots.txt`, identify with a real `User-Agent`, cache aggressively,
and store title + short excerpt + link rather than full article bodies (PLAN §3.3).

Two timestamps, and the distinction is load-bearing: `published_at` is the publisher's claim and
may be wrong, back-dated, or missing. `observed_at` is stamped by us when the item entered the
pipeline, and it is the **only** field a point-in-time filter may use. Filtering a replayed
cycle on `published_at` would let an item we did not have at the time into the decision — the
look-ahead bug that makes a backtest quietly meaningless (DESIGN [L12]).

Failure semantics: a source that is down raises `VenueError` and the cycle proceeds *without*
it — the snapshot's `NewsCoverage` records the gap so the panel knows its coverage is partial,
rather than mistaking silence for calm (DESIGN §8.1). A source whose publisher forbids fetching
raises `SourceDisallowedError` and is treated the same way, permanently.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tradebot.core.instrument import Instrument
from tradebot.core.schema import DomainModel, Money, UtcDatetime
from tradebot.core.snapshot import NewsCoverage, NewsItemView


class RawNewsItem(DomainModel):
    """A fetched item before normalization and dedup."""

    source_id: str
    title: str
    body: str
    url: str
    published_at: UtcDatetime
    observed_at: UtcDatetime


class NewsItem(DomainModel):
    """A normalized, deduplicated, stored item.

    `url_hash` is the canonical-URL identity that makes the cheap half of dedup work; `item_id`
    is the same hash, so an item's identity is recomputable from its URL alone.
    """

    item_id: str
    source_id: str
    url: str
    url_hash: str
    title: str
    excerpt: str = ""
    published_at: UtcDatetime
    observed_at: UtcDatetime

    @property
    def text(self) -> str:
        """The embedding and relevance input: everything we are entitled to keep."""
        return f"{self.title}. {self.excerpt}".strip()

    def view(self, relevance: Money) -> NewsItemView:
        """The panel's view of this item. Data inside a delimited block, never instructions."""
        return NewsItemView(
            source=self.source_id,
            title=self.title,
            summary=self.excerpt,
            published_at=self.published_at,
            observed_at=self.observed_at,
            relevance=relevance,
        )


@runtime_checkable
class NewsSource(Protocol):
    """One feed. Registered by id and selected per basket."""

    source_id: str

    async def fetch_latest(self) -> tuple[RawNewsItem, ...]: ...


@runtime_checkable
class RelevanceFilter(Protocol):
    """Scores an item against the instruments a basket trades.

    Scoring only ranks and filters. **Interpreting** the news is the panel's job — that is what
    the seats are for (DESIGN §6.4).
    """

    def relevance(self, item: NewsItem, instruments: tuple[Instrument, ...]) -> Money: ...


@runtime_checkable
class NewsFeed(Protocol):
    """What the ContextBuilder asks for: the items to put in one snapshot, plus the gaps.

    One call rather than refresh-then-select, so the builder cannot accidentally read a store
    that nobody refreshed, and so the coverage report always describes the fetch that produced
    these exact items.
    """

    async def snapshot_news(
        self, instruments: tuple[Instrument, ...], as_of: UtcDatetime, limit: int
    ) -> tuple[tuple[NewsItemView, ...], NewsCoverage]: ...
