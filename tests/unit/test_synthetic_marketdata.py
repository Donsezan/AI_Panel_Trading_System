"""The simulated venue's price feed.

Two defects it exists to make impossible, both of which shipped as
`DataStaleError: no replay series for sim:XRP/USDT 1h`:

* **The universe is not fixed at wiring.** The dashboard publishes baskets while the process
  runs and the supervisor's resync sweep picks them up, so a feed that only answers for the
  instruments configured at start-up leaves the new basket unable to cycle and its chart pane
  showing a stack trace. The same applies to a timeframe the basket editor offers.
* **A series may not age out.** Bars sit on the venue's grid, so the newest closed bar is at most
  one interval old however long the process has been up. Anchoring the series at start-up instead
  put every cycle of a `serve --mode sim` run past `require_fresh` about an hour in.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.core.clock import ManualClock
from tradebot.core.enums import AssetClass
from tradebot.core.errors import DataStaleError
from tradebot.core.instrument import Instrument
from tradebot.core.market import TIMEFRAME_INTERVALS, timeframe_interval
from tradebot.dashboard.routes.configure import TIMEFRAMES as EDITABLE_TIMEFRAMES
from tradebot.marketdata.synthetic import MAX_BARS, QUOTE_TIMEFRAME, SyntheticMarketData

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def instrument(symbol: str) -> Instrument:
    base, quote = symbol.split("/")
    return Instrument(
        symbol=symbol,
        venue="sim",
        asset_class=AssetClass.CRYPTO,
        base_currency=base,
        quote_currency=quote,
        lot_size=Decimal("0.001"),
        tick_size=Decimal("0.01"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("10"),
    )


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOW)


@pytest.fixture
def provider(clock: ManualClock) -> SyntheticMarketData:
    return SyntheticMarketData(clock)


class TestItAnswersForAnything:
    """The reported defect: a basket published after wiring has no prices."""

    async def test_an_instrument_the_process_never_saw_is_served(
        self, provider: SyntheticMarketData
    ) -> None:
        series = await provider.get_candles(instrument("XRP/USDT"), "1h", 200)
        assert len(series) == 200

    @pytest.mark.parametrize("timeframe", EDITABLE_TIMEFRAMES)
    async def test_every_timeframe_the_editor_offers_is_served(
        self, provider: SyntheticMarketData, timeframe: str
    ) -> None:
        """The basket form offers 15m, which the fixed map this replaces never generated."""
        series = await provider.get_candles(instrument("LTC/USDT"), timeframe, 100)
        assert len(series) == 100

    async def test_a_timeframe_the_engine_does_not_know_fails_closed(
        self, provider: SyntheticMarketData
    ) -> None:
        with pytest.raises(DataStaleError):
            await provider.get_candles(instrument("BTC/USDT"), "7y", 10)

    async def test_capabilities_declare_every_known_timeframe(
        self, provider: SyntheticMarketData
    ) -> None:
        capabilities = provider.capabilities()
        assert set(capabilities.timeframes) == set(TIMEFRAME_INTERVALS)
        assert capabilities.supports_point_in_time


class TestItNeverGoesStale:
    """The second defect: a feed anchored at wiring stops every cycle an interval later."""

    @pytest.mark.parametrize("timeframe", EDITABLE_TIMEFRAMES)
    async def test_the_newest_bar_is_within_one_interval_however_long_the_process_runs(
        self, provider: SyntheticMarketData, clock: ManualClock, timeframe: str
    ) -> None:
        interval = timeframe_interval(timeframe)
        await provider.get_candles(instrument("BTC/USDT"), timeframe, 100)
        for _ in range(5):
            clock.advance(interval.total_seconds() * 3 + 1)
            series = await provider.get_candles(instrument("BTC/USDT"), timeframe, 100)
            assert series.age(clock.now()) < interval

    async def test_bars_sit_on_the_venues_grid(self, provider: SyntheticMarketData) -> None:
        """A 1d bar closes at midnight UTC and a 4h bar on the four-hour boundary, like a
        venue's."""
        for timeframe in ("15m", "1h", "4h", "1d"):
            series = await provider.get_candles(instrument("BTC/USDT"), timeframe, 5)
            interval = timeframe_interval(timeframe)
            for candle in series.candles:
                assert (candle.open_time - datetime(1970, 1, 1, tzinfo=UTC)) % interval == (
                    timedelta()
                )

    async def test_a_clock_jumped_further_than_the_window_still_serves_a_full_series(
        self, provider: SyntheticMarketData, clock: ManualClock
    ) -> None:
        """A process resumed after a long sleep re-opens its window rather than filling the gap."""
        await provider.get_candles(instrument("BTC/USDT"), "1h", 100)
        clock.advance(timedelta(days=90).total_seconds())
        series = await provider.get_candles(instrument("BTC/USDT"), "1h", MAX_BARS)
        assert len(series) == MAX_BARS
        assert series.age(clock.now()) < timedelta(hours=1)
        assert not series.gaps, "the window a cycle reads must be one continuous walk"


