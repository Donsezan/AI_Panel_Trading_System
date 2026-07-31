"""The market-data stack, assembled the way the composition root assembles it.

Wiring deserves a test of its own: a stack that is individually correct at four layers and
misassembled at the seam produces exactly the same symptoms as a bug in the venue.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from tests.doubles import FakeTransport, klines

from tradebot.core.clock import ManualClock
from tradebot.core.enums import AssetClass
from tradebot.core.instrument import Instrument
from tradebot.marketdata.binance import klines_weight
from tradebot.marketdata.cache import CachingMarketData
from tradebot.marketdata.factory import binance_spot_market_data, live_binance_spot

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


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
def transport() -> FakeTransport:
    return FakeTransport(
        {
            "klines": klines(NOW - HOUR * 5, HOUR, ["100", "101", "102", "103", "104"]),
            "ticker24h": {
                "symbol": "BTCUSDT",
                "bidPrice": "104.00",
                "askPrice": "104.10",
                "lastPrice": "104.05",
            },
        }
    )


class TestAssembledStack:
    async def test_bars_flow_from_the_wire_to_a_normalized_series(
        self, transport: FakeTransport, clock: ManualClock, binance_instrument: Instrument
    ) -> None:
        provider = binance_spot_market_data(transport, clock)
        series = await provider.get_candles(binance_instrument, "1h", 100)
        assert series.instrument_key == "binance:BTC/USDT"
        assert series.latest.close == Decimal("104")
        assert series.observed_at == clock.now()

    async def test_the_stack_is_cached(
        self, transport: FakeTransport, clock: ManualClock, binance_instrument: Instrument
    ) -> None:
        """Without the cache on top, every basket and timeframe is its own venue call."""
        provider = binance_spot_market_data(transport, clock)
        assert isinstance(provider, CachingMarketData)
        for _ in range(4):
            await provider.get_candles(binance_instrument, "1h", 100)
        assert len(transport.calls) == 1

    async def test_weight_is_charged_through_the_stack(
        self, transport: FakeTransport, clock: ManualClock, binance_instrument: Instrument
    ) -> None:
        provider = binance_spot_market_data(transport, clock)
        await provider.get_candles(binance_instrument, "1h", 100)
        assert transport.total_weight == klines_weight(100)

    async def test_quotes_flow_through_too(
        self, transport: FakeTransport, clock: ManualClock, binance_instrument: Instrument
    ) -> None:
        provider = binance_spot_market_data(transport, clock)
        quote = await provider.get_quote(binance_instrument)
        assert quote.instrument_key == "binance:BTC/USDT"
        assert quote.bid == Decimal("104.00")

    def test_the_provider_is_identified_by_its_venue(
        self, transport: FakeTransport, clock: ManualClock
    ) -> None:
        assert binance_spot_market_data(transport, clock).provider_id == "binance"


class TestLiveConstruction:
    """Builds the real ccxt client. No request is issued, so this stays offline."""

    async def test_the_live_stack_assembles_and_closes(self, clock: ManualClock) -> None:
        provider, transport = live_binance_spot(clock)
        try:
            assert provider.provider_id == "binance"
            assert provider.capabilities().supports_point_in_time
        finally:
            await transport.close()

    async def test_the_sandbox_stack_assembles(self, clock: ManualClock) -> None:
        provider, transport = live_binance_spot(clock, sandbox=True)
        try:
            assert provider.provider_id == "binance"
        finally:
            await transport.close()
