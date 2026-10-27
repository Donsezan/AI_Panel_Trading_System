# decision_lab/newsfeed.py
"""`ArchiveNewsFeed` — a `NewsFeed` that never fetches (spec §6.8).

It holds no fetcher, no HTTP client and no transport, and §16.2 asserts that structurally: it
cannot reach the network even by accident, because it owns no object that could. The archives are
loaded once at construction and indexed in memory. A replay's archive is immutable for the length
of a pass, so re-reading per cycle would buy nothing — and the one thing it could buy is a file
handle on a path.

**Only the fetch half is replaced.** Relevance, the minimum-relevance floor, the ordering and the
truncation are the bot's and are imported rather than restated (§2.4): `KeywordRelevanceFilter`,
`DEFAULT_MIN_RELEVANCE`, `DEFAULT_SNAPSHOT_ITEMS`, and `NewsHub.select`'s exact sort key. A replay
that ranked news differently from production would be measuring a different pipeline — ADR 0020's
argument, one door along.

**Silence and absence are different, and the coverage line is where that is said.** A cycle inside
the archive's span whose window holds nothing was a genuinely quiet two days, and reporting that as
a failure would teach the panel to discount real calm. A cycle *outside* the span was never
measured, and that source is reported failed, so `NewsCoverage.summary` renders "no coverage from
<source>" into the prompt. `_render_news` already states the reason this matters: a seat told
nothing happened will reason as though nothing happened.

**A documented deviation from §6.8.3.** That section says the seat shown 8 of 65 items must be told
it is 8 of 65, and that the existing `NewsCoverage` line carries it. It cannot: `NewsCoverage` has
two fields and `summary` counts *sources*, not items. Adding a field is a second `tradebot` change
and this design licenses exactly one (§2.2). So the cut is logged and reconstructible here, and
§6.8.2's `reports=N` carries the clustering half into the prompt; the "8 of 65" figure reaching the
seat is an open item needing either a `NewsCoverage` field or a §6.8.3 correction.

Failure semantics: `snapshot_news` always returns — items and a coverage report — and raises
nothing. There is no source to be down. A window with no rows is a coverage fact, never an
exception, which is the behaviour `NewsHub` already has and the panel already handles.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from decision_lab.archive.config import ArchivePolicy
from decision_lab.archive.publish import archive_path
from decision_lab.archive.published import ArchiveHeader, ArchiveItem, PublishedArchive
from decision_lab.clustering import annotate, fold
from tradebot.core.errors import ConfigError
from tradebot.core.instrument import Instrument
from tradebot.core.logging import get_logger
from tradebot.core.schema import Money
from tradebot.core.snapshot import NewsCoverage, NewsItemView
from tradebot.interfaces.news import NewsItem, RelevanceFilter
from tradebot.news.hub import DEFAULT_MIN_RELEVANCE, DEFAULT_SNAPSHOT_ITEMS
from tradebot.news.relevance import KeywordRelevanceFilter

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _Indexed:
    """One archive, with its rows in `observed_at` order and that order extracted for bisect."""

    source: str
    items: tuple[ArchiveItem, ...]
    stamps: tuple[datetime, ...]
    digest: str

    @classmethod
    def of(cls, archive: PublishedArchive) -> _Indexed:
        rows = tuple(sorted(archive.items, key=lambda i: (i.observed_at, i.url)))
        return cls(
            source=archive.header.source,
            items=rows,
            stamps=tuple(i.observed_at for i in rows),
            digest=archive.header.archive_digest,
        )

    def covers(self, as_of: datetime) -> bool:
        """Whether this archive was *measured* at `as_of`. Empty covers nothing."""
        return bool(self.stamps) and self.stamps[0] <= as_of <= self.stamps[-1]

    def window(self, as_of: datetime, lookback: timedelta) -> tuple[ArchiveItem, ...]:
        """`observed_at` in `(as_of - lookback, as_of]`, inclusive at the instant.

        Inclusive because that is the bot's own boundary (`NewsStore.select`). Exclusive would
        silently drop whatever landed exactly on a cycle boundary, which on an 8h grid is a
        recurring class of item rather than a curiosity.
        """
        end = bisect_right(self.stamps, as_of)
        start = bisect_right(self.stamps, as_of - lookback)
        return self.items[start:end]


class ArchiveNewsFeed:
    """The §6.8 feed. Satisfies `NewsFeed`; reaches nothing."""

    def __init__(
        self,
        archives: Sequence[PublishedArchive],
        relevance: RelevanceFilter,
        *,
        window: timedelta,
        floor: Money,
        min_relevance: Money = DEFAULT_MIN_RELEVANCE,
    ) -> None:
        self._archives = tuple(_Indexed.of(a) for a in archives)
        self._headers = tuple(a.header for a in archives)
        self._relevance = relevance
        self._window = window
        self._floor = floor
        self._min_relevance = min_relevance

    @property
    def headers(self) -> tuple[ArchiveHeader, ...]:
        """Each archive's provenance line, in the order given. A corpus copies these into its
        meta so `report` - a separate process that reads only that file - can state them."""
        return self._headers

    @property
    def digest(self) -> str:
        """Every source's `archive_digest`, joined in source order. Task 10 threads this into
        `corpus_identity`, so adding a source is a different corpus rather than a quiet change of
        evidence under a stable id."""
        return "+".join(a.digest for a in sorted(self._archives, key=lambda a: a.source))

    def covers(self, as_of: datetime) -> bool:
        return any(a.covers(as_of) for a in self._archives)

    def in_window(self, as_of: datetime) -> tuple[ArchiveItem, ...]:
        """The point-in-time read, before relevance and before the limit. Public because §16.2
        asserts on it directly, and because the drill-down reconstructs a cycle's evidence from
        exactly this set."""
        return tuple(row for a in self._archives for row in a.window(as_of, self._window))

    async def snapshot_news(
        self,
        instruments: tuple[Instrument, ...],
        as_of: datetime,
        limit: int = DEFAULT_SNAPSHOT_ITEMS,
    ) -> tuple[tuple[NewsItemView, ...], NewsCoverage]:
        """What was knowable at `as_of`, ranked and cut by the bot's own rules."""
        candidates = self.in_window(as_of)
        # Forward-only and never precomputed (§6.8.2): `candidates` is already cut at `as_of`, so
        # a count cannot encode coverage the event attracted afterwards.
        elected = tuple(annotate(c) for c in fold(candidates, floor=self._floor))
        scored = [
            (self._relevance.relevance(_as_news_item(row), instruments), row) for row in elected
        ]
        keep = [pair for pair in scored if pair[0] >= self._min_relevance]
        # `NewsHub.select`'s exact key, reversed. Imported behaviour would be better still; this
        # is the one line of it that is a literal rather than a call, and it is pinned by test.
        keep.sort(key=lambda pair: (pair[0], pair[1].observed_at, pair[1].url_hash), reverse=True)
        views = tuple(_as_news_item(row).view(score) for score, row in keep[:limit])

        coverage = NewsCoverage(
            sources_ok=tuple(a.source for a in self._archives if a.covers(as_of)),
            sources_failed=tuple(a.source for a in self._archives if not a.covers(as_of)),
        )
        logger.debug(
            "archive news selected",
            extra={
                "as_of": as_of.isoformat(),
                "in_window": len(candidates),
                "clusters": len(elected),
                "relevant": len(keep),
                "served": len(views),
            },
        )
        return views, coverage


