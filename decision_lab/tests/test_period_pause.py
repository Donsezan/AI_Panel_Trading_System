"""A loss-streak pause ends the month, not the run (period-evaluation spec §5.3, §5.4).

Nothing here depends on luck. The prices are written out and the panel's votes are scripted per
day and per symbol, so each round trip loses exactly what fees and the crossed spread cost it.

**The held-stop scenarios trade two instruments, and that is forced.** A round trip closes only
when a position goes flat, so on `PERIOD_SYMBOL` alone the streak reaches its limit while flat, and
the next BUY is what is vetoed: a one-instrument pause normally holds nothing. The streak is counted
per basket rather than per instrument, so a second instrument's losses are the real route to a pause
over an open position. `period_basket` is widened for those runs only.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from decision_lab import candidates as cd
from decision_lab import cli
from decision_lab import period as pd
from decision_lab.candidates import Candidate
from decision_lab.records import records_from_store
from decision_lab.tests import factories as f
from decision_lab.tests.test_period_run import read_log, with_loss_limit
from tradebot.app import dataset_basket, select_panel
from tradebot.control.config_store import ConfigStore
from tradebot.control.supervisor import BasketWorker
from tradebot.core.clock import ManualClock
from tradebot.core.config import Basket, ConfigRef, Schedule
from tradebot.core.enums import ConfigKind, CycleOutcome
from tradebot.core.events import EventType
from tradebot.core.market import Candle
from tradebot.core.money import ZERO
from tradebot.decision.providers import registry
from tradebot.decision.providers.stub import DEFAULT_RESPONSE, StubLLMProvider
from tradebot.interfaces.llm import CompletionRequest
from tradebot.marketdata.recorder import ReplayDataset
from tradebot.persistence.database import SingleWriter, open_database
from tradebot.persistence.store import EventStore

#: Daily, so a quarter costs about ninety cycles. The tick reads the same bar of the oscillation
#: every day, so a flat price is flat at every decision and only costs can decide a round trip.
CADENCE = 86_400
START_EQUITY = Decimal(10_000)
DAYS = 91
BTC = "BTC/USDT"
ETH = "ETH/USDT"
#: Below an entry's stop of about 99.55 and above its limit of about 99.05: the bars here have no
#: range, so a price past the limit would arm the stop and then never fill it.
THROUGH_THE_STOP = "99.3"
DROP_DAY = date(2024, 1, 15)

Votes = Mapping[tuple[str, date], str]


def day(month: int, number: int) -> date:
    return date(2024, month, number)


def utc(month: int, number: int = 1) -> datetime:
    return datetime(2024, month, number, tzinfo=UTC)


def _oscillation(*, drop_on: date | None = None) -> tuple[Candle, ...]:
    """A flat market with a real ATR, optionally stepping down through a stop on one day."""
    closes = []
    for hour in range(DAYS * 24):
        when = f.EPOCH + timedelta(hours=hour)
        dropped = drop_on is not None and when.date() >= drop_on
        low, high = (THROUGH_THE_STOP, "99.8") if dropped else ("100", "100.5")
        # Held at the low all through the drop day, so the stop's trigger is plainly crossed.
        closes.append(low if hour % 2 == 0 or when.date() == drop_on else high)
    return f.walk(closes)


class Plan(StubLLMProvider):
    """Answers by the date and the symbol in the prompt. Everything unnamed is a WAIT."""

    def __init__(self, votes: Votes) -> None:
        super().__init__(provider_id="stub")
        self._votes = votes

    def _text_for(self, request: CompletionRequest) -> str:
        vote = json.loads(DEFAULT_RESPONSE)
        vote["action"] = self._votes.get((symbol_of(request), as_of(request).date()), "WAIT")
        # A half-sized SELL would leave the round trip open, and only a closed one is a loss.
        vote["size_hint"] = "full"
        return json.dumps(vote)

    def calls_between(self, start: datetime, end: datetime) -> list[CompletionRequest]:
        return [call for call in self.calls if start <= as_of(call) < end]


def as_of(request: CompletionRequest) -> datetime:
    line = next(one for one in request.user.splitlines() if one.startswith("As of: "))
    return datetime.fromisoformat(line.removeprefix("As of: ").split(" ")[0])


def symbol_of(request: CompletionRequest) -> str:
    line = next(one for one in request.user.splitlines() if one.startswith("Instrument: "))
    return line.removeprefix("Instrument: ").split(" ")[0]


@dataclass(frozen=True)
class Ran:
    meta: pd.PeriodMeta
    database: Path
    plan: Plan


def _verified(directory: Path, lock: Path, *, drop_on: date | None) -> Path:
    f.write_dataset(
        directory,
        {
            (f.instrument(), "1h"): _oscillation(drop_on=drop_on),
            (f.instrument(ETH), "1h"): _oscillation(),
        },
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("DECISION_LAB_WORKSPACE", str(lock))
        assert cli.main(["dataset", "verify", "--data", str(directory)]) == cli.EXIT_OK
    return directory


@pytest.fixture(scope="module")
def flat_data(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("pause-flat")
    return _verified(root / "history", root / "lock", drop_on=None)


@pytest.fixture(scope="module")
def dropping_data(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("pause-drop")
    return _verified(root / "history", root / "lock", drop_on=DROP_DAY)


def candidate_over(data: Path, limit: int) -> Candidate:
    dataset = ReplayDataset.load(data, ManualClock(utc(1)))
    reference = dataset_basket(
        dataset, select_panel("sim"), basket_id="reference", every_seconds=CADENCE
    )
    loaded = cd.load_matrix(f.stub_matrix_file(), reference=reference).candidates[0]
    return with_loss_limit(loaded, limit)


def both_instruments(candidate: Candidate, dataset: ReplayDataset, cadence: int) -> Basket:
    """`period_basket`, minus the one line that would make a held pause unreachable."""
    return candidate.basket.model_copy(
        update={
            "basket_id": "period",
            "instruments": dataset.instruments,
            "timeframes": dataset.timeframes,
            "schedule": Schedule(every_seconds=cadence),
        }
    )


async def execute(
    data: Path,
    workspace: Path,
    label: str,
    votes: Votes,
    *,
    limit: int = 2,
    two_instruments: bool = False,
    before_cycle: Callable[[BasketWorker], Awaitable[None]] | None = None,
) -> Ran:
    plan = Plan(votes)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(registry, "StubLLMProvider", lambda **_: plan)
        if two_instruments:
            patch.setattr(pd, "period_basket", both_instruments)
        if before_cycle is not None:
            real = BasketWorker.cycle

            async def cycle(self: BasketWorker) -> object:
                await before_cycle(self)
                return await real(self)

            patch.setattr(BasketWorker, "cycle", cycle)
        meta = await pd.run(
            data_dir=data,
            candidate=candidate_over(data, limit),
            period=pd.parse_period(label),
            cadence_seconds=CADENCE,
            start_equity=START_EQUITY,
            budget=Decimal(5),
            wall_clock=ManualClock(datetime(2026, 11, 1, tzinfo=UTC)),
            workspace=workspace,
        )
    return Ran(meta, pd.period_dir(meta.run_id, workspace=workspace) / pd.PERIOD_DB, plan)


#: Two losing round trips against a limit of two, then a BUY the rule vetoes. Cooldown is respected.
TWO_LOSSES = {
    (BTC, day(1, 10)): "BUY",
    (BTC, day(1, 13)): "SELL",
    (BTC, day(1, 16)): "BUY",
    (BTC, day(1, 19)): "SELL",
    (BTC, day(1, 22)): "BUY",
}
PAUSED_ON = utc(1, 22)
#: The same again in February, under the lifted limit of four.
TWO_MORE_LOSSES = {
    (BTC, day(2, 5)): "BUY",
    (BTC, day(2, 8)): "SELL",
    (BTC, day(2, 11)): "BUY",
    (BTC, day(2, 14)): "SELL",
    (BTC, day(2, 17)): "BUY",
}
#: BTC stays open while ETH loses, so the veto on the second ETH BUY pauses over a held position.
HELD_THROUGH_THE_DROP = {
    (BTC, day(1, 6)): "BUY",
    (ETH, day(1, 7)): "BUY",
    (ETH, day(1, 10)): "SELL",
    (ETH, day(1, 13)): "BUY",
}
HELD_PAUSED_ON = utc(1, 13)
#: As above, entered after the drop, so nothing fires and the position is still open at month end.
HELD_TO_MONTH_END = {
    (BTC, day(1, 17)): "BUY",
    (ETH, day(1, 18)): "BUY",
    (ETH, day(1, 21)): "SELL",
    (ETH, day(1, 24)): "BUY",
}


class Scenarios:
    """Each run is paid for once. Votes are not in its identity, so each has its own directory."""

    def __init__(self, root: Path, flat: Path, dropping: Path) -> None:
        self._root, self._flat, self._dropping = root, flat, dropping
        self._done: dict[str, Ran] = {}

    async def _once(self, name: str, make: Callable[[Path], Awaitable[Ran]]) -> Ran:
        if name not in self._done:
            self._done[name] = await make(self._root / name)
        return self._done[name]

    async def paused_in_january(self) -> Ran:
        return await self._once(
            "january", lambda at: execute(self._flat, at, "2024-Q1", TWO_LOSSES)
        )

    async def paused_twice(self) -> Ran:
        votes = {**TWO_LOSSES, **TWO_MORE_LOSSES}
        return await self._once("twice", lambda at: execute(self._flat, at, "2024-Q1", votes))

    async def held_through_the_drop(self) -> Ran:
        return await self._once(
            "held-stop",
            lambda at: execute(
                self._dropping,
                at,
                "2024-01",
                HELD_THROUGH_THE_DROP,
                limit=1,
                two_instruments=True,
            ),
        )

    async def held_to_month_end(self) -> Ran:
        return await self._once(
            "held-end",
            lambda at: execute(
                self._dropping, at, "2024-01", HELD_TO_MONTH_END, limit=1, two_instruments=True
            ),
        )


@pytest.fixture(scope="module")
def scenarios(
    tmp_path_factory: pytest.TempPathFactory, flat_data: Path, dropping_data: Path
) -> Scenarios:
    return Scenarios(tmp_path_factory.mktemp("pause-runs"), flat_data, dropping_data)


def sells_between(database: Path, start: datetime, end: datetime) -> list[datetime]:
    def read(store: EventStore) -> list[datetime]:
        rows = (e.payload["fill"] for e in store.read_types(EventType.FILL_RECEIVED))
        stamps = (
            (datetime.fromisoformat(row["filled_at"]), row["side"], row["instrument_key"])
            for row in rows
        )
        return [
            at
            for at, side, key in stamps
            if side == "sell" and key == f.instrument().key and start <= at < end
        ]

    return read_log(database, read)


def limits_pinned(database: Path) -> dict[datetime, int]:
    """The `max_consecutive_losses` each cycle ran under, resolved from the version it pinned."""
    engine = open_database(database)
    writer = SingleWriter(engine)
    try:
        store = EventStore(engine, writer)
        configs = ConfigStore(engine, writer, store, ManualClock(utc(1)))
        pinned = {}
        for event in store.read_types(EventType.CYCLE_STARTED):
            ref = ConfigRef(
                kind=ConfigKind.BASKET,
                config_id="period",
                version=event.payload["config_versions"]["basket:period"],
            )
            pinned[event.ts] = configs.at(ref).document.risk_policy.max_consecutive_losses
        return pinned
    finally:
        writer.close()
        engine.dispose()


async def test_a_pause_stops_panel_calls_but_lets_a_held_stop_fire(scenarios: Scenarios) -> None:
    """The plan's stop condition: a stop not firing here would make §5.4's mechanism wrong."""
    ran = await scenarios.held_through_the_drop()

    month = ran.meta.months[0]
    assert month.paused_at == HELD_PAUSED_ON
    assert not ran.plan.calls_between(HELD_PAUSED_ON + timedelta(seconds=1), utc(2))
    assert sells_between(ran.database, HELD_PAUSED_ON, utc(2)), "the resting stop never fired"
    assert not sells_between(ran.database, utc(1), HELD_PAUSED_ON)
    # Any loss in that stretch counts (§5.4): both round trips' costs and the stop's own loss.
    assert ZERO < month.end_equity < START_EQUITY


async def test_the_quarantined_stretch_is_neither_scored_nor_counted(scenarios: Scenarios) -> None:
    ran = await scenarios.paused_in_january()

    # Every cycle the panel answered, read from the panel's side rather than from the same fold.
    answered = {as_of(call) for call in ran.plan.calls_between(utc(1), utc(2))}
    assert ran.meta.months[0].ran_cycles == len(answered)
    assert max(answered) == PAUSED_ON

    records = read_log(ran.database, records_from_store)
    stretch = [r for r in records if r.outcome == CycleOutcome.QUARANTINED.value]
    assert [r.as_of for r in stretch] == [utc(1, n) for n in range(23, 32)]
    # `score_records` scores decisions, so a cycle that holds none has nothing to be scored on.
    assert all(not r.decisions and not r.responses for r in stretch)
    assert all(r.cost_usd == ZERO for r in stretch)


async def test_a_paused_month_end_values_rather_than_unvaluable(scenarios: Scenarios) -> None:
    """Held across the pause and still open: only the stretch's marks can value this."""
    ran = await scenarios.held_to_month_end()

    month = ran.meta.months[0]
    assert month.paused_at == utc(1, 24)
    assert not month.unvaluable
    assert not ran.meta.profit.unvaluable
    assert month.end_equity > ZERO
    assert ran.meta.profit.unrealized != ZERO, "nothing was held, so nothing here needed a mark"


