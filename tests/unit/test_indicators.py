"""Indicators: value correctness, fail-closed history checks, and golden verbalization.

The verbalization tests are golden on purpose. That text goes into the prompt, so rewording a
band is a silent change to the strategy — it must fail a test, not slip through review.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.core.enums import MarketSession
from tradebot.core.errors import ConfigError, DataStaleError
from tradebot.core.market import Candle, CandleSeries
from tradebot.indicators.base import Band, describe, display
from tradebot.indicators.library import (
    ATR,
    DEFAULT_INDICATORS,
    EMA,
    MACD,
    REGISTRY,
    RSI,
    SMA,
    BollingerPercentB,
    VolumeProfilePoc,
    compute_readings,
    get_indicator,
    required_history,
)

START = datetime(2026, 1, 1, tzinfo=UTC)


def series(
    closes: list[str],
    *,
    highs: list[str] | None = None,
    lows: list[str] | None = None,
    volumes: list[str] | None = None,
    sessions: list[MarketSession] | None = None,
) -> CandleSeries:
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
                volume=Decimal(volumes[index]) if volumes else Decimal(10),
                session=sessions[index] if sessions else MarketSession.CONTINUOUS,
            )
        )
    return CandleSeries(
        instrument_key="sim:BTC/USDT",
        timeframe="1h",
        candles=tuple(candles),
        observed_at=START + timedelta(hours=len(closes)),
    )


def ramp(count: int, start: int = 100, step: int = 1) -> list[str]:
    return [str(start + index * step) for index in range(count)]


class TestRSI:
    def test_uninterrupted_gains_saturate_at_100(self) -> None:
        assert RSI(period=14).compute(series(ramp(20))) == Decimal(100)

    def test_uninterrupted_losses_bottom_at_zero(self) -> None:
        assert RSI(period=14).compute(series(ramp(20, start=200, step=-1))) == Decimal(0)

    def test_a_flat_market_is_neutral(self) -> None:
        assert RSI(period=14).compute(series(["100"] * 20)) == Decimal(50)

    def test_value_stays_within_bounds(self) -> None:
        mixed = ["100", "102", "101", "105", "103", "108", "107", "110", "106", "112"]
        value = RSI(period=14).compute(series(mixed + mixed))
        assert Decimal(0) <= value <= Decimal(100)

    def test_insufficient_history_fails_closed(self) -> None:
        """A partial window would understate volatility and therefore oversize the position."""
        with pytest.raises(DataStaleError, match="needs 15 candles"):
            RSI(period=14).compute(series(["100"] * 10))


class TestATR:
    def test_constant_range_gives_that_range(self) -> None:
        value = ATR(period=14).compute(series(["100"] * 20, highs=["102"] * 20, lows=["98"] * 20))
        assert value == Decimal(4)

    def test_atr_is_absolute_not_a_fraction_of_price(self) -> None:
        """Sizing divides by `stop_multiple × ATR`; a ratio here would be off by ~price."""
        cheap = ATR(period=14).compute(series(["1"] * 20, highs=["1.02"] * 20, lows=["0.98"] * 20))
        assert cheap == Decimal("0.04")

    def test_gaps_count_toward_true_range(self) -> None:
        """True range includes the move from the previous close, not just the bar's own range."""
        steady = ATR(period=14).compute(series(["100"] * 20, highs=["101"] * 20, lows=["99"] * 20))
        gapping = ATR(period=14).compute(series(ramp(20, step=10)))
        assert gapping > steady

    def test_insufficient_history_fails_closed(self) -> None:
        with pytest.raises(DataStaleError):
            ATR(period=14).compute(series(["100"] * 5))


