"""Recording venue history into a replay dataset (PLAN Phase 7).

Two things have to hold for a backtest over this data to mean anything: the bars must survive
the round trip through CSV exactly, and the venue trading rules the prices were recorded under
must travel with them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tradebot.core.clock import ManualClock
from tradebot.core.errors import ConfigError
from tradebot.core.instrument import Instrument
from tradebot.core.market import Candle, CandleSeries, Quote
from tradebot.interfaces.market_data import DataCapabilities
from tradebot.marketdata.recorder import MANIFEST, ReplayDataset, record
from tradebot.marketdata.replay import ReplayMarketData, synthetic_candles

SERIES_START = datetime(2026, 1, 1, tzinfo=UTC)
BARS = 200


class PagedProvider:
    """Serves at most `page` bars per call, the way every venue's kline endpoint does."""

    provider_id = "paged"

    def __init__(self, inner: ReplayMarketData, page: int) -> None:
        self._inner = inner
        self._page = page
        self.calls = 0

    async def get_candles(
        self,
        instrument: Instrument,
        timeframe: str,
        limit: int,
        end: datetime | None = None,
    ) -> CandleSeries:
        self.calls += 1
        return await self._inner.get_candles(instrument, timeframe, min(limit, self._page), end)

    async def get_quote(self, instrument: Instrument) -> Quote:
        return await self._inner.get_quote(instrument)

    def capabilities(self) -> DataCapabilities:
        return DataCapabilities(
            timeframes=("1h",), max_history=self._page, supports_point_in_time=True
        )


@pytest.fixture
def source(instrument: Instrument, clock: ManualClock) -> ReplayMarketData:
    return ReplayMarketData(
        {
            (instrument.key, "1h"): synthetic_candles(
                start=SERIES_START,
                timeframe="1h",
                count=BARS,
                open_price=Decimal("50000"),
                step=Decimal("25"),
            )
        },
        clock,
    )


