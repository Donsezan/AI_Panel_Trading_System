"""One basket, one log, month after month (period-evaluation spec §5.1, §5.2, §5.4).

Offline: a real recorded dataset on disk and a seeded `varied-*` stub, driven through the real
`build_sim` wiring. The log the run leaves behind is read back rather than trusted through the
meta, because "the meta agrees with itself" proves nothing about what the loop did.
"""

from __future__ import annotations

import asyncio
import functools
import random
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TypeVar

import pytest

from decision_lab import candidates as cd
from decision_lab import cli
from decision_lab import period as pd
from decision_lab.candidates import Candidate
from decision_lab.tests import factories as f
from decision_lab.tests.test_slice_b_end_to_end import STUB_SEED
from tradebot.app import Application, build_sim, dataset_basket, dataset_catalogue, select_panel
from tradebot.control.basket_runner import CycleResult
from tradebot.control.supervisor import BasketWorker
from tradebot.core.clock import ManualClock
from tradebot.core.errors import ConfigError
from tradebot.core.events import EventType
from tradebot.core.money import ZERO
from tradebot.decision.providers import registry
from tradebot.decision.providers.stub import StubLLMProvider
from tradebot.marketdata.recorder import ReplayDataset
from tradebot.persistence.database import SingleWriter, open_database
from tradebot.persistence.store import EventStore
from tradebot.risk.aggregate import PortfolioAggregate
from tradebot.validation.backtest import BacktestHarness
from tradebot.validation.evidence import Evidence

T = TypeVar("T")

#: Eight-hourly. A cycle costs about 45ms through the real wiring, so this is as coarse as it can go
#: and still mean something: under `STUB_SEED` it is the grid that fills in March, stops on a limit
#: of one, and holds a position across a month end. Daily and twelve-hourly lose one of the three.
CADENCE = 28_800
START_EQUITY = Decimal(10_000)
#: January, February and March 2024: a leap year's first quarter.
DAYS = 91
#: filled_at, side, qty, price, fee. Order ids are excluded: they differ between two databases.
Fill = tuple[datetime, str, Decimal, Decimal, Decimal]
BTC_BARS = f.shocked_walk(days=DAYS, shock_up=(40, 70), shock_down=(20, 55, 80))


def utc(year: int, month: int, day: int = 1) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


def seed_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    # Re-applied before every pass: the partial holds one `Random`, so a second pass under the same
    # patch would continue the first one's draws and "the same run" would not be the same.
    monkeypatch.setattr(
        registry,
        "StubLLMProvider",
        functools.partial(StubLLMProvider, rng=random.Random(STUB_SEED)),
    )


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Two coins over one quarter, verified once. The one dataset every test here reads."""
    root = tmp_path_factory.mktemp("period")
    data = root / "history"
    other = f.shocked_walk(days=DAYS, base="50")
    f.write_dataset(
        data, {(f.instrument(), "1h"): BTC_BARS, (f.instrument("ETH/USDT"), "1h"): other}
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("DECISION_LAB_WORKSPACE", str(root / "lock"))
        assert cli.main(["dataset", "verify", "--data", str(data)]) == cli.EXIT_OK
    return data


def candidate_for(data: Path) -> Candidate:
    dataset = ReplayDataset.load(data, ManualClock(utc(2024, 1)))
    reference = dataset_basket(
        dataset, select_panel("sim"), basket_id="reference", every_seconds=CADENCE
    )
    return cd.load_matrix(f.stub_matrix_file(), reference=reference).candidates[0]


def with_loss_limit(candidate: Candidate, limit: int) -> Candidate:
    policy = candidate.basket.risk_policy.model_copy(update={"max_consecutive_losses": limit})
    return replace(candidate, basket=candidate.basket.model_copy(update={"risk_policy": policy}))


@dataclass
class Runner:
    data: Path
    workspace: Path
    monkeypatch: pytest.MonkeyPatch

    async def __call__(
        self,
        label: str,
        *,
        candidate: Candidate | None = None,
        budget: str = "5",
        archive_digest: str = "",
    ) -> pd.PeriodMeta:
        seed_stub(self.monkeypatch)
        return await pd.run(
            data_dir=self.data,
            candidate=candidate or candidate_for(self.data),
            period=pd.parse_period(label),
            cadence_seconds=CADENCE,
            start_equity=START_EQUITY,
            budget=Decimal(budget),
            wall_clock=ManualClock(utc(2026, 11)),
            archive_digest=archive_digest,
            workspace=self.workspace,
        )

    def database(self, meta: pd.PeriodMeta) -> Path:
        return pd.period_dir(meta.run_id, workspace=self.workspace) / pd.PERIOD_DB


@pytest.fixture(scope="module")
def shared_workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("period-runs")


@pytest.fixture
def run_period(data_dir: Path, shared_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> Runner:
    """A workspace the reading tests share: a finished run is reused at its identity, so each
    distinct run is paid for once. The seed is fixed, so which test pays changes nothing."""
    return Runner(data_dir, shared_workspace, monkeypatch)


@pytest.fixture
def isolated(data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Runner:
    """A workspace of the test's own, for one whose run or directory must not be shared."""
    return Runner(data_dir, tmp_path / "workspace", monkeypatch)