async def test_the_next_month_trades_with_the_allowance_restored(scenarios: Scenarios) -> None:
    ran = await scenarios.paused_in_january()

    assert ran.meta.months[0].paused_at == PAUSED_ON
    assert ran.meta.months[1].paused_at is None
    assert len(ran.meta.lifts) == 1
    lift = ran.meta.lifts[0]
    assert lift == pd.PauseLift(at=utc(2), streak=2, new_limit=4)
    assert ran.meta.months[1].ran_cycles == ran.meta.months[1].planned_cycles
    assert ran.meta.months[2].ran_cycles == ran.meta.months[2].planned_cycles
    assert len({as_of(c) for c in ran.plan.calls_between(utc(2), utc(3))}) == 29

    pinned = limits_pinned(ran.database)
    assert {limit for at, limit in pinned.items() if at < lift.at} == {2}
    assert {limit for at, limit in pinned.items() if at >= lift.at} == {lift.new_limit}

    def lifted(store: EventStore) -> list[dict[str, object]]:
        return [
            e.payload
            for e in store.read_types(EventType.RISK_EVENT)
            if e.payload["rule"] == pd.PAUSE_LIFTED_RULE
        ]

    events = read_log(ran.database, lifted)
    assert len(events) == 1
    assert "streak 2" in str(events[0]["detail"])
    assert "limit 4" in str(events[0]["detail"])


