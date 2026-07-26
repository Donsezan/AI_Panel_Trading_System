"""Shared fixtures. Every test runs on a `ManualClock` and an in-memory database.

Nothing here touches the network, the wall clock, or the filesystem, so the suite is
deterministic and free — which is what lets it run on every commit (PLAN §7).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradebot.core.clock import ManualClock
from tradebot.core.config import Basket, PanelConfig, RiskPolicy, SeatConfig
from tradebot.core.enums import AssetClass
from tradebot.core.instrument import Instrument
from tradebot.core.market import Quote
from tradebot.core.snapshot import (
    BasketState,
    ContextSnapshot,
    IndicatorReading,
    InstrumentContext,
)
from tradebot.ledger.portfolio import Ledger
from tradebot.marketdata.replay import ReplayMarketData, synthetic_candles
from tradebot.persistence.database import SingleWriter, create_database
from tradebot.persistence.store import EventStore

START = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
SERIES_START = datetime(2026, 1, 1, tzinfo=UTC)
TIMEFRAMES = ("1h", "4h", "1d")


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(START)


@pytest.fixture
def instrument() -> Instrument:
    return Instrument(
        symbol="BTC/USDT",
        venue="sim",
        asset_class=AssetClass.CRYPTO,
        base_currency="BTC",
        quote_currency="USDT",
        lot_size=Decimal("0.00001"),
        tick_size=Decimal("0.01"),
        min_qty=Decimal("0.00001"),
        min_notional=Decimal("10"),
    )


@pytest.fixture
def seat() -> SeatConfig:
    return SeatConfig(
        seat_id="technical",
        role="Technical Analyst",
        provider_id="stub",
        model="stub-technical",
        evidence=("indicators", "news", "position"),
    )


@pytest.fixture
def panel(seat: SeatConfig) -> PanelConfig:
    return PanelConfig(panel_id="p1", seats=(seat,))


@pytest.fixture
def basket(instrument: Instrument, panel: PanelConfig) -> Basket:
    return Basket(
        basket_id="b1",
        name="test basket",
        instruments=(instrument,),
        panel=panel,
        risk_policy=RiskPolicy(),
    )


@pytest.fixture
def market_data(instrument: Instrument, clock: ManualClock) -> ReplayMarketData:
    return ReplayMarketData(
        {
            (instrument.key, timeframe): synthetic_candles(
                start=SERIES_START,
                timeframe=timeframe,
                count=200,
                open_price=Decimal("50000"),
                step=Decimal("25"),
            )
            for timeframe in TIMEFRAMES
        },
        clock,
    )


@pytest.fixture
def ledger(clock: ManualClock) -> Ledger:
    return Ledger(clock, venue="sim", balances={"USDT": Decimal(10_000)})


@pytest.fixture
def store() -> Iterator[EventStore]:
    engine = create_database(None)
    writer = SingleWriter(engine)
    yield EventStore(engine, writer)
    writer.close()


@pytest.fixture
def quote(instrument: Instrument) -> Quote:
    return Quote(
        instrument_key=instrument.key,
        bid=Decimal("49990"),
        ask=Decimal("50010"),
        last=Decimal("50000"),
        observed_at=START,
    )


@pytest.fixture
def snapshot(instrument: Instrument, quote: Quote) -> ContextSnapshot:
    return ContextSnapshot(
        snapshot_id="snap-1",
        basket_id="b1",
        as_of=START,
        instruments=(
            InstrumentContext(
                instrument=instrument,
                quote=quote,
                candle_summaries=(("1h", "200 bars; last close 50000"),),
                indicators=(
                    IndicatorReading(
                        name="RSI",
                        timeframe="1h",
                        value=Decimal("62"),
                        text="RSI(14)=62.00 — firm momentum",
                    ),
                    IndicatorReading(
                        name="ATR",
                        timeframe="1h",
                        value=Decimal("500"),
                        text="ATR(14)=500.00 — absolute volatility per unit, in quote currency",
                    ),
                ),
            ),
        ),
        basket_state=BasketState(),
    )
