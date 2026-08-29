"""Which market each decision was taken in (spec §8).

Every bar of the scoring timeframe gets a label from its own realised volatility relative to that
instrument's own distribution across the dataset. Per instrument, because a 90th percentile taken
across two instruments measures which of them is louder rather than which bars were violent.

**A shock carries its sign.** Realised volatility is a magnitude and therefore direction-blind,
and the direction is the whole question: the system is long-only, so an up-shock asks whether the
seats caught the move and a down-shock asks whether they protected capital. Those are opposite
competences. A blended `SHOCK` figure averages them and hides both — the same sin §8.3 forbids one
level up.

The estimator is `decision_lab.volatility`, the one §4.5 draws calibration days with. Same
measurement, different window: §4.5 measures a calendar day, this measures a trailing 30 bars. A
day pinned as a shock is therefore a day whose bars this labeller also calls a shock, and the
report's regime rows cannot disagree with its day set.

Bars near the start of a series are measured over the bars available rather than skipped. The
corpus's own indicator warm-up (`BacktestHarness.warmup_for`) puts every real decision hundreds of
bars past that region, so it is a definition that keeps the function total rather than one that
affects a number anybody reads.

Failure semantics: `label_at` raises `KeyError` for an instrument the index does not hold or an
instant before its first bar. Both are defects in the caller — a decision the dataset has no
prices for cannot be scored either — and a silent default would put a real decision in the wrong
regime.
"""

from __future__ import annotations

import bisect
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

from pydantic import ConfigDict, Field

from decision_lab.calibration_days import Pool
from decision_lab.dataset import read_series
from decision_lab.params import DEFAULT_SHOCK_PERCENTILE, DEFAULT_VOL_WINDOW_BARS
from decision_lab.volatility import percentile, realised_volatility, window_return
from tradebot.core.errors import ConfigError
from tradebot.core.market import Candle
from tradebot.core.money import ZERO
from tradebot.core.schema import DomainModel, Money, UtcDatetime
from tradebot.marketdata.recorder import ReplayDataset

#: The regime vocabulary is `Pool`, aliased rather than redeclared: a day pinned `SHOCK_DOWN` and
#: a bar labelled `SHOCK_DOWN` must be the same string, or the report joins on nothing.
RegimeLabel = Pool


def direction_of(window_return_: Decimal) -> RegimeLabel:
    """Which way a shock went. Zero breaks up — a tie-break, never a judgement (§8.1)."""
    return RegimeLabel.SHOCK_DOWN if window_return_ < ZERO else RegimeLabel.SHOCK_UP


class BarLabel(DomainModel):
    """One bar's regime, and the two numbers that produced it."""

    close_time: UtcDatetime
    label: RegimeLabel
    volatility: Money
    window_return_: Money


def label_bars(
    candles: Sequence[Candle],
    *,
    window_bars: int = DEFAULT_VOL_WINDOW_BARS,
    shock_percentile: Decimal = DEFAULT_SHOCK_PERCENTILE,
) -> tuple[BarLabel, ...]:
    """Label every bar. Two passes: measure the whole series, then threshold it against itself."""
    measured = [
        (
            candle.close_time,
            realised_volatility(window := candles[max(0, index - window_bars + 1) : index + 1]),
            window_return(window),
        )
        for index, candle in enumerate(candles)
    ]
    threshold = percentile([vol for _, vol, _ in measured], shock_percentile)
    return tuple(
        BarLabel(
            close_time=close_time,
            label=direction_of(ret) if _is_shock(vol, threshold) else RegimeLabel.NORMAL,
            volatility=vol,
            window_return_=ret,
        )
        for close_time, vol, ret in measured
    )


def _is_shock(volatility: Decimal, threshold: Decimal) -> bool:
    """At or above this instrument's own threshold — and it actually moved.

    The second half is not redundant. `percentile` is nearest-rank, so a series that never moves
    has a threshold of zero and every one of its bars sits "at or above" it: a motionless market
    labelled `SHOCK_UP` from end to end. Zero realised volatility is the definition of calm, and
    it is calm in neither direction rather than in one of them.
    """
    return volatility > ZERO and volatility >= threshold