def read_log(database: Path, reader: Callable[[EventStore], T]) -> T:
    """The run's own database, read as a finished artefact."""
    engine = open_database(database)
    writer = SingleWriter(engine)
    try:
        return reader(EventStore(engine, writer))
    finally:
        writer.close()
        engine.dispose()


def cycle_stamps(database: Path) -> tuple[datetime, ...]:
    def read(store: EventStore) -> tuple[datetime, ...]:
        return tuple(cycle.started_at for cycle in Evidence.gather(store).cycles)

    return read_log(database, read)


def fills_in(database: Path) -> tuple[Fill, ...]:
    def read(store: EventStore) -> tuple[Fill, ...]:
        rows = (e.payload["fill"] for e in store.read_types(EventType.FILL_RECEIVED))
        return tuple(
            (
                datetime.fromisoformat(fill["filled_at"]),
                fill["side"],
                Decimal(fill["qty"]),
                Decimal(fill["price"]),
                Decimal(fill["fee"]),
            )
            for fill in rows
        )

    return read_log(database, read)


def decided_instruments(database: Path) -> set[str]:
    def read(store: EventStore) -> set[str]:
        return {
            event.payload["decision"]["instrument_key"]
            for event in store.read_types(EventType.DECISION_MADE)
        }

    return read_log(database, read)


def equity_from_log(database: Path, through: datetime) -> tuple[Decimal, Decimal]:
    """Equity and quantity held at the last cycle before `through`, rebuilt from fills and bars.

    Independent of the ledger and of `Application.valuation`: the cash each fill moved, plus what
    is still held at the close of the last bar that cycle could read.
    """
    cash = START_EQUITY
    held = ZERO
    for filled_at, side, qty, price, fee in fills_in(database):
        if filled_at < through:
            signed = qty if side == "buy" else -qty
            held += signed
            cash -= signed * price + fee
    last = max(stamp for stamp in cycle_stamps(database) if stamp < through)
    close = next(bar.close for bar in reversed(BTC_BARS) if bar.close_time <= last)
    return cash + held * close, held


async def test_only_the_selected_month_runs(run_period: Runner) -> None:
    meta = await run_period("2024-03")

    stamps = cycle_stamps(run_period.database(meta))
    assert stamps and all(utc(2024, 3) <= stamp < utc(2024, 4) for stamp in stamps)
    # The bars before March were read for indicators only. `next_tick` is strict, so this is what
    # catches a driver that loses the tick on the period's first instant (§5.2).
    assert meta.first_cycle_at == utc(2024, 3)
    assert min(stamps) == utc(2024, 3)
    assert meta.ending is pd.RunEnding.COMPLETED
    assert [month.label for month in meta.months] == ["2024-03"]


async def test_a_short_history_starts_late_and_says_so(run_period: Runner) -> None:
    meta = await run_period("2024-01")

    full_month = timedelta(days=31) // timedelta(seconds=CADENCE)
    assert meta.first_cycle_at > utc(2024, 1)
    # The whole grid, or what the warm-up cost would vanish from both sides of coverage (§5.2).
    assert meta.months[0].planned_cycles == full_month
    assert meta.months[0].ran_cycles < meta.months[0].planned_cycles


async def test_money_and_position_carry_between_months(run_period: Runner) -> None:
    meta = await run_period("2024-Q1")
    database = run_period.database(meta)

    assert [month.label for month in meta.months] == ["2024-01", "2024-02", "2024-03"]
    assert meta.months[0].start_equity == START_EQUITY
    assert meta.months[1].start_equity == meta.months[0].end_equity
    assert meta.months[2].start_equity == meta.months[1].end_equity

    # February's figure runs through January's cash and holdings: a run that reset anything at the
    # boundary would disagree with this rebuild of the whole log.
    held_at_a_boundary = False
    for index, boundary in enumerate((utc(2024, 2), utc(2024, 3))):
        expected, held = equity_from_log(database, boundary)
        assert meta.months[index].end_equity == expected
        held_at_a_boundary = held_at_a_boundary or held != ZERO
    assert held_at_a_boundary, "no position crossed a month end, so none was shown to carry"


