"""One estimator, two windows (spec §4.5, §8.1).

The property that matters is scale invariance: a shock is a shock whether the instrument trades
at 60,000 or at 0.60. Without it every calibration day would be drawn from whichever instrument
happens to carry the largest absolute numbers, and the reference instrument would be a
formality.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from decision_lab.volatility import (
    log_returns,
    percentile,
    realised_volatility,
    window_return,
)
from tradebot.core.enums import MarketSession
from tradebot.core.errors import ConfigError
from tradebot.core.market import Candle

START = datetime(2024, 1, 1, tzinfo=UTC)


def series(closes: list[str]) -> tuple[Candle, ...]:
    """One 1h candle per close. Open/high/low are irrelevant here — the estimator reads closes."""
    return tuple(
        Candle(
            open_time=START + timedelta(hours=i),
            close_time=START + timedelta(hours=i + 1),
            open=Decimal(close),
            high=Decimal(close),
            low=Decimal(close),
            close=Decimal(close),
            volume=Decimal(1),
            session=MarketSession.CONTINUOUS,
        )
        for i, close in enumerate(closes)
    )


def test_a_flat_series_has_zero_volatility() -> None:
    assert realised_volatility(series(["100", "100", "100", "100"])) == Decimal(0)


def test_volatility_rises_with_the_size_of_the_moves() -> None:
    calm = realised_volatility(series(["100", "101", "100", "101"]))
    violent = realised_volatility(series(["100", "110", "100", "110"]))
    assert violent > calm > Decimal(0)


def test_volatility_is_invariant_to_price_scale() -> None:
    """The same relative moves on BTC and on XRP are the same volatility."""
    btc = realised_volatility(series(["60000", "63000", "60000"]))
    xrp = realised_volatility(series(["0.60", "0.63", "0.60"]))
    assert btc == pytest.approx(xrp, rel=Decimal("1e-20"))


@given(scale=st.integers(min_value=1, max_value=10_000))
def test_volatility_is_invariant_to_any_scale(scale: int) -> None:
    base = ["100", "104", "97", "103"]
    scaled = [str(Decimal(c) * scale) for c in base]
    assert realised_volatility(series(base)) == realised_volatility(series(scaled))


def test_window_return_carries_the_sign() -> None:
    assert window_return(series(["100", "110"])) > Decimal(0)
    assert window_return(series(["110", "100"])) < Decimal(0)
    assert window_return(series(["100", "100"])) == Decimal(0)


def test_a_single_bar_has_no_return_and_no_volatility() -> None:
    """One bar is one price. Refusing would make the first day of every dataset an error."""
    assert log_returns(series(["100"])) == ()
    assert realised_volatility(series(["100"])) == Decimal(0)
    assert window_return(series(["100"])) == Decimal(0)


def test_an_empty_window_refuses() -> None:
    with pytest.raises(ConfigError, match="no candles"):
        realised_volatility(())


def test_a_non_positive_close_refuses() -> None:
    """A log return needs a positive price. A zero close is corrupt data, not a 100% loss."""
    broken = series(["100", "100"])
    zeroed = (broken[0], broken[1].model_copy(update={"close": Decimal(0)}))
    with pytest.raises(ConfigError, match="non-positive close"):
        realised_volatility(zeroed)


@pytest.mark.parametrize(
    "fraction,expected",
    [("0.00", "1"), ("0.10", "1"), ("0.50", "5"), ("0.90", "9"), ("1.00", "10")],
)
def test_percentile_is_nearest_rank(fraction: str, expected: str) -> None:
    """Nearest-rank, so a threshold is always a value the data actually took."""
    values = [Decimal(n) for n in range(1, 11)]
    assert percentile(values, Decimal(fraction)) == Decimal(expected)


def test_percentile_sorts_its_input() -> None:
    assert percentile([Decimal(9), Decimal(1), Decimal(5)], Decimal("0.50")) == Decimal(5)


def test_percentile_refuses_an_empty_population() -> None:
    with pytest.raises(ConfigError, match="empty population"):
        percentile([], Decimal("0.50"))


def test_percentile_refuses_a_fraction_outside_the_unit_interval() -> None:
    """A fraction above 1 would index past the population and read as the maximum."""
    with pytest.raises(ConfigError, match="within"):
        percentile([Decimal(1)], Decimal("1.5"))
