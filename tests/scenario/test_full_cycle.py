"""Rung 3: the full loop, and its behaviour under injected faults.

These assert the *response* from DESIGN §8.1 — never PnL. A trading system is correct when it
does the right thing on a bad day, and the bad days are all injected here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from tests.scenario.harness import Harness

from tradebot.app import build, build_sim
from tradebot.core.clock import ManualClock
from tradebot.core.config import Basket, PanelConfig, SeatConfig
from tradebot.core.decision import Decision
from tradebot.core.enums import Action, CycleOutcome, Mode, OrderRole, OrderState, SizeHint
from tradebot.core.errors import ConfigError
from tradebot.core.events import EventType
from tradebot.decision.providers import DEFAULT_RESPONSE, FAIL
from tradebot.marketdata.replay import ReplayMarketData
from tradebot.persistence.schema import cycles, orders

pytestmark = pytest.mark.scenario

#: The complete chain one filled entry produces, asserted literally so a change to the loop has
#: to be a deliberate edit here. Reading downwards: the panel decides, both risk tiers record a
#: verdict, the entry is written before it is sent, its fill moves the ledger, and only then are
#: the two OCO protective legs placed against what actually filled.
EXPECTED_CHAIN = (
    EventType.CYCLE_STARTED,
    EventType.SNAPSHOT_FROZEN,
    EventType.SEAT_RESPONDED,
    EventType.DECISION_MADE,
    EventType.RISK_CHECKED,  # tier 1
    EventType.RISK_CHECKED,  # tier 2
    EventType.ORDER_SUBMITTED,
    EventType.ORDER_STATE_CHANGED,
    EventType.FILL_RECEIVED,
    EventType.POSITION_UPDATED,
    *(  # stop-loss leg, then take-profit leg: submitted → acknowledged → resting
        (
            EventType.ORDER_SUBMITTED,
            EventType.ORDER_STATE_CHANGED,
            EventType.ORDER_STATE_CHANGED,
        )
        * 2
    ),
    EventType.PROTECTIVE_PLACED,
    EventType.CYCLE_COMPLETED,
)

SELL_RESPONSE = """{
  "action": "SELL", "conviction": 5, "size_hint": "full",
  "thesis": "Take the position off.", "key_risks": [], "invalidation": "n/a"
}"""
HOLD_RESPONSE = """{
  "action": "HOLD", "conviction": 3, "size_hint": "none",
  "thesis": "Nothing to do.", "key_risks": [], "invalidation": "n/a"
}"""
LOW_CONVICTION = """{
  "action": "BUY", "conviction": 1, "size_hint": "quarter",
  "thesis": "Weak signal.", "key_risks": [], "invalidation": "n/a"
}"""


@pytest.fixture
async def harness(
    basket: Basket, clock: ManualClock, market_data: ReplayMarketData
) -> AsyncIterator[Harness]:
    built = Harness(basket, clock, market_data, [DEFAULT_RESPONSE])
    await built.start()
    yield built
    built.close()


async def make_harness(
    basket: Basket, clock: ManualClock, market_data: ReplayMarketData, responses: list[str], **kw
) -> Harness:
    built = Harness(basket, clock, market_data, responses, **kw)
    await built.start()
    return built


class TestHappyPath:
    async def test_the_full_event_chain_is_emitted_in_order(self, harness: Harness) -> None:
        """PLAN Phase 1 exit criterion, asserted literally."""
        result = await harness.runner.run_once()
        assert result.outcome is CycleOutcome.ORDERS_PLACED
        assert harness.store.event_types(result.cycle_id) == EXPECTED_CHAIN

    async def test_the_order_reaches_the_ledger_as_a_position(self, harness: Harness) -> None:
        result = await harness.runner.run_once()
        order = result.orders[0]
        assert order.state is OrderState.FILLED
        position = harness.ledger.position(harness.basket.instruments[0].key)
        assert position.qty == order.filled_qty
        assert position.qty > 0

    async def test_replaying_the_log_reproduces_identical_projections(
        self, harness: Harness
    ) -> None:
        """The audit guarantee end to end: the log alone reconstructs the read model."""
        await harness.runner.run_once()
        before = harness.projections()
        await harness.store.rebuild()
        assert harness.projections() == before

    async def test_the_snapshot_hash_is_recorded_for_replay_verification(
        self, harness: Harness
    ) -> None:
        result = await harness.runner.run_once()
        with harness.store.engine.connect() as connection:
            row = connection.execute(
                select(cycles).where(cycles.c.cycle_id == result.cycle_id)
            ).one()
        assert row.snapshot_digest
        assert len(row.snapshot_digest) == 32

    async def test_the_cooldown_stops_the_same_thesis_becoming_a_second_order(
        self, harness: Harness
    ) -> None:
        """One conclusion, one order: the next cycle is metered out even if the panel repeats."""
        first = await harness.runner.run_once()
        key = harness.basket.instruments[0].key
        opened = harness.ledger.position(key).qty

        second = await harness.runner.run_once()

        assert first.outcome is CycleOutcome.ORDERS_PLACED
        assert second.outcome is CycleOutcome.RISK_VETOED
        assert harness.ledger.position(key).qty == opened

    async def test_the_position_cap_holds_once_the_cooldown_has_elapsed(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """With metering out of the way, the per-instrument cap is what stops compounding."""
        eager = basket.model_copy(
            update={"risk_policy": basket.risk_policy.model_copy(update={"cooldown_cycles": 0})}
        )
        harness = await make_harness(eager, clock, market_data, [DEFAULT_RESPONSE] * 6)
        try:
            key = eager.instruments[0].key
            outcomes = [(await harness.runner.run_once()).outcome for _ in range(4)]
            held = harness.ledger.position(key)
            budget = harness.ledger.equity({}, quote_currency="USDT")

            assert outcomes[0] is CycleOutcome.ORDERS_PLACED
            assert CycleOutcome.RISK_VETOED in outcomes, "the cap must bite before the cash does"
            ceiling = budget * Decimal("0.1") * Decimal("0.25")
            assert held.market_value(held.avg_entry) <= ceiling * Decimal("1.01")
        finally:
            harness.close()


class TestPanelFaults:
    async def test_a_seat_returning_junk_degrades_to_wait_and_places_nothing(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        harness = await make_harness(basket, clock, market_data, ["not json at all"])
        try:
            result = await harness.runner.run_once()
            assert result.outcome is CycleOutcome.PANEL_DEGRADED
            assert result.decisions[0].action is Action.WAIT
            assert not result.orders
            assert EventType.ORDER_SUBMITTED not in harness.store.event_types(result.cycle_id)
        finally:
            harness.close()

    async def test_a_provider_outage_degrades_to_wait(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        harness = await make_harness(basket, clock, market_data, [FAIL])
        try:
            result = await harness.runner.run_once()
            assert result.outcome is CycleOutcome.PANEL_DEGRADED
            assert not result.orders
        finally:
            harness.close()

    async def test_a_hold_majority_places_no_order_but_is_not_a_veto(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        harness = await make_harness(basket, clock, market_data, [HOLD_RESPONSE])
        try:
            result = await harness.runner.run_once()
            assert result.decisions[0].action is Action.HOLD
            assert result.outcome is CycleOutcome.NO_ACTION
        finally:
            harness.close()


class TestRiskFaults:
    async def test_low_conviction_is_vetoed_and_recorded(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        harness = await make_harness(basket, clock, market_data, [LOW_CONVICTION])
        try:
            result = await harness.runner.run_once()
            assert result.outcome is CycleOutcome.RISK_VETOED
            assert not result.orders
            chain = harness.store.event_types(result.cycle_id)
            assert EventType.RISK_CHECKED in chain
            assert EventType.ORDER_SUBMITTED not in chain
        finally:
            harness.close()

    async def test_sell_while_flat_never_opens_a_short(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """R13: the cheapest-to-prevent catastrophic failure in the system."""
        harness = await make_harness(basket, clock, market_data, [SELL_RESPONSE])
        try:
            result = await harness.runner.run_once()
            assert result.decisions[0].action is Action.SELL
            assert result.outcome is CycleOutcome.RISK_VETOED
            assert harness.ledger.position(basket.instruments[0].key).is_flat
        finally:
            harness.close()

    async def test_a_position_can_be_reduced_but_not_reversed(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        harness = await make_harness(basket, clock, market_data, [DEFAULT_RESPONSE, SELL_RESPONSE])
        try:
            await harness.runner.run_once()
            opened = harness.ledger.position(basket.instruments[0].key).qty
            assert opened > 0

            harness.provider._responses = iter([SELL_RESPONSE] * 4)
            await harness.runner.run_once()
            assert harness.ledger.position(basket.instruments[0].key).qty >= 0
        finally:
            harness.close()

    async def test_an_account_too_small_for_the_venue_minimum_is_vetoed(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """Never bumped up to the minimum — that would oversize past the risk limit."""
        harness = await make_harness(
            basket, clock, market_data, [DEFAULT_RESPONSE], equity=Decimal("50")
        )
        try:
            result = await harness.runner.run_once()
            assert result.outcome is CycleOutcome.RISK_VETOED
        finally:
            harness.close()


class TestDataFaults:
    async def test_stale_market_data_aborts_the_cycle(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        harness = Harness(
            basket,
            clock,
            market_data,
            [DEFAULT_RESPONSE],
            staleness_tolerance=timedelta(minutes=15),
        )
        await harness.start()
        try:
            result = await harness.runner.run_once()
            assert result.outcome is CycleOutcome.DATA_STALE
            assert not result.orders
            assert harness.store.event_types(result.cycle_id) == (
                EventType.CYCLE_STARTED,
                EventType.CYCLE_COMPLETED,
            )
        finally:
            harness.close()

    async def test_a_missing_series_aborts_the_cycle_rather_than_deciding_blind(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        harness = Harness(basket, clock, market_data, [DEFAULT_RESPONSE])
        await harness.start()
        harness.clock.set(datetime(2020, 1, 1, tzinfo=UTC))
        try:
            result = await harness.runner.run_once()
            assert result.outcome is CycleOutcome.DATA_STALE
        finally:
            harness.close()


class TestExecutionFaults:
    async def test_an_ambiguous_submit_adopts_the_venue_order_instead_of_resubmitting(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """The orphan-order test (PLAN §7): one order at the venue, never two."""
        harness = await make_harness(basket, clock, market_data, [DEFAULT_RESPONSE])
        harness.broker.fail_next_submit = True
        try:
            result = await harness.runner.run_once()
            assert result.outcome is CycleOutcome.ORDERS_PLACED

            order = result.orders[0]
            assert order.state is OrderState.FILLED
            assert len(order.fills) == 1

            with harness.store.engine.connect() as connection:
                rows = connection.execute(
                    select(orders).where(orders.c.role == OrderRole.ENTRY.value)
                ).all()
            assert len(rows) == 1, "an ambiguous submit must never produce a second order"
        finally:
            harness.close()

    async def test_submit_unknown_is_recorded_in_the_state_history(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        harness = await make_harness(basket, clock, market_data, [DEFAULT_RESPONSE])
        harness.broker.fail_next_submit = True
        try:
            result = await harness.runner.run_once()
            states = [
                event.payload["state"]
                for event in harness.store.read_all()
                if event.type is EventType.ORDER_STATE_CHANGED
            ]
            assert OrderState.SUBMIT_UNKNOWN.value in states
            assert result.orders[0].state is OrderState.FILLED
        finally:
            harness.close()

    async def test_partial_fills_are_booked_from_fills_not_from_terminal_state(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        harness = await make_harness(
            basket, clock, market_data, [DEFAULT_RESPONSE], fill_ratio=Decimal("0.5")
        )
        try:
            result = await harness.runner.run_once()
            order = result.orders[0]
            assert order.state is OrderState.PARTIALLY_FILLED
            assert order.filled_qty == order.qty / 2
            assert harness.ledger.position(basket.instruments[0].key).qty == order.filled_qty
        finally:
            harness.close()


class TestMultiInstrumentBasket:
    async def test_each_instrument_gets_its_own_decision_and_order(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        second = basket.instruments[0].model_copy(update={"symbol": "ETH/USDT"})
        wider = basket.model_copy(update={"instruments": (*basket.instruments, second)})
        series = dict(market_data._series)
        for (key, timeframe), candles in list(series.items()):
            if key == basket.instruments[0].key:
                series[(second.key, timeframe)] = candles
        harness = Harness(wider, clock, ReplayMarketData(series, clock), [DEFAULT_RESPONSE])
        await harness.start()
        try:
            result = await harness.runner.run_once()
            assert len(result.decisions) == 2
            assert {order.instrument_key for order in result.orders} == {
                basket.instruments[0].key,
                second.key,
            }
            assert len({order.client_order_id for order in result.orders}) == 2
        finally:
            harness.close()


class TestModeSafety:
    def test_paper_and_live_have_no_wiring_and_refuse_to_start(self) -> None:
        """A mode that quietly does something else is the catastrophic failure (PLAN §2.4)."""
        with pytest.raises(ConfigError, match="no wiring"):
            build(Mode.PAPER)

    def test_live_refuses_without_the_typed_confirmation(self) -> None:
        with pytest.raises(ConfigError, match="typed confirmation"):
            build(Mode.LIVE)

    def test_live_still_refuses_even_with_the_confirmation(self) -> None:
        """Live ships disabled: there is no wiring to reach, confirmation or not."""
        with pytest.raises(ConfigError, match="no wiring"):
            build(Mode.LIVE, confirmation="I ACCEPT REAL MONEY RISK")

    def test_each_mode_uses_its_own_database_file(self) -> None:
        from tradebot.app import database_path

        paths = {mode: database_path(mode) for mode in Mode}
        assert len({str(path) for path in paths.values()}) == len(Mode)

    def test_sim_wiring_builds_and_runs(self, clock: ManualClock) -> None:
        application = build_sim(clock=clock)
        try:
            assert application.mode is Mode.SIM
            assert len(application.runners) == 1
        finally:
            application.close()


class TestDecisionRecording:
    async def test_the_decision_and_its_dissent_are_queryable_afterwards(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """The drill-down view is the core research artifact (DESIGN §6.10)."""
        panel = PanelConfig(
            panel_id="p",
            seats=tuple(
                SeatConfig(seat_id=f"s{i}", role=f"role-{i}", provider_id="stub", model=f"m{i}")
                for i in range(3)
            ),
        )
        harness = Harness(
            basket.model_copy(update={"panel": panel}), clock, market_data, [DEFAULT_RESPONSE]
        )
        await harness.start()
        try:
            result = await harness.runner.run_once()
            seat_events = [
                event
                for event in harness.store.read_all()
                if event.type is EventType.SEAT_RESPONDED
            ]
            assert len(seat_events) == 3
            assert all(event.payload["response"]["raw_text"] for event in seat_events)

            with harness.store.engine.connect() as connection:
                from tradebot.persistence.schema import decisions as decisions_table

                row = connection.execute(select(decisions_table)).one()
            assert row.action == Action.BUY.value
            assert row.size_hint == SizeHint.HALF.value
            assert result.decisions[0].conviction > 0
        finally:
            harness.close()

    async def test_a_decision_is_recorded_even_when_no_order_follows(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        harness = await make_harness(basket, clock, market_data, [HOLD_RESPONSE])
        try:
            result = await harness.runner.run_once()
            with harness.store.engine.connect() as connection:
                from tradebot.persistence.schema import decisions as decisions_table

                row = connection.execute(select(decisions_table)).one()
            assert row.action == Action.HOLD.value
            assert row.cycle_id == result.cycle_id
        finally:
            harness.close()


def test_decision_is_a_proposal_not_an_order() -> None:
    """A structural reminder: nothing about a Decision can name a quantity or a venue."""
    fields = set(Decision.model_fields)
    assert not fields & {"qty", "quantity", "price", "client_order_id", "venue"}
