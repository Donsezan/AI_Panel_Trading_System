"""Rung 4: the backtest harness driving the real loop over recorded history.

Asserts the *machinery*, never the PnL — which is the whole claim the banner makes. What matters
here is that a replay cycles on the schedule's own grid, that the log it leaves behind is the
same log a live run leaves, and that the report says out loud what the numbers are not evidence
of (DESIGN §9 rung 4, [L12]).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from tradebot.app import Application, backtest_database_path, build_sim, dataset_basket
from tradebot.core.clock import ManualClock
from tradebot.core.config import PanelConfig, ProviderSettings, SeatConfig
from tradebot.core.enums import AssetClass, CycleOutcome, ProviderKind
from tradebot.core.errors import ConfigError
from tradebot.core.instrument import Instrument
from tradebot.decision.presets import FREE_PANEL, STUB_PANEL
from tradebot.marketdata.recorder import ReplayDataset, record
from tradebot.marketdata.replay import ReplayMarketData, synthetic_candles
from tradebot.persistence.schema import PROJECTION_TABLES
from tradebot.validation.backtest import BANNER, BacktestHarness, panel_models, plan_ticks
from tradebot.validation.cutoffs import Contaminated
from tradebot.validation.evidence import IncidentKind
from tradebot.validation.render import backtest_markdown

pytestmark = pytest.mark.scenario

HISTORY_START = datetime(2026, 1, 1, tzinfo=UTC)
BARS = 400
TIMEFRAMES = ("1h", "4h")

#: The dataset is sized so that, after the indicators' warm-up has eaten the front of it, what
#: is left is long enough for the loss streak, the daily cap and TTL expiry to each get a turn.


@pytest.fixture
def history_instrument() -> Instrument:
    """Priced on a venue named `binance`, as a recorded dataset would be — while `SimBroker`
    still takes the orders. A backtest is a simulation of a real market's prices."""
    return Instrument(
        symbol="BTC/USDT",
        venue="binance",
        asset_class=AssetClass.CRYPTO,
        base_currency="BTC",
        quote_currency="USDT",
        lot_size=Decimal("0.00001"),
        tick_size=Decimal("0.01"),
        min_qty=Decimal("0.00001"),
        min_notional=Decimal("10"),
    )


@pytest.fixture
def replay_clock() -> ManualClock:
    """One clock for the dataset *and* the application.

    Deliberately shared: a replay provider holding a different clock serves bars from beyond the
    cycle's cutoff, which reads as very fresh data and is a look-ahead leak. `require_fresh` now
    refuses that, so this fixture is also what keeps these tests honest rather than passing.
    """
    return ManualClock(HISTORY_START)


@pytest.fixture
async def dataset(
    tmp_path: Path, history_instrument: Instrument, replay_clock: ManualClock
) -> ReplayDataset:
    source = ReplayMarketData(
        {
            (history_instrument.key, timeframe): synthetic_candles(
                start=HISTORY_START,
                timeframe=timeframe,
                count=BARS,
                open_price=Decimal("50000"),
                step=Decimal("25"),
            )
            for timeframe in TIMEFRAMES
        },
        replay_clock,
    )
    return await record(
        source,
        (history_instrument,),
        TIMEFRAMES,
        start=HISTORY_START,
        end=HISTORY_START + timedelta(hours=BARS),
        directory=tmp_path / "history",
        clock=replay_clock,
        source="synthetic fixture",
    )


@pytest.fixture
async def replayed(
    dataset: ReplayDataset, tmp_path: Path, replay_clock: ManualClock
) -> AsyncIterator[tuple[Application, ManualClock]]:
    application = await build_sim(
        clock=replay_clock,
        db_path=backtest_database_path(tmp_path),
        baskets=(dataset_basket(dataset, STUB_PANEL),),
        market_data=dataset.market_data,
    )
    yield application, replay_clock
    await application.shutdown()


class TestPlanning:
    def test_ticks_follow_the_basket_schedule(self, dataset: ReplayDataset) -> None:
        basket = dataset_basket(dataset, STUB_PANEL, every_seconds=3600)
        start = dataset.coverage[0]

        ticks = plan_ticks((basket,), start=start, end=start + timedelta(hours=5), limit=100)
        assert [moment for moment, _ in ticks] == [
            start + timedelta(hours=hour) for hour in range(1, 6)
        ]

    def test_an_absurd_window_refuses_rather_than_running_for_a_week(
        self, dataset: ReplayDataset
    ) -> None:
        basket = dataset_basket(dataset, STUB_PANEL, every_seconds=60)
        start = dataset.coverage[0]

        with pytest.raises(ConfigError, match="plans more than"):
            plan_ticks((basket,), start=start, end=start + timedelta(days=30), limit=100)

    def test_a_stub_panel_contacts_no_model(self, dataset: ReplayDataset) -> None:
        assert panel_models(dataset_basket(dataset, STUB_PANEL)) == ()

    def test_every_binding_a_real_panel_could_reach_is_analysed(
        self, dataset: ReplayDataset
    ) -> None:
        """Fallbacks count: a seat that spent the run on its backup was answered by that model."""
        models = panel_models(dataset_basket(dataset, FREE_PANEL))

        assert len(models) == sum(len(seat.bindings) for seat in FREE_PANEL.seats)

    def test_an_undeclared_provider_is_treated_as_a_real_model(
        self, dataset: ReplayDataset
    ) -> None:
        """Assuming "probably a stub" would drop a model out of the analysis on a config gap."""
        panel = PanelConfig(
            panel_id="undeclared",
            seats=(SeatConfig(seat_id="s", role="r", provider_id="mystery", model="some-model"),),
        )

        assert panel_models(dataset_basket(dataset, panel)) == ("some-model",)


