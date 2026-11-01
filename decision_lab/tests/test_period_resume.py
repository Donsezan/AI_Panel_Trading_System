"""Checkpoints and Continue: an error continues from the last good point (period spec §5.7).

Every comparison here is against an uninterrupted run, and the panel is `Plan`, which answers from
the date and the symbol in the prompt. That is forced: a seeded `varied-*` stub keeps its draws in
one `Random`, so a resumed process would start that sequence again and decide differently from the
run it is being compared with, and the test would measure the seed instead of the restore.

A kill is a `BaseException` raised after a checkpoint. `run` catches `Exception`, so the meta stays
`RUNNING` exactly as a dead process leaves it, where an ordinary exception writes `INTERRUPTED`.

Fills, round trips and cycles are compared by time, price and quantity. Order and fill ids are
fresh uuids in every process, so they cannot match and say nothing if they do not.
"""

from __future__ import annotations

import gc
import re
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from decision_lab import cli
from decision_lab import period as pd
from decision_lab.candidates import Candidate
from decision_lab.longrun import ProfitEvidence
from decision_lab.tests import factories as f
from decision_lab.tests.test_period_pause import (
    BTC,
    ETH,
    PAUSED_ON,
    TWO_LOSSES,
    Ran,
    Votes,
    _oscillation,
    candidate_over,
    day,
    execute,
    limits_pinned,
    sells_between,
    utc,
)
from decision_lab.tests.test_period_run import cycle_stamps, fills_in, read_log
from tradebot.app import Application
from tradebot.control import startup as startup_module
from tradebot.control.supervisor import BasketWorker
from tradebot.core.clock import ManualClock
from tradebot.core.config import Basket
from tradebot.core.errors import ConfigError, FailClosedError
from tradebot.core.market import Candle
from tradebot.core.money import ZERO
from tradebot.execution import monitor as monitor_module
from tradebot.maintenance.backup import BackupError
from tradebot.persistence.store import EventStore
from tradebot.validation.evidence import Evidence

COST = Decimal("0.01")
QUARTER = "2024-Q1"
RISE_DAY = date(2024, 2, 10)
#: Past the entry's stop in March, so a position carried over the February boundary is closed by a
#: stop that only the restored venue can fire.
DROP_DAY = date(2024, 3, 10)
#: One trip closed in January, one entry carried over the February boundary, and an entry after it.
TRADES: Votes = {
    (BTC, day(1, 10)): "BUY",
    (BTC, day(1, 13)): "SELL",
    (BTC, day(2, 20)): "BUY",
    (BTC, day(3, 20)): "BUY",
}
#: Held over the boundary at which the bar below has already armed the stop.
HELD_INTO_THE_GAP: Votes = {(BTC, day(2, 20)): "BUY"}
#: A win in February hands the limit back, and March can pause under the configured limit again.
WIN_AFTER_A_LIFT: Votes = {
    **TWO_LOSSES,
    (BTC, day(2, 5)): "BUY",
    (BTC, day(2, 14)): "SELL",
    (BTC, day(3, 1)): "BUY",
    (BTC, day(3, 4)): "SELL",
    (BTC, day(3, 7)): "BUY",
    (BTC, day(3, 10)): "SELL",
    (BTC, day(3, 13)): "BUY",
}


class Killed(BaseException):
    """A process dying. Not an `Exception`, so nothing in `run` can tidy up after it."""


Trip = tuple[str, datetime, Decimal]
Cycle = tuple[datetime, str, Decimal]


def _verified(root: Path, name: str, btc: tuple[Candle, ...]) -> Path:
    data = root / name
    f.write_dataset(data, {(f.instrument(), "1h"): btc, (f.instrument(ETH), "1h"): _oscillation()})
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("DECISION_LAB_WORKSPACE", str(root / f"{name}-lock"))
        assert cli.main(["dataset", "verify", "--data", str(data)]) == cli.EXIT_OK
    return data


