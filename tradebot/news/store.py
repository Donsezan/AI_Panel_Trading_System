"""The typed news record: what we saw, when we saw it, and nothing we may not keep.

This is the point-in-time store the ContextBuilder selects from. Two properties matter:

* **`observed_at` is the only filter a replay may use.** Selection is `observed_at <= as_of`,
  never `published_at <= as_of`. A publisher can back-date, and an item stamped an hour ago that
  we only received now would otherwise leak into a decision that did not have it (DESIGN [L12]).
* **`url_hash` is unique.** The cheap half of dedup is a database constraint rather than a
  policy, so the same article arriving from two feeds cannot be stored twice and counted twice
  as evidence.

Failure semantics: reads that find nothing return nothing — a cycle with no news is a normal
cycle, and the snapshot's `NewsCoverage` says whether that is because nothing happened or
because a feed was down. Writes go through the single writer (PLAN §2.6).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import Connection, Engine, select

from tradebot.interfaces.news import NewsItem
from tradebot.persistence.database import SingleWriter
from tradebot.persistence.schema import news_items, upsert

#: How far back a snapshot may reach for news. Older items are history, not a catalyst, and the
#: vector store is what answers "what did we know about this last month".
DEFAULT_LOOKBACK = timedelta(hours=48)

#: Rows pulled per selection before relevance ranking. Bounds the work per cycle.
DEFAULT_CANDIDATES = 200


class NewsStore:
    """Reads and writes normalized news items."""

    def __init__(
        self,
        engine: Engine,
        writer: SingleWriter,
        *,
        lookback: timedelta = DEFAULT_LOOKBACK,
        candidates: int = DEFAULT_CANDIDATES,
    ) -> None:
        self._engine = engine
        self._writer = writer
        self._lookback = lookback
        self._candidates = candidates

    async def add(self, items: tuple[NewsItem, ...]) -> None:
        """Store items, overwriting any row with the same identity."""
        if not items:
            return
        rows = [item.model_dump() for item in items]

        def write(connection: Connection) -> None:
            for row in rows:
                upsert(connection, news_items, row, ["item_id"])

        await self._writer.run(write)

    def known_hashes(self, hashes: frozenset[str]) -> frozenset[str]:
        """Which of these canonical-URL hashes we already hold."""
        if not hashes:
            return frozenset()
        query = select(news_items.c.url_hash).where(news_items.c.url_hash.in_(sorted(hashes)))
        with self._engine.connect() as connection:
            return frozenset(row.url_hash for row in connection.execute(query))

    def select(
        self, observed_before: datetime, *, limit: int | None = None
    ) -> tuple[NewsItem, ...]:
        """Recent items we had observed by `observed_before`, newest first.

        The cutoff is the whole point-in-time guarantee, and it is a `WHERE` clause so no caller
        can forget it.
        """
        query = (
            select(news_items)
            .where(
                news_items.c.observed_at <= observed_before,
                news_items.c.observed_at >= observed_before - self._lookback,
            )
            .order_by(news_items.c.observed_at.desc(), news_items.c.item_id)
            .limit(limit or self._candidates)
        )
        with self._engine.connect() as connection:
            return tuple(NewsItem.model_validate(row._mapping) for row in connection.execute(query))
