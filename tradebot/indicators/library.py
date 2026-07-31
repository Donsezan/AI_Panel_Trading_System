"""The indicator registry (DESIGN §6.3).

RSI, MACD and the moving averages are the panel's momentum and trend evidence; ATR is the
**risk layer's** stop-distance basis, which is why the indicator engine is a dependency of risk
and not only of the panel.

Two conventions run through the whole file:

* **Absolute values report their units; judged values are percentages.** ATR, moving-average
  levels and Bollinger band edges are quote-currency amounts and carry an open band naming the
  units. Anything the panel is meant to interpret — MACD histogram, distance from an average,
  band width, %B, volume concentration — is normalized to a percentage first, so one band table
  is correct for a $0.30 stock and for BTC alike.
* **Shared math is a function, not inheritance.** Wilder smoothing, EMA and standard deviation
  are module-level pure functions; a family's members are siblings over a small shared base,
  never subclasses of each other.

Every formula is `Decimal` end to end. Registry keys are the reading names, so config, prompts
and the risk layer all address an indicator by exactly one string.
"""

from __future__ import annotations

from decimal import Decimal
from itertools import pairwise

from tradebot.core.errors import ConfigError, DataStaleError
from tradebot.core.market import CandleSeries
from tradebot.core.money import MONEY_CONTEXT, ZERO, divide, multiply
from tradebot.core.snapshot import IndicatorReading
from tradebot.indicators.base import Band, Indicator

HUNDRED = Decimal(100)
TWO = Decimal(2)
THREE = Decimal(3)
HALF = Decimal("0.5")

_UNITS = "absolute level in quote currency"


# ------------------------------------------------------------------ shared math


def _mean(values: list[Decimal]) -> Decimal:
    return divide(sum(values, start=ZERO), Decimal(len(values)))


def _wilder_average(values: list[Decimal], period: int) -> Decimal:
    """Wilder's smoothing: seed with the first `period` mean, then decay the rest into it."""
    average = _mean(values[:period])
    for value in values[period:]:
        average = divide(multiply(average, Decimal(period - 1)) + value, Decimal(period))
    return average


def _ema_series(values: list[Decimal], period: int) -> list[Decimal]:
    """EMA at every index from `period - 1` onward, seeded with the first `period` mean.

    The whole series is returned because MACD's signal line is an EMA *of the MACD line*: a
    single trailing value could not be smoothed again.
    """
    if len(values) < period:
        raise DataStaleError(f"EMA({period}) needs {period} values, got {len(values)}")
    alpha = divide(TWO, Decimal(period + 1))
    ema = _mean(values[:period])
    series = [ema]
    for value in values[period:]:
        ema = multiply(value, alpha) + multiply(ema, Decimal(1) - alpha)
        series.append(ema)
    return series


def _stdev(values: list[Decimal]) -> Decimal:
    """Population standard deviation — the convention Bollinger bands are defined with."""
    mean = _mean(values)
    variance = divide(
        sum(((value - mean) * (value - mean) for value in values), start=ZERO),
        Decimal(len(values)),
    )
    return MONEY_CONTEXT.sqrt(variance)


def _closes(window: CandleSeries) -> list[Decimal]:
    return [candle.close for candle in window.candles]


def _pct_of(part: Decimal, whole: Decimal) -> Decimal:
    return multiply(divide(part, whole), HUNDRED)


# ------------------------------------------------------------------ momentum


class RSI(Indicator):
    """Relative Strength Index (Wilder). Bounded 0–100."""

    kind = "RSI"
    bands = (
        Band(Decimal(30), "oversold territory"),
        Band(Decimal(45), "weak momentum"),
        Band(Decimal(55), "neutral"),
        Band(Decimal(70), "firm momentum"),
        Band(None, "overbought territory"),
    )

    def _value(self, window: CandleSeries) -> Decimal:
        deltas = [later - earlier for earlier, later in pairwise(_closes(window))]
        average_gain = _wilder_average([max(delta, ZERO) for delta in deltas], self.period)
        average_loss = _wilder_average([max(-delta, ZERO) for delta in deltas], self.period)
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

    kind = "ATR"
    bands = (Band(None, "absolute volatility per unit, in quote currency"),)

    def _value(self, window: CandleSeries) -> Decimal:
        true_ranges = [
            max(
                candle.high - candle.low,
                abs(candle.high - previous.close),
                abs(candle.low - previous.close),
            )
            for previous, candle in pairwise(window.candles)
        ]
        return _wilder_average(true_ranges, self.period)


