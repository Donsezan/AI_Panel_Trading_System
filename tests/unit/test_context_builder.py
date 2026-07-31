"""The ContextBuilder's Phase 3 responsibilities: news, coverage, and config validation.

The builder is where the system fails closed on data, so its refusals matter as much as its
output: an unknown indicator, a timeframe nobody serves, and a feed slower than the cycle are all
configuration defects that must surface at wiring time rather than as a cycle that quietly
computes less than it was asked to.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.control.context_builder import ContextBuilder
from tradebot.core.clock import ManualClock
from tradebot.core.config import Basket, Schedule
from tradebot.core.errors import ConfigError
from tradebot.core.instrument import Instrument
from tradebot.core.market import timeframe_interval
from tradebot.core.snapshot import NewsCoverage, NewsItemView
from tradebot.indicators.library import DEFAULT_INDICATORS, required_history
from tradebot.interfaces.market_data import DataCapabilities
from tradebot.ledger.portfolio import Ledger
from tradebot.marketdata.replay import ReplayMarketData, synthetic_candles

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


class ScriptedFeed:
    """A `NewsFeed` that returns a fixed packet and records that it was asked."""

    def __init__(
        self,
        items: tuple[NewsItemView, ...] = (),
        coverage: NewsCoverage | None = None,
    ) -> None:
        self.items = items
        self.coverage = coverage or NewsCoverage()
        self.calls: list[tuple[int, datetime]] = []

    async def snapshot_news(
        self, instruments: tuple[Instrument, ...], as_of: datetime, limit: int
    ) -> tuple[tuple[NewsItemView, ...], NewsCoverage]:
        self.calls.append((limit, as_of))
        return self.items, self.coverage


class DelayedProvider:
    """Wraps a provider and claims a publication delay, to exercise the cadence check."""

    provider_id = "delayed"

    def __init__(self, inner: ReplayMarketData, delay: timedelta) -> None:
        self._inner = inner
        self._delay = delay

    async def get_candles(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        return await self._inner.get_candles(*args, **kwargs)  # type: ignore[arg-type]

    async def get_quote(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        return await self._inner.get_quote(*args, **kwargs)  # type: ignore[arg-type]

    def capabilities(self) -> DataCapabilities:
        return DataCapabilities(timeframes=("1h", "4h", "1d"), max_history=500, delay=self._delay)


@pytest.fixture
def market_data(instrument: Instrument, clock: ManualClock) -> ReplayMarketData:
    """Series ending at *now*, so the staleness policy is exercised rather than tripped.

    The shared fixture is anchored to the start of the year, which is exactly what the replay
    provider's own tests want and exactly what a snapshot build cannot use.
    """
    bars = 260
    return ReplayMarketData(
        {
            (instrument.key, timeframe): synthetic_candles(
                start=NOW - timeframe_interval(timeframe) * bars,
                timeframe=timeframe,
                count=bars,
                open_price=Decimal("50000"),
                step=Decimal("25"),
            )
            for timeframe in ("1h", "4h", "1d")
        },
        clock,
    )


def news_view(title: str, relevance: str = "0.9") -> NewsItemView:
    return NewsItemView(
        source="rss",
        title=title,
        summary="",
        published_at=NOW,
        observed_at=NOW,
        relevance=Decimal(relevance),
    )


class TestConfigValidation:
    def test_an_unknown_indicator_refuses_to_wire(
        self, market_data: ReplayMarketData, ledger: Ledger, clock: ManualClock
    ) -> None:
        with pytest.raises(ConfigError, match="unknown indicator"):
            ContextBuilder(market_data, ledger, clock, indicators=("RSI", "STOCHASTIC"))

    def test_an_unknown_timeframe_refuses_to_wire(
        self, market_data: ReplayMarketData, ledger: Ledger, clock: ManualClock
    ) -> None:
        with pytest.raises(Exception, match="unsupported timeframe"):
            ContextBuilder(market_data, ledger, clock, timeframes=("1h", "7m"))

    def test_empty_config_falls_back_to_the_engine_defaults(
        self, market_data: ReplayMarketData, ledger: Ledger, clock: ManualClock
    ) -> None:
        builder = ContextBuilder(market_data, ledger, clock)
        assert builder._indicators == DEFAULT_INDICATORS

    async def test_history_covers_the_longest_indicator_window(
        self, market_data: ReplayMarketData, ledger: Ledger, clock: ManualClock, basket: Basket
    ) -> None:
        """A fetch one bar short of an indicator's period aborts every cycle as DATA_STALE."""
        builder = ContextBuilder(market_data, ledger, clock, indicators=("SMA200",))
        assert builder._history >= required_history(("SMA200",))

    async def test_a_feed_slower_than_the_cycle_is_refused(
        self, market_data: ReplayMarketData, ledger: Ledger, clock: ManualClock, basket: Basket
    ) -> None:
        """A 15-minute-delayed feed cannot back a 5-minute cycle (DESIGN §6.2)."""
        provider = DelayedProvider(market_data, timedelta(minutes=15))
        fast = basket.model_copy(update={"schedule": Schedule(every_seconds=300)})
        with pytest.raises(ConfigError, match="publishes with a"):
            await ContextBuilder(provider, ledger, clock).build(fast)

    async def test_a_feed_faster_than_the_cycle_is_fine(
        self, market_data: ReplayMarketData, ledger: Ledger, clock: ManualClock, basket: Basket
    ) -> None:
        provider = DelayedProvider(market_data, timedelta(minutes=1))
        snapshot = await ContextBuilder(provider, ledger, clock).build(basket)
        assert snapshot.instruments


