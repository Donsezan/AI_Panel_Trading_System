"""The calibration day set: selected once, pinned to a file, reused (spec §4.5).

§10's first two scenarios need a normal day, an up-shock and a down-shock. The operator has no
preference about *which*, so the tool chooses — but it chooses **once**. Two setups measured on
two different sets of days are not measured against each other at all, which is the same reason
§3 freezes the corpus: a difference in score must be a difference in reasoning.

The measurement is `volatility.realised_volatility`, the estimator §8.1's bar labeller uses,
evaluated over each calendar day's bars rather than over a trailing 30-bar window. Same
measurement, different window — so a day selected as a shock is a day the labeller also calls a
shock, and the report's regime rows and its calibration days cannot disagree.

A shock day is a shock *for something*. The reference instrument is recorded and printed on every
report: a day violent for XRP and calm for BTC is a legitimate test and a different one.

Failure semantics: a pool holding fewer than `DAYS_PER_POOL` eligible days raises `ConfigError`
naming the pool and the count — calibrating on one day and presenting it as three is worse than
refusing. A distribution too flat to separate the two labels refuses before it can mislabel. A
pinned file whose `dataset_digest` no longer matches is stale and refuses, naming `--reselect`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from decision_lab.dataset import CoverageAudit, dataset_digest, read_series, series_key
from decision_lab.params import (
    DAYS_PER_POOL,
    DAYSET_FILE,
    DEFAULT_HORIZON_BARS,
    DEFAULT_SHOCK_PERCENTILE,
    NORMAL_PERCENTILE_BAND,
)
from decision_lab.volatility import percentile, realised_volatility, window_return
from tradebot.core.clock import Clock
from tradebot.core.errors import ConfigError
from tradebot.core.instrument import Instrument
from tradebot.core.market import Candle, timeframe_interval
from tradebot.core.money import ZERO
from tradebot.core.schema import DomainModel, Money, UtcDatetime
from tradebot.marketdata.recorder import ReplayDataset


class Pool(StrEnum):
    """The three regimes, as pools to draw calibration days from.

    Declared here because §4.5 needs them before §8 exists, and slice B's `regimes.py` imports
    this enum rather than declaring a second one: a day pinned as `SHOCK_DOWN` and a bar labelled
    `SHOCK_DOWN` must be the same string, or the report joins on nothing.
    """

    NORMAL = "NORMAL"
    SHOCK_UP = "SHOCK_UP"
    SHOCK_DOWN = "SHOCK_DOWN"


class Thresholds(DomainModel):
    """The percentile rules in force when a set was selected. Recorded, so a set is explicable."""

    normal_band: tuple[Money, Money] = NORMAL_PERCENTILE_BAND
    shock_percentile: Money = DEFAULT_SHOCK_PERCENTILE


class CalibrationDays(DomainModel):
    """`decision_lab-calibration-days.json` — the pinned set and everything that produced it."""

    selected_at: UtcDatetime
    seed: int
    reference_instrument: str
    scoring_timeframe: str
    thresholds: Thresholds = Thresholds()
    dataset_digest: str
    dayset_digest: str
    days: dict[str, tuple[date, ...]]

    @property
    def all_days(self) -> tuple[date, ...]:
        return tuple(sorted({day for pool in self.days.values() for day in pool}))

    def pool_of(self, day: date) -> Pool | None:
        for name, days in self.days.items():
            if day in days:
                return Pool(name)
        return None


class DayFacts(DomainModel):
    """One calendar day of the reference instrument, measured."""

    day: date
    volatility: Money
    day_return: Money
    bars: int
    #: The day's own first open and last close, not midnight to midnight: a dataset opening at
    #: 06:00 has a first day whose window starts there, and checking a hole against a stretch of
    #: time the dataset never covered would make that day ineligible for nothing.
    first_open: UtcDatetime
    last_close: UtcDatetime


def _by_day(candles: Sequence[Candle]) -> dict[date, list[Candle]]:
    grouped: dict[date, list[Candle]] = {}
    for candle in candles:
        grouped.setdefault(candle.open_time.date(), []).append(candle)
    return grouped


def measure_days(candles: Sequence[Candle]) -> tuple[DayFacts, ...]:
    """Realised volatility and signed return per calendar day, in UTC."""
    return tuple(
        DayFacts(
            day=day,
            volatility=realised_volatility(bars),
            day_return=window_return(bars),
            bars=len(bars),
            first_open=bars[0].open_time,
            last_close=bars[-1].close_time,
        )
        for day, bars in sorted(_by_day(candles).items())
    )


def classify(facts: DayFacts, *, normal: tuple[Decimal, Decimal], shock: Decimal) -> Pool | None:
    """Which pool a day belongs to, or `None` when it is neither ordinary nor violent.

    A zero return at or above the shock threshold is `SHOCK_UP` — a tie-break, never a judgement,
    and the same default §8.1's dispatch table takes one level down.
    """
    if facts.volatility >= shock:
        return Pool.SHOCK_DOWN if facts.day_return < ZERO else Pool.SHOCK_UP
    if normal[0] <= facts.volatility <= normal[1]:
        return Pool.NORMAL
    return None


def _draw(days: Sequence[date], *, seed: int, count: int) -> tuple[date, ...]:
    """A seeded, reproducible draw without `random`.

    Ordering by `blake2s(seed, day)` is uniform enough for choosing three days out of a pool and
    is obviously stable across Python versions — which matters, because the seed is printed on
    every report as the thing that makes a re-run comparable.
    """
    keyed = sorted(days, key=lambda d: hashlib.blake2s(f"{seed}|{d.isoformat()}".encode()).digest())
    return tuple(sorted(keyed[:count]))


def _crosses_a_hole(
    facts: DayFacts, holes: Sequence[tuple[datetime, datetime]], horizon: timedelta
) -> bool:
    """A day is ineligible if the day itself, or the window it will be scored over, holds a hole.

    The forward horizon is included because §9.2 reads `pH` from the dataset: a hole in the
    scoring window makes the verdict wrong, not merely unavailable.
    """
    end = facts.last_close + horizon
    return any(start < end and stop > facts.first_open for start, stop in holes)


async def select(
    dataset: ReplayDataset,
    audit: CoverageAudit,
    clock: Clock,
    *,
    seed: int,
    reference_instrument: str,
    scoring_timeframe: str,
    horizon_bars: int = DEFAULT_HORIZON_BARS,
    thresholds: Thresholds | None = None,
    pinned: Sequence[date] = (),
) -> CalibrationDays:
    """Draw `DAYS_PER_POOL` days from each pool, against the declared reference instrument."""
    thresholds = thresholds or Thresholds()
    instrument = _reference(dataset, reference_instrument, scoring_timeframe)

    series = await read_series(dataset, instrument, scoring_timeframe)
    facts = measure_days(series.candles)
    if not facts:
        raise ConfigError(f"{reference_instrument} has no bars to measure")

    population = [fact.volatility for fact in facts]
    normal = (
        percentile(population, thresholds.normal_band[0]),
        percentile(population, thresholds.normal_band[1]),
    )
    shock = percentile(population, thresholds.shock_percentile)
    if shock <= normal[1]:
        raise ConfigError(
            f"{reference_instrument} cannot tell a shock from an ordinary day over this window: "
            f"its {thresholds.shock_percentile} percentile volatility is {shock}, no higher than "
            f"the {thresholds.normal_band[1]} percentile's {normal[1]}, so both labels name the "
            "same number and every day would classify as a shock by a tie-break. Widen the "
            "dataset or choose a reference instrument that moved in it"
        )

    holes = [
        (hole.from_, hole.to)
        for hole in audit.holes_for(series_key(instrument.key, scoring_timeframe))
    ]
    horizon = timeframe_interval(scoring_timeframe) * horizon_bars
    covered_end = series.candles[-1].close_time

    pools: dict[Pool, list[date]] = {pool: [] for pool in Pool}
    for fact in facts:
        if fact.last_close + horizon > covered_end:
            continue  # (a) no full forward horizon inside the dataset
        if _crosses_a_hole(fact, holes, horizon):
            continue  # (b) the day or its scoring window crosses a known hole
        pool = classify(fact, normal=normal, shock=shock)
        if pool is not None:
            pools[pool].append(fact.day)

    chosen = {
        pool: list(_draw(days, seed=seed, count=DAYS_PER_POOL)) for pool, days in pools.items()
    }
    _add_pinned(chosen, pinned, facts=facts, normal=normal, shock=shock)

    for pool, days in chosen.items():
        if len(days) < DAYS_PER_POOL:
            raise ConfigError(
                f"pool {pool.value} holds only {len(days)} eligible day(s), "
                f"{DAYS_PER_POOL} are needed. Widen the dataset, loosen "
                f"--shock-percentile, or pin days by hand with --pin"
            )

    days_by_pool = {pool.value: tuple(sorted(days)) for pool, days in chosen.items()}
    return CalibrationDays(
        selected_at=clock.now(),
        seed=seed,
        reference_instrument=reference_instrument,
        scoring_timeframe=scoring_timeframe,
        thresholds=thresholds,
        dataset_digest=audit.dataset_digest,
        dayset_digest=_digest(
            seed, reference_instrument, scoring_timeframe, thresholds, days_by_pool
        ),
        days=days_by_pool,
    )


def _reference(dataset: ReplayDataset, instrument_key: str, timeframe: str) -> Instrument:
    """The instrument whose distribution the days are drawn from, or a refusal listing what is."""
    instrument = next((i for i in dataset.instruments if i.key == instrument_key), None)
    if instrument is None:
        raise ConfigError(
            f"the dataset does not hold {instrument_key!r}; it holds "
            f"{', '.join(i.key for i in dataset.instruments)}"
        )
    if timeframe not in dataset.timeframes:
        raise ConfigError(
            f"the dataset has no {timeframe} series for every instrument; it has "
            f"{', '.join(dataset.timeframes)}"
        )
    return instrument


def _add_pinned(
    chosen: dict[Pool, list[date]],
    pinned: Sequence[date],
    *,
    facts: Sequence[DayFacts],
    normal: tuple[Decimal, Decimal],
    shock: Decimal,
) -> None:
    """Add each hand-pinned day to the pool its own volatility implies.

    Refusing a day that belongs to no pool rather than dropping it: an operator who named a day
    and saw it silently vanish would read every later report as being about a set that includes
    it.
    """
    for day in pinned:
        fact = next((f for f in facts if f.day == day), None)
        if fact is None:
            raise ConfigError(f"--pin {day.isoformat()} is not a day this dataset covers")
        pool = classify(fact, normal=normal, shock=shock)
        if pool is None:
            raise ConfigError(
                f"--pin {day.isoformat()} is neither ordinary nor violent by the thresholds in "
                f"force (volatility {fact.volatility}); it belongs to no pool"
            )
        if day not in chosen[pool]:
            chosen[pool].append(day)


def _digest(
    seed: int,
    reference_instrument: str,
    scoring_timeframe: str,
    thresholds: Thresholds,
    days: dict[str, tuple[date, ...]],
) -> str:
    """Identity of a day set. Moving it invalidates every §11 run derived from it, by design."""
    payload = "|".join(
        [
            str(seed),
            reference_instrument,
            scoring_timeframe,
            thresholds.model_dump_json(),
            *(
                f"{pool}:{','.join(d.isoformat() for d in dates)}"
                for pool, dates in sorted(days.items())
            ),
        ]
    )
    return hashlib.blake2s(payload.encode("utf-8"), digest_size=16).hexdigest()


def write(directory: Path, days: CalibrationDays) -> Path:
    path = directory / DAYSET_FILE
    path.write_text(days.model_dump_json(indent=2), encoding="utf-8")
    return path


def read(directory: Path) -> CalibrationDays:
    path = directory / DAYSET_FILE
    if not path.is_file():
        raise ConfigError(
            f"{directory} has no {DAYSET_FILE}: run `python -m decision_lab dataset days "
            f"--data {directory}` first"
        )
    return CalibrationDays.model_validate_json(path.read_text(encoding="utf-8"))


def require_pinned(directory: Path) -> CalibrationDays:
    """The pinned set for this dataset *as it stands now*, or a refusal (§15)."""
    days = read(directory)
    current = dataset_digest(directory)
    if days.dataset_digest != current:
        raise ConfigError(
            f"the pinned day set was selected against a different {directory} "
            f"({days.dataset_digest} to {current}); a repair may have moved the distribution the "
            "days were drawn from. Re-run `python -m decision_lab dataset days --reselect`"
        )
    return days