class _MacdFamily(Indicator):
    """Shared MACD parameters and line computation."""

    def __init__(self, fast: int, slow: int, signal: int, *, name: str) -> None:
        if not 0 < fast < slow:
            raise ValueError(f"MACD needs 0 < fast < slow, got fast={fast} slow={slow}")
        if signal < 1:
            raise ValueError(f"MACD signal period must be positive, got {signal}")
        super().__init__(slow, name=name)
        self.fast, self.slow, self.signal = fast, slow, signal

    @property
    def params(self) -> str:
        return f"{self.fast},{self.slow},{self.signal}"

    @property
    def min_history(self) -> int:
        return self.slow + self.signal - 1

    def _lines(self, window: CandleSeries) -> tuple[Decimal, Decimal]:
        """The trailing `(macd, signal)` pair, aligned to the same bar."""
        closes = _closes(window)
        fast_ema = _ema_series(closes, self.fast)
        slow_ema = _ema_series(closes, self.slow)
        offset = len(fast_ema) - len(slow_ema)
        macd = [fast_ema[index + offset] - slow_ema[index] for index in range(len(slow_ema))]
        return macd[-1], _ema_series(macd, self.signal)[-1]


class MACD(_MacdFamily):
    """MACD line: `EMA(fast) − EMA(slow)`, absolute in quote currency.

    Ships its signal line and a *normalized* histogram as companions. The histogram is a
    percentage of price rather than an absolute spread, because the same absolute histogram means
    opposite things on a $0.30 stock and on BTC.
    """

    kind = "MACD"
    bands = (Band(None, f"MACD line, {_UNITS}"),)

    def __init__(
        self, fast: int = 12, slow: int = 26, signal: int = 9, *, name: str = "MACD"
    ) -> None:
        super().__init__(fast, slow, signal, name=name)
        self.companions = (
            MacdSignal(fast, slow, signal, name=f"{name}_SIGNAL"),
            MacdHistogramPct(fast, slow, signal, name=f"{name}_HIST_PCT"),
        )

    def _value(self, window: CandleSeries) -> Decimal:
        return self._lines(window)[0]


class MacdSignal(_MacdFamily):
    """The MACD signal line: `EMA(signal)` of the MACD line."""

    kind = "MACD signal"
    bands = (Band(None, f"MACD signal line, {_UNITS}"),)

    def _value(self, window: CandleSeries) -> Decimal:
        return self._lines(window)[1]


class MacdHistogramPct(_MacdFamily):
    """`(MACD − signal)` as a percentage of the last close — scale-free, so bandable."""

    kind = "MACD histogram"
    bands = (
        Band(Decimal("-0.5"), "momentum clearly below its signal"),
        Band(Decimal("-0.05"), "momentum below its signal"),
        Band(Decimal("0.05"), "momentum level with its signal"),
        Band(Decimal("0.5"), "momentum above its signal"),
        Band(None, "momentum clearly above its signal"),
    )

    def _value(self, window: CandleSeries) -> Decimal:
        macd, signal = self._lines(window)
        return _pct_of(macd - signal, window.latest.close)


# ------------------------------------------------------------------ trend


class MovingAverage(Indicator):
    """A moving average of closes, absolute in quote currency.

    Ships a distance-from-price companion: the level is context, the distance is the reading the
    panel judges, and only the distance can share one band table across instruments.
    """

    @property
    def min_history(self) -> int:
        return self.period

    def __init__(self, period: int, *, name: str | None = None, distance: bool = True) -> None:
        super().__init__(period, name=name)
        if distance:
            self.companions = (AverageDistancePct(self, name=f"{self.name}_DIST_PCT"),)


class SMA(MovingAverage):
    """Simple moving average."""

    kind = "SMA"
    bands = (Band(None, f"simple moving average, {_UNITS}"),)

    def _value(self, window: CandleSeries) -> Decimal:
        return _mean(_closes(window)[-self.period :])


class EMA(MovingAverage):
    """Exponential moving average."""

    kind = "EMA"
    bands = (Band(None, f"exponential moving average, {_UNITS}"),)

    def _value(self, window: CandleSeries) -> Decimal:
        return _ema_series(_closes(window), self.period)[-1]


