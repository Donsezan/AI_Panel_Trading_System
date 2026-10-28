"""Rung 3, part two: the DESIGN §8.1 failure table, one test per row.

These assert the documented *response*, never PnL. A trading system is correct when it does the
right thing on a bad day, and the bad days are all injected here. Where a row belongs to a later
phase (rate limits, corporate-action feeds, clock skew against a real venue), the part that
exists now is asserted and the part that does not is named in the test.

Phase 2 exit criterion: the kill switch is demonstrated tripping from each of its three
triggers — drawdown, an unexplained reconciliation mismatch, and a human.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from tests.conftest import SERIES_START, TIMEFRAMES
from tests.scenario.harness import IGNORE_STALENESS, Harness

from tradebot.control.basket_runner import BasketRunner
from tradebot.control.context_builder import ContextBuilder
from tradebot.core.clock import ManualClock
from tradebot.core.config import Basket, GlobalRiskPolicy
from tradebot.core.enums import (
    CycleOutcome,
    KillSwitchState,
    Mode,
    OrderRole,
    OrderState,
    ReconcileClass,
    Side,
)
from tradebot.core.errors import VenueError
from tradebot.core.events import EventType
from tradebot.core.instrument import Instrument
from tradebot.core.orders import Fill
from tradebot.core.portfolio import AccountState
from tradebot.decision.engine import DecisionEngine
from tradebot.decision.providers import DEFAULT_RESPONSE, FAIL, StubLLMProvider
from tradebot.decision.seat import SeatRunner
from tradebot.execution.brokers.sim import Tick
from tradebot.ledger.portfolio import ExternalFlow
from tradebot.marketdata.replay import ReplayMarketData, synthetic_candles
from tradebot.risk.aggregate import PortfolioAggregate
from tradebot.risk.state import REARM_PHRASE, assert_rearm_phrase
from tradebot.risk.tier1 import Tier1RiskEngine
from tradebot.risk.tier2 import Tier2RiskEngine

pytestmark = pytest.mark.scenario


def valued(harness: Harness, equity: str) -> PortfolioAggregate:
    """A stated equity, in the shape the watchdog now reads.

    These tests are about the *watchdog's* response to a number, not about how the number is
    computed; `tests/unit/test_aggregate.py` owns the latter (PHASE_12 §1.5).
    """
    return PortfolioAggregate(
        equity=Decimal(equity),
        cash=Decimal(equity),
        gross_exposure=Decimal(0),
        as_of=harness.clock.now(),
    )


async def make_harness(
    basket: Basket, clock: ManualClock, market_data: ReplayMarketData, responses: list[str], **kw
) -> Harness:
    built = Harness(basket, clock, market_data, responses, **kw)
    await built.start()
    return built


class TestDataAndPanelRows:
    async def test_stale_market_data_aborts_the_cycle(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """§8.1: staleness policy in ContextBuilder ⇒ cycle aborts DATA_STALE, no trade."""
        harness = await make_harness(
            basket,
            clock,
            market_data,
            [DEFAULT_RESPONSE],
            staleness_tolerance=timedelta(minutes=15),
        )
        try:
            result = await harness.runner.run_once()

            assert result.outcome is CycleOutcome.DATA_STALE
            assert not result.orders
        finally:
            harness.close()

    async def test_a_provider_outage_abstains_and_degrades_the_panel(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """§8.1: LLM provider down ⇒ seat falls back, else ABSTAIN; >⅓ abstain ⇒ WAIT."""
        harness = await make_harness(basket, clock, market_data, [FAIL])
        try:
            result = await harness.runner.run_once()

            assert result.outcome is CycleOutcome.PANEL_DEGRADED
            assert not result.orders
        finally:
            harness.close()

    async def test_junk_from_a_seat_never_reaches_a_venue(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """§8.1: schema validation ⇒ one repair attempt ⇒ ABSTAIN. [L8]"""
        harness = await make_harness(basket, clock, market_data, ["}{ not json"])
        try:
            result = await harness.runner.run_once()

            assert EventType.ORDER_SUBMITTED not in harness.store.event_types(result.cycle_id)
        finally:
            harness.close()


class TestExecutionRows:
    async def test_a_venue_error_while_settling_the_snapshot_places_no_order(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """ADR 0030: the in-snapshot poll failing is the worker's failure path — no trade."""
        harness = await make_harness(basket, clock, market_data, [DEFAULT_RESPONSE])
        try:
            await harness.runner.run_once()  # buys; legs rest, so the next snapshot must poll
            frozen = len(
                [e for e in harness.store.read_all() if e.type is EventType.SNAPSHOT_FROZEN]
            )
            sent = len([e for e in harness.store.read_all() if e.type is EventType.ORDER_SUBMITTED])

            async def down(_ref: object) -> None:
                raise VenueError("503 from the venue")

            harness.broker.fetch_order = down  # type: ignore[method-assign]
            with pytest.raises(VenueError):
                await harness.runner.run_once()

            after = harness.store.read_all()
            assert len([e for e in after if e.type is EventType.SNAPSHOT_FROZEN]) == frozen
            assert len([e for e in after if e.type is EventType.ORDER_SUBMITTED]) == sent
        finally:
            harness.close()

    async def test_an_ambiguous_submit_queries_rather_than_resubmitting(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """§8.1: no ack ⇒ SUBMIT_UNKNOWN ⇒ query by client order id; found ⇒ adopt. [L9]"""
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

    async def test_a_vanished_order_halts_the_basket_for_human_review(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """§8.1: not found after the bounded window ⇒ mark failed **and halt the basket**."""
        harness = await make_harness(basket, clock, market_data, [DEFAULT_RESPONSE])

        async def swallow(intent: object) -> None:
            harness.broker.fail_next_submit = False
            raise_ = __import__("tradebot.core.errors", fromlist=["SubmitUnknownError"])
            raise raise_.SubmitUnknownError(
                "timeout",
                client_order_id=intent.client_order_id,  # type: ignore[attr-defined]
            )

        harness.broker.submit = swallow  # type: ignore[method-assign]
        try:
            result = await harness.runner.run_once()

            assert result.outcome is CycleOutcome.FAILED
            assert harness.states.halted_baskets()
            rules = [rule for rule, _, _ in harness.risk_events()]
            assert "order_vanished" in rules
        finally:
            harness.close()

    async def test_a_partial_fill_at_ttl_cancels_the_remainder_and_keeps_the_fills(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """§8.1: partial fill at TTL ⇒ cancel remainder, book fills, record fill ratio."""
        harness = await make_harness(
            basket, clock, market_data, [DEFAULT_RESPONSE], fill_ratio=Decimal("0.5")
        )
        try:
            result = await harness.runner.run_once()
            filled = result.orders[0].filled_qty

            clock.advance(basket.order_ttl_seconds + 1)
            await harness.monitor.poll()

            entry = next(o for o in harness.monitor.tracked if not o.role.is_protective)
            assert entry.state is OrderState.EXPIRED
            assert entry.filled_qty == filled
            assert harness.ledger.position(basket.instruments[0].key).qty == filled
        finally:
            harness.close()

    async def test_a_transient_venue_error_does_not_place_an_order(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """§8.1: 5xx ⇒ bounded retry; here the cycle simply ends with nothing placed."""
        harness = await make_harness(basket, clock, market_data, [DEFAULT_RESPONSE])

        async def flaky(_intent: object) -> None:
            raise VenueError("503 from the venue")

        harness.broker.submit = flaky  # type: ignore[method-assign]
        try:
            with pytest.raises(VenueError):
                await harness.runner.run_once()

            assert harness.ledger.position(basket.instruments[0].key).is_flat
        finally:
            harness.close()


class TestReconciliationRows:
    async def test_a_deposit_is_absorbed_and_flow_adjusts_the_baselines(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """§8.1: external deposit ⇒ absorb; flow-adjust HWM and day start — never a drawdown."""
        harness = await make_harness(basket, clock, market_data, [DEFAULT_RESPONSE])
        try:
            before = harness.states.load().high_water_mark
            harness.broker.credit("USDT", Decimal("5000"))

            report = await harness.reconciler.reconcile()
            for flow in harness.reconciler.apply_external_flows(report):
                await harness.watchdog.record_flow(flow)

            assert report.classification is ReconcileClass.EXTERNAL_CHANGE
            assert harness.states.load().high_water_mark == before + Decimal("5000")
        finally:
            harness.close()

    async def test_a_withdrawal_never_reads_as_a_drawdown(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """R16: the whole reason the baselines are flow-adjusted."""
        harness = await make_harness(basket, clock, market_data, [DEFAULT_RESPONSE])
        try:
            await harness.watchdog.record_flow(
                ExternalFlow(currency="USDT", amount=Decimal("-4000"), reason="withdrawal")
            )

            verdict = await harness.watchdog.check(valued(harness, "6000"))

            assert not verdict.tripped
            assert verdict.state.kill_switch is KillSwitchState.ARMED
        finally:
            harness.close()

    async def test_a_venue_reset_halts_and_notifies_rather_than_killing(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """§8.1 / R15: a testnet wipe is routine ops, not a reason to stop everything."""
        harness = await make_harness(basket, clock, market_data, [DEFAULT_RESPONSE])
        try:
            await harness.runner.run_once()
            harness.broker.wipe({"USDT": Decimal(10_000)})

            report = await harness.reconciler.reconcile()

            assert report.classification is ReconcileClass.VENUE_RESET
            assert harness.states.load().kill_switch is KillSwitchState.ARMED
        finally:
            harness.close()

    async def test_an_unexplained_mismatch_halts_the_affected_baskets(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """§8.1: ledger vs exchange mismatch ⇒ halt; above tolerance ⇒ kill switch. [L10]"""
        harness = await make_harness(basket, clock, market_data, [DEFAULT_RESPONSE])
        try:
            await harness.runner.run_once()
            harness.broker.credit("USDT", Decimal("-500"))

            report = await harness.reconciler.reconcile()

            assert report.classification is ReconcileClass.MISMATCH
            assert not report.clean
            rules = [rule for rule, _, _ in harness.risk_events()]
            assert ReconcileClass.MISMATCH.value in rules
        finally:
            harness.close()


class TestKillSwitchTriggers:
    """The Phase 2 exit criterion: each of the three triggers, demonstrated."""

    async def test_trigger_one_a_drawdown_breach(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        harness = await make_harness(basket, clock, market_data, [DEFAULT_RESPONSE])
        try:
            verdict = await harness.watchdog.check(valued(harness, "8000"))  # 20% below the mark

            assert verdict.tripped
            assert harness.states.load().kill_switch is KillSwitchState.TRIPPED
        finally:
            harness.close()

    async def test_trigger_two_a_reconciliation_mismatch_above_tolerance(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        harness = await make_harness(basket, clock, market_data, [DEFAULT_RESPONSE])
        try:
            harness.broker.credit("USDT", Decimal("-6000"))
            report = await harness.reconciler.reconcile()
            equity = harness.valuation().equity

            assert harness.reconciler.exceeds_kill_tolerance(report, equity)
            await harness.watchdog.trip(report.classification.value, report.detail)

            assert harness.states.load().kill_switch is KillSwitchState.TRIPPED
        finally:
            harness.close()

    async def test_trigger_three_a_human(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        harness = await make_harness(basket, clock, market_data, [DEFAULT_RESPONSE])
        try:
            await harness.watchdog.trip("manual", "operator pressed the button")

            assert harness.states.load().kill_switch is KillSwitchState.TRIPPED
        finally:
            harness.close()

    async def test_a_tripped_switch_blocks_the_next_cycle_before_the_panel_runs(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """No tokens are spent deliberating a decision that cannot be acted on."""
        harness = await make_harness(basket, clock, market_data, [DEFAULT_RESPONSE])
        try:
            await harness.watchdog.trip("manual", "operator pressed the button")

            result = await harness.runner.run_once()

            assert result.outcome is CycleOutcome.BLOCKED
            assert harness.store.event_types(result.cycle_id) == (
                EventType.CYCLE_STARTED,
                EventType.CYCLE_COMPLETED,
            )
        finally:
            harness.close()

    async def test_only_a_typed_phrase_re_arms_it(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        harness = await make_harness(basket, clock, market_data, [DEFAULT_RESPONSE] * 3)
        try:
            await harness.watchdog.trip("manual", "operator pressed the button")

            assert_rearm_phrase(REARM_PHRASE)
            await harness.watchdog.rearm(Decimal(10_000), actor="cli")
            result = await harness.runner.run_once()

            assert result.outcome is not CycleOutcome.BLOCKED
        finally:
            harness.close()

    async def test_the_kill_switch_does_not_liquidate(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """`flatten_on_kill` defaults false: flattening into a broken market is often worse."""
        harness = await make_harness(basket, clock, market_data, [DEFAULT_RESPONSE])
        try:
            await harness.runner.run_once()
            held = harness.ledger.position(basket.instruments[0].key).qty

            await harness.watchdog.trip("max_drawdown", "equity fell")

            assert harness.ledger.position(basket.instruments[0].key).qty == held
            assert GlobalRiskPolicy().flatten_on_kill is False
        finally:
            harness.close()


class TestBasketAutoPause:
    async def test_a_run_of_losses_pauses_the_basket_for_review(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """§6.6: max consecutive losses ⇒ auto-pause, which only a human clears."""
        strict = basket.model_copy(
            update={
                "risk_policy": basket.risk_policy.model_copy(
                    update={"max_consecutive_losses": 1, "cooldown_cycles": 0}
                )
            }
        )
        harness = await make_harness(strict, clock, market_data, [DEFAULT_RESPONSE] * 4)
        try:
            await _record_losing_trip(harness, strict)

            result = await harness.runner.run_once()

            assert result.outcome is CycleOutcome.RISK_VETOED
            assert strict.basket_id in harness.states.halted_baskets()
        finally:
            harness.close()


async def _record_losing_trip(harness: Harness, basket: Basket) -> None:
    """Open and close one position at a loss, so the history reader sees a losing round trip."""
    from tradebot.core.events import EventFactory
    from tradebot.core.portfolio import RoundTrip

    events = EventFactory(clock=harness.clock, basket_id=basket.basket_id, cycle_id="seed")
    await harness.store.append(
        events.round_trip_closed(
            RoundTrip(
                instrument_key=basket.instruments[0].key,
                qty=Decimal("0.1"),
                entry_price=Decimal("50000"),
                exit_price=Decimal("49000"),
                realized_pnl=Decimal("-100"),
                closed_at=harness.clock.now(),
            )
        )
    )


class TestProcessCrash:
    async def test_a_restart_recovers_from_the_log_and_resolves_open_orders(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """§8.1: process crash ⇒ recover from the log ⇒ reconcile ⇒ resolve, then resume."""
        harness = await make_harness(basket, clock, market_data, [DEFAULT_RESPONSE] * 2)
        try:
            await harness.runner.run_once()

            recovery = await harness.startup.recover()
            after_restart = harness.projections()
            await harness.store.rebuild()

            assert not recovery.halted
            assert harness.projections() == after_restart, "replay reproduces the read model"
            assert all(order.state.is_open for order in recovery.resolved)
        finally:
            harness.close()

    async def test_a_restart_that_cannot_reach_the_venue_stays_up_and_halted(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """§8.2 step 5: any step failing ⇒ process stays up, halted, nothing trades."""
        harness = await make_harness(basket, clock, market_data, [DEFAULT_RESPONSE])

        async def unreachable() -> AccountState:
            raise VenueError("connection reset")

        harness.broker.fetch_positions_and_balances = unreachable  # type: ignore[method-assign]
        try:
            recovery = await harness.startup.recover()

            assert recovery.halted
            assert not recovery.may_run(basket)
        finally:
            harness.close()


@pytest.fixture
def two_markets(
    instrument: Instrument, second_instrument: Instrument, clock: ManualClock
) -> ReplayMarketData:
    return ReplayMarketData(
        {
            (held.key, timeframe): synthetic_candles(
                start=SERIES_START,
                timeframe=timeframe,
                count=200,
                open_price=Decimal("50000"),
                step=Decimal("25"),
            )
            for held in (instrument, second_instrument)
            for timeframe in TIMEFRAMES
        },
        clock,
    )


def sibling_runner(harness: Harness, sibling: Basket) -> BasketRunner:
    """A second basket on the harness's one venue portfolio: its ledger, execution and monitor."""
    return BasketRunner(
        sibling,
        mode=Mode.SIM,
        context_builder=ContextBuilder(
            harness.market_data,
            harness.ledger,
            harness.clock,
            staleness_tolerance=IGNORE_STALENESS,
            protective_orders_supported=True,
            trading_history=harness.history,
        ),
        decision_engine=DecisionEngine(
            SeatRunner({"stub": StubLLMProvider([DEFAULT_RESPONSE])}, harness.clock)
        ),
        risk_engine=Tier1RiskEngine(harness.clock),
        tier2=Tier2RiskEngine(harness.policy),
        watchdog=harness.watchdog,
        history=harness.history,
        execution=harness.execution,
        monitor=harness.monitor,
        ledger=harness.ledger,
        store=harness.store,
        clock=harness.clock,
        venue=harness.broker.venue_id,
        marks=harness.marks,
        universe=lambda: (*harness.basket.instruments, *sibling.instruments),
        global_policy=harness.policy,
    )


class TestUnbookableFill:
    async def test_an_unbookable_fill_halts_one_basket_not_all(
        self,
        basket: Basket,
        clock: ManualClock,
        two_markets: ReplayMarketData,
        second_instrument: Instrument,
    ) -> None:
        harness = await make_harness(basket, clock, two_markets, [DEFAULT_RESPONSE])
        sibling = basket.model_copy(update={"basket_id": "b2", "instruments": (second_instrument,)})
        other = sibling_runner(harness, sibling)
        btc = basket.instruments[0].key
        try:
            assert (await harness.runner.run_once()).outcome is CycleOutcome.ORDERS_PLACED
            held = harness.ledger.position(btc).qty
            # Fault injection: the ledger already lost half the holding, then the venue's stop
            # sells the whole of it -- a fill the ledger must refuse.
            harness.ledger.apply_fill(
                Fill(
                    fill_id="external",
                    client_order_id="elsewhere",
                    instrument_key=btc,
                    side=Side.SELL,
                    qty=held / 2,
                    price=Decimal("50000"),
                    filled_at=clock.now(),
                ),
                base_currency="BTC",
                quote_currency="USDT",
            )
            one = Decimal(1)
            harness.broker.observe(
                Tick(
                    instrument_key=btc,
                    bid=one,
                    ask=one,
                    last=one,
                    high=one,
                    low=one,
                    covers_since=clock.now(),
                    observed_at=clock.now(),
                )
            )

            failed = await harness.runner.run_once()
            assert failed.outcome is CycleOutcome.FAILED
            assert "exceeds holding" in failed.detail
            assert not harness.states.status_of(basket.basket_id).may_trade

            traded = await other.run_once()
            assert traded.outcome is CycleOutcome.ORDERS_PLACED
            assert harness.ledger.position(second_instrument.key).qty > 0
            assert harness.states.status_of("b2").may_trade

            await harness.monitor.poll()  # the supervisor's sweep: unscoped, must not raise
            assert len([r for r in harness.risk_events() if r[0] == "unbookable_fill"]) == 1
            assert [
                o
                for o in harness.monitor.tracked
                if o.role is OrderRole.STOP_LOSS and o.instrument_key == btc and o.state.is_open
            ], "the fill is kept, not dropped"
        finally:
            harness.close()