async def test_a_tick_on_a_month_boundary_belongs_to_the_later_month(run_period: Runner) -> None:
    meta = await run_period("2024-Q1")

    every = timedelta(seconds=CADENCE)
    # Whole months on the cadence's grid: no tick lost and none doubled at either boundary.
    assert meta.months[1].planned_cycles == timedelta(days=29) // every
    assert meta.months[2].planned_cycles == timedelta(days=31) // every


async def test_btc_only_on_a_two_coin_dataset(run_period: Runner) -> None:
    meta = await run_period("2024-03")

    assert decided_instruments(run_period.database(meta)) == {f.instrument().key}


async def test_the_driver_matches_the_harness_on_one_window(
    run_period: Runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop must not drift from `BacktestHarness` (spec §5.4).

    The two windows are made identical rather than assumed so. The harness's `plan_ticks` window is
    strict at its start and inclusive at its end, and the driver's month is the reverse, so a
    harness window of `(March - 1s, April - 1s]` with no warm-up names exactly the driver's ticks,
    `[March, April)`. Both then start from the same seeded draws and read the same dataset through
    the same `build_sim` arguments. One difference is left in: the harness recovers one second
    before the driver does, on a fresh database where recovery depends on neither.
    """
    candidate = candidate_for(run_period.data)
    meta = await run_period("2024-03", candidate=candidate)
    driven = fills_in(run_period.database(meta))

    second = tmp_path / "harness.db"
    clock = ManualClock(utc(2024, 3))
    dataset = ReplayDataset.load(run_period.data, clock)
    seed_stub(monkeypatch)
    application = await build_sim(
        clock=clock,
        db_path=second,
        baskets=(pd.period_basket(candidate, dataset, CADENCE),),
        start_equity=START_EQUITY,
        market_data=dataset.market_data,
        catalogue=dataset_catalogue(dataset),
        news_sources=(),
    )
    try:
        report = await BacktestHarness(
            application,
            clock,
            start=utc(2024, 3) - timedelta(seconds=1),
            end=utc(2024, 4) - timedelta(seconds=1),
            warmup=timedelta(0),
        ).run()
    finally:
        await application.shutdown()

    assert driven, "a window with no fills would make this comparison vacuous"
    assert driven == fills_in(second)
    assert meta.months[0].ran_cycles == report.ran_cycles
    assert meta.months[0].planned_cycles == report.planned_cycles


async def test_the_same_identity_is_reused_without_spend(
    run_period: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = await run_period("2024-03")

    async def refuse(**_: object) -> None:
        raise AssertionError("a finished run was wired again")

    monkeypatch.setattr(pd, "build_sim", refuse)
    # A different budget is the same experiment: it bounds spend and changes nothing produced.
    assert await run_period("2024-03", budget="99") == first


async def test_a_changed_risk_limit_mints_a_new_run(run_period: Runner) -> None:
    candidate = candidate_for(run_period.data)
    first = await run_period("2024-03", candidate=candidate)

    second = await run_period("2024-03", candidate=with_loss_limit(candidate, 9))

    assert second.run_id != first.run_id
    assert second.basket_digest != first.basket_digest
    assert second.panel_digest == first.panel_digest


async def test_an_interrupted_database_with_no_meta_refuses(
    run_period: Runner, isolated: Runner
) -> None:
    meta = await run_period("2024-03")
    directory = pd.period_dir(meta.run_id, workspace=isolated.workspace)
    directory.mkdir(parents=True)
    (directory / pd.PERIOD_DB).touch()

    with pytest.raises(ConfigError, match="interrupted"):
        await isolated("2024-03")

    assert (directory / pd.PERIOD_DB).is_file(), "that file is the record of why the pass failed"


async def test_a_meta_still_running_is_an_interruption_not_a_result(
    run_period: Runner, isolated: Runner
) -> None:
    """The meta is written after every month, so one reading RUNNING is a run that died. Returning
    it as a result would report part of a period as the whole."""
    meta = await run_period("2024-03")
    directory = pd.period_dir(meta.run_id, workspace=isolated.workspace)
    directory.mkdir(parents=True)
    (directory / pd.PERIOD_DB).touch()
    (directory / pd.PERIOD_META).write_text(
        meta.model_copy(update={"ending": pd.RunEnding.RUNNING}).model_dump_json(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="interrupted"):
        await isolated("2024-03")


async def test_a_month_ending_frozen_is_unvaluable_with_no_figure(
    isolated: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = Application.valuation

    def frozen(self: Application) -> PortfolioAggregate:
        return real(self).model_copy(update={"frozen_reason": "no mark"})

    monkeypatch.setattr(Application, "valuation", frozen)

    meta = await isolated("2024-03")

    assert meta.months[0].unvaluable
    assert meta.months[0].end_equity == ZERO
    assert meta.profit.unvaluable
    assert meta.profit.total == ZERO


async def test_the_month_after_a_frozen_one_has_no_start_figure(
    isolated: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Carrying the frozen month's zero would read February's whole equity as February's profit."""
    real = Application.valuation

    def frozen_in_january(self: Application) -> PortfolioAggregate:
        valuation = real(self)
        if valuation.as_of < utc(2024, 2):
            return valuation.model_copy(update={"frozen_reason": "no mark"})
        return valuation

    monkeypatch.setattr(Application, "valuation", frozen_in_january)

    january, february, march = (await isolated("2024-Q1")).months

    assert january.start_equity == START_EQUITY
    assert january.unvaluable
    assert february.start_equity is None
    assert not february.unvaluable
    assert february.end_equity > ZERO
    # Valuable again, so the carry resumes rather than staying blank for the rest of the run.
    assert march.start_equity == february.end_equity


async def test_an_archive_digest_mints_a_new_run_and_lifts_news_blind(run_period: Runner) -> None:
    blind = await run_period("2024-03")

    fed = await run_period("2024-03", archive_digest="digest")

    assert fed.run_id != blind.run_id
    assert blind.news_blind
    assert not fed.news_blind


async def test_a_stop_that_is_no_loss_pause_skips_the_rest_of_the_period(
    isolated: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only `AUTO_PAUSE_RULE` is lifted (§5.3); any other halt is simply the end of cycling."""
    real = BasketWorker.cycle

    async def halted_in_february(self: BasketWorker) -> CycleResult | None:
        if self._clock.now() == utc(2024, 2, 10):
            await self._watchdog.halt_basket(self.basket_id, "injected: not a loss streak")
        return await real(self)

    monkeypatch.setattr(BasketWorker, "cycle", halted_in_february)

    meta = await isolated("2024-Q1")

    assert meta.lifts == ()
    assert sum(m.ran_cycles for m in meta.months) < sum(m.planned_cycles for m in meta.months)
    assert meta.months[-1].ran_cycles == 0
    assert meta.months[-1].planned_cycles > 0
    assert meta.ending is pd.RunEnding.COMPLETED


async def test_a_period_the_history_does_not_cover_refuses(run_period: Runner) -> None:
    with pytest.raises(ConfigError, match="not fully covered"):
        await run_period("2024-04")


def test_a_period_shorter_than_the_warm_up_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Daily bars: the indicators' warm-up alone outlasts January, so no cycle could run. An empty
    result would read like a system that did nothing wrong."""
    monkeypatch.setenv("DECISION_LAB_WORKSPACE", str(tmp_path / "lock"))
    data = tmp_path / "daily"
    closes = [str(Decimal(100) + index) for index in range(31)]
    f.write_dataset(data, {(f.instrument(), "1d"): f.walk(closes, timeframe="1d")})
    assert cli.main(["dataset", "verify", "--data", str(data)]) == cli.EXIT_OK
    runner = Runner(data, tmp_path / "workspace", monkeypatch)

    with pytest.raises(ConfigError, match="warm-up"):
        asyncio.run(runner("2024-01"))


def test_period_basket_names_btc_alone(data_dir: Path) -> None:
    candidate = candidate_for(data_dir)
    dataset = ReplayDataset.load(data_dir, ManualClock(utc(2024, 1)))

    basket = pd.period_basket(candidate, dataset, CADENCE)

    assert basket.basket_id == "period"
    assert [i.symbol for i in basket.instruments] == ["BTC/USDT"]
    assert basket.schedule.every_seconds == CADENCE
    assert basket.risk_policy == candidate.basket.risk_policy


def test_period_basket_refuses_a_dataset_without_btc(tmp_path: Path, data_dir: Path) -> None:
    data = tmp_path / "eth"
    f.write_dataset(data, {(f.instrument("ETH/USDT"), "1h"): f.shocked_walk(days=2)})
    dataset = ReplayDataset.load(data, ManualClock(utc(2024, 1)))

    with pytest.raises(ConfigError, match="BTC/USDT"):
        pd.period_basket(candidate_for(data_dir), dataset, CADENCE)


def test_period_dir_is_scoped_by_run_id(tmp_path: Path) -> None:
    assert pd.period_dir("abc", workspace=tmp_path) == tmp_path / "period-abc"