async def test_the_lift_is_published_under_this_tools_own_name(scenarios: Scenarios) -> None:
    ran = await scenarios.paused_in_january()

    def published(store: EventStore) -> list[tuple[object, object]]:
        return [
            (e.payload["actor"], e.payload["version"])
            for e in store.read_types(EventType.CONFIG_CHANGED)
            if e.payload["config_id"] == "period"
        ]

    # The quarantine and the lift, on top of whichever version wiring published first.
    actors = [actor for actor, _ in read_log(ran.database, published)]
    assert actors[-2:] == [pd.ACTOR, pd.ACTOR]
    assert actors.count(pd.ACTOR) == 2


async def test_two_pauses_lift_twice_from_the_fresh_streak(scenarios: Scenarios) -> None:
    ran = await scenarios.paused_twice()

    assert [m.paused_at for m in ran.meta.months] == [PAUSED_ON, utc(2, 17), None]
    # Read off the streak each time and the candidate's own limit, so allowances never compound:
    # 4 + 2, not 4 + 4.
    assert ran.meta.lifts == (
        pd.PauseLift(at=utc(2), streak=2, new_limit=4),
        pd.PauseLift(at=utc(3), streak=4, new_limit=6),
    )
    assert ran.meta.months[2].ran_cycles == ran.meta.months[2].planned_cycles


