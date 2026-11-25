# decision_lab/tests/test_newsfeed.py
"""`ArchiveNewsFeed`: what was knowable at `as_of`, ranked by the bot's own filter.

The look-ahead test here is the one §16.2 calls the most important in the table, and it is run
again in `test_contamination.py` through the real `ContextBuilder`. Both are wanted: this one
fails fast and names the field, that one proves the real path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from decision_lab.archive.config import ArchivePolicy
from decision_lab.archive.fields import EventType, Modality
from decision_lab.archive.publish import archive_path
from decision_lab.archive.published import ArchiveHeader, ArchiveItem, PublishedArchive
from decision_lab.newsfeed import ArchiveNewsFeed, load_feed
from tradebot.core.enums import AssetClass
from tradebot.core.errors import ConfigError
from tradebot.core.instrument import Instrument
from tradebot.news.relevance import KeywordRelevanceFilter

FLOOR = Decimal("0.25")
BTC = Instrument(
    symbol="BTC/USDT",
    venue="binance",
    asset_class=AssetClass.CRYPTO,
    base_currency="BTC",
    quote_currency="USDT",
    lot_size=Decimal("0.00001"),
    tick_size=Decimal("0.01"),
)


def item(
    *,
    at: datetime,
    title: str,
    summary: str,
    url: str | None = None,
    entities: tuple[str, ...] = ("Kraken",),
    event_type: EventType = EventType.REGULATORY,
) -> ArchiveItem:
    link = url or f"https://x.test/{abs(hash(title)) % 10_000}"
    return ArchiveItem(
        source_id="cryptoslate",
        url=link,
        url_hash=f"h{abs(hash(link)) % 10_000}",
        title=title,
        summary=summary,
        event_type=event_type,
        entities=entities,
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
    return ArchiveNewsFeed((archive(*items),), KeywordRelevanceFilter(), window=window, floor=FLOOR)


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
async def test_an_instant_after_the_archives_last_row_reports_no_coverage() -> None:
    """Deliberate, and pinned so it is not "fixed" by accident: the header declares no span, so
    coverage is [first row, last row] and an instant past the last row was never measured. A seat
    told a source is silent after its archive ended would reason as though nothing happened."""
    only = item(
        at=datetime(2025, 3, 4, tzinfo=UTC), title="Kraken licensed", summary="Kraken was licensed."
    )

    _, coverage = await feed(only).snapshot_news((BTC,), datetime(2025, 3, 5, tzinfo=UTC), 8)

    assert coverage.sources_failed == ("cryptoslate",)
    assert coverage.sources_ok == ()


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
            # Distinct events: one shared key would fold all twelve into a single slot.
            entities=(f"Outlet{n}",),
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
        (archive(btc_item), second),
        KeywordRelevanceFilter(),
        window=timedelta(hours=48),
        floor=FLOOR,
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


def _published(tmp_path: Path, *, window_seconds: int = 172_800) -> Path:
    """Write one archive under `tmp_path` the way `archive publish` does."""
    one = archive(item(at=datetime(2026, 9, 1, tzinfo=UTC), title="Kraken settles", summary="s"))
    header = one.header.model_copy(update={"news_window_seconds": window_seconds})
    PublishedArchive.write(archive_path(tmp_path, "cryptoslate"), header, one.items)
    return tmp_path


def _policy() -> ArchivePolicy:
    return ArchivePolicy(provider_id="stub", model="stub-summary", cluster_floor=FLOOR)


def test_load_feed_reads_an_archive_published_under_the_policy_in_force(tmp_path: Path) -> None:
    loaded = load_feed(_published(tmp_path), ("cryptoslate",), _policy())

    assert loaded.digest
    assert [h.source for h in loaded.headers] == ["cryptoslate"]


def test_load_feed_refuses_an_archive_published_under_another_window(tmp_path: Path) -> None:
    """The window is inside `archive_digest`: reading through another one yields a corpus id
    describing evidence nobody selected."""
    data = _published(tmp_path, window_seconds=3_600)

    with pytest.raises(ConfigError, match="different window or floor"):
        load_feed(data, ("cryptoslate",), _policy())


# ------------------------------------------------ commentary is a record, never evidence (§6.1)

NOON = datetime(2025, 3, 4, 12, tzinfo=UTC)


def _commentary_pair() -> tuple[ArchiveItem, ArchiveItem]:
    real = item(at=NOON - timedelta(hours=2), title="Bitcoin ETF approved", summary="Approved.")
    comment = item(
        at=NOON - timedelta(hours=1),
        title="Bitcoin to 200k says analyst",
        summary="An analyst predicts a rise.",
        event_type=EventType.MARKET_COMMENTARY,
    )
    return real, comment


@pytest.mark.asyncio
async def test_commentary_is_never_served() -> None:
    real, comment = _commentary_pair()
    views, _ = await feed(real, comment).snapshot_news((BTC,), NOON)
    assert [v.title for v in views] == ["Bitcoin ETF approved"]


def test_commentary_is_not_in_the_drill_down_window() -> None:
    real, comment = _commentary_pair()
    assert feed(real, comment).in_window(NOON) == (real,)


@pytest.mark.asyncio
async def test_a_window_of_only_commentary_inside_the_span_reports_ok() -> None:
    """Silence of the evidence is not silence of the source: the span still covers the cycle."""
    early = item(at=NOON - timedelta(days=5), title="Old", summary="Old news.")
    _, comment = _commentary_pair()
    late = item(at=NOON + timedelta(days=1), title="Later", summary="Later news.")
    views, coverage = await feed(early, comment, late).snapshot_news((BTC,), NOON)
    assert views == () and coverage.sources_ok == ("cryptoslate",)
    assert coverage.sources_failed == ()


def test_items_keeps_commentary() -> None:
    real, comment = _commentary_pair()
    assert set(feed(real, comment).items) == {real, comment}