class TestNewsWiring:
    async def test_the_snapshot_carries_the_feeds_items_and_coverage(
        self, market_data: ReplayMarketData, ledger: Ledger, clock: ManualClock, basket: Basket
    ) -> None:
        feed = ScriptedFeed(
            (news_view("Bitcoin rallies"),),
            NewsCoverage(sources_ok=("a",), sources_failed=("b",)),
        )
        snapshot = await ContextBuilder(market_data, ledger, clock, news_feed=feed).build(basket)
        assert [item.title for item in snapshot.news] == ["Bitcoin rallies"]
        assert snapshot.news_coverage.sources_failed == ("b",)

    async def test_the_feed_is_asked_as_of_the_snapshot_moment(
        self, market_data: ReplayMarketData, ledger: Ledger, clock: ManualClock, basket: Basket
    ) -> None:
        """Point-in-time discipline: the cutoff is the snapshot's own `as_of`."""
        feed = ScriptedFeed()
        snapshot = await ContextBuilder(market_data, ledger, clock, news_feed=feed).build(basket)
        assert feed.calls[0][1] == snapshot.as_of

    async def test_no_feed_means_no_news_and_a_stated_absence(
        self, market_data: ReplayMarketData, ledger: Ledger, clock: ManualClock, basket: Basket
    ) -> None:
        snapshot = await ContextBuilder(market_data, ledger, clock).build(basket)
        assert snapshot.news == ()
        assert "no news sources are configured" in snapshot.news_coverage.summary

    async def test_an_explicit_packet_overrides_the_feed(
        self, market_data: ReplayMarketData, ledger: Ledger, clock: ManualClock, basket: Basket
    ) -> None:
        """Tests and replays must be able to pin exactly what the panel saw."""
        feed = ScriptedFeed((news_view("from the feed"),))
        builder = ContextBuilder(market_data, ledger, clock, news_feed=feed)
        snapshot = await builder.build(basket, news=(news_view("pinned"),))
        assert [item.title for item in snapshot.news] == ["pinned"]
        assert feed.calls == []

    async def test_the_item_limit_is_passed_through(
        self, market_data: ReplayMarketData, ledger: Ledger, clock: ManualClock, basket: Basket
    ) -> None:
        feed = ScriptedFeed()
        await ContextBuilder(market_data, ledger, clock, news_feed=feed, news_items=3).build(basket)
        assert feed.calls[0][0] == 3


class TestSnapshotShape:
    async def test_every_configured_indicator_produces_a_reading(
        self, market_data: ReplayMarketData, ledger: Ledger, clock: ManualClock, basket: Basket
    ) -> None:
        builder = ContextBuilder(
            market_data, ledger, clock, timeframes=("1h",), indicators=("RSI", "ATR")
        )
        snapshot = await builder.build(basket)
        names = {reading.name for reading in snapshot.instruments[0].indicators}
        assert names == {"RSI", "ATR"}

    async def test_companion_readings_are_included(
        self, market_data: ReplayMarketData, ledger: Ledger, clock: ManualClock, basket: Basket
    ) -> None:
        builder = ContextBuilder(
            market_data, ledger, clock, timeframes=("1h",), indicators=("MACD",)
        )
        snapshot = await builder.build(basket)
        names = {reading.name for reading in snapshot.instruments[0].indicators}
        assert names == {"MACD", "MACD_SIGNAL", "MACD_HIST_PCT"}

    async def test_readings_are_computed_per_timeframe(
        self, market_data: ReplayMarketData, ledger: Ledger, clock: ManualClock, basket: Basket
    ) -> None:
        builder = ContextBuilder(
            market_data, ledger, clock, timeframes=("1h", "4h"), indicators=("RSI",)
        )
        snapshot = await builder.build(basket)
        assert {r.timeframe for r in snapshot.instruments[0].indicators} == {"1h", "4h"}