async def test_a_pause_in_the_last_month_has_no_lift_and_still_values(
    scenarios: Scenarios,
) -> None:
    ran = await scenarios.held_to_month_end()

    assert ran.meta.lifts == ()
    assert ran.meta.months[0].paused_at is not None
    assert not ran.meta.months[0].unvaluable


async def test_a_pause_on_a_month_without_later_months_ends_completed(
    scenarios: Scenarios,
) -> None:
    ran = await scenarios.held_to_month_end()

    assert ran.meta.ending is pd.RunEnding.COMPLETED
    assert [m.label for m in ran.meta.months] == ["2024-01"]


async def test_a_pause_on_the_months_last_tick_lifts_with_no_stretch(
    flat_data: Path, tmp_path: Path
) -> None:
    """A pause only reaches the worker's own flag at its *next* cycle. Waiting for that flag would
    put this pause in February, and February would open halted with nothing to lift it."""
    votes = {
        (BTC, day(1, 19)): "BUY",
        (BTC, day(1, 22)): "SELL",
        (BTC, day(1, 25)): "BUY",
        (BTC, day(1, 28)): "SELL",
        (BTC, day(1, 31)): "BUY",
    }

    ran = await execute(flat_data, tmp_path / "w", "2024-Q1", votes)

    assert ran.meta.months[0].paused_at == utc(1, 31)
    assert ran.meta.months[1].paused_at is None
    assert ran.meta.lifts == (pd.PauseLift(at=utc(2), streak=2, new_limit=4),)
    assert ran.meta.months[1].ran_cycles == ran.meta.months[1].planned_cycles