def _as_news_item(row: ArchiveItem) -> NewsItem:
    """The archive row as the bot's own model, so the bot's own relevance filter scores it.

    `item_id` is the url hash, exactly as `NewsHub._normalize` sets it — an item's identity is
    recomputable from its URL alone, and a second identity scheme would make the drill-down's
    reconstruction a different set from the one the panel read.
    """
    return NewsItem(
        item_id=row.url_hash,
        source_id=row.source_id,
        url=row.url,
        url_hash=row.url_hash,
        title=row.title,
        excerpt=row.summary,
        published_at=row.published_at,
        observed_at=row.observed_at,
    )


def load_feed(data_dir: Path, sources: tuple[str, ...], policy: ArchivePolicy) -> ArchiveNewsFeed:
    """Read each source's published archive and build one feed over all of them.

    The window comes from `policy`, not from each archive's header: a feed reading two archives
    must read them through one window, and a header's own value is the provenance of how it was
    published. Task 10 refuses a mismatch rather than picking one.
    """
    archives = tuple(PublishedArchive.read(archive_path(data_dir, source)) for source in sources)
    mismatched = [
        a.header.source
        for a in archives
        if a.header.news_window_seconds != policy.news_window_seconds
        or a.header.cluster_floor != policy.cluster_floor
    ]
    if mismatched:
        raise ConfigError(
            f"{mismatched} were published under a different window or floor than the policy "
            "in force declares. Re-publish them, or point --policy at the archive.toml they "
            "were published with: the window and the floor are inside archive_digest, so "
            "reading an archive through a policy it was not published for yields a corpus id "
            "describing evidence nobody selected"
        )
    return ArchiveNewsFeed(
        archives, KeywordRelevanceFilter(), window=policy.window, floor=policy.cluster_floor
    )
