"""The caching layer exists to not get us banned — and to be incapable of hiding staleness.

Two properties carry the weight:

* a closed bar cannot change, so one venue call per bar interval is correct *and* sufficient;
* a cached series keeps its original `observed_at`, so `require_fresh` ages it exactly as if it
  had just been fetched. A cache that stopped refreshing must produce `DATA_STALE`, never a
  fresh-looking stale price.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.core.clock import ManualClock
from tradebot.core.enums import AssetClass
from tradebot.core.errors import DataStaleError, VenueError
from tradebot.core.instrument import Instrument
from tradebot.core.market import Candle, CandleSeries, Quote
from tradebot.interfaces.market_data import DataCapabilities
from tradebot.marketdata.cache import CachingMarketData

START = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


class CountingProvider:
    """Counts calls, so a cache hit is observable rather than inferred."""

    provider_id = "counting"

    def __init__(self, clock: ManualClock) -> None:
        self._clock = clock
        self.candle_calls = 0
        self.quote_calls = 0
        self.error: Exception | None = None

    async def get_candles(
        self,
        instrument: Instrument,
        timeframe: str,
        limit: int,
        end: datetime | None = None,
    ) -> CandleSeries:
        self.candle_calls += 1
        if self.error is not None:
            raise self.error
        observed = end or self._clock.now()
        return CandleSeries(
            instrument_key=instrument.key,
            timeframe=timeframe,
            candles=(
                Candle(
                    open_time=START - HOUR,
                    close_time=START,
                    open=Decimal(100),
                    high=Decimal(101),
                    low=Decimal(99),
                    close=Decimal(100),
                    volume=Decimal(10),
                ),
            ),
            observed_at=observed,
        )

    async def get_quote(self, instrument: Instrument) -> Quote:
        self.quote_calls += 1
        return Quote(
            instrument_key=instrument.key,
            bid=Decimal(100),
            ask=Decimal(101),
            last=Decimal("100.5"),
            observed_at=self._clock.now(),
        )

    def capabilities(self) -> DataCapabilities:
        return DataCapabilities(timeframes=("1h",), max_history=500)


@pytest.fixture
def binance_instrument() -> Instrument:
    return Instrument(
        symbol="BTC/USDT",
        venue="binance",
        asset_class=AssetClass.CRYPTO,
        base_currency="BTC",
        quote_currency="USDT",
        lot_size=Decimal("0.00001"),
        tick_size=Decimal("0.01"),
    )


@pytest.fixture
def inner(clock: ManualClock) -> CountingProvider:
    return CountingProvider(clock)


@pytest.fixture
def cache(inner: CountingProvider, clock: ManualClock) -> CachingMarketData:
    return CachingMarketData(inner, clock)


class TestCandleCaching:
    async def test_repeat_reads_cost_one_venue_call(
        self, cache: CachingMarketData, inner: CountingProvider, binance_instrument: Instrument
    ) -> None:
        for _ in range(5):
            await cache.get_candles(binance_instrument, "1h", 100)
        assert inner.candle_calls == 1
        assert cache.hits == 4

    async def test_the_entry_expires_when_the_next_bar_could_have_closed(
        self,
        cache: CachingMarketData,
        inner: CountingProvider,
        binance_instrument: Instrument,
        clock: ManualClock,
    ) -> None:
        await cache.get_candles(binance_instrument, "1h", 100)
        clock.advance(HOUR.total_seconds() - 1)
        await cache.get_candles(binance_instrument, "1h", 100)
        assert inner.candle_calls == 1
        clock.advance(2)
        await cache.get_candles(binance_instrument, "1h", 100)
        assert inner.candle_calls == 2

    async def test_different_timeframes_are_separate_entries(
        self, inner: CountingProvider, clock: ManualClock, binance_instrument: Instrument
    ) -> None:
        cache = CachingMarketData(inner, clock)
        await cache.get_candles(binance_instrument, "1h", 100)
        await cache.get_candles(binance_instrument, "4h", 100)
        assert inner.candle_calls == 2

    async def test_a_point_in_time_request_is_keyed_by_its_cutoff(
        self, cache: CachingMarketData, inner: CountingProvider, binance_instrument: Instrument
    ) -> None:
        """Two replayed moments are two different questions and must not share an answer."""
        await cache.get_candles(binance_instrument, "1h", 100, end=START)
        await cache.get_candles(binance_instrument, "1h", 100, end=START - HOUR)
        await cache.get_candles(binance_instrument, "1h", 100, end=START)
        assert inner.candle_calls == 2

    async def test_a_live_read_and_a_point_in_time_read_do_not_share_an_entry(
        self, cache: CachingMarketData, inner: CountingProvider, binance_instrument: Instrument
    ) -> None:
        await cache.get_candles(binance_instrument, "1h", 100)
        await cache.get_candles(binance_instrument, "1h", 100, end=START)
        assert inner.candle_calls == 2

    async def test_a_failed_fetch_is_not_cached(
        self, cache: CachingMarketData, inner: CountingProvider, binance_instrument: Instrument
    ) -> None:
        """Caching an error would make one blip look like a dead feed for a whole bar."""
        inner.error = VenueError("down")
        with pytest.raises(VenueError):
            await cache.get_candles(binance_instrument, "1h", 100)
        inner.error = None
        await cache.get_candles(binance_instrument, "1h", 100)
        assert inner.candle_calls == 2


class TestStalenessIsNotHidden:
    async def test_a_cached_series_keeps_its_original_observed_at(
        self, cache: CachingMarketData, clock: ManualClock, binance_instrument: Instrument
    ) -> None:
        """A hit must not look freshly fetched, or the cache would launder a stale price."""
        series = await cache.get_candles(binance_instrument, "1h", 100)
        clock.advance(timedelta(minutes=30).total_seconds())
        cached = await cache.get_candles(binance_instrument, "1h", 100)
        assert cached.observed_at == series.observed_at
        assert cached.fetch_age(clock.now()) == timedelta(minutes=30)
        with pytest.raises(DataStaleError):
            cached.require_fresh(clock.now(), timedelta(minutes=5))

    async def test_a_feed_stuck_on_an_old_bar_still_trips_staleness(
        self, cache: CachingMarketData, clock: ManualClock, binance_instrument: Instrument
    ) -> None:
        """Content age, not fetch age, is what catches a provider that stopped advancing."""
        clock.advance(timedelta(days=1).total_seconds())
        series = await cache.get_candles(binance_instrument, "1h", 100)
        assert series.observed_at == clock.now()  # the fetch is current
        with pytest.raises(DataStaleError, match="content is"):
            series.require_fresh(clock.now(), HOUR)  # the data is not


class TestQuoteCaching:
    async def test_quotes_are_cached_for_their_ttl(
        self, inner: CountingProvider, clock: ManualClock, binance_instrument: Instrument
    ) -> None:
        cache = CachingMarketData(inner, clock, quote_ttl=timedelta(seconds=5))
        await cache.get_quote(binance_instrument)
        clock.advance(4)
        await cache.get_quote(binance_instrument)
        assert inner.quote_calls == 1
        clock.advance(2)
        await cache.get_quote(binance_instrument)
        assert inner.quote_calls == 2


class TestSingleFlight:
    async def test_concurrent_readers_share_one_venue_call(
        self, cache: CachingMarketData, inner: CountingProvider, binance_instrument: Instrument
    ) -> None:
        """Several baskets cycling at once must not each pay for the same bars."""
        await asyncio.gather(*(cache.get_candles(binance_instrument, "1h", 100) for _ in range(6)))
        assert inner.candle_calls == 1


class TestEviction:
    async def test_entries_are_bounded(
        self, inner: CountingProvider, clock: ManualClock, binance_instrument: Instrument
    ) -> None:
        cache = CachingMarketData(inner, clock, max_entries=3)
        for hours in range(10):
            await cache.get_candles(binance_instrument, "1h", 100, end=START - HOUR * hours)
        # The oldest cutoffs were evicted, so re-reading one is a miss.
        before = inner.candle_calls
        await cache.get_candles(binance_instrument, "1h", 100, end=START)
        assert inner.candle_calls == before + 1

    async def test_invalidate_drops_everything(
        self, cache: CachingMarketData, inner: CountingProvider, binance_instrument: Instrument
    ) -> None:
        """After a connectivity gap nothing cached is trustworthy (DESIGN [L10])."""
        await cache.get_candles(binance_instrument, "1h", 100)
        await cache.get_quote(binance_instrument)
        cache.invalidate()
        await cache.get_candles(binance_instrument, "1h", 100)
        await cache.get_quote(binance_instrument)
        assert (inner.candle_calls, inner.quote_calls) == (2, 2)


def test_capabilities_and_id_pass_through(inner: CountingProvider, clock: ManualClock) -> None:
    cache = CachingMarketData(inner, clock)
    assert cache.provider_id == "counting"
    assert cache.capabilities().timeframes == ("1h",)