class TestMovingAverages:
    def test_sma_is_the_mean_of_the_window(self) -> None:
        assert SMA(4).compute(series(["1", "2", "3", "10", "20", "30", "40"])) == Decimal(25)

    def test_ema_weights_recent_closes_more_than_sma(self) -> None:
        """A step up must show in the EMA before the SMA's window has absorbed it.

        Note the series is a *step*, not a ramp: on a perfectly linear ramp the two averages are
        mathematically equal, so a ramp would assert nothing.
        """
        stepped = series(["100"] * 40 + ["200"] * 5)
        assert EMA(20).compute(stepped) > SMA(20).compute(stepped)

    def test_a_flat_market_puts_both_averages_at_price(self) -> None:
        flat = series(["100"] * 40)
        assert SMA(20).compute(flat) == Decimal(100)
        assert EMA(20).compute(flat) == Decimal(100)

    def test_distance_companion_is_a_percentage_not_a_level(self) -> None:
        """A percentage is what lets one band table serve a $0.30 stock and BTC alike."""
        readings = {r.name: r for r in EMA(20, name="EMA20").readings(series(ramp(60)))}
        assert readings["EMA20"].value > Decimal(100)
        assert Decimal(0) < readings["EMA20_DIST_PCT"].value < Decimal(20)

    def test_distance_is_negative_when_price_is_below_the_average(self) -> None:
        readings = {r.name: r for r in EMA(20, name="EMA20").readings(series(ramp(60, step=-1)))}
        assert readings["EMA20_DIST_PCT"].value < Decimal(0)


class TestMACD:
    def test_a_flat_market_has_no_momentum(self) -> None:
        readings = {r.name: r for r in MACD().readings(series(["100"] * 60))}
        assert readings["MACD"].value == Decimal(0)
        assert readings["MACD_SIGNAL"].value == Decimal(0)
        assert readings["MACD_HIST_PCT"].value == Decimal(0)

    def test_a_rising_market_puts_the_fast_ema_above_the_slow(self) -> None:
        assert MACD().compute(series(ramp(80))) > Decimal(0)

    def test_a_falling_market_inverts_the_line(self) -> None:
        assert MACD().compute(series(ramp(80, start=300, step=-1))) < Decimal(0)

    def test_the_histogram_is_scale_free(self) -> None:
        """The same shape at two price scales must produce the same normalized histogram."""
        cheap = MACD().readings(series([str(Decimal(index) / 100) for index in range(100, 180)]))
        rich = MACD().readings(series([str(Decimal(index) * 100) for index in range(100, 180)]))
        assert _named(cheap, "MACD_HIST_PCT").value == _named(rich, "MACD_HIST_PCT").value
        assert _named(cheap, "MACD").value != _named(rich, "MACD").value

    def test_history_requirement_covers_slow_plus_signal(self) -> None:
        assert MACD(fast=12, slow=26, signal=9).min_history == 34
        with pytest.raises(DataStaleError, match="needs 34 candles"):
            MACD().compute(series(["100"] * 33))

    def test_a_fast_period_above_the_slow_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="0 < fast < slow"):
            MACD(fast=26, slow=12)


class TestBollinger:
    def test_a_flat_window_has_no_channel_and_sits_at_the_mean(self) -> None:
        readings = {r.name: r for r in BollingerPercentB().readings(series(["100"] * 30))}
        assert readings["BBANDS"].value == Decimal(50)
        assert readings["BBANDS_UPPER"].value == readings["BBANDS_LOWER"].value == Decimal(100)
        assert readings["BBANDS_WIDTH_PCT"].value == Decimal(0)

    def test_percent_b_exceeds_100_above_the_upper_band(self) -> None:
        """The informative case is the close leaving the channel, so the range is not clamped."""
        breakout = series(["100"] * 25 + ["140"])
        assert BollingerPercentB(period=20).compute(breakout) > Decimal(100)

    def test_bands_straddle_the_mean(self) -> None:
        readings = {r.name: r for r in BollingerPercentB().readings(series(ramp(40)))}
        assert readings["BBANDS_LOWER"].value < readings["BBANDS_UPPER"].value
        assert readings["BBANDS_WIDTH_PCT"].value > Decimal(0)

    def test_a_non_positive_multiple_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="multiple must be positive"):
            BollingerPercentB(20, Decimal(0))