def _with_a_gap(hour: datetime, close: str) -> tuple[Candle, ...]:
    """The flat market, with one bar wholly below a stop's limit: it arms the stop, unfilled."""
    bars = list(_oscillation())
    index = int((hour - f.EPOCH) // timedelta(hours=1))
    price = Decimal(close)
    bars[index] = bars[index].model_copy(
        update={"open": price, "high": price, "low": price, "close": price}
    )
    return tuple(bars)


@pytest.fixture(scope="module")
def data(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("resume")
    # The last tick of February is 29 Feb 00:00, and it reads the bar that opened an hour before.
    gap_hour = datetime(2024, 2, 28, 23, tzinfo=utc(1).tzinfo)
    return {
        "flat": _verified(root, "flat", _oscillation()),
        "drop": _verified(root, "drop", _oscillation(drop_on=DROP_DAY)),
        "rise": _verified(root, "rise", _oscillation(rise_on=RISE_DAY)),
        "gap": _verified(root, "gap", _with_a_gap(gap_hour, "98")),
    }


@pytest.fixture(scope="module")
def whole_runs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Ran]:
    return {}


Uninterrupted = Callable[[Path, str, Votes], Awaitable[Ran]]


@pytest.fixture
def uninterrupted(
    whole_runs: dict[str, Ran], tmp_path_factory: pytest.TempPathFactory
) -> Uninterrupted:
    """The run each test is compared with, paid for once. Votes are not in a run's identity."""

    async def make(data_dir: Path, name: str, votes: Votes) -> Ran:
        if name not in whole_runs:
            workspace = tmp_path_factory.mktemp(f"whole-{name}")
            whole_runs[name] = await execute(data_dir, workspace, QUARTER, votes, cost_usd=COST)
        return whole_runs[name]

    return make


async def killed_after(
    data_dir: Path, workspace: Path, label: str, votes: Votes, **options: Any
) -> None:
    """Run until `label`'s checkpoint is on disk, then die."""
    real = pd.write_checkpoint

    def then_die(directory: Path, month_label: str, **options: Any) -> Path:
        written = real(directory, month_label, **options)
        if month_label == label:
            raise Killed
        return written

    with pytest.MonkeyPatch.context() as patch, pytest.raises(Killed):
        patch.setattr(pd, "write_checkpoint", then_die)
        await execute(data_dir, workspace, QUARTER, votes, cost_usd=COST, **options)


def explode_on(moment: datetime) -> Callable[[BasketWorker], Awaitable[None]]:
    async def explode(worker: BasketWorker) -> None:
        if worker._clock.now() == moment:
            raise RuntimeError("injected crash")

    return explode


def run_dir(workspace: Path) -> Path:
    return next(workspace.glob("period-*"))


def abandoned_files(workspace: Path) -> list[Path]:
    return sorted(run_dir(workspace).glob(f"{pd.PERIOD_DB}.abandoned-*"))


def stored_meta(workspace: Path) -> pd.PeriodMeta:
    text = (run_dir(workspace) / pd.PERIOD_META).read_text(encoding="utf-8")
    return pd.PeriodMeta.model_validate_json(text)


def cycles_of(database: Path) -> tuple[Cycle, ...]:
    def read(store: EventStore) -> tuple[Cycle, ...]:
        return tuple(
            (cycle.started_at, str(cycle.outcome), cycle.cost_usd)
            for cycle in Evidence.gather(store).cycles
        )

    return read_log(database, read)


def round_trips_of(database: Path) -> tuple[Trip, ...]:
    def read(store: EventStore) -> tuple[Trip, ...]:
        return tuple(
            (trip.instrument_key, trip.at, trip.realized_pnl)
            for trip in Evidence.gather(store).round_trips
        )

    return read_log(database, read)


def per_cycle(run: Ran) -> Decimal:
    """What one panel-answered cycle costs, off February's whole, unpaused month of 29."""
    return run.meta.months[1].cost_usd / 29


def assert_same_outcomes(resumed: Ran, whole: Ran) -> None:
    assert fills_in(resumed.database) == fills_in(whole.database)
    assert round_trips_of(resumed.database) == round_trips_of(whole.database)
    assert cycles_of(resumed.database) == cycles_of(whole.database)
    assert limits_pinned(resumed.database) == limits_pinned(whole.database)
    assert resumed.meta.months == whole.meta.months
    assert resumed.meta.lifts == whole.meta.lifts
    assert resumed.meta.vetoes == whole.meta.vetoes
    assert resumed.meta.incidents == whole.meta.incidents


async def test_continuing_equals_an_uninterrupted_run(
    data: dict[str, Path], tmp_path: Path, uninterrupted: Uninterrupted
) -> None:
    """Spec §5.7's deciding test: killed between months 2 and 3, holding a position with legs."""
    whole = await uninterrupted(data["drop"], "trades", TRADES)
    assert sells_between(whole.database, utc(2, 20), utc(3, 20)), "the carried stop never fired"

    await killed_after(data["drop"], tmp_path, "2024-02", TRADES)
    assert stored_meta(tmp_path).ending is pd.RunEnding.RUNNING
    resumed = await execute(data["drop"], tmp_path, QUARTER, TRADES, cost_usd=COST, resume=True)

    assert_same_outcomes(resumed, whole)
    assert resumed.meta == whole.meta


async def test_a_trade_with_a_triggered_unfilled_stop_survives_a_continue(
    data: dict[str, Path], tmp_path: Path, uninterrupted: Uninterrupted
) -> None:
    """`SimBroker.restore` comes back un-armed: must equal the uninterrupted run, or the seam is
    needed (§5.7). February's last tick reads a bar below the stop's limit, which arms the stop and
    cannot fill it, and every later bar is back above the stop. Only an armed stop sells there."""
    whole = await uninterrupted(data["gap"], "gap", HELD_INTO_THE_GAP)
    assert sells_between(whole.database, utc(3), utc(3, 2)), "the armed stop never filled"

    await killed_after(data["gap"], tmp_path, "2024-02", HELD_INTO_THE_GAP)
    resumed = await execute(
        data["gap"], tmp_path, QUARTER, HELD_INTO_THE_GAP, cost_usd=COST, resume=True
    )

    assert_same_outcomes(resumed, whole)


async def test_a_crash_mid_month_redoes_only_that_month(
    data: dict[str, Path], tmp_path: Path, uninterrupted: Uninterrupted
) -> None:
    whole = await uninterrupted(data["drop"], "trades", TRADES)

    with pytest.raises(RuntimeError, match="injected crash"):
        await execute(
            data["drop"],
            tmp_path,
            QUARTER,
            TRADES,
            cost_usd=COST,
            before_cycle=explode_on(utc(2, 25)),
        )
    resumed = await execute(data["drop"], tmp_path, QUARTER, TRADES, cost_usd=COST, resume=True)

    assert [m.label for m in resumed.meta.months] == ["2024-01", "2024-02", "2024-03"]
    assert resumed.meta.ending is pd.RunEnding.COMPLETED
    # No cycle from the abandoned work: its log is in another file, and every instant ran once.
    stamps = cycle_stamps(resumed.database)
    assert len(stamps) == len(set(stamps))
    assert_same_outcomes(resumed, whole)
    # January was not redone: the only calls are from the restored checkpoint onward.
    assert not resumed.plan.calls_between(utc(1), utc(2))

    kept = abandoned_files(tmp_path)
    assert len(kept) == 1
    # The crash came before the cycle on the 25th, and the file kept everything up to it.
    assert max(cycle_stamps(kept[0])) == utc(2, 24)


async def test_spent_budget_still_counts_after_continue(
    data: dict[str, Path], tmp_path: Path, uninterrupted: Uninterrupted
) -> None:
    whole = await uninterrupted(data["flat"], "quiet", {})

    with pytest.raises(RuntimeError, match="injected crash"):
        await execute(
            data["flat"], tmp_path, QUARTER, {}, cost_usd=COST, before_cycle=explode_on(utc(2, 11))
        )
    with pytest.raises(RuntimeError, match="injected crash"):
        await execute(
            data["flat"],
            tmp_path,
            QUARTER,
            {},
            cost_usd=COST,
            resume=True,
            before_cycle=explode_on(utc(2, 6)),
        )
    resumed = await execute(data["flat"], tmp_path, QUARTER, {}, cost_usd=COST, resume=True)

    # Ten February cycles, then five: each crash's work is counted, and the second does not forget
    # the first even though both restored the same January checkpoint.
    abandoned = 15 * per_cycle(whole)
    assert resumed.meta.abandoned_cost_usd == abandoned
    assert len(abandoned_files(tmp_path)) == 2
    assert resumed.meta.months == whole.meta.months
    assert resumed.meta.profit.net == whole.meta.profit.net - abandoned
    assert resumed.meta.profit.cost_usd == whole.meta.profit.cost_usd + abandoned


async def test_abandoned_spend_reaches_the_budget_check(
    data: dict[str, Path], tmp_path: Path, uninterrupted: Uninterrupted
) -> None:
    """Without the abandoned ten cycles, February's end would be inside this budget."""
    whole = await uninterrupted(data["flat"], "quiet", {})
    january, february, _ = whole.meta.months
    budget = january.cost_usd + february.cost_usd + 5 * per_cycle(whole)
    with pytest.raises(RuntimeError, match="injected crash"):
        await execute(
            data["flat"],
            tmp_path,
            QUARTER,
            {},
            cost_usd=COST,
            budget=budget,
            before_cycle=explode_on(utc(2, 11)),
        )

    resumed = await execute(
        data["flat"], tmp_path, QUARTER, {}, cost_usd=COST, budget=budget, resume=True
    )

    assert resumed.meta.ending is pd.RunEnding.INTERRUPTED
    assert "budget" in resumed.meta.ending_reason
    assert [m.label for m in resumed.meta.months] == ["2024-01", "2024-02"]


async def test_continuing_past_a_budget_stop_needs_a_larger_budget(
    data: dict[str, Path], tmp_path: Path, uninterrupted: Uninterrupted
) -> None:
    whole = await uninterrupted(data["flat"], "quiet", {})
    small, larger = COST, Decimal(5)
    first = await execute(data["flat"], tmp_path, QUARTER, {}, cost_usd=COST, budget=small)
    assert first.meta.ending is pd.RunEnding.INTERRUPTED
    gc.collect()  # the finished wiring's connections, or its WAL files would come and go
    before = sorted(path.name for path in run_dir(tmp_path).iterdir())

    with pytest.raises(pd.BudgetReached, match="larger budget"):
        await execute(data["flat"], tmp_path, QUARTER, {}, cost_usd=COST, budget=small, resume=True)
    assert sorted(path.name for path in run_dir(tmp_path).iterdir()) == before

    resumed = await execute(
        data["flat"], tmp_path, QUARTER, {}, cost_usd=COST, budget=larger, resume=True
    )

    assert resumed.meta.budget == larger
    assert resumed.meta.ending is pd.RunEnding.COMPLETED
    # Nothing ran past that checkpoint, so nothing was abandoned to count.
    assert resumed.meta.abandoned_cost_usd == ZERO
    assert resumed.meta.months == whole.meta.months


async def test_continue_with_no_checkpoint_restarts_from_the_start_keeping_the_old_db(
    data: dict[str, Path], tmp_path: Path
) -> None:
    with pytest.raises(RuntimeError, match="injected crash"):
        await execute(
            data["flat"],
            tmp_path,
            "2024-02",
            {},
            cost_usd=COST,
            before_cycle=explode_on(utc(2, 11)),
        )
    assert not (run_dir(tmp_path) / pd.CHECKPOINT_DIR).exists()

    resumed = await execute(data["flat"], tmp_path, "2024-02", {}, cost_usd=COST, resume=True)

    assert resumed.meta.ending is pd.RunEnding.COMPLETED
    assert min(cycle_stamps(resumed.database)) == utc(2)
    assert resumed.meta.abandoned_cost_usd == 10 * resumed.meta.months[0].cost_usd / 29
    assert max(cycle_stamps(abandoned_files(tmp_path)[0])) == utc(2, 10)


async def test_a_database_killed_before_any_meta_can_continue(
    data: dict[str, Path], tmp_path: Path
) -> None:
    """Nothing but the file: a process killed in its first month, or before the schema existed."""
    meta = (await execute(data["flat"], tmp_path / "a", "2024-02", {})).meta
    directory = pd.period_dir(meta.run_id, workspace=tmp_path / "b")
    directory.mkdir(parents=True)
    (directory / pd.PERIOD_DB).touch()

    resumed = await execute(data["flat"], tmp_path / "b", "2024-02", {}, resume=True)

    assert resumed.meta.ending is pd.RunEnding.COMPLETED
    assert resumed.meta.abandoned_cost_usd == ZERO
    assert len(abandoned_files(tmp_path / "b")) == 1


def test_a_dead_process_leaves_its_sidecars_with_the_abandoned_file(tmp_path: Path) -> None:
    """A stale `-wal` beside the restored database would be read as belonging to it, and a second
    Continue in the same second must not overwrite the first's record."""
    database = tmp_path / pd.PERIOD_DB
    clock = ManualClock(utc(1))
    for generation in (b"first", b"second"):
        database.write_bytes(generation)
        (tmp_path / f"{pd.PERIOD_DB}-wal").write_bytes(generation)
        pd._abandon(database, clock)

    assert not database.exists()
    assert not (tmp_path / f"{pd.PERIOD_DB}-wal").exists()
    first = tmp_path / f"{pd.PERIOD_DB}.abandoned-20240101T000000Z"
    second = tmp_path / f"{first.name}-2"
    assert first.read_bytes() == b"first"
    assert Path(f"{first}-wal").read_bytes() == b"first"
    assert second.read_bytes() == b"second"
    assert Path(f"{second}-wal").read_bytes() == b"second"


async def test_a_meta_with_no_database_is_a_continue_that_died_between_its_moves(
    data: dict[str, Path], tmp_path: Path, uninterrupted: Uninterrupted
) -> None:
    whole = await uninterrupted(data["flat"], "quiet", {})
    await killed_after(data["flat"], tmp_path, "2024-01", {})
    gc.collect()  # the dead wiring's connections: see `execute`
    (run_dir(tmp_path) / pd.PERIOD_DB).unlink()

    with pytest.raises(ConfigError, match="Continue"):
        await execute(data["flat"], tmp_path, QUARTER, {}, cost_usd=COST)
    resumed = await execute(data["flat"], tmp_path, QUARTER, {}, cost_usd=COST, resume=True)

    assert resumed.meta.months == whole.meta.months


async def test_killed_after_the_last_month_finishes_without_redoing_anything(
    data: dict[str, Path], tmp_path: Path, uninterrupted: Uninterrupted
) -> None:
    """No month is left to spend on, so a spent budget refuses nothing here (§5.5)."""
    whole = await uninterrupted(data["flat"], "quiet", {})
    await killed_after(data["flat"], tmp_path, "2024-03", {})

    resumed = await execute(
        data["flat"], tmp_path, QUARTER, {}, cost_usd=COST, budget=COST, resume=True
    )

    assert resumed.meta.ending is pd.RunEnding.COMPLETED
    assert resumed.meta.months == whole.meta.months
    assert not resumed.plan.calls


async def test_a_rule_ended_run_refuses_continue(data: dict[str, Path], tmp_path: Path) -> None:
    async def trip(worker: BasketWorker) -> None:
        if worker._clock.now() == utc(1, 25):
            await worker._watchdog.trip("drawdown", "injected")

    first = await execute(data["flat"], tmp_path, QUARTER, {}, before_cycle=trip)
    assert first.meta.ending is pd.RunEnding.RULE_ENDED

    with pytest.raises(ConfigError, match=r"rule.*kill switch.*2024-01-25"):
        await execute(data["flat"], tmp_path, QUARTER, {}, resume=True)

    assert not abandoned_files(tmp_path)


async def test_a_rule_verdict_survives_a_shutdown_that_raises(
    data: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def trip(worker: BasketWorker) -> None:
        if worker._clock.now() == utc(1, 25):
            await worker._watchdog.trip("drawdown", "injected")

    real = Application.shutdown

    async def shutdown(self: Application) -> None:
        await real(self)
        raise RuntimeError("injected shutdown failure")

    monkeypatch.setattr(Application, "shutdown", shutdown)

    with pytest.raises(RuntimeError, match="injected shutdown failure"):
        await execute(data["flat"], tmp_path, QUARTER, {}, before_cycle=trip)

    assert stored_meta(tmp_path).ending is pd.RunEnding.RULE_ENDED


async def test_continuing_a_completed_run_reuses_it(
    data: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = await execute(data["flat"], tmp_path, "2024-02", {})

    async def refuse(**_: object) -> None:
        raise AssertionError("a finished run was wired again")

    monkeypatch.setattr(pd, "build_sim", refuse)

    assert (await execute(data["flat"], tmp_path, "2024-02", {}, resume=True)).meta == first.meta


def with_changed_seat(candidate: Candidate) -> Candidate:
    panel = candidate.basket.panel
    seat = panel.seats[0].model_copy(update={"instruction": "Trust nothing."})
    changed: Basket = candidate.basket.model_copy(
        update={"panel": panel.model_copy(update={"seats": (seat, *panel.seats[1:])})}
    )
    return replace(candidate, basket=changed)


async def test_continue_after_a_seat_change_starts_a_new_run(
    data: dict[str, Path], tmp_path: Path
) -> None:
    """Review Focus 2: identity differs, so the old directory is untouched."""
    await killed_after(data["flat"], tmp_path, "2024-01", {})
    old = run_dir(tmp_path)
    gc.collect()  # the dead wiring's connections, which touch the file as they close
    before = {path.name: path.stat().st_mtime_ns for path in old.iterdir()}

    resumed = await execute(
        data["flat"],
        tmp_path,
        QUARTER,
        {},
        resume=True,
        candidate=with_changed_seat(candidate_over(data["flat"], 2)),
    )

    assert resumed.database.parent != old
    assert [m.label for m in resumed.meta.months] == ["2024-01", "2024-02", "2024-03"]
    assert {path.name: path.stat().st_mtime_ns for path in old.iterdir()} == before


async def test_an_interrupted_run_refuses_without_continue_and_names_it(
    data: dict[str, Path], tmp_path: Path
) -> None:
    await killed_after(data["flat"], tmp_path, "2024-01", {})
    before = sorted(path.name for path in run_dir(tmp_path).iterdir())

    with pytest.raises(ConfigError, match="Continue"):
        await execute(data["flat"], tmp_path, QUARTER, {})

    assert sorted(path.name for path in run_dir(tmp_path).iterdir()) == before


async def test_continue_across_a_boundary_after_a_paused_month_still_lifts(
    data: dict[str, Path], tmp_path: Path, uninterrupted: Uninterrupted
) -> None:
    whole = await uninterrupted(data["flat"], "pause", TWO_LOSSES)
    assert whole.meta.months[0].paused_at == PAUSED_ON
    assert whole.meta.lifts

    await killed_after(data["flat"], tmp_path, "2024-01", TWO_LOSSES)
    resumed = await execute(data["flat"], tmp_path, QUARTER, TWO_LOSSES, cost_usd=COST, resume=True)

    assert_same_outcomes(resumed, whole)
    assert resumed.meta.lifts == whole.meta.lifts


async def test_a_budget_stop_on_a_paused_month_still_lifts_on_continue(
    data: dict[str, Path], tmp_path: Path, uninterrupted: Uninterrupted
) -> None:
    whole = await uninterrupted(data["flat"], "pause", TWO_LOSSES)

    stopped = await execute(data["flat"], tmp_path, QUARTER, TWO_LOSSES, cost_usd=COST, budget=COST)
    assert stopped.meta.ending is pd.RunEnding.INTERRUPTED
    assert stopped.meta.months[-1].paused_at == PAUSED_ON
    assert stopped.meta.lifts == ()

    resumed = await execute(data["flat"], tmp_path, QUARTER, TWO_LOSSES, cost_usd=COST, resume=True)

    assert_same_outcomes(resumed, whole)


@pytest.mark.parametrize("kill_after", ["2024-01", "2024-02"])
async def test_a_restored_run_lifts_and_restores_the_limit_as_the_unbroken_one_did(
    data: dict[str, Path], tmp_path: Path, uninterrupted: Uninterrupted, kill_after: str
) -> None:
    """Before the lift, and after the win handed the limit back: the stored version is what runs,
    so a Continue that republished the candidate's basket would disagree on both."""
    whole = await uninterrupted(data["rise"], "win", WIN_AFTER_A_LIFT)
    pinned = limits_pinned(whole.database)
    assert (pinned[utc(2, 5)], pinned[utc(2, 15)]) == (4, 2)

    await killed_after(data["rise"], tmp_path, kill_after, WIN_AFTER_A_LIFT)
    resumed = await execute(
        data["rise"], tmp_path, QUARTER, WIN_AFTER_A_LIFT, cost_usd=COST, resume=True
    )

    assert_same_outcomes(resumed, whole)


async def test_a_halted_month_is_redone_from_the_month_before(
    data: dict[str, Path], tmp_path: Path, uninterrupted: Uninterrupted
) -> None:
    """A month cut short is no last good point: it writes no checkpoint and its row is dropped."""
    whole = await uninterrupted(data["flat"], "quiet", {})

    async def halt(worker: BasketWorker) -> None:
        if worker._clock.now() == utc(2, 10):
            await worker._watchdog.halt_basket(worker.basket_id, "injected: not a loss streak")

    stopped = await execute(data["flat"], tmp_path, QUARTER, {}, cost_usd=COST, before_cycle=halt)
    assert stopped.meta.ending is pd.RunEnding.INTERRUPTED
    assert [m.label for m in stopped.meta.months] == ["2024-01", "2024-02"]
    written = run_dir(tmp_path) / pd.CHECKPOINT_DIR
    assert [path.name for path in written.iterdir()] == ["2024-01"]

    resumed = await execute(data["flat"], tmp_path, QUARTER, {}, cost_usd=COST, resume=True)

    assert resumed.meta.months == whole.meta.months
    assert resumed.meta.ending is pd.RunEnding.COMPLETED


async def test_a_checkpoint_that_cannot_be_written_interrupts_the_run(
    data: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_: object, **__: object) -> None:
        raise BackupError("injected: no room")

    monkeypatch.setattr(pd, "take_backup", refuse)

    with pytest.raises(BackupError, match="no room"):
        await execute(data["flat"], tmp_path, QUARTER, {})

    assert stored_meta(tmp_path).ending is pd.RunEnding.INTERRUPTED
    assert not (run_dir(tmp_path) / pd.CHECKPOINT_DIR / "2024-01").exists()


async def test_each_month_end_writes_a_whole_checkpoint(
    data: dict[str, Path], tmp_path: Path
) -> None:
    ran = await execute(data["flat"], tmp_path, QUARTER, {})
    root = run_dir(tmp_path) / pd.CHECKPOINT_DIR

    assert sorted(path.name for path in root.iterdir()) == ["2024-01", "2024-02", "2024-03"]
    january = root / "2024-01"
    assert sorted(path.name for path in january.iterdir()) == sorted(
        [pd.CHECKPOINT_ARMED, pd.PERIOD_DB, pd.PERIOD_META]
    )
    meta = pd.PeriodMeta.model_validate_json((january / pd.PERIOD_META).read_text(encoding="utf-8"))
    assert [m.label for m in meta.months] == ["2024-01"]
    assert max(cycle_stamps(january / pd.PERIOD_DB)) == utc(1, 31)
    assert ran.meta.ending is pd.RunEnding.COMPLETED


def test_restore_latest_finds_nothing_in_an_empty_run(tmp_path: Path) -> None:
    assert pd.restore_latest(tmp_path) is None


async def test_restore_latest_refuses_over_a_live_database(
    data: dict[str, Path], tmp_path: Path
) -> None:
    await execute(data["flat"], tmp_path, "2024-02", {})

    with pytest.raises(FailClosedError, match="still in place"):
        pd.restore_latest(run_dir(tmp_path))


def _vetoes_of(risk_events: dict[str, int]) -> set[str]:
    evidence = cast(ProfitEvidence, SimpleNamespace(risk_events=risk_events))
    return {f"{row.rule}/{row.action_taken}" for row in pd._vetoes(evidence)}


def test_only_what_refuses_nothing_leaves_the_veto_table() -> None:
    assert _vetoes_of(
        {
            "valuation_basis/recorded": 1,
            "tracked_group/group_retired": 1,
            "tracked_group/group_closed": 1,
            "tracked_group/group_lapsed": 1,
            "max_consecutive_losses/veto": 2,
        }
    ) == {"max_consecutive_losses/veto"}


def test_a_new_action_under_either_rule_stays_on_the_veto_table() -> None:
    """The exclusion is by what the row did, so a refusal could not hide under the rule's name."""
    rows = {"tracked_group/vetoed": 1, "valuation_basis/vetoed": 1}

    assert _vetoes_of(rows) == set(rows)


def test_the_excluded_actions_are_the_ones_the_bot_emits() -> None:
    """Read from the source: the table above is a claim about these two emitters on every run."""
    monitor = Path(monitor_module.__file__).read_text(encoding="utf-8")
    startup = Path(startup_module.__file__).read_text(encoding="utf-8")

    assert (
        set(re.findall(r'_release\(\s*group,\s*action="(\w+)"', monitor))
        == (pd._NOT_REFUSALS[monitor_module.TRACKED_GROUP_RULE])
    )
    around = startup[startup.index('rule="valuation_basis"') :]
    assert re.search(r'action="(\w+)"', around)[1] in pd._NOT_REFUSALS["valuation_basis"]  # type: ignore[index]
