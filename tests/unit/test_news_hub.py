"""Storage, dedup, coverage and point-in-time selection.

The load-bearing test here is `test_news_observed_after_the_cutoff_cannot_leak_in` — PLAN §7's
look-ahead test. A replayed cycle at time T given news that reached us at T+1h must produce a
snapshot containing none of it, or every backtest and paper result is measuring a system that
could not have existed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine

from tradebot.core.clock import ManualClock
from tradebot.core.enums import AssetClass
from tradebot.core.errors import ConfigError, VenueError
from tradebot.core.instrument import Instrument
from tradebot.interfaces.news import NewsItem, RawNewsItem
from tradebot.interfaces.vectorstore import StoredDocument
from tradebot.news.hub import NewsHub
from tradebot.news.normalize import url_hash
from tradebot.news.relevance import KeywordRelevanceFilter
from tradebot.news.store import NewsStore
from tradebot.news.vectorstore import SqliteVectorStore
from tradebot.persistence.database import SingleWriter

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)

BTC = Instrument(
    symbol="BTC/USDT",
    venue="binance",
    asset_class=AssetClass.CRYPTO,
    base_currency="BTC",
    quote_currency="USDT",
    lot_size=Decimal("0.001"),
    tick_size=Decimal("0.01"),
)


def stored(title: str, url: str, *, observed_at: datetime, source: str = "rss") -> NewsItem:
    digest = url_hash(url)
    return NewsItem(
        item_id=digest,
        source_id=source,
        url=url,
        url_hash=digest,
        title=title,
        excerpt="",
        published_at=observed_at,
        observed_at=observed_at,
    )


def raw(title: str, url: str, *, observed_at: datetime, source: str = "rss") -> RawNewsItem:
    return RawNewsItem(
        source_id=source,
        title=title,
        body="",
        url=url,
        published_at=observed_at,
        observed_at=observed_at,
    )


class ScriptedSource:
    """A `NewsSource` that returns what a test tells it to, or fails how a test tells it to."""

    def __init__(
        self, source_id: str, items: tuple[RawNewsItem, ...] = (), error: Exception | None = None
    ) -> None:
        self.source_id = source_id
        self.items = items
        self.error = error
        self.fetches = 0

    async def fetch_latest(self) -> tuple[RawNewsItem, ...]:
        self.fetches += 1
        if self.error is not None:
            raise self.error
        return self.items


@pytest.fixture
def engine(database: tuple[Engine, SingleWriter]) -> Engine:
    return database[0]


@pytest.fixture
def writer(database: tuple[Engine, SingleWriter]) -> SingleWriter:
    return database[1]


@pytest.fixture
def news_store(engine: Engine, writer: SingleWriter) -> NewsStore:
    return NewsStore(engine, writer)


@pytest.fixture
def vectors(engine: Engine, writer: SingleWriter, clock: ManualClock) -> SqliteVectorStore:
    return SqliteVectorStore(engine, writer, clock)


def build_hub(
    sources: tuple[ScriptedSource, ...],
    news_store: NewsStore,
    vectors: SqliteVectorStore,
    clock: ManualClock,
    **kwargs: object,
) -> NewsHub:
    return NewsHub(
        sources,
        news_store,
        vectors,
        KeywordRelevanceFilter(),
        clock,
        **kwargs,  # type: ignore[arg-type]
    )


class TestNewsStore:
    async def test_items_round_trip(self, news_store: NewsStore) -> None:
        item = stored("Bitcoin rallies", "https://x/1", observed_at=NOW)
        await news_store.add((item,))
        assert news_store.select(NOW) == (item,)

    async def test_re_adding_the_same_item_does_not_duplicate_it(
        self, news_store: NewsStore
    ) -> None:
        item = stored("Bitcoin rallies", "https://x/1", observed_at=NOW)
        await news_store.add((item, item))
        assert len(news_store.select(NOW)) == 1

    async def test_known_hashes_reports_what_we_hold(self, news_store: NewsStore) -> None:
        item = stored("Bitcoin rallies", "https://x/1", observed_at=NOW)
        await news_store.add((item,))
        assert news_store.known_hashes(frozenset({item.url_hash, "other"})) == frozenset(
            {item.url_hash}
        )

    def test_asking_about_nothing_returns_nothing(self, news_store: NewsStore) -> None:
        assert news_store.known_hashes(frozenset()) == frozenset()

    async def test_items_beyond_the_lookback_are_history_not_news(
        self, engine: Engine, writer: SingleWriter
    ) -> None:
        store = NewsStore(engine, writer, lookback=timedelta(hours=6))
        await store.add(
            (
                stored("recent", "https://x/1", observed_at=NOW - timedelta(hours=1)),
                stored("ancient", "https://x/2", observed_at=NOW - timedelta(days=3)),
            )
        )
        assert [item.title for item in store.select(NOW)] == ["recent"]

    async def test_selection_is_newest_first(self, news_store: NewsStore) -> None:
        await news_store.add(
            (
                stored("older", "https://x/1", observed_at=NOW - timedelta(hours=2)),
                stored("newer", "https://x/2", observed_at=NOW - timedelta(hours=1)),
            )
        )
        assert [item.title for item in news_store.select(NOW)] == ["newer", "older"]

    async def test_adding_nothing_is_a_no_op(self, news_store: NewsStore) -> None:
        await news_store.add(())
        assert news_store.select(NOW) == ()


class TestVectorStore:
    async def test_documents_round_trip_and_rank_by_similarity(
        self, vectors: SqliteVectorStore
    ) -> None:
        await vectors.add(
            (
                StoredDocument(
                    doc_id="a", text="Bitcoin ETF approved by regulators", observed_at=NOW
                ),
                StoredDocument(
                    doc_id="b", text="Storm warnings for coastal counties", observed_at=NOW
                ),
            )
        )
        found = await vectors.query("Bitcoin ETF approved by regulators", limit=1)
        assert found[0].document.doc_id == "a"

    async def test_the_point_in_time_cutoff_is_enforced_in_the_query(
        self, vectors: SqliteVectorStore
    ) -> None:
        """The guard belongs where a caller cannot forget it."""
        await vectors.add(
            (StoredDocument(doc_id="future", text="Bitcoin ETF approved", observed_at=NOW),)
        )
        assert await vectors.query("Bitcoin ETF approved", 5, NOW - timedelta(hours=1)) == ()
        assert await vectors.query("Bitcoin ETF approved", 5, NOW) != ()

    async def test_metadata_survives_storage(self, vectors: SqliteVectorStore) -> None:
        await vectors.add(
            (
                StoredDocument(
                    doc_id="a", text="text", metadata={"source_id": "rss"}, observed_at=NOW
                ),
            )
        )
        found = await vectors.query("text", limit=1)
        assert found[0].document.metadata == {"source_id": "rss"}

    async def test_re_adding_a_doc_id_overwrites_it(self, vectors: SqliteVectorStore) -> None:
        for text in ("first version", "second version"):
            await vectors.add((StoredDocument(doc_id="a", text=text, observed_at=NOW),))
        found = await vectors.query("second version", limit=5)
        assert len(found) == 1
        assert found[0].document.text == "second version"

    async def test_empty_input_is_a_no_op(self, vectors: SqliteVectorStore) -> None:
        await vectors.add(())
        assert await vectors.query("anything", limit=5) == ()

    async def test_precomputed_vectors_are_reused(self, vectors: SqliteVectorStore) -> None:
        """The dedup pass has already embedded these; paying twice would be waste."""
        document = StoredDocument(doc_id="a", text="Bitcoin ETF approved", observed_at=NOW)
        precomputed = await vectors.embed_many((document.text,))
        await vectors.add((document,), vectors=precomputed)
        assert (await vectors.query("Bitcoin ETF approved", limit=1))[0].document.doc_id == "a"

    async def test_an_empty_query_matches_nothing(self, vectors: SqliteVectorStore) -> None:
        await vectors.add((StoredDocument(doc_id="a", text="something", observed_at=NOW),))
        assert await vectors.query("", limit=5) == ()

    async def test_a_zero_limit_returns_nothing(self, vectors: SqliteVectorStore) -> None:
        await vectors.add((StoredDocument(doc_id="a", text="something", observed_at=NOW),))
        assert await vectors.query("something", limit=0) == ()

    async def test_the_lookback_bounds_the_scan(
        self, engine: Engine, writer: SingleWriter, clock: ManualClock
    ) -> None:
        store = SqliteVectorStore(engine, writer, clock, lookback=timedelta(hours=1))
        await store.add(
            (StoredDocument(doc_id="old", text="Bitcoin ETF", observed_at=NOW - timedelta(days=2)),)
        )
        assert await store.query("Bitcoin ETF", limit=5, observed_before=NOW) == ()


class TestDeduplication:
    async def test_the_same_url_from_two_feeds_is_stored_once(
        self, news_store: NewsStore, vectors: SqliteVectorStore, clock: ManualClock
    ) -> None:
        url = "https://news.example.com/story"
        hub = build_hub(
            (
                ScriptedSource("a", (raw("SEC approves ETF", url, observed_at=NOW),)),
                ScriptedSource(
                    "b", (raw("SEC approves ETF", f"{url}?utm_source=b", observed_at=NOW),)
                ),
            ),
            news_store,
            vectors,
            clock,
        )
        report = await hub.refresh()
        assert report.stored == 1
        assert report.duplicates == 1

    async def test_an_already_stored_url_is_not_stored_again(
        self, news_store: NewsStore, vectors: SqliteVectorStore, clock: ManualClock
    ) -> None:
        source = ScriptedSource("a", (raw("SEC approves ETF", "https://x/1", observed_at=NOW),))
        hub = build_hub((source,), news_store, vectors, clock, min_interval=timedelta(0))
        assert (await hub.refresh()).stored == 1
        assert (await hub.refresh()).stored == 0

    async def test_the_same_story_under_two_urls_is_caught_by_similarity(
        self, news_store: NewsStore, vectors: SqliteVectorStore, clock: ManualClock
    ) -> None:
        """Syndication would otherwise give one story five times the weight in the evidence."""
        headline = "SEC approves spot Bitcoin ETF applications from eight issuers"
        hub = build_hub(
            (
                ScriptedSource("a", (raw(headline, "https://a.com/x", observed_at=NOW),)),
                ScriptedSource("b", (raw(f"{headline}.", "https://b.com/y", observed_at=NOW),)),
            ),
            news_store,
            vectors,
            clock,
        )
        report = await hub.refresh()
        assert report.stored == 1
        assert report.duplicates == 1

    async def test_two_different_stories_are_both_kept(
        self, news_store: NewsStore, vectors: SqliteVectorStore, clock: ManualClock
    ) -> None:
        hub = build_hub(
            (
                ScriptedSource(
                    "a",
                    (
                        raw("SEC approves spot Bitcoin ETF", "https://a.com/x", observed_at=NOW),
                        raw(
                            "Ethereum Dencun upgrade goes live", "https://a.com/y", observed_at=NOW
                        ),
                    ),
                ),
            ),
            news_store,
            vectors,
            clock,
        )
        assert (await hub.refresh()).stored == 2

    async def test_a_story_similar_to_one_already_stored_is_dropped(
        self, news_store: NewsStore, vectors: SqliteVectorStore, clock: ManualClock
    ) -> None:
        headline = "SEC approves spot Bitcoin ETF applications from eight issuers"
        first = ScriptedSource("a", (raw(headline, "https://a.com/x", observed_at=NOW),))
        hub = build_hub((first,), news_store, vectors, clock, min_interval=timedelta(0))
        await hub.refresh()
        first.items = (raw(f"{headline} today", "https://a.com/z", observed_at=NOW),)
        assert (await hub.refresh()).stored == 0


class TestCoverage:
    async def test_a_failed_source_is_reported_not_raised(
        self, news_store: NewsStore, vectors: SqliteVectorStore, clock: ManualClock
    ) -> None:
        """A dead feed must never stop a basket managing an open position (DESIGN §8.1)."""
        hub = build_hub(
            (
                ScriptedSource("good", (raw("Bitcoin rallies", "https://x/1", observed_at=NOW),)),
                ScriptedSource("bad", error=VenueError("down")),
            ),
            news_store,
            vectors,
            clock,
        )
        report = await hub.refresh()
        assert report.coverage.sources_ok == ("good",)
        assert report.coverage.sources_failed == ("bad",)
        assert not report.coverage.is_complete
        assert report.stored == 1

    async def test_a_fatal_source_error_is_still_only_a_coverage_gap(
        self, news_store: NewsStore, vectors: SqliteVectorStore, clock: ManualClock
    ) -> None:
        """A 404 feed URL is an operator problem, not a reason to stop trading."""
        hub = build_hub(
            (ScriptedSource("bad", error=ConfigError("404")),), news_store, vectors, clock
        )
        assert (await hub.refresh()).coverage.sources_failed == ("bad",)

    async def test_an_unclassified_source_defect_is_contained(
        self, news_store: NewsStore, vectors: SqliteVectorStore, clock: ManualClock
    ) -> None:
        hub = build_hub(
            (ScriptedSource("bad", error=RuntimeError("bug")),), news_store, vectors, clock
        )
        assert (await hub.refresh()).coverage.sources_failed == ("bad",)

    async def test_full_coverage_is_reported_as_complete(
        self, news_store: NewsStore, vectors: SqliteVectorStore, clock: ManualClock
    ) -> None:
        hub = build_hub((ScriptedSource("a"), ScriptedSource("b")), news_store, vectors, clock)
        coverage = (await hub.refresh()).coverage
        assert coverage.is_complete
        assert "all 2 configured sources responded" in coverage.summary

    async def test_a_source_is_not_refetched_before_its_interval(
        self, news_store: NewsStore, vectors: SqliteVectorStore, clock: ManualClock
    ) -> None:
        """Publishers notice bots that poll harder than they publish (PLAN §3.3)."""
        source = ScriptedSource("a", (raw("Bitcoin rallies", "https://x/1", observed_at=NOW),))
        hub = build_hub((source,), news_store, vectors, clock, min_interval=timedelta(minutes=5))
        await hub.refresh()
        await hub.refresh()
        assert source.fetches == 1
        clock.advance(301)
        await hub.refresh()
        assert source.fetches == 2

    async def test_a_throttled_source_is_not_a_coverage_gap(
        self, news_store: NewsStore, vectors: SqliteVectorStore, clock: ManualClock
    ) -> None:
        """We already hold its data; skipping a poll is not the same as having no coverage."""
        hub = build_hub(
            (ScriptedSource("a"),), news_store, vectors, clock, min_interval=timedelta(minutes=5)
        )
        await hub.refresh()
        assert (await hub.refresh()).coverage.sources_failed == ()


class TestSelection:
    async def test_news_observed_after_the_cutoff_cannot_leak_in(
        self, news_store: NewsStore, vectors: SqliteVectorStore, clock: ManualClock
    ) -> None:
        """The look-ahead test (PLAN §7). Tomorrow's news is not evidence for today."""
        await news_store.add(
            (
                stored("Bitcoin rallies now", "https://x/1", observed_at=NOW - timedelta(hours=1)),
                stored(
                    "Bitcoin crashes later", "https://x/2", observed_at=NOW + timedelta(hours=1)
                ),
            )
        )
        hub = build_hub((), news_store, vectors, clock)
        selected = hub.select((BTC,), NOW, limit=10)
        assert [view.title for view in selected] == ["Bitcoin rallies now"]

    async def test_a_backdated_publish_time_cannot_smuggle_an_item_in(
        self, news_store: NewsStore, vectors: SqliteVectorStore, clock: ManualClock
    ) -> None:
        """`published_at` is the publisher's claim; only `observed_at` may filter a replay."""
        backdated = stored(
            "Bitcoin rallies", "https://x/1", observed_at=NOW + timedelta(hours=2)
        ).model_copy(update={"published_at": NOW - timedelta(days=1)})
        await news_store.add((backdated,))
        assert build_hub((), news_store, vectors, clock).select((BTC,), NOW, limit=10) == ()

    async def test_items_are_ranked_by_relevance(
        self, news_store: NewsStore, vectors: SqliteVectorStore, clock: ManualClock
    ) -> None:
        await news_store.add(
            (
                stored("Crypto market mixed", "https://x/1", observed_at=NOW),
                stored("Bitcoin breaks resistance", "https://x/2", observed_at=NOW),
            )
        )
        selected = build_hub((), news_store, vectors, clock).select((BTC,), NOW, limit=10)
        assert [view.title for view in selected] == [
            "Bitcoin breaks resistance",
            "Crypto market mixed",
        ]
        assert selected[0].relevance > selected[1].relevance

    async def test_irrelevant_items_are_filtered_out(
        self, news_store: NewsStore, vectors: SqliteVectorStore, clock: ManualClock
    ) -> None:
        """Irrelevant headlines are not free: they spend tokens diluting the evidence."""
        await news_store.add((stored("Local bakery wins award", "https://x/1", observed_at=NOW),))
        assert build_hub((), news_store, vectors, clock).select((BTC,), NOW, limit=10) == ()

    async def test_the_limit_is_honoured(
        self, news_store: NewsStore, vectors: SqliteVectorStore, clock: ManualClock
    ) -> None:
        await news_store.add(
            tuple(
                stored(f"Bitcoin story {index}", f"https://x/{index}", observed_at=NOW)
                for index in range(10)
            )
        )
        assert len(build_hub((), news_store, vectors, clock).select((BTC,), NOW, limit=3)) == 3

    async def test_the_view_carries_both_timestamps_and_the_score(
        self, news_store: NewsStore, vectors: SqliteVectorStore, clock: ManualClock
    ) -> None:
        item = stored("Bitcoin rallies", "https://x/1", observed_at=NOW)
        await news_store.add((item,))
        view = build_hub((), news_store, vectors, clock).select((BTC,), NOW, limit=1)[0]
        assert view.source == "rss"
        assert view.published_at == item.published_at
        assert view.observed_at == item.observed_at
        assert view.relevance == Decimal("0.90")


class TestSnapshotSeam:
    async def test_snapshot_news_refreshes_then_selects(
        self, news_store: NewsStore, vectors: SqliteVectorStore, clock: ManualClock
    ) -> None:
        hub = build_hub(
            (ScriptedSource("a", (raw("Bitcoin rallies", "https://x/1", observed_at=NOW),)),),
            news_store,
            vectors,
            clock,
        )
        items, coverage = await hub.snapshot_news((BTC,), NOW, limit=5)
        assert [view.title for view in items] == ["Bitcoin rallies"]
        assert coverage.sources_ok == ("a",)

    async def test_no_sources_is_stated_rather_than_implied(
        self, news_store: NewsStore, vectors: SqliteVectorStore, clock: ManualClock
    ) -> None:
        _, coverage = await build_hub((), news_store, vectors, clock).snapshot_news(
            (BTC,), NOW, limit=5
        )
        assert "no news sources are configured" in coverage.summary
