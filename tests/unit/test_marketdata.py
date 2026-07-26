"""Replay provider: point-in-time discipline.

The look-ahead test is load-bearing (PLAN §7). A backtest that can see a bar which had not
closed yet is not merely optimistic — it is measuring a strategy that could not have existed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

import pytest

from tradebot.core.clock import ManualClock
from tradebot.core.errors import DataStaleError
from tradebot.core.instrument import Instrument
from tradebot.marketdata.replay import ReplayMarketData, synthetic_candles

SERIES_START = datetime(2026, 1, 1, tzinfo=UTC)


class TestPointInTime:
    async def test_only_closed_bars_are_visible(
        self, market_data: ReplayMarketData, instrument: Instrument
    ) -> None:
        cutoff = SERIES_START + timedelta(hours=10)
        series = await market_data.get_candles(instrument, "1h", limit=100, end=cutoff)
        assert all(candle.close_time <= cutoff for candle in series.candles)
        assert len(series) == 10

    async def test_a_bar_that_has_not_closed_is_not_visible(
        self, market_data: ReplayMarketData, instrument: Instrument
    ) -> None:
        """The look-ahead test: mid-bar, the forming bar must not appear (DESIGN [L12])."""
        mid_bar = SERIES_START + timedelta(hours=10, minutes=30)
        series = await market_data.get_candles(instrument, "1h", limit=100, end=mid_bar)
        assert series.latest.close_time == SERIES_START + timedelta(hours=10)

    async def test_the_clock_is_the_cutoff_when_end_is_omitted(
        self, market_data: ReplayMarketData, instrument: Instrument, clock: ManualClock
    ) -> None:
        clock.set(SERIES_START + timedelta(hours=5))
        series = await market_data.get_candles(instrument, "1h", limit=100)
        assert len(series) == 5

    async def test_no_data_before_the_series_starts_fails_closed(
        self, market_data: ReplayMarketData, instrument: Instrument
    ) -> None:
        with pytest.raises(DataStaleError, match="no 1h candles closed"):
            await market_data.get_candles(
                instrument, "1h", limit=10, end=SERIES_START - timedelta(days=1)
            )

    async def test_limit_takes_the_most_recent_bars(
        self, market_data: ReplayMarketData, instrument: Instrument
    ) -> None:
        cutoff = SERIES_START + timedelta(hours=50)
        series = await market_data.get_candles(instrument, "1h", limit=5, end=cutoff)
        assert len(series) == 5
        assert series.latest.close_time == cutoff


class TestLookups:
    async def test_unknown_instrument_fails_closed(
        self, market_data: ReplayMarketData, instrument: Instrument
    ) -> None:
        other = instrument.model_copy(update={"symbol": "DOGE/USDT"})
        with pytest.raises(DataStaleError, match="no replay series"):
            await market_data.get_candles(other, "1h", limit=10)

    async def test_unknown_timeframe_fails_closed(
        self, market_data: ReplayMarketData, instrument: Instrument
    ) -> None:
        with pytest.raises(DataStaleError, match="no replay series"):
            await market_data.get_candles(instrument, "30m", limit=10)

    def test_capabilities_report_what_is_loaded(self, market_data: ReplayMarketData) -> None:
        capabilities = market_data.capabilities()
        assert capabilities.timeframes == ("1d", "1h", "4h")
        assert capabilities.supports_point_in_time


class TestQuote:
    async def test_quote_derives_from_the_last_closed_bar(
        self, market_data: ReplayMarketData, instrument: Instrument
    ) -> None:
        quote = await market_data.get_quote(instrument)
        assert quote.bid < quote.last < quote.ask
        assert quote.spread > 0

    async def test_quote_respects_the_same_cutoff_as_candles(
        self, market_data: ReplayMarketData, instrument: Instrument, clock: ManualClock
    ) -> None:
        clock.set(SERIES_START + timedelta(hours=3))
        series = await market_data.get_candles(instrument, "1h", limit=1)
        quote = await market_data.get_quote(instrument)
        assert quote.last == series.latest.close


class TestSyntheticSeries:
    def test_generation_is_deterministic(self) -> None:
        kwargs = {
            "start": SERIES_START,
            "timeframe": "1h",
            "count": 20,
            "open_price": Decimal("100"),
        }
        assert synthetic_candles(**kwargs) == synthetic_candles(**kwargs)  # type: ignore[arg-type]

    def test_a_different_seed_gives_a_different_series(self) -> None:
        base = synthetic_candles(
            start=SERIES_START, timeframe="1h", count=20, open_price=Decimal("100"), seed=1
        )
        other = synthetic_candles(
            start=SERIES_START, timeframe="1h", count=20, open_price=Decimal("100"), seed=2
        )
        assert base != other

    def test_prices_stay_exact_decimals(self) -> None:
        """No float anywhere in the walk, so replayed prices are reproducible bit for bit."""
        for candle in synthetic_candles(
            start=SERIES_START, timeframe="1h", count=50, open_price=Decimal("100")
        ):
            assert candle.close == candle.close.quantize(Decimal("0.1"))

    def test_bars_are_contiguous_and_ordered(self) -> None:
        candles = synthetic_candles(
            start=SERIES_START, timeframe="4h", count=10, open_price=Decimal("100")
        )
        for previous, current in pairwise(candles):
            assert previous.close_time == current.open_time

    def test_unsupported_timeframe_is_rejected(self) -> None:
        with pytest.raises(DataStaleError, match="unsupported timeframe"):
            synthetic_candles(
                start=SERIES_START, timeframe="7m", count=5, open_price=Decimal("100")
            )


class TestCsvLoading:
    def test_round_trips_a_recorded_series(self, tmp_path: Path, clock: ManualClock) -> None:
        path = tmp_path / "sim__BTC_USDT__1h.csv"
        path.write_text(
            "open_time,close_time,open,high,low,close,volume\n"
            "2026-01-01T00:00:00+00:00,2026-01-01T01:00:00+00:00,100,110,90,105,12\n"
            "2026-01-01T01:00:00+00:00,2026-01-01T02:00:00+00:00,105,115,95,110,13\n",
            encoding="utf-8",
        )
        provider = ReplayMarketData.from_directory(tmp_path, clock)
        assert provider.capabilities().max_history == 2
