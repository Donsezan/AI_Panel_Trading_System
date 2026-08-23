"""Selected once, pinned, reused (spec §4.5).

Pinning is what makes §10 a comparison rather than three anecdotes: two setups measured on two
different sets of days are not measured against each other at all. So the file is the authority,
`--reselect` is the only way to move it, and moving it moves `dayset_digest` and therefore every
§11 run identity derived from it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from decision_lab import calibration_days as cd
from decision_lab import dataset as ds
from decision_lab.params import DAYSET_FILE
from decision_lab.tests import factories as f
from tradebot.core.clock import ManualClock
from tradebot.core.errors import ConfigError
from tradebot.marketdata.recorder import ReplayDataset

NOW = datetime(2026, 8, 23, 9, 31, tzinfo=UTC)
SEED = 20260823

#: Day `i` of the fixture is `EPOCH + i days`.
#:
#: The shock count is arithmetic, not taste. Nearest-rank means the `p` percentile of `n` days
#: admits exactly `n - ceil(p * n) + 1` of them — seven, for the default 0.90 over sixty days. One
#: shock day fewer and the threshold lands on a calm day, so every day classifies as a shock by
#: the tie-break; one more and the quietest shock falls below its own threshold. Four up and three
#: down is that seven, split so that knocking one up-day out still leaves a full pool.
DAYS = 60
SHOCK_UP_DAYS = (3, 11, 19, 27)
SHOCK_DOWN_DAYS = (7, 15, 23)


def day(index: int) -> date:
    return (f.EPOCH + timedelta(days=index)).date()


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOW)


@pytest.fixture
async def verified(tmp_path: Path, clock: ManualClock) -> tuple[ReplayDataset, ds.CoverageAudit]:
    """Sixty days of 1h bars with the loud days above, audited clean."""
    bars = f.shocked_walk(days=DAYS, shock_up=SHOCK_UP_DAYS, shock_down=SHOCK_DOWN_DAYS)
    f.write_dataset(tmp_path, {(f.instrument(), "1h"): bars})
    dataset = ReplayDataset.load(tmp_path, clock)
    return dataset, await ds.audit(dataset, clock)


async def select(
    dataset: ReplayDataset,
    audit: ds.CoverageAudit,
    clock: ManualClock,
    *,
    seed: int = SEED,
    reference_instrument: str = "binance:BTC/USDT",
    scoring_timeframe: str = "1h",
    horizon_bars: int = 6,
    pinned: Sequence[date] = (),
) -> cd.CalibrationDays:
    return await cd.select(
        dataset,
        audit,
        clock,
        seed=seed,
        reference_instrument=reference_instrument,
        scoring_timeframe=scoring_timeframe,
        horizon_bars=horizon_bars,
        pinned=pinned,
    )


async def test_three_days_are_drawn_from_each_pool(
    verified: tuple[ReplayDataset, ds.CoverageAudit], clock: ManualClock
) -> None:
    dataset, audit = verified
    days = await select(dataset, audit, clock)

    assert len(days.days[cd.Pool.NORMAL]) == 3
    assert len(days.days[cd.Pool.SHOCK_UP]) == 3
    assert len(days.days[cd.Pool.SHOCK_DOWN]) == 3
    assert len(days.all_days) == 9, "no day in two pools"


async def test_the_selection_is_reproducible_from_the_seed(
    verified: tuple[ReplayDataset, ds.CoverageAudit], clock: ManualClock
) -> None:
    dataset, audit = verified
    first = await select(dataset, audit, clock)
    second = await select(dataset, audit, clock)
    assert first.days == second.days
    assert first.dayset_digest == second.dayset_digest


async def test_a_different_seed_draws_a_different_set(
    verified: tuple[ReplayDataset, ds.CoverageAudit], clock: ManualClock
) -> None:
    dataset, audit = verified
    first = await select(dataset, audit, clock)
    second = await select(dataset, audit, clock, seed=SEED + 1)
    assert first.dayset_digest != second.dayset_digest


async def test_shock_days_carry_their_direction(
    verified: tuple[ReplayDataset, ds.CoverageAudit], clock: ManualClock
) -> None:
    """An up-shock asks whether the seats caught the move; a down-shock whether they protected
    capital. A day in the wrong pool asks the wrong question of every candidate (§8.1)."""
    dataset, audit = verified
    days = await select(dataset, audit, clock)

    ups = set(days.days[cd.Pool.SHOCK_UP])
    downs = set(days.days[cd.Pool.SHOCK_DOWN])
    assert ups.isdisjoint(downs)
    assert ups <= {day(i) for i in SHOCK_UP_DAYS}
    assert downs <= {day(i) for i in SHOCK_DOWN_DAYS}


async def test_a_thin_pool_refuses_by_name(tmp_path: Path, clock: ManualClock) -> None:
    """One up-shock day in the whole dataset. Calibrating on it and calling it three is worse
    than refusing.

    Four down-shocks keep the 90th percentile above the calm days, so the distribution can tell
    a shock from an ordinary day at all — which is what makes the refusal be about `SHOCK_UP`
    rather than about a dataset too flat to classify.
    """
    bars = f.shocked_walk(days=40, shock_up=(5,), shock_down=(9, 13, 17, 21))
    f.write_dataset(tmp_path, {(f.instrument(), "1h"): bars})
    dataset = ReplayDataset.load(tmp_path, clock)
    audit = await ds.audit(dataset, clock)

    with pytest.raises(ConfigError, match="SHOCK_UP"):
        await select(dataset, audit, clock)


async def test_a_flat_dataset_refuses_before_it_mislabels(
    tmp_path: Path, clock: ManualClock
) -> None:
    """Every day equally calm: the 90th percentile is the 60th, so "ordinary" and "violent" name
    the same number and every day would be classified a shock by a tie-break."""
    f.write_dataset(tmp_path, {(f.instrument(), "1h"): f.walk(["100"] * 24 * 40)})
    dataset = ReplayDataset.load(tmp_path, clock)
    audit = await ds.audit(dataset, clock)

    with pytest.raises(ConfigError, match="cannot tell a shock"):
        await select(dataset, audit, clock)


async def test_a_day_without_a_full_forward_horizon_is_ineligible(
    verified: tuple[ReplayDataset, ds.CoverageAudit], clock: ManualClock
) -> None:
    """§9.2 scores over H bars after the decision; a day at the very end has nowhere to score."""
    dataset, audit = verified
    days = await select(dataset, audit, clock, horizon_bars=6)
    assert max(days.all_days) < day(DAYS - 1), "the last covered day cannot carry a forward horizon"


async def test_a_day_crossing_a_known_hole_is_ineligible(
    tmp_path: Path, clock: ManualClock
) -> None:
    """A band computed across a hole is wrong while looking right (§4.4)."""
    bars = f.shocked_walk(days=DAYS, shock_up=SHOCK_UP_DAYS, shock_down=SHOCK_DOWN_DAYS)
    f.write_dataset(tmp_path, {(f.instrument(), "1h"): bars})
    dataset = ReplayDataset.load(tmp_path, clock)
    audit = await ds.audit(dataset, clock)
    key = "binance:BTC/USDT|1h"
    holed = audit.model_copy(
        update={
            "series": {
                key: audit.series[key].model_copy(
                    update={
                        "known_holes": (
                            ds.KnownHole(
                                **{
                                    "from": datetime(2024, 1, 4, 4, tzinfo=UTC),
                                    "to": datetime(2024, 1, 4, 9, tzinfo=UTC),
                                    "reason": "test",
                                }
                            ),
                        )
                    }
                )
            }
        }
    )

    days = await select(dataset, holed, clock)

    assert day(3) == date(2024, 1, 4)
    assert date(2024, 1, 4) not in days.days[cd.Pool.SHOCK_UP]


async def test_a_pinned_day_joins_the_pool_its_own_volatility_implies(
    verified: tuple[ReplayDataset, ds.CoverageAudit], clock: ManualClock
) -> None:
    dataset, audit = verified
    days = await select(dataset, audit, clock, pinned=(day(7),))
    assert day(7) in days.days[cd.Pool.SHOCK_DOWN]


async def test_a_pinned_day_belonging_to_no_pool_refuses(
    verified: tuple[ReplayDataset, ds.CoverageAudit], clock: ManualClock
) -> None:
    """Silently dropping it would leave an operator believing a day they named was measured."""
    dataset, audit = verified
    with pytest.raises(ConfigError, match=r"belongs to no pool|not a day this dataset covers"):
        await select(dataset, audit, clock, pinned=(date(2030, 1, 1),))


async def test_the_day_set_round_trips(
    tmp_path: Path, verified: tuple[ReplayDataset, ds.CoverageAudit], clock: ManualClock
) -> None:
    dataset, audit = verified
    days = await select(dataset, audit, clock)

    path = cd.write(tmp_path, days)
    assert path.name == DAYSET_FILE
    reread = cd.read(tmp_path)
    assert reread == days
    assert reread.dayset_digest == days.dayset_digest


def test_require_pinned_refuses_when_nothing_is_pinned(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="dataset days"):
        cd.require_pinned(tmp_path)


async def test_require_pinned_refuses_a_day_set_from_another_dataset(
    tmp_path: Path, verified: tuple[ReplayDataset, ds.CoverageAudit], clock: ManualClock
) -> None:
    """§15: a set selected against a dataset that has since been repaired is stale, because the
    repair may have changed the distribution the days were drawn from."""
    dataset, audit = verified
    days = await select(dataset, audit, clock)
    cd.write(tmp_path, days.model_copy(update={"dataset_digest": "stale"}))

    with pytest.raises(ConfigError, match="--reselect"):
        cd.require_pinned(tmp_path)
