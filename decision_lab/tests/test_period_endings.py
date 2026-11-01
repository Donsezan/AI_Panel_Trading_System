"""How a period run ends: a rule is a verdict, an error is not (period-evaluation spec §5.3, §5.7).

Scripted as `test_period_pause.py` is, so every ending lands on a date this file can name. The kill
switch is tripped through the real `Watchdog` rather than by a real drawdown: what is under test is
what the run does about a tripped switch, whichever rule tripped it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from decision_lab import period as pd
from decision_lab.tests import factories as f
from decision_lab.tests.test_period_pause import (
    PAUSED_ON,
    TWO_LOSSES,
    _verified,
    candidate_over,
    execute,
    utc,
)
from decision_lab.tests.test_period_run import cycle_stamps
from tradebot.app import Application
from tradebot.control.basket_runner import BasketRunner
from tradebot.control.supervisor import BasketWorker
from tradebot.core.clock import ManualClock
from tradebot.core.config import Basket
from tradebot.core.errors import ConfigError
from tradebot.core.money import ZERO
from tradebot.core.portfolio import Position
from tradebot.ledger.portfolio import Ledger


@pytest.fixture(scope="module")
def flat_data(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("endings")
    return _verified(root / "history", root / "lock", drop_on=None)


def trip_on(moment: datetime) -> Callable[[BasketWorker], Awaitable[None]]:
    async def trip(worker: BasketWorker) -> None:
        if worker._clock.now() == moment:
            await worker._watchdog.trip("drawdown", "injected")

    return trip


async def test_a_kill_switch_trip_is_final_and_names_the_rule(
    flat_data: Path, tmp_path: Path
) -> None:
    ran = await execute(flat_data, tmp_path / "w", "2024-Q1", {}, before_cycle=trip_on(utc(2, 10)))

    assert ran.meta.ending is pd.RunEnding.RULE_ENDED
    assert "kill switch" in ran.meta.ending_reason
    assert "2024-02-10" in ran.meta.ending_reason
    assert "drawdown: injected" in ran.meta.ending_reason
    assert not ran.meta.lifts
    assert pd.not_run_months(ran.meta) == ("2024-03",)
    assert [m.label for m in ran.meta.months] == ["2024-01", "2024-02"]
    # No cycle after the trip, or later months would read as run under a switch nobody cleared.
    assert max(cycle_stamps(ran.database)) == utc(2, 10)


async def test_a_kill_switch_during_a_stretch_ends_the_run_too(
    flat_data: Path, tmp_path: Path
) -> None:
    """The stretch cycles on BLOCKED and QUARANTINED outcomes, which `_runnable` never objects to:
    left to the worker, every later month would report all its cycles run and the run COMPLETED."""
    ran = await execute(
        flat_data, tmp_path / "w", "2024-Q1", TWO_LOSSES, before_cycle=trip_on(utc(1, 25))
    )

    assert ran.meta.ending is pd.RunEnding.RULE_ENDED
    assert "kill switch" in ran.meta.ending_reason
    assert ran.meta.lifts == ()
    assert [m.paused_at for m in ran.meta.months] == [PAUSED_ON]
    assert pd.not_run_months(ran.meta) == ("2024-02", "2024-03")
    assert max(cycle_stamps(ran.database)) == utc(1, 25)


async def test_out_of_money_ends_the_run_as_a_rule(flat_data: Path, tmp_path: Path) -> None:
    ran = await execute(flat_data, tmp_path / "w", "2024-Q1", {}, start_equity=Decimal(4))

    assert ran.meta.ending is pd.RunEnding.RULE_ENDED
    assert "out of money" in ran.meta.ending_reason
    assert ran.meta.months[0].ran_cycles == 1
    assert pd.not_run_months(ran.meta) == ("2024-02", "2024-03")


def _reading(cash: str, *positions: Position) -> Application:
    ledger = Ledger(ManualClock(utc(1)), venue="binance", balances={"USDT": Decimal(cash)})
    for position in positions:
        ledger.adopt_position(position)
    return cast(Application, SimpleNamespace(ledger=ledger, quote_currency="USDT"))


def _basket(data: Path) -> Basket:
    return candidate_over(data, 2).basket


@pytest.mark.parametrize(
    ("cash", "expected"),
    [("4.99", True), ("0", True), ("5", False), ("5000", False)],
)
def test_flat_is_out_of_money_only_below_the_minimum(
    flat_data: Path, cash: str, expected: bool
) -> None:
    assert pd.out_of_money(_reading(cash), _basket(flat_data)) is expected


def test_low_cash_while_holding_is_not_out_of_money(flat_data: Path) -> None:
    held = Position(
        instrument_key=f.instrument().key, qty=Decimal(1), avg_entry=Decimal(100), opened_at=utc(1)
    )

    assert not pd.out_of_money(_reading("0", held), _basket(flat_data))


def test_a_position_closed_flat_does_not_count_as_holding(flat_data: Path) -> None:
    """The ledger keeps a closed position as a row of zero."""
    flat = Position(instrument_key=f.instrument().key, qty=ZERO)

    assert pd.out_of_money(_reading("0", flat), _basket(flat_data))


async def test_budget_is_checked_at_month_end_and_is_resumable(
    flat_data: Path, tmp_path: Path
) -> None:
    ran = await execute(
        flat_data,
        tmp_path / "w",
        "2024-Q1",
        {},
        budget=Decimal("0.01"),
        cost_usd=Decimal("0.01"),
    )

    assert ran.meta.ending is pd.RunEnding.INTERRUPTED
    assert "budget" in ran.meta.ending_reason
    assert len(ran.meta.months) == 1
    # Checked at the boundary and not before: all of January ran, overshooting the ceiling (§5.5).
    assert ran.meta.months[0].cost_usd > ran.meta.budget
    assert max(cycle_stamps(ran.database)) == utc(1, 31)
    assert pd.not_run_months(ran.meta) == ("2024-02", "2024-03")


async def test_a_budget_passed_in_the_last_month_stops_nothing(
    flat_data: Path, tmp_path: Path
) -> None:
    """No month is left to refuse, so nothing is interrupted and nothing is there to continue."""
    ran = await execute(
        flat_data,
        tmp_path / "w",
        "2024-02",
        {},
        budget=Decimal("0.01"),
        cost_usd=Decimal("0.01"),
    )

    assert ran.meta.months[0].cost_usd > ran.meta.budget
    assert ran.meta.ending is pd.RunEnding.COMPLETED


async def test_a_failed_cycle_halt_is_interrupted_not_a_verdict(
    flat_data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = BasketRunner.run_once

    async def fail_in_february(self: BasketRunner) -> object:
        if self._clock.now() >= utc(2, 10):
            raise RuntimeError("injected defect")
        return await real(self)

    monkeypatch.setattr(BasketRunner, "run_once", fail_in_february)

    ran = await execute(flat_data, tmp_path / "w", "2024-Q1", {})

    assert ran.meta.ending is pd.RunEnding.INTERRUPTED
    assert "3 consecutive failed cycles" in ran.meta.ending_reason
    assert pd.not_run_months(ran.meta) == ("2024-03",)


async def test_an_unexpected_exception_is_written_and_raised(
    flat_data: Path, tmp_path: Path
) -> None:
    async def explode(worker: BasketWorker) -> None:
        if worker._clock.now() == utc(2, 10):
            raise RuntimeError("injected escape")

    with pytest.raises(RuntimeError, match="injected escape"):
        await execute(flat_data, tmp_path / "w", "2024-Q1", {}, before_cycle=explode)

    written = next((tmp_path / "w").glob(f"period-*/{pd.PERIOD_META}"))
    meta = pd.PeriodMeta.model_validate_json(written.read_text(encoding="utf-8"))
    assert meta.ending is pd.RunEnding.INTERRUPTED
    assert "injected escape" in meta.ending_reason
    # February never reached its month end, so the last good point is January.
    assert [m.label for m in meta.months] == ["2024-01"]


async def test_a_rule_ended_run_is_reused_at_its_identity(
    flat_data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = await execute(
        flat_data, tmp_path / "w", "2024-Q1", {}, before_cycle=trip_on(utc(1, 25))
    )

    async def refuse(**_: object) -> None:
        raise AssertionError("a finished run was wired again")

    monkeypatch.setattr(pd, "build_sim", refuse)

    again = await execute(flat_data, tmp_path / "w", "2024-Q1", {})
    assert again.meta == first.meta


async def test_an_interrupted_run_refuses_and_says_why(flat_data: Path, tmp_path: Path) -> None:
    async def halt(worker: BasketWorker) -> None:
        if worker._clock.now() == utc(1, 25):
            await worker._watchdog.halt_basket(worker.basket_id, "injected: not a loss streak")

    await execute(flat_data, tmp_path / "w", "2024-Q1", {}, before_cycle=halt)

    with pytest.raises(ConfigError, match=r"interrupted.*injected: not a loss streak"):
        await execute(flat_data, tmp_path / "w", "2024-Q1", {})


def test_not_run_months_are_derived_from_the_period() -> None:
    period = pd.parse_period("2024-Q1")
    meta = SimpleNamespace(period=period, months=(SimpleNamespace(label="2024-01"),))

    assert pd.not_run_months(cast(pd.PeriodMeta, meta)) == ("2024-02", "2024-03")
