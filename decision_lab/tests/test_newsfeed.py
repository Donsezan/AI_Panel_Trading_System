# decision_lab/tests/test_newsfeed.py
"""`ArchiveNewsFeed`: what was knowable at `as_of`, ranked by the bot's own filter.

The look-ahead test here is the one §16.2 calls the most important in the table, and it is run
again in `test_contamination.py` through the real `ContextBuilder`. Both are wanted: this one
fails fast and names the field, that one proves the real path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from decision_lab.archive.fields import EventType, Modality
from decision_lab.archive.published import ArchiveHeader, ArchiveItem, PublishedArchive
from decision_lab.newsfeed import ArchiveNewsFeed
from tradebot.core.enums import AssetClass
from tradebot.core.instrument import Instrument
from tradebot.news.relevance import KeywordRelevanceFilter

BTC = Instrument(
    symbol="BTC/USDT",
    venue="binance",
    asset_class=AssetClass.CRYPTO,
    base_currency="BTC",
    quote_currency="USDT",
    lot_size=Decimal("0.00001"),
    tick_size=Decimal("0.01"),
)


def item(*, at: datetime, title: str, summary: str, url: str | None = None) -> ArchiveItem:
    link = url or f"https://x.test/{abs(hash(title)) % 10_000}"
    return ArchiveItem(
        source_id="cryptoslate",
        url=link,
        url_hash=f"h{abs(hash(link)) % 10_000}",
        title=title,
        summary=summary,
        event_type=EventType.REGULATORY,
        entities=("Kraken",),
        assets=("BTC",),
        magnitudes=(),
        modality=Modality.CONFIRMED,
        published_at=at - timedelta(minutes=12),
        observed_at=at,
    )


def archive(*items: ArchiveItem) -> PublishedArchive:
    header = ArchiveHeader(
        source="cryptoslate",
        capture="sitemap",
        captured_at=datetime(2026, 10, 5, tzinfo=UTC),
        observed_at_policy="published_at+00:12:00",
        summarizer_provider="stub",
        summarizer_model="stub-summary",
        prompt_digest="abc",
        news_window_seconds=172_800,
        cluster_floor=Decimal("0.25"),
        rows=len(items),
    )
    return PublishedArchive(header=header, items=tuple(sorted(items, key=lambda i: i.observed_at)))


def feed(*items: ArchiveItem, window: timedelta = timedelta(hours=48)) -> ArchiveNewsFeed:
    return ArchiveNewsFeed((archive(*items),), KeywordRelevanceFilter(), window=window)


@pytest.mark.asyncio
async def test_an_item_observed_after_the_instant_is_not_served() -> None:
    """§2.2, and §16.2's third test. The exact look-ahead the seam exists to prevent, reached
    from inside the archive rather than from a live feed."""
    before = item(
        at=datetime(2025, 3, 4, 8, tzinfo=UTC),
        title="Bitcoin ETF approved",
        summary="The SEC approved a bitcoin ETF.",
    )
    after = item(
        at=datetime(2025, 3, 4, 12, tzinfo=UTC),
        title="Bitcoin crashes",
        summary="Bitcoin fell sharply.",
    )

    views, _ = await feed(before, after).snapshot_news(
        (BTC,), datetime(2025, 3, 4, 10, tzinfo=UTC), 8
    )

    titles = [v.title for v in views]
    assert "Bitcoin ETF approved" in titles
    assert "Bitcoin crashes" not in titles


@pytest.mark.asyncio
async def test_an_item_at_exactly_the_instant_is_served() -> None:
    """`observed_at <= as_of`, the bot's own boundary. Exclusive would silently drop whatever
    landed on a cycle boundary, which on an 8h grid is a recurring class of item."""
    at = datetime(2025, 3, 4, 10, tzinfo=UTC)
    views, _ = await feed(
        item(at=at, title="Bitcoin ETF approved", summary="The SEC approved a bitcoin ETF.")
    ).snapshot_news((BTC,), at, 8)
    assert len(views) == 1


@pytest.mark.asyncio
async def test_an_item_older_than_the_window_is_not_served() -> None:
    """§6.8.1's 48 hours, matching `NewsStore.DEFAULT_LOOKBACK`. A replay measuring a panel the
    bot does not run measures the wrong panel."""
    old = item(
        at=datetime(2025, 3, 1, tzinfo=UTC),
        title="Bitcoin ETF approved",
        summary="The SEC approved a bitcoin ETF.",
    )
    views, _ = await feed(old).snapshot_news((BTC,), datetime(2025, 3, 4, 10, tzinfo=UTC), 8)
    assert views == ()


@pytest.mark.asyncio
async def test_a_quiet_window_inside_the_archives_span_reports_the_source_as_ok() -> None:
    """Review Focus 3, the half that must *not* alarm. Two quiet days are a fact about the news,
    and reporting them as a coverage failure would teach the panel to discount real calm."""
    early = item(
        at=datetime(2025, 3, 1, tzinfo=UTC), title="Kraken licensed", summary="Kraken was licensed."
    )
    late = item(
        at=datetime(2025, 3, 20, tzinfo=UTC), title="Kraken expands", summary="Kraken expanded."
    )

    views, coverage = await feed(early, late).snapshot_news(
        (BTC,), datetime(2025, 3, 10, tzinfo=UTC), 8
    )

    assert views == ()
    assert coverage.sources_ok == ("cryptoslate",)
    assert coverage.is_complete


@pytest.mark.asyncio
async def test_an_instant_outside_the_archives_span_reports_no_coverage() -> None:
    """Review Focus 3, the half that must alarm: a 2024 corpus pointed at the 2025 archive, or a
    cycle before the first row. Never measured is not the same as nothing happened, and a seat
    told nothing happened will reason as though nothing happened."""
    only = item(
        at=datetime(2025, 3, 4, tzinfo=UTC), title="Kraken licensed", summary="Kraken was licensed."
    )

    _, coverage = await feed(only).snapshot_news((BTC,), datetime(2024, 3, 4, tzinfo=UTC), 8)

    assert coverage.sources_failed == ("cryptoslate",)
    assert not coverage.is_complete
    assert "no coverage from cryptoslate" in coverage.summary


@pytest.mark.asyncio
async def test_an_irrelevant_item_is_filtered_by_the_bots_own_floor() -> None:
    """Relevance is the bot's and is imported, never restated. An item naming no instrument and
    no macro term scores below `DEFAULT_MIN_RELEVANCE` and spends no tokens diluting evidence."""
    noise = item(
        at=datetime(2025, 3, 4, 9, tzinfo=UTC),
        title="Local bakery wins award",
        summary="A bakery in Leeds won a prize.",
    )
    signal = item(
        at=datetime(2025, 3, 4, 9, tzinfo=UTC),
        title="Bitcoin ETF approved",
        summary="The SEC approved a bitcoin ETF.",
    )

    views, _ = await feed(noise, signal).snapshot_news(
        (BTC,), datetime(2025, 3, 4, 10, tzinfo=UTC), 8
    )

    assert [v.title for v in views] == ["Bitcoin ETF approved"]


@pytest.mark.asyncio
async def test_the_limit_truncates_most_relevant_first() -> None:
    items = tuple(
        item(
            at=datetime(2025, 3, 4, 9, n, tzinfo=UTC),
            title=f"Bitcoin story {n}",
            summary=f"Bitcoin news {n}.",
        )
        for n in range(12)
    )
    views, _ = await feed(*items).snapshot_news((BTC,), datetime(2025, 3, 4, 10, tzinfo=UTC), 8)
    assert len(views) == 8


@pytest.mark.asyncio
async def test_the_view_carries_the_summary_as_the_panels_text() -> None:
    """§6.5's output lands in `NewsItem.excerpt`, which `NewsItem.view` renders as `summary`. No
    new field and no second rendering path."""
    one = item(
        at=datetime(2025, 3, 4, 9, tzinfo=UTC),
        title="Bitcoin ETF approved",
        summary="The SEC approved a bitcoin ETF.",
    )
    views, _ = await feed(one).snapshot_news((BTC,), datetime(2025, 3, 4, 10, tzinfo=UTC), 8)
    assert views[0].summary == "The SEC approved a bitcoin ETF."


@pytest.mark.asyncio
async def test_two_sources_both_appear_in_coverage() -> None:
    """One feed over N archives. Each source answers for itself, so one store ending early is
    visible as that source's gap rather than as a thinner day."""
    btc_item = item(
        at=datetime(2025, 3, 4, 9, tzinfo=UTC),
        title="Bitcoin ETF approved",
        summary="The SEC approved a bitcoin ETF.",
    )
    other = btc_item.model_copy(
        update={"source_id": "bitcoincom", "url": "https://y.test/1", "url_hash": "hy1"}
    )
    second = PublishedArchive(
        header=archive(other).header.model_copy(update={"source": "bitcoincom"}),
        items=(other,),
    )
    multi = ArchiveNewsFeed(
        (archive(btc_item), second), KeywordRelevanceFilter(), window=timedelta(hours=48)
    )

    # At the rows' own instant: an instant past an archive's last row is outside its span.
    _, coverage = await multi.snapshot_news((BTC,), datetime(2025, 3, 4, 9, tzinfo=UTC), 8)

    assert set(coverage.sources_ok) == {"cryptoslate", "bitcoincom"}


def test_the_feed_holds_no_fetcher_and_no_http_client() -> None:
    """§16.2's first contamination test, asserted structurally: it cannot reach the network even
    by accident, because it owns no object that could."""
    built = feed(
        item(at=datetime(2025, 3, 4, tzinfo=UTC), title="Bitcoin ETF approved", summary="Approved.")
    )
    names = {type(v).__name__.lower() for v in vars(built).values()}
    assert not any("client" in n or "fetcher" in n or "transport" in n for n in names)