class AverageDistancePct(Indicator):
    """Where price sits relative to a moving average, as a percentage of the average."""

    bands = (
        Band(Decimal(-5), "price well below the average"),
        Band(Decimal(-1), "price below the average"),
        Band(Decimal(1), "price at the average"),
        Band(Decimal(5), "price above the average"),
        Band(None, "price well above the average"),
    )

    def __init__(self, average: MovingAverage, *, name: str) -> None:
        super().__init__(average.period, name=name)
        self.kind = f"{average.kind} distance"
        self._average = average

    @property
    def min_history(self) -> int:
        return self._average.min_history

    def _value(self, window: CandleSeries) -> Decimal:
        average = self._average.compute(window)
        return _pct_of(window.latest.close - average, average)


# ------------------------------------------------------------------ volatility


class _BollingerFamily(Indicator):
    """Shared Bollinger parameters and channel computation."""

    def __init__(self, period: int, multiple: Decimal, *, name: str) -> None:
        if multiple <= ZERO:
            raise ValueError(f"Bollinger multiple must be positive, got {multiple}")
        super().__init__(period, name=name)
        self.multiple = multiple

    @property
    def params(self) -> str:
        return f"{self.period},{self.multiple}"

    @property
    def min_history(self) -> int:
        return self.period

    def _channel(self, window: CandleSeries) -> tuple[Decimal, Decimal, Decimal]:
        """`(lower, middle, upper)` over the trailing window."""
        closes = _closes(window)[-self.period :]
        middle = _mean(closes)
        offset = multiply(_stdev(closes), self.multiple)
        return middle - offset, middle, middle + offset


class BollingerPercentB(_BollingerFamily):
    """%B: where the close sits across the channel, on a 0–100 scale.

    0 is the lower band and 100 the upper; outside the channel the value leaves that range,
    which is the informative case and why the outer bands are open-ended.
    """

    kind = "Bollinger %B"
    bands = (
        Band(ZERO, "close at or below the lower band"),
        Band(Decimal(20), "close near the lower band"),
        Band(Decimal(45), "close below the channel mean"),
        Band(Decimal(55), "close at the channel mean"),
        Band(Decimal(80), "close above the channel mean"),
        Band(HUNDRED, "close near the upper band"),
        Band(None, "close above the upper band"),
    )

    def __init__(self, period: int = 20, multiple: Decimal = TWO, *, name: str = "BBANDS") -> None:
        super().__init__(period, multiple, name=name)
        self.companions = (
            BollingerBand(period, multiple, side=1, name=f"{name}_UPPER"),
            BollingerBand(period, multiple, side=-1, name=f"{name}_LOWER"),
            BollingerWidthPct(period, multiple, name=f"{name}_WIDTH_PCT"),
        )

    def _value(self, window: CandleSeries) -> Decimal:
        lower, _, upper = self._channel(window)
        if upper == lower:
            # A perfectly flat window has no channel; the close is by definition at the mean.
            return Decimal(50)
        return _pct_of(window.latest.close - lower, upper - lower)


class BollingerBand(_BollingerFamily):
    """One edge of the channel, absolute in quote currency."""

    kind = "Bollinger band"
    bands = (Band(None, f"Bollinger band edge, {_UNITS}"),)

    def __init__(self, period: int, multiple: Decimal, *, side: int, name: str) -> None:
        super().__init__(period, multiple, name=name)
        self.side = side
        self.kind = f"Bollinger {'upper' if side > 0 else 'lower'} band"

    def _value(self, window: CandleSeries) -> Decimal:
        lower, _, upper = self._channel(window)
        return upper if self.side > 0 else lower


class BollingerWidthPct(_BollingerFamily):
    """Channel width as a percentage of its mean — the standard squeeze/expansion read."""

    kind = "Bollinger width"
    bands = (
        Band(TWO, "bands squeezed, volatility compressed"),
        Band(Decimal(6), "normal band width"),
        Band(None, "bands wide, volatility elevated"),
    )

    def _value(self, window: CandleSeries) -> Decimal:
        lower, middle, upper = self._channel(window)
        return _pct_of(upper - lower, middle)


# ------------------------------------------------------------------ volume