class TestVolumeProfile:
    def test_the_poc_lands_where_the_volume_traded(self) -> None:
        closes = ["100"] * 40 + ["200"] * 8
        volumes = ["1"] * 40 + ["500"] * 8
        readings = {
            r.name: r
            for r in VolumeProfilePoc(period=48, buckets=10).readings(
                series(closes, volumes=volumes)
            )
        }
        assert readings["VPROFILE"].value > Decimal(150)
        assert readings["VPROFILE_SHARE_PCT"].value > Decimal(90)

    def test_a_flat_window_collapses_to_one_price(self) -> None:
        """Zero range means no buckets to distribute across, so the POC is that single price."""
        flat = series(["50"] * 25, highs=["50"] * 25, lows=["50"] * 25)
        readings = {r.name: r for r in VolumeProfilePoc(period=20, buckets=8).readings(flat)}
        assert readings["VPROFILE"].value == Decimal(50)
        assert readings["VPROFILE_SHARE_PCT"].value == Decimal(100)

    def test_an_untraded_window_fails_closed(self) -> None:
        """A window with no volume is not a market to size a position against."""
        with pytest.raises(DataStaleError, match="traded no volume"):
            VolumeProfilePoc(period=20).compute(series(["100"] * 25, volumes=["0"] * 25))

    def test_zero_buckets_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one bucket"):
            VolumeProfilePoc(48, buckets=0)


class TestSessionAwareness:
    """Extended-hours bars are thin and wide; averaging them misstates the stop distance."""

    def test_extended_hours_bars_are_excluded_from_indicators(self) -> None:
        closes = ["100"] * 20
        regular = series(closes, sessions=[MarketSession.REGULAR] * 20)
        spiked = series(
            [*closes, "500"],
            highs=[*(["101"] * 20), "900"],
            lows=[*(["99"] * 20), "100"],
            sessions=[*([MarketSession.REGULAR] * 20), MarketSession.EXTENDED],
        )
        assert ATR(period=14).compute(spiked) == ATR(period=14).compute(regular)

    def test_continuous_series_are_untouched(self) -> None:
        crypto = series(ramp(30))
        assert crypto.indicator_window() is crypto

    def test_the_window_still_fails_closed_if_filtering_leaves_too_little(self) -> None:
        thin = series(
            ["100"] * 20,
            sessions=[MarketSession.EXTENDED] * 12 + [MarketSession.REGULAR] * 8,
        )
        with pytest.raises(DataStaleError, match="has 8"):
            RSI(period=14).compute(thin)


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

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("-1.2", "MACD histogram(12,26,9)=-1.20 — momentum clearly below its signal"),
            ("-0.2", "MACD histogram(12,26,9)=-0.20 — momentum below its signal"),
            ("0", "MACD histogram(12,26,9)=0.00 — momentum level with its signal"),
            ("0.2", "MACD histogram(12,26,9)=0.20 — momentum above its signal"),
            ("3", "MACD histogram(12,26,9)=3.00 — momentum clearly above its signal"),
        ],
    )
    def test_macd_histogram_wording_is_pinned(self, value: str, expected: str) -> None:
        histogram = MACD().companions[1]
        assert histogram.verbalize(Decimal(value)) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("-9", "EMA distance(20)=-9.00 — price well below the average"),
            ("-2", "EMA distance(20)=-2.00 — price below the average"),
            ("0", "EMA distance(20)=0.00 — price at the average"),
            ("3", "EMA distance(20)=3.00 — price above the average"),
            ("11", "EMA distance(20)=11.00 — price well above the average"),
        ],
    )
    def test_average_distance_wording_is_pinned(self, value: str, expected: str) -> None:
        assert EMA(20, name="EMA20").companions[0].verbalize(Decimal(value)) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("-5", "Bollinger %B(20,2)=-5.00 — close at or below the lower band"),
            ("10", "Bollinger %B(20,2)=10.00 — close near the lower band"),
            ("30", "Bollinger %B(20,2)=30.00 — close below the channel mean"),
            ("50", "Bollinger %B(20,2)=50.00 — close at the channel mean"),
            ("70", "Bollinger %B(20,2)=70.00 — close above the channel mean"),
            ("95", "Bollinger %B(20,2)=95.00 — close near the upper band"),
            ("120", "Bollinger %B(20,2)=120.00 — close above the upper band"),
        ],
    )
    def test_percent_b_wording_is_pinned(self, value: str, expected: str) -> None:
        assert BollingerPercentB().verbalize(Decimal(value)) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("1", "Bollinger width(20,2)=1.00 — bands squeezed, volatility compressed"),
            ("4", "Bollinger width(20,2)=4.00 — normal band width"),
            ("12", "Bollinger width(20,2)=12.00 — bands wide, volatility elevated"),
        ],
    )
    def test_band_width_wording_is_pinned(self, value: str, expected: str) -> None:
        assert BollingerPercentB().companions[2].verbalize(Decimal(value)) == expected

    def test_absolute_indicators_state_their_units(self) -> None:
        """A bare number is unreadable; a level must say what it is denominated in."""
        for name in ("ATR", "MACD", "EMA20", "SMA200", "BBANDS_UPPER", "VPROFILE"):
            indicator = _find(name)
            assert "quote currency" in indicator.verbalize(Decimal(1))

    def test_display_precision_is_fixed(self) -> None:
        assert display(Decimal("1.239")) == "1.23"
        assert display(Decimal("1")) == "1.00"

    def test_the_final_band_must_be_open(self) -> None:
        with pytest.raises(ValueError, match="final band must have upper=None"):
            describe(Decimal(100), (Band(Decimal(50), "low"),))