@dataclass(frozen=True, slots=True)
class RegimeIndex:
    """Every instrument's labelled bars, answerable at an arbitrary instant."""

    timeframe: str
    window_bars: int
    shock_percentile: Money
    labels: dict[str, tuple[BarLabel, ...]]
    threshold: dict[str, Money]
    #: Named episodes that override the automatic label for the bars they cover (§8.2).
    windows: tuple[EventWindow, ...] = ()

    def with_windows(self, windows: Sequence[EventWindow]) -> RegimeIndex:
        return replace(self, windows=tuple(windows))

    def window_at(self, as_of: datetime) -> EventWindow | None:
        """The named episode covering this instant, if any. First match wins."""
        return next((window for window in self.windows if window.covers(as_of)), None)

    def label_at(self, instrument_key: str, as_of: datetime) -> RegimeLabel:
        """The bar's label, unless a named window covers the instant and says otherwise.

        The same point-in-time rule `CandleSeries.point_in_time` applies to prices: a bar still
        forming at `as_of` is not a fact yet, and a decision taken mid-bar was taken knowing only
        the bars behind it.

        A named window is an operator's assertion that this period is an episode, and the label
        it carries is measured over the *window's* own bars rather than the trailing 30. That is
        the point of naming it: an episode is a shape a fixed-width window can straddle.
        """
        window = self.window_at(as_of)
        if window is not None:
            return self._window_label(instrument_key, window)
        bars = self.labels[instrument_key]
        index = bisect.bisect_right([bar.close_time for bar in bars], as_of) - 1
        if index < 0:
            raise KeyError(
                f"{instrument_key} has no bar closed at or before {as_of.isoformat()}; "
                f"its series starts at {bars[0].close_time.isoformat()}"
            )
        return bars[index].label

    def _window_label(self, instrument_key: str, window: EventWindow) -> RegimeLabel:
        covered = [bar for bar in self.labels[instrument_key] if window.covers(bar.close_time)]
        if not covered:
            raise KeyError(f"{instrument_key} has no bars inside window {window.name!r}")
        # Measured the same way §4.5 measures a day: the window's own realised volatility against
        # the instrument's own threshold, with the sign from its own return.
        returns = [bar.window_return_ for bar in covered]
        volatility = max(bar.volatility for bar in covered)
        if volatility < self.threshold[instrument_key]:
            return RegimeLabel.NORMAL
        return direction_of(sum(returns, start=ZERO))


async def index_dataset(
    dataset: ReplayDataset,
    timeframe: str,
    *,
    window_bars: int = DEFAULT_VOL_WINDOW_BARS,
    shock_percentile: Decimal = DEFAULT_SHOCK_PERCENTILE,
) -> RegimeIndex:
    """Label every instrument's scoring-timeframe series, each against its own distribution."""
    labels: dict[str, tuple[BarLabel, ...]] = {}
    threshold: dict[str, Money] = {}
    for instrument in dataset.instruments:
        series = await read_series(dataset, instrument, timeframe)
        bars = label_bars(
            series.candles, window_bars=window_bars, shock_percentile=shock_percentile
        )
        labels[instrument.key] = bars
        threshold[instrument.key] = percentile([bar.volatility for bar in bars], shock_percentile)
    return RegimeIndex(
        timeframe=timeframe,
        window_bars=window_bars,
        shock_percentile=shock_percentile,
        labels=labels,
        threshold=threshold,
    )


#: The windows the repo ships. Overridable with `--regimes`.
DEFAULT_REGIMES_TOML: Final = Path(__file__).parent / "config" / "regimes.toml"


class EventWindow(DomainModel):
    """One named episode. Its direction is measured from its bars, never declared (§8.2)."""

    model_config = ConfigDict(populate_by_name=True)

    name: str
    from_: UtcDatetime = Field(alias="from")
    to: UtcDatetime

    def covers(self, moment: datetime) -> bool:
        return self.from_ <= moment < self.to


def load_windows(path: Path) -> tuple[EventWindow, ...]:
    """Read `regimes.toml`, or nothing at all when there is no such file.

    Absent is not a refusal: named windows are an annotation on top of the automatic labeller,
    which answers on its own. A *malformed* file is a refusal, because a window silently dropped
    would move numbers on a report that still claims to cover the episode.
    """
    if not path.is_file():
        return ()
    with path.open("rb") as handle:
        document = tomllib.load(handle)
    windows = tuple(EventWindow.model_validate(row) for row in document.get("window", ()))
    for window in windows:
        if window.from_ >= window.to:
            raise ConfigError(
                f"named window {window.name!r} ends before it begins "
                f"({window.from_.isoformat()} → {window.to.isoformat()})"
            )
    return windows
