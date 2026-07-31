"""The venue-agnostic provider: point-in-time discipline, gaps, and venue matching.

These are the rules that must hold identically for every venue, which is why they are tested
against a fake gateway rather than any particular wire format.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from tests.doubles import FakeGateway, symbol_entry

from tradebot.core.clock import ManualClock
from tradebot.core.enums import AssetClass
from tradebot.core.errors import ConfigError, DataStaleError
from tradebot.core.instrument import Instrument
from tradebot.core.market import Candle
from tradebot.interfaces.exchange import TopOfBook
from tradebot.marketdata.binance import parse_market
from tradebot.marketdata.venue import VenueMarketData, instrument_for

START = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


def bars(count: int, *, start: datetime = START, step: timedelta = HOUR) -> list[Candle]:
    return [
        Candle(
            open_time=start + step * index,
            close_time=start + step * (index + 1),
            open=Decimal(100 + index),
            high=Decimal(101 + index),
            low=Decimal(99 + index),
            close=Decimal(100 + index),
            volume=Decimal(10),
        )
        for index in range(count)
    ]


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


def provider(gateway: FakeGateway, clock: ManualClock) -> VenueMarketData:
    return VenueMarketData(gateway, clock)


class TestPointInTime:
    async def test_the_clock_is_the_cutoff_when_end_is_omitted(
        self, clock: ManualClock, binance_instrument: Instrument
    ) -> None:
        clock.set(START + HOUR * 3)
        gateway = FakeGateway(bars(10))
        series = await provider(gateway, clock).get_candles(binance_instrument, "1h", 100)
        assert len(series) == 3
        assert series.latest.close_time == START + HOUR * 3

    async def test_a_bar_that_has_not_closed_is_not_visible(
        self, clock: ManualClock, binance_instrument: Instrument
    ) -> None:
        """The look-ahead guard: a forming bar's close is not a fact yet (DESIGN [L12])."""
        clock.set(START + HOUR * 3 + timedelta(minutes=30))
        series = await provider(FakeGateway(bars(10)), clock).get_candles(
            binance_instrument, "1h", 100
        )
        assert series.latest.close_time == START + HOUR * 3

    async def test_observed_at_is_the_cutoff_not_the_wire_time(
        self, clock: ManualClock, binance_instrument: Instrument
    ) -> None:
        """Stamping the cutoff is what lets a live series and a replayed one match exactly."""
        cutoff = START + HOUR * 5
        series = await provider(FakeGateway(bars(10)), clock).get_candles(
            binance_instrument, "1h", 100, end=cutoff
        )
        assert series.observed_at == cutoff

    async def test_no_visible_bars_fails_closed(
        self, clock: ManualClock, binance_instrument: Instrument
    ) -> None:
        with pytest.raises(DataStaleError, match="no 1h candles closed"):
            await provider(FakeGateway(bars(10)), clock).get_candles(
                binance_instrument, "1h", 100, end=START - HOUR
            )

    async def test_limit_takes_the_most_recent_bars(
        self, clock: ManualClock, binance_instrument: Instrument
    ) -> None:
        clock.set(START + HOUR * 10)
        series = await provider(FakeGateway(bars(10)), clock).get_candles(
            binance_instrument, "1h", 3
        )
        assert len(series) == 3
        assert series.latest.close_time == START + HOUR * 10

    async def test_a_naive_end_is_rejected(
        self, clock: ManualClock, binance_instrument: Instrument
    ) -> None:
        with pytest.raises(ConfigError, match="naive datetime"):
            await provider(FakeGateway(bars(3)), clock).get_candles(
                binance_instrument,
                "1h",
                10,
                end=datetime(2026, 3, 1, 12, 0),
            )


class TestGaps:
    async def test_a_missing_bar_stays_a_hole(
        self, clock: ManualClock, binance_instrument: Instrument
    ) -> None:
        """Interpolating a halt would feed a fabricated indicator value into a real order."""
        contiguous = bars(3)
        after_gap = bars(2, start=START + HOUR * 6)
        clock.set(START + HOUR * 20)
        series = await provider(FakeGateway(contiguous + after_gap), clock).get_candles(
            binance_instrument, "1h", 100
        )
        assert len(series) == 5
        assert series.gaps == ((START + HOUR * 3, START + HOUR * 6),)

    async def test_a_contiguous_series_reports_no_gaps(
        self, clock: ManualClock, binance_instrument: Instrument
    ) -> None:
        clock.set(START + HOUR * 20)
        series = await provider(FakeGateway(bars(5)), clock).get_candles(
            binance_instrument, "1h", 100
        )
        assert series.gaps == ()


