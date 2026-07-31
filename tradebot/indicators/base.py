"""Indicator contract and deterministic verbalization.

Four design choices worth stating:

* **Indicators compute in `Decimal`, not `float`.** PLAN §2.1 permits float inside indicator
  math, but ATR is a direct input to position sizing — keeping the whole path decimal removes
  the conversion boundary rather than guarding it.
* **Verbalization is code, and it is banded by a table.** The panel reads text, so the wording
  is part of the decision input. A golden test pins it, because a reworded band is a silently
  changed prompt and therefore a silently changed strategy (DESIGN §6.3).
* **Scale-dependent values are never banded.** A MACD histogram of `0.4` is enormous for a
  penny stock and noise for BTC. Absolute indicators (ATR, moving-average levels, band edges)
  report their units in an open band; anything the panel is meant to *judge* is normalized to a
  percentage first, so one band table is correct for every instrument.
* **One value per indicator, several indicators per registry entry.** `compute` stays a pure
  scalar function — which is what risk consumes and what property tests can reason about — and
  derived readings (MACD's signal line, a moving average's distance from price) are declared as
  `companions` rather than turning the contract into a bag of values.

Failure semantics: an indicator with insufficient history raises `DataStaleError`. Returning a
partial-window value would understate volatility and therefore *oversize* the position.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from tradebot.core.errors import DataStaleError
from tradebot.core.market import CandleSeries
from tradebot.core.money import round_to_step
from tradebot.core.snapshot import IndicatorReading

DISPLAY_STEP = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class Band:
    """A labelled region of an indicator's range, scanned in order until `upper` is exceeded."""

    upper: Decimal | None
    label: str


def describe(value: Decimal, bands: tuple[Band, ...]) -> str:
    """First band whose upper bound the value does not exceed. The last band must be open."""
    for band in bands:
        if band.upper is None or value <= band.upper:
            return band.label
    raise ValueError(f"no band matches {value}; the final band must have upper=None")


def display(value: Decimal) -> str:
    """Fixed-precision rendering, so the same value always reads identically in a prompt."""
    return str(round_to_step(value, DISPLAY_STEP, ROUND_DOWN))


class Indicator(ABC):
    """A pure function from a candle series to one value, plus how to say it in words."""

    #: Indicator family, used in the human-readable label (`EMA`, `RSI`, …).
    kind: str
    bands: tuple[Band, ...] = ()

    def __init__(self, period: int, *, name: str | None = None) -> None:
        if period < 1:
            raise ValueError(f"{type(self).__name__} period must be positive, got {period}")
        self.period = period
        #: Lookup key on the reading. Distinct from `kind` so two periods of the same family can
        #: coexist (`EMA20`, `EMA50`) and be addressed unambiguously by config and by risk.
        self.name = name or self.kind
        self.companions: tuple[Indicator, ...] = ()

    @property
    def params(self) -> str:
        """Parameters as they appear in the label. Overridden by multi-parameter indicators."""
        return str(self.period)

    @property
    def label(self) -> str:
        return f"{self.kind}({self.params})"

    @property
    def min_history(self) -> int:
        """Bars required for a full-window value. One more than the period by default, because
        most indicators consume differences between consecutive closes."""
        return self.period + 1

    def compute(self, series: CandleSeries) -> Decimal:
        """The indicator's current value over the session-eligible window.

        Extended-hours bars are excluded here, once, rather than in each indicator: the session
        policy is a property of the data, not of the formula (DESIGN §6.2).
        """
        window = series.indicator_window()
        self.require_history(window, self.min_history)
        return self._value(window)

    @abstractmethod
    def _value(self, window: CandleSeries) -> Decimal:
        """The formula. History and session filtering are already applied."""

    def verbalize(self, value: Decimal) -> str:
        return f"{self.label}={display(value)} — {describe(value, self.bands)}"

    def readings(self, series: CandleSeries) -> tuple[IndicatorReading, ...]:
        """This indicator's reading followed by its companions', depth-first."""
        value = self.compute(series)
        own = IndicatorReading(
            name=self.name,
            timeframe=series.timeframe,
            value=value,
            text=self.verbalize(value),
        )
        return (own, *(r for companion in self.companions for r in companion.readings(series)))

    def require_history(self, series: CandleSeries, needed: int) -> None:
        if len(series) < needed:
            raise DataStaleError(
                f"{self.label} needs {needed} candles, {series.instrument_key} "
                f"{series.timeframe} has {len(series)}"
            )