class TestBacktest:
    async def test_the_loop_runs_on_the_schedule_grid_and_leaves_a_normal_log(
        self, replayed: tuple[Application, ManualClock], dataset: ReplayDataset
    ) -> None:
        application, clock = replayed
        start, end = dataset.coverage

        report = await BacktestHarness(
            application, clock, start=start, end=end, data_source=dataset.manifest.source
        ).run()

        assert report.evidence.cycles_by_venue == {"sim": report.ran_cycles}
        # The panel is scripted to BUY every cycle; what stops it trading every hour is the risk
        # layer, so a run that only ever placed orders would mean the limits never engaged.
        outcomes = set(report.evidence.outcomes)
        assert {CycleOutcome.ORDERS_PLACED.value, CycleOutcome.RISK_VETOED.value} <= outcomes

    async def test_a_basket_that_auto_pauses_ends_the_replay(
        self, replayed: tuple[Application, ManualClock], dataset: ReplayDataset
    ) -> None:
        """A loss streak halts the basket, and no human inside a replay can clear it.

        Long-horizon replay is where a rule like this actually gets exercised, which is the whole
        argument for rung 4: the halt is a *result*, and the report shows the window it left
        unused rather than reporting hundreds of empty cycles.
        """
        application, clock = replayed
        start, end = dataset.coverage

        report = await BacktestHarness(application, clock, start=start, end=end).run()

        assert [incident.kind for incident in report.evidence.incidents] == [
            IncidentKind.BASKET_HALTED
        ]
        assert report.skipped_cycles > 0
        assert report.ran_cycles + report.skipped_cycles == report.planned_cycles

    async def test_the_log_alone_reproduces_the_projections(
        self, replayed: tuple[Application, ManualClock], dataset: ReplayDataset
    ) -> None:
        """A backtest's audit trail is a live run's audit trail, or it is not validating it."""
        application, clock = replayed
        start, end = dataset.coverage
        await BacktestHarness(application, clock, start=start, end=end).run()

        before = _projection_rows(application)
        await application.store.rebuild()
        assert _projection_rows(application) == before

    async def test_protective_legs_arm_and_fill_over_a_long_replay(
        self, replayed: tuple[Application, ManualClock], dataset: ReplayDataset
    ) -> None:
        """The reason the harness polls between cycles: a stop fills on a bar, not on a decision."""
        application, clock = replayed
        start, end = dataset.coverage

        report = await BacktestHarness(application, clock, start=start, end=end).run()

        assert report.evidence.fills > 0
        assert report.evidence.round_trips

    async def test_the_report_states_what_it_is_not_evidence_of(
        self, replayed: tuple[Application, ManualClock], dataset: ReplayDataset
    ) -> None:
        application, clock = replayed
        start, end = dataset.coverage

        report = await BacktestHarness(application, clock, start=start, end=end).run()
        markdown = backtest_markdown(report)

        assert BANNER in markdown
        assert "NOT ALPHA EVIDENCE" in markdown
        assert "offline scripted panel" in markdown

    async def test_a_contaminated_window_is_reported_as_such(
        self, dataset: ReplayDataset, tmp_path: Path, replay_clock: ManualClock
    ) -> None:
        """The models memorized 2026; a replay of it can only ever be a plumbing test."""
        panel = PanelConfig(
            panel_id="hosted",
            providers=(
                ProviderSettings(
                    provider_id="openrouter",
                    kind=ProviderKind.OPENAI_COMPAT,
                    base_url="https://openrouter.ai/api/v1",
                ),
            ),
            seats=(
                SeatConfig(
                    seat_id="technical",
                    role="Technical Analyst",
                    provider_id="openrouter",
                    model="meta-llama/llama-3.3-70b-instruct:free",
                ),
            ),
        )
        application = await build_sim(
            clock=replay_clock,
            db_path=backtest_database_path(tmp_path / "contaminated"),
            baskets=(dataset_basket(dataset, panel),),
            market_data=dataset.market_data,
        )
        try:
            start, end = dataset.coverage
            harness = BacktestHarness(application, replay_clock, start=start, end=end)
            report = await harness.run()
        finally:
            await application.shutdown()

        (verdict,) = report.contamination
        assert verdict.verdict is Contaminated.CLEAN
        assert "meta-llama/llama-3.3" in backtest_markdown(report)


def _projection_rows(application: Application) -> dict[str, list[tuple[object, ...]]]:
    with application.store.engine.connect() as connection:
        return {
            table.name: [tuple(row) for row in connection.execute(select(table))]
            for table in PROJECTION_TABLES
        }
