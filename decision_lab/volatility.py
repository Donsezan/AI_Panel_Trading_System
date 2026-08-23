"""Realised volatility, its sign, and percentiles — all in `Decimal`.

One estimator serves two callers with different windows: §8.1 labels each *bar* from a trailing
30-bar window, and §4.5 labels each *day* from that day's own bars. Same measurement, so a day
selected as a shock is a day the labeller also calls a shock.

Realised volatility here is the root mean square of close-to-close log returns — no mean
subtraction, which is the standard construction and the one that stays scale-free. Scale-free is
the load-bearing property: an absolute-move estimator would make every calibration day a day
whichever instrument carries the largest numbers happened to move on.

`Decimal.ln` and `Decimal.sqrt` are exact-context operations, so none of this passes through a
float (spec §9.2). Every arithmetic step goes through `MONEY_CONTEXT` rather than through the
thread-local default one, including the accumulation: a `sum()` would fold 34-digit terms at the
default 28 digits, which is a quieter version of the same rounding this package exists to avoid.
`MONEY_CONTEXT` traps `InvalidOperation` and `DivisionByZero`, which is why a non-positive close
is refused explicitly rather than left to produce a trapped `ln(0)` whose message names no
instrument.

Failure semantics: this module has no dependencies and cannot fail from outside. Bad input — an
empty window, a non-positive price — raises `ConfigError`, because the caller's next step is to
repair the dataset, not to retry.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_CEILING, Decimal
from itertools import pairwise

from tradebot.core.errors import ConfigError
from tradebot.core.market import Candle
from tradebot.core.money import MONEY_CONTEXT, ONE, ZERO, divide, multiply


def _closes(candles: Sequence[Candle]) -> tuple[Decimal, ...]:
    if not candles:
        raise ConfigError("no candles in the window: nothing to measure")
    closes = tuple(candle.close for candle in candles)
    if any(close <= ZERO for close in closes):
        raise ConfigError(
            "a non-positive close in the window: a log return is undefined there, and a zero "
            "price is corrupt data rather than a total loss. Run `dataset verify --repair`"
        )
    return closes


def log_returns(candles: Sequence[Candle]) -> tuple[Decimal, ...]:
    """Close-to-close log returns. `n` bars yield `n - 1` returns; one bar yields none."""
    closes = _closes(candles)
    return tuple(MONEY_CONTEXT.ln(divide(later, earlier)) for earlier, later in pairwise(closes))


def realised_volatility(candles: Sequence[Candle]) -> Decimal:
    """Root mean square of the window's log returns. Zero for a flat or single-bar window."""
    returns = log_returns(candles)
    if not returns:
        return ZERO
    total = ZERO
    for value in returns:
        total = MONEY_CONTEXT.add(total, multiply(value, value))
    return MONEY_CONTEXT.sqrt(divide(total, Decimal(len(returns))))


def window_return(candles: Sequence[Candle]) -> Decimal:
    """The window's signed return, close to close.

    Close to close rather than open to close, so the sign is the sign of the same series
    `realised_volatility` squares. A direction measured on one series and a magnitude on another
    can disagree, and a `SHOCK_UP` day whose magnitude came from a fall is worse than no label.
    """
    closes = _closes(candles)
    return MONEY_CONTEXT.subtract(divide(closes[-1], closes[0]), ONE)


def percentile(values: Sequence[Decimal], fraction: Decimal) -> Decimal:
    """Nearest-rank percentile: the smallest value at or above `fraction` of the population.

    Nearest-rank rather than interpolated, so a threshold is always a number the data actually
    took. An interpolated 90th percentile is a volatility no bar ever had, which reads oddly on a
    report that has to justify why a particular day was chosen.
    """
    if not values:
        raise ConfigError("percentile of an empty population")
    if not ZERO <= fraction <= ONE:
        raise ConfigError(f"percentile fraction must be within [0, 1], got {fraction}")
    ordered = sorted(values)
    rank = int(multiply(fraction, Decimal(len(ordered))).to_integral_value(rounding=ROUND_CEILING))
    return ordered[max(rank, 1) - 1]
