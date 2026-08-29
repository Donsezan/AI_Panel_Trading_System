"""A shock carries its sign, and that is the whole point (spec §8.1).

Realised volatility is a magnitude, so it is direction-blind. The system is long-only: an
up-shock asks *did the seats catch the move*, a down-shock asks *did the seats protect capital*.
Those are opposite competences, and a blended `SHOCK` figure averages them and hides both.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from decision_lab import regimes as rg
from decision_lab.calibration_days import Pool
from decision_lab.tests import factories as f
from tradebot.core.clock import ManualClock
from tradebot.core.market import Candle
from tradebot.marketdata.recorder import ReplayDataset

NOW = datetime(2026, 8, 23, tzinfo=UTC)


#: A step and its exact reciprocal. `realised_volatility` is the RMS of *log* returns, so equal
#: arithmetic steps are not equal magnitudes: +5 a bar from 100 is a factor 1.5 over ten bars and
#: -5 a bar is a factor 2. Only a ratio and its inverse make an up-shock and a down-shock the
#: same size, which is what `test_the_same_magnitude_in_two_directions_gets_two_labels` asserts.
#: Both are exact in decimal (5/4 and 4/5), so the two volatilities are equal to the last digit
#: rather than to a tolerance.
STEP_UP = Decimal("1.25")
STEP_DOWN = Decimal("0.8")


def _calm_then_move(step: Decimal, *, calm: int, shock: int) -> tuple[Candle, ...]:
    price = Decimal(100)
    closes = ["100"] * calm
    for _ in range(shock):
        price *= step
        closes.append(str(price))
    return f.walk(closes)


def rising_then_calm(*, calm: int = 60, shock: int = 10) -> tuple[Candle, ...]:
    """A long calm stretch, then a violent rally. The rally must label `SHOCK_UP`."""
    return _calm_then_move(STEP_UP, calm=calm, shock=shock)


def falling_then_calm(*, calm: int = 60, shock: int = 10) -> tuple[Candle, ...]:
    return _calm_then_move(STEP_DOWN, calm=calm, shock=shock)


def test_a_calm_stretch_is_normal() -> None:
    labels = rg.label_bars(rising_then_calm(), window_bars=10, shock_percentile=Decimal("0.90"))
    assert labels[30].label is Pool.NORMAL


def test_a_rally_is_shock_up() -> None:
    labels = rg.label_bars(rising_then_calm(), window_bars=10, shock_percentile=Decimal("0.90"))
    assert labels[-1].label is Pool.SHOCK_UP


def test_a_crash_is_shock_down() -> None:
    labels = rg.label_bars(falling_then_calm(), window_bars=10, shock_percentile=Decimal("0.90"))
    assert labels[-1].label is Pool.SHOCK_DOWN


def test_the_same_magnitude_in_two_directions_gets_two_labels() -> None:
    """The failure this split exists to prevent: one `SHOCK` bucket averaging both."""
    up = rg.label_bars(rising_then_calm(), window_bars=10, shock_percentile=Decimal("0.90"))
    down = rg.label_bars(falling_then_calm(), window_bars=10, shock_percentile=Decimal("0.90"))
    assert up[-1].volatility == down[-1].volatility
    assert up[-1].label is not down[-1].label


def test_a_flat_window_at_or_above_the_threshold_breaks_up() -> None:
    """A tie-break, never a judgement, and the same default §4.5's `classify` takes."""
    assert rg.direction_of(Decimal(0)) is Pool.SHOCK_UP
    assert rg.direction_of(Decimal("-0.0001")) is Pool.SHOCK_DOWN


def test_every_bar_gets_exactly_one_label() -> None:
    candles = rising_then_calm()
    labels = rg.label_bars(candles, window_bars=10, shock_percentile=Decimal("0.90"))
    assert len(labels) == len(candles)
    assert [b.close_time for b in labels] == [c.close_time for c in candles]


def test_the_threshold_is_the_instruments_own_distribution() -> None:
    """A 90th percentile taken across two instruments would make the quieter one all-normal and
    the louder one all-shock, which measures the instruments rather than the panel."""
    quiet = rg.label_bars(
        f.walk([str(100 + i % 2) for i in range(80)]),
        window_bars=10,
        shock_percentile=Decimal("0.90"),
    )
    assert any(b.label is not Pool.NORMAL for b in quiet), "a quiet series still has a top decile"


async def test_the_index_answers_for_an_instant(tmp_path: Path) -> None:
    clock = ManualClock(NOW)
    inst = f.instrument()
    f.write_dataset(tmp_path, {(inst, "1h"): rising_then_calm()})
    dataset = ReplayDataset.load(tmp_path, clock)

    index = await rg.index_dataset(dataset, "1h", window_bars=10, shock_percentile=Decimal("0.90"))

    last_close = f.EPOCH + timedelta(hours=70)
    assert index.label_at(inst.key, last_close) is Pool.SHOCK_UP


async def test_the_index_answers_between_bars_with_the_last_closed_one(tmp_path: Path) -> None:
    """A decision is taken at an instant, not on a bar boundary. The label is the most recent
    *closed* bar's, the same point-in-time rule `CandleSeries.point_in_time` applies."""
    clock = ManualClock(NOW)
    inst = f.instrument()
    f.write_dataset(tmp_path, {(inst, "1h"): rising_then_calm()})
    index = await rg.index_dataset(
        ReplayDataset.load(tmp_path, clock), "1h", window_bars=10, shock_percentile=Decimal("0.90")
    )

    mid_bar = f.EPOCH + timedelta(hours=69, minutes=30)
    assert index.label_at(inst.key, mid_bar) is index.label_at(
        inst.key, f.EPOCH + timedelta(hours=69)
    )


async def test_an_instant_before_the_series_refuses(tmp_path: Path) -> None:
    clock = ManualClock(NOW)
    inst = f.instrument()
    f.write_dataset(tmp_path, {(inst, "1h"): rising_then_calm()})
    index = await rg.index_dataset(
        ReplayDataset.load(tmp_path, clock), "1h", window_bars=10, shock_percentile=Decimal("0.90")
    )

    with pytest.raises(KeyError):
        index.label_at(inst.key, f.EPOCH - timedelta(hours=1))