class _VolumeProfileFamily(Indicator):
    """Shared volume-profile bucketing.

    An approximation by construction — a true profile needs tick data, and this buckets each
    bar's typical price. Stated rather than hidden, because a panel told "POC" will read it as
    exact.
    """

    def __init__(self, period: int, buckets: int, *, name: str) -> None:
        if buckets < 1:
            raise ValueError(f"volume profile needs at least one bucket, got {buckets}")
        super().__init__(period, name=name)
        self.buckets = buckets

    @property
    def params(self) -> str:
        return f"{self.period},{self.buckets}"

    @property
    def min_history(self) -> int:
        return self.period

    def _profile(self, window: CandleSeries) -> tuple[Decimal, Decimal, Decimal]:
        """`(poc_price, poc_volume, total_volume)` over the trailing window."""
        candles = window.candles[-self.period :]
        low = min(candle.low for candle in candles)
        high = max(candle.high for candle in candles)
        total = sum((candle.volume for candle in candles), start=ZERO)
        if total <= ZERO:
            raise DataStaleError(
                f"{self.label}: {window.instrument_key} {window.timeframe} traded no volume in "
                f"{self.period} bars; an untraded window is not a market to size against"
            )
        if high == low:
            return high, total, total
        width = divide(high - low, Decimal(self.buckets))
        volumes = [ZERO] * self.buckets
        for candle in candles:
            typical = divide(candle.high + candle.low + candle.close, THREE)
            bucket = min(int(divide(typical - low, width)), self.buckets - 1)
            volumes[bucket] += candle.volume
        peak = volumes.index(max(volumes))
        return low + multiply(width, Decimal(peak) + HALF), volumes[peak], total


class VolumeProfilePoc(_VolumeProfileFamily):
    """Point of control: the price bucket that traded the most volume in the window."""

    kind = "Volume profile POC"
    bands = (Band(None, f"most-traded price in the window, {_UNITS}"),)

    def __init__(self, period: int = 48, buckets: int = 12, *, name: str = "VPROFILE") -> None:
        super().__init__(period, buckets, name=name)
        self.companions = (VolumeProfileSharePct(period, buckets, name=f"{name}_SHARE_PCT"),)

    def _value(self, window: CandleSeries) -> Decimal:
        return self._profile(window)[0]


class VolumeProfileSharePct(_VolumeProfileFamily):
    """Share of window volume that traded in the point-of-control bucket."""

    kind = "Volume profile concentration"
    bands = (
        Band(Decimal(15), "volume spread across the range"),
        Band(Decimal(30), "volume moderately concentrated"),
        Band(None, "volume heavily concentrated at one price"),
    )

    def _value(self, window: CandleSeries) -> Decimal:
        _, peak_volume, total = self._profile(window)
        return _pct_of(peak_volume, total)


# ------------------------------------------------------------------ registry

#: Everything a basket's config may select. Keys are reading names, so config, prompts and the
#: risk layer address an indicator by exactly one string and a rename is a visible change.
REGISTRY: dict[str, Indicator] = {
    indicator.name: indicator
    for indicator in (
        RSI(period=14),
        ATR(period=14),
        MACD(),
        EMA(20, name="EMA20"),
        EMA(50, name="EMA50"),
        SMA(200, name="SMA200"),
        BollingerPercentB(),
        VolumeProfilePoc(),
    )
}

#: What a basket gets when it does not choose: trend, momentum, volatility, and the ATR that risk
#: sizing needs — without paying the token cost of the whole registry in every prompt.
DEFAULT_INDICATORS: tuple[str, ...] = ("RSI", "MACD", "EMA20", "EMA50", "ATR", "BBANDS")


def get_indicator(name: str) -> Indicator:
    if name not in REGISTRY:
        raise ConfigError(f"unknown indicator {name!r}; known: {sorted(REGISTRY)}")
    return REGISTRY[name]


def required_history(names: tuple[str, ...]) -> int:
    """Longest window any of these indicators needs. Sizes the candle fetch honestly."""
    return max((get_indicator(name).min_history for name in names), default=0)


def compute_readings(series: CandleSeries, names: tuple[str, ...]) -> tuple[IndicatorReading, ...]:
    """Compute and verbalize a set of indicators, with their companions, over one series."""
    return tuple(reading for name in names for reading in get_indicator(name).readings(series))