class TestVenueMatching:
    async def test_an_instrument_from_another_venue_is_refused(
        self, clock: ManualClock, binance_instrument: Instrument
    ) -> None:
        """Pricing an Alpaca ticker off Binance's book would be silently, expensively wrong."""
        foreign = binance_instrument.model_copy(update={"venue": "alpaca"})
        with pytest.raises(ConfigError, match="belongs to venue 'alpaca'"):
            await provider(FakeGateway(bars(3)), clock).get_candles(foreign, "1h", 10)

    async def test_a_quote_from_another_venue_is_refused(
        self, clock: ManualClock, binance_instrument: Instrument
    ) -> None:
        foreign = binance_instrument.model_copy(update={"venue": "alpaca"})
        with pytest.raises(ConfigError, match="belongs to venue"):
            await provider(FakeGateway(bars(3)), clock).get_quote(foreign)

    async def test_an_unsupported_timeframe_is_refused(
        self, clock: ManualClock, binance_instrument: Instrument
    ) -> None:
        gateway = FakeGateway(bars(3), timeframes=("1h", "1d"))
        with pytest.raises(ConfigError, match="does not serve '4h'"):
            await provider(gateway, clock).get_candles(binance_instrument, "4h", 10)

    async def test_a_non_positive_limit_is_refused(
        self, clock: ManualClock, binance_instrument: Instrument
    ) -> None:
        with pytest.raises(ConfigError, match="limit must be positive"):
            await provider(FakeGateway(bars(3)), clock).get_candles(binance_instrument, "1h", 0)

    async def test_the_limit_is_clamped_to_the_providers_history_depth(
        self, clock: ManualClock, binance_instrument: Instrument
    ) -> None:
        gateway = FakeGateway(bars(5), max_history=2)
        clock.set(START + HOUR * 20)
        await provider(gateway, clock).get_candles(binance_instrument, "1h", 500)
        assert gateway.requests[0][2] == 2


class TestQuote:
    async def test_the_instrument_key_is_stamped_by_the_provider(
        self, clock: ManualClock, binance_instrument: Instrument
    ) -> None:
        """The gateway does not know how keys are composed; exactly one place does."""
        book = TopOfBook(
            bid=Decimal("100"), ask=Decimal("101"), last=Decimal("100.5"), observed_at=START
        )
        quote = await provider(FakeGateway(bars(1), book=book), clock).get_quote(binance_instrument)
        assert quote.instrument_key == "binance:BTC/USDT"
        assert quote.spread == Decimal(1)


class TestInstrumentResolution:
    async def test_precision_comes_from_the_venue(self, clock: ManualClock) -> None:
        """A hand-copied lot size drifts the day the venue changes it."""
        gateway = FakeGateway(bars(1), markets=[parse_market(symbol_entry())])
        (instrument,) = await provider(gateway, clock).instruments("BTC/USDT")
        assert instrument.key == "binance:BTC/USDT"
        assert instrument.lot_size == Decimal("0.00001")
        assert instrument.min_notional == Decimal("5.00000000")
        assert instrument.asset_class is AssetClass.CRYPTO

    async def test_an_unlisted_symbol_is_a_config_error(self, clock: ManualClock) -> None:
        gateway = FakeGateway(bars(1), markets=[parse_market(symbol_entry())])
        with pytest.raises(ConfigError, match="does not list DOGE/USDT"):
            await provider(gateway, clock).instruments("DOGE/USDT")

    def test_an_untradable_market_cannot_become_an_instrument(self) -> None:
        market = parse_market(symbol_entry(status="BREAK"))
        with pytest.raises(ConfigError, match="not tradable"):
            instrument_for(market, "binance", AssetClass.CRYPTO)


class TestLifecycle:
    async def test_provider_id_and_capabilities_come_from_the_gateway(
        self, clock: ManualClock
    ) -> None:
        gateway = FakeGateway(bars(1), venue_id="binance", max_history=750)
        assert provider(gateway, clock).provider_id == "binance"
        assert provider(gateway, clock).capabilities().max_history == 750

    async def test_closing_propagates_to_the_gateway(self, clock: ManualClock) -> None:
        gateway = FakeGateway(bars(1))
        await provider(gateway, clock).close()
        assert gateway.closed