async def test_an_older_pause_does_not_excuse_a_later_halt(flat_data: Path, tmp_path: Path) -> None:
    """The log still holds January's pause when February's halt arrives, for another reason. §5.3:
    that one is not what the operator agreed to lift."""

    async def halt_in_february(worker: BasketWorker) -> None:
        if worker._clock.now() == utc(2, 10):
            await worker._watchdog.halt_basket(worker.basket_id, "injected: not a loss streak")

    ran = await execute(
        flat_data, tmp_path / "w", "2024-Q1", TWO_LOSSES, before_cycle=halt_in_february
    )

    january, february, march = ran.meta.months
    assert january.paused_at == PAUSED_ON
    assert february.paused_at is None
    assert february.ran_cycles < february.planned_cycles
    assert march.ran_cycles == 0
    assert len(ran.meta.lifts) == 1


async def test_a_kill_switch_during_the_stretch_is_not_lifted_past(
    flat_data: Path, tmp_path: Path
) -> None:
    """Lifting is for the loss streak alone (§5.3): a tripped switch keeps the basket as it is."""

    async def trip_in_the_stretch(worker: BasketWorker) -> None:
        if worker._clock.now() == utc(1, 25):
            await worker._watchdog.trip("test", "injected")

    ran = await execute(
        flat_data, tmp_path / "w", "2024-Q1", TWO_LOSSES, before_cycle=trip_in_the_stretch
    )

    assert ran.meta.months[0].paused_at == PAUSED_ON
    assert ran.meta.lifts == ()