class TestABarIsFinal:
    async def test_history_is_not_redrawn_as_time_advances(
        self, provider: SyntheticMarketData, clock: ManualClock
    ) -> None:
        """A pane refreshed a minute later must show the walk the panel deliberated on."""
        before = await provider.get_candles(instrument("BTC/USDT"), "1h", 50)
        clock.advance(timedelta(hours=6).total_seconds())
        after = await provider.get_candles(instrument("BTC/USDT"), "1h", 200)
        overlap = {candle.open_time: candle for candle in after.candles}
        assert all(overlap[candle.open_time] == candle for candle in before.candles)

    async def test_two_providers_on_the_same_clock_agree(self, clock: ManualClock) -> None:
        """Seeded from the instrument key, never from `hash()`, which is randomized per run."""
        one = await SyntheticMarketData(clock).get_candles(instrument("XRP/USDT"), "1h", 100)
        other = await SyntheticMarketData(clock).get_candles(instrument("XRP/USDT"), "1h", 100)
        assert one == other

    async def test_different_instruments_walk_differently(
        self, provider: SyntheticMarketData
    ) -> None:
        btc = await provider.get_candles(instrument("BTC/USDT"), "1h", 100)
        xrp = await provider.get_candles(instrument("XRP/USDT"), "1h", 100)
        assert [c.close for c in btc.candles] != [c.close for c in xrp.candles]


class TestQuotes:
    async def test_a_quote_straddles_the_last_close_of_the_charted_timeframe(
        self, provider: SyntheticMarketData
    ) -> None:
        quote = await provider.get_quote(instrument("LTC/USDT"))
        series = await provider.get_candles(instrument("LTC/USDT"), QUOTE_TIMEFRAME, 1)
        assert quote.last == series.latest.close
        assert quote.bid < quote.last < quote.ask

    async def test_a_quote_is_observed_now_not_at_the_bars_close(
        self, provider: SyntheticMarketData, clock: ManualClock
    ) -> None:
        """It is what the mark is aged against, and the tolerance is minutes, not bars."""
        clock.advance(timedelta(minutes=31).total_seconds())
        quote = await provider.get_quote(instrument("BTC/USDT"))
        assert quote.observed_at == clock.now()


class TestItStatesItsLimits:
    async def test_nothing_exists_before_this_venue_started_publishing(
        self, clock: ManualClock
    ) -> None:
        provider = SyntheticMarketData(clock, inception=NOW - timedelta(hours=10))
        with pytest.raises(DataStaleError):
            await provider.get_candles(
                instrument("BTC/USDT"), "1h", 10, end=NOW - timedelta(days=1)
            )

    async def test_history_starts_at_inception_rather_than_being_invented_backwards(
        self, clock: ManualClock
    ) -> None:
        provider = SyntheticMarketData(clock, inception=NOW - timedelta(hours=10))
        series = await provider.get_candles(instrument("BTC/USDT"), "1h", 200)
        assert len(series) == 10
