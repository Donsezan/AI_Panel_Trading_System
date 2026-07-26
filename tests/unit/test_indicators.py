"""Indicators: value correctness, fail-closed history checks, and golden verbalization.

The verbalization tests are golden on purpose. That text goes into the prompt, so rewording a
band is a silent change to the strategy — it must fail a test, not slip through review.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.core.errors import ConfigError, DataStaleError
from tradebot.core.market import Candle, CandleSeries
from tradebot.indicators.base import Band, describe, display
from tradebot.indicators.library import ATR, REGISTRY, RSI, compute_readings, get_indicator

START = datetime(2026, 1, 1, tzinfo=UTC)


def series(closes: list[str], *, highs: list[str] | None = None, lows: list[str] | None = None):
    candles = []
    for index, close in enumerate(closes):
        value = Decimal(close)
        candles.append(
            Candle(
                open_time=START + timedelta(hours=index),
                close_time=START + timedelta(hours=index + 1),
                open=value,
                high=Decimal(highs[index]) if highs else value + Decimal(1),
                low=Decimal(lows[index]) if lows else value - Decimal(1),
                close=value,
                volume=Decimal(10),
            )
        )
    return CandleSeries(
        instrument_key="sim:BTC/USDT",
        timeframe="1h",
        candles=tuple(candles),
        observed_at=START + timedelta(hours=len(closes)),
    )


class TestRSI:
    def test_uninterrupted_gains_saturate_at_100(self) -> None:
        rising = [str(100 + i) for i in range(20)]
        assert RSI(period=14).compute(series(rising)) == Decimal(100)

    def test_uninterrupted_losses_bottom_at_zero(self) -> None:
        falling = [str(200 - i) for i in range(20)]
        assert RSI(period=14).compute(series(falling)) == Decimal(0)

    def test_a_flat_market_is_neutral(self) -> None:
        assert RSI(period=14).compute(series(["100"] * 20)) == Decimal(50)

    def test_value_stays_within_bounds(self) -> None:
        mixed = [
            "100",
            "102",
            "101",
            "105",
            "103",
            "108",
            "107",
            "110",
            "106",
            "112",
            "111",
            "115",
            "113",
            "118",
            "116",
            "120",
        ]
        value = RSI(period=14).compute(series(mixed))
        assert Decimal(0) <= value <= Decimal(100)

    def test_insufficient_history_fails_closed(self) -> None:
        """A partial window would understate volatility and therefore oversize the position."""
        with pytest.raises(DataStaleError, match="needs 15 candles"):
            RSI(period=14).compute(series(["100"] * 10))


class TestATR:
    def test_constant_range_gives_that_range(self) -> None:
        closes = ["100"] * 20
        highs = ["102"] * 20
        lows = ["98"] * 20
        assert ATR(period=14).compute(series(closes, highs=highs, lows=lows)) == Decimal(4)

    def test_atr_is_absolute_not_a_fraction_of_price(self) -> None:
        """Sizing divides by `stop_multiple × ATR`; a ratio here would be off by ~price."""
        cheap = ATR(period=14).compute(series(["1"] * 20, highs=["1.02"] * 20, lows=["0.98"] * 20))
        assert cheap == Decimal("0.04")

    def test_gaps_count_toward_true_range(self) -> None:
        """True range includes the move from the previous close, not just the bar's own range."""
        steady = ATR(period=14).compute(series(["100"] * 20, highs=["101"] * 20, lows=["99"] * 20))
        gapping = ATR(period=14).compute(series([str(100 + i * 10) for i in range(20)]))
        assert gapping > steady

    def test_insufficient_history_fails_closed(self) -> None:
        with pytest.raises(DataStaleError):
            ATR(period=14).compute(series(["100"] * 5))


class TestVerbalization:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("10", "RSI(14)=10.00 — oversold territory"),
            ("30", "RSI(14)=30.00 — oversold territory"),
            ("40", "RSI(14)=40.00 — weak momentum"),
            ("50", "RSI(14)=50.00 — neutral"),
            ("62.345", "RSI(14)=62.34 — firm momentum"),
            ("71.3", "RSI(14)=71.30 — overbought territory"),
            ("100", "RSI(14)=100.00 — overbought territory"),
        ],
    )
    def test_rsi_wording_is_pinned(self, value: str, expected: str) -> None:
        assert RSI(period=14).verbalize(Decimal(value)) == expected

    def test_display_precision_is_fixed(self) -> None:
        assert display(Decimal("1.239")) == "1.23"
        assert display(Decimal("1")) == "1.00"

    def test_the_final_band_must_be_open(self) -> None:
        with pytest.raises(ValueError, match="final band must have upper=None"):
            describe(Decimal(100), (Band(Decimal(50), "low"),))


class TestRegistry:
    def test_known_indicators_are_registered(self) -> None:
        assert set(REGISTRY) == {"RSI", "ATR"}

    def test_unknown_indicator_is_a_config_error(self) -> None:
        with pytest.raises(ConfigError, match="unknown indicator"):
            get_indicator("MACD")

    def test_readings_carry_value_and_verbalization(self) -> None:
        readings = compute_readings(series(["100"] * 20), ("RSI", "ATR"))
        assert [reading.name for reading in readings] == ["RSI", "ATR"]
        assert all(reading.timeframe == "1h" for reading in readings)
        assert all(reading.text for reading in readings)

    def test_a_negative_period_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="period must be positive"):
            RSI(period=0)
