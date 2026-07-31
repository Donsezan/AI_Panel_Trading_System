"""The NewsHub: fetch → normalize → dedupe → score → store → select (DESIGN §6.4).

Dedup is two-stage on purpose, cheapest first:

1. **Canonical URL hash**, which catches the same link arriving from two feeds and is a database
   uniqueness constraint rather than a policy.
2. **Embedding similarity**, which catches the same story under two URLs — syndication, a
   publisher re-posting, an aggregator rewriting the headline. Without it a widely-syndicated
   story enters the snapshot five times and gets five times the weight in the panel's evidence,
   which is a real way to bias a decision with no bad actor involved.

**News never halts trading.** A source that is down, blocked, or misconfigured costs coverage,
not the cycle: every per-source failure is caught, logged, and reported in `NewsCoverage`, which
the snapshot carries and the prompt renders so the panel knows its view is partial (DESIGN §8.1).
That is the one place in this system where a `FatalError` is deliberately absorbed rather than
propagated — news is evidence, not a correctness dependency, and a dead RSS feed must not be
able to stop a basket from managing an open position.

**Point-in-time discipline is enforced by the store, in SQL.** Selection filters on `observed_at
<= as_of`, so a replayed cycle at time T cannot see an item that reached us at T+1h even if the
publisher back-dated it (DESIGN [L12]).

Failure semantics: `snapshot_news` always returns — items and a coverage report — and raises only
if the *database* is unavailable, which is not a news failure.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final

from tradebot.core.clock import Clock
from tradebot.core.errors import FatalError, TradebotError
from tradebot.core.instrument import Instrument
from tradebot.core.logging import get_logger
from tradebot.core.snapshot import NewsCoverage, NewsItemView
from tradebot.interfaces.news import NewsItem, NewsSource, RawNewsItem, RelevanceFilter
from tradebot.interfaces.vectorstore import StoredDocument
from tradebot.news.embedding import DEFAULT_DUPLICATE_THRESHOLD, Vector, similarity
from tradebot.news.normalize import canonical_url, url_hash
from tradebot.news.store import NewsStore
from tradebot.news.vectorstore import SqliteVectorStore

logger = get_logger(__name__)

#: Minimum gap between two fetches of the same source. Publishers notice bots that poll harder
#: than they publish, and conditional GET already makes an unchanged feed cheap (PLAN §3.3).
DEFAULT_MIN_INTERVAL: Final = timedelta(minutes=5)

#: Relevance floor for entering a snapshot. Irrelevant headlines are not free: they spend tokens
#: to dilute the evidence the panel weighs.
DEFAULT_MIN_RELEVANCE: Final = Decimal("0.25")

#: Items in one snapshot. Enough for context, few enough that the news seat reads all of them.
DEFAULT_SNAPSHOT_ITEMS: Final = 8


@dataclass(frozen=True, slots=True)
class RefreshReport:
    """What one refresh did. Returned for tests and the dashboard."""

    coverage: NewsCoverage
    fetched: int = 0
    stored: int = 0
    duplicates: int = 0


class NewsHub:
    """Owns the news pipeline for every basket. One instance per process."""

    def __init__(
        self,
        sources: tuple[NewsSource, ...],
        store: NewsStore,
        vectors: SqliteVectorStore,
        relevance: RelevanceFilter,
        clock: Clock,
        *,
        min_interval: timedelta = DEFAULT_MIN_INTERVAL,
        min_relevance: Decimal = DEFAULT_MIN_RELEVANCE,
        duplicate_threshold: Decimal = DEFAULT_DUPLICATE_THRESHOLD,
    ) -> None:
        self._sources = sources
        self._store = store
        self._vectors = vectors
        self._relevance = relevance
        self._clock = clock
        self._min_interval = min_interval.total_seconds()
        self._min_relevance = min_relevance
        self._threshold = duplicate_threshold
        self._last_fetch: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def snapshot_news(
        self,
        instruments: tuple[Instrument, ...],
        as_of: datetime,
        limit: int = DEFAULT_SNAPSHOT_ITEMS,
    ) -> tuple[tuple[NewsItemView, ...], NewsCoverage]:
        """The `NewsFeed` seam: refresh, then select what was known at `as_of`."""
        report = await self.refresh()
        return self.select(instruments, as_of, limit), report.coverage

    async def refresh(self) -> RefreshReport:
        """Fetch every due source, store what is new. Never raises on a source failure.

        Serialized by a lock: two baskets cycling at once must not both fetch and then both
        decide the other's items are duplicates of their own.
        """
        async with self._lock:
            results = await asyncio.gather(
                *(self._fetch(source) for source in self._sources), return_exceptions=False
            )
            ok = tuple(source_id for source_id, items in results if items is not None)
            failed = tuple(source_id for source_id, items in results if items is None)
            raw = [item for _, items in results for item in (items or ())]
            stored, duplicates = await self._ingest(tuple(raw))
            return RefreshReport(
                coverage=NewsCoverage(sources_ok=ok, sources_failed=failed),
                fetched=len(raw),
                stored=stored,
                duplicates=duplicates,
            )

    def select(
        self, instruments: tuple[Instrument, ...], as_of: datetime, limit: int
    ) -> tuple[NewsItemView, ...]:
        """Top-`limit` relevant items observed at or before `as_of`, most relevant first."""
        scored = [
            (self._relevance.relevance(item, instruments), item)
            for item in self._store.select(as_of)
        ]
        keep = [pair for pair in scored if pair[0] >= self._min_relevance]
        keep.sort(key=lambda pair: (pair[0], pair[1].observed_at, pair[1].item_id), reverse=True)
        return tuple(item.view(score) for score, item in keep[:limit])

    async def _fetch(self, source: NewsSource) -> tuple[str, tuple[RawNewsItem, ...] | None]:
        """`None` items means the source failed; an empty tuple means nothing new."""
        if not self._is_due(source.source_id):
            return source.source_id, ()
        try:
            items = await source.fetch_latest()
        except TradebotError as exc:
            # Deliberate: news is evidence, not a dependency. A dead feed degrades coverage and
            # is reported in the snapshot; it must never stop a basket managing a live position.
            level = logger.error if isinstance(exc, FatalError) else logger.warning
            level(
                "news source unavailable",
                extra={
                    "source": source.source_id,
                    "error": str(exc),
                    "kind": type(exc).__name__,
                },
            )
            return source.source_id, None
        except Exception:
            # An unclassified failure in a feed parser is a defect, logged with its traceback for
            # exactly that reason. It is still contained here: a bug in news handling must not
            # crash the task that is managing an open position.
            logger.exception(
                "news source raised an unclassified error", extra={"source": source.source_id}
            )
            return source.source_id, None
        self._last_fetch[source.source_id] = self._clock.monotonic()
        return source.source_id, items

    def _is_due(self, source_id: str) -> bool:
        last = self._last_fetch.get(source_id)
        return last is None or self._clock.monotonic() - last >= self._min_interval

    async def _ingest(self, raw: tuple[RawNewsItem, ...]) -> tuple[int, int]:
        """Normalize, dedupe both ways, and persist. Returns `(stored, duplicates)`."""
        candidates, url_duplicates = self._normalize(raw)
        if not candidates:
            return 0, url_duplicates
        vectors = await self._vectors.embed_many(tuple(item.text for item in candidates))
        neighbours = await self._vectors.nearest_many(vectors, limit=1, observed_before=self._now())
        accepted: list[tuple[NewsItem, Vector]] = []
        near_duplicates = 0
        for item, vector, found in zip(candidates, vectors, neighbours, strict=True):
            stored_match = found[0].similarity if found else Decimal(0)
            if max(stored_match, self._best_in_batch(vector, accepted)) >= self._threshold:
                near_duplicates += 1
                logger.debug("dropped near-duplicate story", extra={"url": item.url})
                continue
            accepted.append((item, vector))
        await self._persist(accepted)
        return len(accepted), url_duplicates + near_duplicates

    def _normalize(self, raw: tuple[RawNewsItem, ...]) -> tuple[tuple[NewsItem, ...], int]:
        """Raw entries → storable items, dropping URLs we already hold or have seen in-batch."""
        by_hash: dict[str, NewsItem] = {}
        for entry in raw:
            digest = url_hash(entry.url)
            by_hash.setdefault(
                digest,
                NewsItem(
                    item_id=digest,
                    source_id=entry.source_id,
                    url=canonical_url(entry.url),
                    url_hash=digest,
                    title=entry.title,
                    excerpt=entry.body,
                    published_at=entry.published_at,
                    observed_at=entry.observed_at,
                ),
            )
        known = self._store.known_hashes(frozenset(by_hash))
        fresh = tuple(item for digest, item in by_hash.items() if digest not in known)
        return fresh, len(raw) - len(fresh)

    def _best_in_batch(self, vector: Vector, accepted: list[tuple[NewsItem, Vector]]) -> Decimal:
        return max((similarity(vector, other) for _, other in accepted), default=Decimal(0))

    async def _persist(self, accepted: list[tuple[NewsItem, Vector]]) -> None:
        if not accepted:
            return
        await self._store.add(tuple(item for item, _ in accepted))
        await self._vectors.add(
            tuple(
                StoredDocument(
                    doc_id=item.item_id,
                    text=item.text,
                    metadata={"source_id": item.source_id, "url": item.url},
                    observed_at=item.observed_at,
                )
                for item, _ in accepted
            ),
            vectors=tuple(vector for _, vector in accepted),
        )

    def _now(self) -> datetime:
        return self._clock.now()
