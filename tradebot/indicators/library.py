"""The indicator registry. Phase 1 ships the two the loop actually depends on.

RSI is the panel's momentum evidence; ATR is the **risk layer's** stop-distance basis, which is
why the indicator engine is a dependency of risk and not only of the panel (DESIGN §6.3).

Phase 3 adds MACD, EMA/SMA, Bollinger and volume profile behind this same registry.
"""

from __future__ import annotations

from decimal import Decimal
from itertools import pairwise

from tradebot.core.errors import ConfigError
from tradebot.core.market import CandleSeries
from tradebot.core.money import ZERO, divide, multiply
from tradebot.core.snapshot import IndicatorReading
from tradebot.indicators.base import Band, Indicator

HUNDRED = Decimal(100)


def _wilder_average(values: list[Decimal], period: int) -> Decimal:
    """Wilder's smoothing: seed with the first `period` mean, then decay the rest into it."""
    average = divide(sum(values[:period], start=ZERO), Decimal(period))
    for value in values[period:]:
        average = divide(
            multiply(average, Decimal(period - 1)) + value,
            Decimal(period),
        )
    return average


class RSI(Indicator):
    """Relative Strength Index (Wilder). Bounded 0–100."""

    name = "RSI"
    bands = (
        Band(Decimal(30), "oversold territory"),
        Band(Decimal(45), "weak momentum"),
        Band(Decimal(55), "neutral"),
        Band(Decimal(70), "firm momentum"),
        Band(None, "overbought territory"),
    )

    def compute(self, series: CandleSeries) -> Decimal:
        self.require_history(series, self.period + 1)
        closes = [candle.close for candle in series.candles]
        deltas = [later - earlier for earlier, later in pairwise(closes)]
        gains = [max(delta, ZERO) for delta in deltas]
        losses = [max(-delta, ZERO) for delta in deltas]

        average_gain = _wilder_average(gains, self.period)
        average_loss = _wilder_average(losses, self.period)
        if average_loss == ZERO:
            # No downward movement in the window: RS is unbounded, RSI saturates at 100.
            return HUNDRED if average_gain > ZERO else Decimal(50)
        strength = divide(average_gain, average_loss)
        return HUNDRED - divide(HUNDRED, Decimal(1) + strength)


class ATR(Indicator):
    """Average True Range (Wilder), **absolute** — quote currency per unit.

    The units matter: sizing is `qty = risk_amount / (stop_multiple × ATR)`. Expressed as a
    fraction of price instead, that formula is off by a factor of price — negligible for BTC,
    absurd for a penny-priced asset (REVIEW A2).
    """

    name = "ATR"
    bands = (Band(None, "absolute volatility per unit, in quote currency"),)

    def compute(self, series: CandleSeries) -> Decimal:
        self.require_history(series, self.period + 1)
        candles = series.candles
        true_ranges = [
            max(
                candle.high - candle.low,
                abs(candle.high - previous.close),
                abs(candle.low - previous.close),
            )
            for previous, candle in pairwise(candles)
        ]
        return _wilder_average(true_ranges, self.period)


REGISTRY: dict[str, Indicator] = {
    indicator.name: indicator for indicator in (RSI(period=14), ATR(period=14))
}


def get_indicator(name: str) -> Indicator:
    if name not in REGISTRY:
        raise ConfigError(f"unknown indicator {name!r}; known: {sorted(REGISTRY)}")
    return REGISTRY[name]


def compute_readings(series: CandleSeries, names: tuple[str, ...]) -> tuple[IndicatorReading, ...]:
    """Compute and verbalize a set of indicators over one series."""
    readings = []
    for name in names:
        indicator = get_indicator(name)
        value = indicator.compute(series)
        readings.append(
            IndicatorReading(
                name=indicator.name,
                timeframe=series.timeframe,
                value=value,
                text=indicator.verbalize(value),
            )
        )
    return tuple(readings)