async def record_into(
    directory: Path,
    provider: PagedProvider,
    instrument: Instrument,
    clock: ManualClock,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> ReplayDataset:
    return await record(
        provider,
        (instrument,),
        ("1h",),
        start=start or SERIES_START,
        end=end or SERIES_START + timedelta(hours=BARS),
        directory=directory,
        clock=clock,
        source="test venue",
    )


class TestRecording:
    async def test_history_is_paged_until_the_window_is_covered(
        self, tmp_path: Path, source: ReplayMarketData, instrument: Instrument, clock: ManualClock
    ) -> None:
        provider = PagedProvider(source, page=50)

        dataset = await record_into(tmp_path, provider, instrument, clock)

        assert provider.calls >= BARS // 50
        assert len(dataset.market_data.keys) == 1
        start, end = dataset.coverage
        assert (start, end) == (SERIES_START, SERIES_START + timedelta(hours=BARS))

    async def test_bars_survive_the_round_trip_exactly(
        self, tmp_path: Path, source: ReplayMarketData, instrument: Instrument, clock: ManualClock
    ) -> None:
        """Prices are decimals on both sides; a float anywhere here would show up as a mismatch."""
        dataset = await record_into(tmp_path, PagedProvider(source, page=50), instrument, clock)

        original = await source.get_candles(instrument, "1h", BARS)
        replayed = await dataset.market_data.get_candles(instrument, "1h", BARS)
        assert replayed.candles == original.candles

    async def test_the_manifest_carries_the_venue_trading_rules(
        self, tmp_path: Path, source: ReplayMarketData, instrument: Instrument, clock: ManualClock
    ) -> None:
        """Quantization must match the venue as it was, not as it is today."""
        dataset = await record_into(tmp_path, PagedProvider(source, page=50), instrument, clock)

        (recorded,) = dataset.instruments
        assert recorded == instrument
        assert (tmp_path / MANIFEST).is_file()
        assert dataset.timeframes == ("1h",)

    async def test_a_window_with_no_bars_refuses_rather_than_writing_an_empty_dataset(
        self, tmp_path: Path, source: ReplayMarketData, instrument: Instrument, clock: ManualClock
    ) -> None:
        """An empty dataset would abort every cycle as DATA_STALE and read as our fault."""
        with pytest.raises(ConfigError, match="served no bars"):
            await record_into(
                tmp_path,
                PagedProvider(source, page=50),
                instrument,
                clock,
                start=SERIES_START - timedelta(days=30),
                end=SERIES_START - timedelta(days=20),
            )

    async def test_an_inverted_window_refuses(
        self, tmp_path: Path, source: ReplayMarketData, instrument: Instrument, clock: ManualClock
    ) -> None:
        with pytest.raises(ConfigError, match="empty window"):
            await record_into(
                tmp_path,
                PagedProvider(source, page=50),
                instrument,
                clock,
                start=SERIES_START + timedelta(hours=10),
                end=SERIES_START,
            )


class TestDataset:
    async def test_a_directory_without_a_manifest_is_not_a_dataset(
        self, tmp_path: Path, clock: ManualClock
    ) -> None:
        with pytest.raises(ConfigError, match=MANIFEST):
            ReplayDataset.load(tmp_path, clock)

    async def test_a_requested_window_is_clamped_into_the_data(
        self, tmp_path: Path, source: ReplayMarketData, instrument: Instrument, clock: ManualClock
    ) -> None:
        dataset = await record_into(tmp_path, PagedProvider(source, page=50), instrument, clock)

        start, end = dataset.window(SERIES_START - timedelta(days=365), None)
        assert (start, end) == dataset.coverage

    async def test_a_window_outside_the_data_refuses(
        self, tmp_path: Path, source: ReplayMarketData, instrument: Instrument, clock: ManualClock
    ) -> None:
        dataset = await record_into(tmp_path, PagedProvider(source, page=50), instrument, clock)

        with pytest.raises(ConfigError, match="no overlap"):
            dataset.window(SERIES_START + timedelta(days=400), SERIES_START + timedelta(days=500))

    async def test_coverage_is_the_intersection_across_series(self, clock: ManualClock) -> None:
        """A backtest may only run where *every* instrument has prices."""
        short = ReplayMarketData(
            {
                ("sim:BTC/USDT", "1h"): synthetic_candles(
                    start=SERIES_START, timeframe="1h", count=100, open_price=Decimal("50000")
                ),
                ("sim:ETH/USDT", "1h"): synthetic_candles(
                    start=SERIES_START + timedelta(hours=10),
                    timeframe="1h",
                    count=100,
                    open_price=Decimal("3000"),
                ),
            },
            clock,
        )

        span = short.coverage()
        assert span == (
            SERIES_START + timedelta(hours=10),
            SERIES_START + timedelta(hours=100),
        )


class TestSessions:
    async def test_a_session_label_survives_the_round_trip(self, tmp_path: Path) -> None:
        """An equity series whose extended bars arrived unlabelled would skew every indicator."""
        from tradebot.core.enums import MarketSession
        from tradebot.marketdata.recorder import _path_for, _write_csv

        instrument = Instrument(
            symbol="AAPL",
            venue="alpaca",
            asset_class="equity",
            base_currency="AAPL",
            quote_currency="USD",
            lot_size=Decimal(1),
            tick_size=Decimal("0.01"),
            min_qty=Decimal(1),
            min_notional=Decimal(1),
        )
        candle = Candle(
            open_time=SERIES_START,
            close_time=SERIES_START + timedelta(hours=1),
            open=Decimal(100),
            high=Decimal(101),
            low=Decimal(99),
            close=Decimal("100.5"),
            volume=Decimal(1000),
            session=MarketSession.EXTENDED,
        )
        _write_csv(_path_for(tmp_path, instrument, "1h"), (candle,))

        loaded = ReplayMarketData.from_directory(
            tmp_path, ManualClock(SERIES_START + timedelta(days=1))
        )
        series = await loaded.get_candles(instrument, "1h", 1)
        assert series.latest.session is MarketSession.EXTENDED
        assert series.indicator_window().candles == ()
