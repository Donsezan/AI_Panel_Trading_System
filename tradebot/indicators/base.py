"""Indicator contract and deterministic verbalization.

Two design choices worth stating:

* **Indicators compute in `Decimal`, not `float`.** PLAN §2.1 permits float inside indicator
  math, but ATR is a direct input to position sizing — keeping the whole path decimal removes
  the conversion boundary rather than guarding it.
* **Verbalization is code, and it is banded by a table.** The panel reads text, so the wording
  is part of the decision input. A golden test pins it, because a reworded band is a silently
  changed prompt and therefore a silently changed strategy (DESIGN §6.3).

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

    name: str
    bands: tuple[Band, ...] = ()

    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError(f"{type(self).__name__} period must be positive, got {period}")
        self.period = period

    @property
    def label(self) -> str:
        return f"{self.name}({self.period})"

    @abstractmethod
    def compute(self, series: CandleSeries) -> Decimal:
        """The indicator's current value. Raises `DataStaleError` on insufficient history."""

    def verbalize(self, value: Decimal) -> str:
        return f"{self.label}={display(value)} — {describe(value, self.bands)}"

    def require_history(self, series: CandleSeries, needed: int) -> None:
        if len(series) < needed:
            raise DataStaleError(
                f"{self.label} needs {needed} candles, {series.instrument_key} "
                f"{series.timeframe} has {len(series)}"
            )