class TestRegistry:
    def test_the_registry_holds_the_full_phase_3_set(self) -> None:
        assert set(REGISTRY) == {
            "RSI",
            "ATR",
            "MACD",
            "EMA20",
            "EMA50",
            "SMA200",
            "BBANDS",
            "VPROFILE",
        }

    def test_every_default_indicator_is_registered(self) -> None:
        assert all(name in REGISTRY for name in DEFAULT_INDICATORS)

    def test_the_defaults_include_the_atr_risk_sizing_needs(self) -> None:
        """Sizing divides by ATR; a default set without it would veto every trade."""
        assert "ATR" in DEFAULT_INDICATORS

    def test_reading_names_are_unique_across_the_whole_registry(self) -> None:
        """Two readings with one name would make `context.indicator(...)` ambiguous."""
        names = [reading for name in REGISTRY for reading in _reading_names(name)]
        assert len(names) == len(set(names))

    def test_unknown_indicator_is_a_config_error(self) -> None:
        with pytest.raises(ConfigError, match="unknown indicator"):
            get_indicator("STOCHASTIC")

    def test_readings_carry_value_and_verbalization(self) -> None:
        readings = compute_readings(series(ramp(60)), ("RSI", "MACD"))
        assert [r.name for r in readings] == ["RSI", "MACD", "MACD_SIGNAL", "MACD_HIST_PCT"]
        assert all(r.timeframe == "1h" for r in readings)
        assert all(r.text for r in readings)

    def test_required_history_reports_the_longest_window(self) -> None:
        assert required_history(("RSI", "SMA200")) == 200
        assert required_history(()) == 0

    def test_the_whole_registry_computes_over_a_realistic_window(self) -> None:
        readings = compute_readings(series(ramp(220, step=1)), tuple(REGISTRY))
        # 8 registry entries expanding to 17 readings via their companions.
        assert len(readings) == 17
        assert all(r.text for r in readings)

    def test_a_negative_period_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="period must be positive"):
            RSI(period=0)


def _named(readings: tuple, name: str):  # type: ignore[type-arg]
    return next(reading for reading in readings if reading.name == name)


def _reading_names(name: str) -> list[str]:
    indicator = get_indicator(name)
    return [indicator.name, *(companion.name for companion in indicator.companions)]


def _find(name: str):  # type: ignore[no-untyped-def]
    for indicator in REGISTRY.values():
        for candidate in (indicator, *indicator.companions):
            if candidate.name == name:
                return candidate
    raise AssertionError(f"{name} is not in the registry")
