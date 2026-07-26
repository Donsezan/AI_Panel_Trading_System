"""News ingestion and relevance.

Sourcing policy is legal, not merely technical: **RSS and official APIs, never scraping**.
Scraping raises ToS and copyright exposure for zero benefit when the publisher offers a feed.
Implementations respect `robots.txt`, identify with a real `User-Agent`, cache aggressively,
and store title + short excerpt + link rather than full article bodies (PLAN §3.3).

Failure semantics: a source that is down raises `VenueError` and the cycle proceeds *without*
it — the snapshot records the gap so the panel knows its news coverage is partial, rather than
mistaking silence for calm (DESIGN §8.1).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tradebot.core.instrument import Instrument
from tradebot.core.schema import DomainModel, Money, UtcDatetime


class RawNewsItem(DomainModel):
    """A fetched item before normalization and dedup.

    `observed_at` is stamped by us and is what point-in-time filtering uses; `published_at` is
    the publisher's claim and cannot be trusted for replay ordering.
    """

    source_id: str
    title: str
    body: str
    url: str
    published_at: UtcDatetime
    observed_at: UtcDatetime


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

    def relevance(self, item: RawNewsItem, instruments: tuple[Instrument, ...]) -> Money: ...
