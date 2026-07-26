"""Builds the frozen `ContextSnapshot` the panel decides on.

Everything the panel will see is computed here, by code, and then frozen. The builder is also
where the system fails closed on data: a series older than its budget aborts the cycle as
`DATA_STALE` rather than producing a decision from a market that has already moved (DESIGN §6.2).

Failure semantics: `DataStaleError` propagates to the runner, which records the cycle outcome
and places no order. That is the intended behaviour, not a degradation.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from tradebot.core.clock import Clock
from tradebot.core.config import Basket
from tradebot.core.ids import new_uuid
from tradebot.core.instrument import Instrument
from tradebot.core.market import CandleSeries, timeframe_interval
from tradebot.core.snapshot import (
    BasketState,
    ContextSnapshot,
    IndicatorReading,
    InstrumentContext,
    NewsItemView,
    PositionView,
)
from tradebot.indicators.library import compute_readings
from tradebot.interfaces.market_data import MarketDataProvider
from tradebot.ledger.history import HistoryReader
from tradebot.ledger.portfolio import Ledger

DEFAULT_TIMEFRAMES = ("1h", "4h", "1d")
DEFAULT_INDICATORS = ("RSI", "ATR")


def summarize(series: CandleSeries) -> str:
    """Deterministic prose summary of a series. Golden-tested — this text is prompt input."""
    highs = max(candle.high for candle in series.candles)
    lows = min(candle.low for candle in series.candles)
    latest = series.latest
    direction = "up" if latest.close >= latest.open else "down"
    return (
        f"{len(series)} bars to {latest.close_time.isoformat()}; "
        f"last close {latest.close} ({direction} on the bar); range {lows}–{highs}"
    )


class ContextBuilder:
    """Assembles market data, indicators, position and news into one frozen packet."""

    def __init__(
        self,
        market_data: MarketDataProvider,
        ledger: Ledger,
        clock: Clock,
        *,
        timeframes: tuple[str, ...] = DEFAULT_TIMEFRAMES,
        indicators: tuple[str, ...] = DEFAULT_INDICATORS,
        history: int = 200,
        staleness_tolerance: timedelta = timedelta(minutes=15),
        protective_orders_supported: bool = False,
        trading_history: HistoryReader | None = None,
    ) -> None:
        self._market_data = market_data
        self._ledger = ledger
        self._clock = clock
        self._timeframes = timeframes
        self._indicators = indicators
        self._history = history
        self._tolerance = staleness_tolerance
        self._protective = protective_orders_supported
        self._trading_history = trading_history

    async def build(
        self, basket: Basket, *, news: tuple[NewsItemView, ...] = ()
    ) -> ContextSnapshot:
        as_of = self._clock.now()
        contexts = tuple(
            [await self._build_instrument(i, basket.basket_id) for i in basket.instruments]
        )
        return ContextSnapshot(
            snapshot_id=new_uuid(),
            basket_id=basket.basket_id,
            as_of=as_of,
            instruments=contexts,
            news=news,
            basket_state=BasketState(),
        )

    async def series_for(self, instrument: Instrument, timeframe: str) -> CandleSeries:
        """Fetch and freshness-check one series. Shared by the builder and the risk inputs.

        The budget is the bar interval plus a tolerance, so a freshly closed daily bar is not
        mistaken for a dead feed while a 1h feed that stopped an hour ago still trips.
        """
        series = await self._market_data.get_candles(instrument, timeframe, self._history)
        series.require_fresh(self._clock.now(), timeframe_interval(timeframe) + self._tolerance)
        return series

    async def _build_instrument(self, instrument: Instrument, basket_id: str) -> InstrumentContext:
        summaries: list[tuple[str, str]] = []
        readings: list[IndicatorReading] = []
        for timeframe in self._timeframes:
            series = await self.series_for(instrument, timeframe)
            summaries.append((timeframe, summarize(series)))
            readings.extend(compute_readings(series, self._indicators))

        quote = await self._market_data.get_quote(instrument)
        position = self._ledger.position(instrument.key)
        return InstrumentContext(
            instrument=instrument,
            quote=quote,
            candle_summaries=tuple(summaries),
            indicators=tuple(readings),
            position=None
            if position.is_flat
            else PositionView(
                qty=position.qty,
                unrealized_pnl_pct=position.unrealized_pnl_pct(quote.last),
                held_cycles=self._held_cycles(basket_id, position.opened_at),
            ),
            unprotected_position=not self._protective,
        )

    def _held_cycles(self, basket_id: str, opened_at: datetime | None) -> int:
        if self._trading_history is None:
            return 0
        return self._trading_history.held_cycles(basket_id, opened_at)
