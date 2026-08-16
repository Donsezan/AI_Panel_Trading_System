"""The seam that failed: what the caller passes the watchdog (PHASE_12 §1.5).

Unit-testing `Watchdog.check` harder would not have caught any of the findings — its arithmetic
was always correct. The defect lived entirely in the seam, and the seam had no test: every caller
built its own price map out of `avg_entry`, so the drawdown gate measured the cost basis and a
portfolio that had halved reported no drawdown at all.

So these drive a **real `BasketRunner`** and assert what the gate actually sees.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from tests.scenario.harness import Harness

from tradebot.control.valuation import VALUATION_RULE
from tradebot.core.clock import ManualClock
from tradebot.core.config import Basket, GlobalRiskPolicy
from tradebot.core.enums import CycleOutcome, KillSwitchState, Side
from tradebot.core.errors import FailClosedError
from tradebot.core.orders import Fill
from tradebot.decision.providers import DEFAULT_RESPONSE
from tradebot.ledger.portfolio import ExternalFlow
from tradebot.marketdata.replay import ReplayMarketData


async def make_harness(
    basket: Basket, clock: ManualClock, market_data: ReplayMarketData, responses: list[str], **kw
) -> Harness:
    built = Harness(basket, clock, market_data, responses, **kw)
    await built.start()
    return built


def flow(currency: str, amount: str) -> ExternalFlow:
    return ExternalFlow(currency=currency, amount=Decimal(amount), reason="test")


def hold(harness: Harness, basket: Basket, *, qty: str, price: str) -> None:
    """Put a position on the books directly, so the test controls its entry price exactly."""
    instrument = basket.instruments[0]
    harness.ledger.apply_fill(
        Fill(
            fill_id="seed-1",
            client_order_id="sim-SEED",
            instrument_key=instrument.key,
            side=Side.BUY,
            qty=Decimal(qty),
            price=Decimal(price),
            filled_at=harness.clock.now(),
        ),
        base_currency=instrument.base_currency,
        quote_currency=instrument.quote_currency,
    )


def mark(harness: Harness, basket: Basket, price: str) -> None:
    harness.marks.observe(basket.instruments[0].key, Decimal(price), harness.clock.now())


class TestUnrealizedLossIsVisible:
    """Finding 1, end to end. Every one of these reported zero drawdown before the fix."""

    async def test_an_unrealized_loss_past_the_limit_trips_the_kill_switch(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """10,000 opening; 0.1 BTC at 50,000; BTC falls to 25,000. Equity 7,500 — a 25% drawdown."""
        harness = await make_harness(basket, clock, market_data, [DEFAULT_RESPONSE])
        try:
            hold(harness, basket, qty="0.1", price="50000")
            mark(harness, basket, "25000")

            verdict = await harness.watchdog.check(harness.valuation())

            assert harness.valuation().equity == Decimal("7500")
            assert verdict.tripped
            assert harness.states.load().kill_switch is KillSwitchState.TRIPPED
        finally:
            harness.close()

    async def test_the_cycle_gate_blocks_once_the_switch_has_tripped_on_unrealized_loss(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """The gate is a cycle's only `Watchdog.check`; it must see the same loss."""
        harness = await make_harness(basket, clock, market_data, [DEFAULT_RESPONSE])
        try:
            hold(harness, basket, qty="0.1", price="50000")
            mark(harness, basket, "25000")

            result = await harness.runner.run_once()

            assert result.outcome is CycleOutcome.BLOCKED
            assert harness.states.load().kill_switch is KillSwitchState.TRIPPED
        finally:
            harness.close()

    async def test_an_unrealized_loss_inside_the_limit_does_not_trip(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """0.1 BTC from 50,000 to 47,000: equity 9,700, a 3% drawdown against a 10% limit."""
        harness = await make_harness(basket, clock, market_data, [DEFAULT_RESPONSE])
        try:
            hold(harness, basket, qty="0.1", price="50000")
            mark(harness, basket, "47000")

            verdict = await harness.watchdog.check(harness.valuation())

            assert harness.valuation().equity == Decimal("9700")
            assert not verdict.tripped
        finally:
            harness.close()

    async def test_an_unrealized_daily_loss_halts_orders_without_tripping(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """A 4% unrealized fall against a 3% daily limit: orders stop, the switch does not trip."""
        harness = await make_harness(
            basket,
            clock,
            market_data,
            [DEFAULT_RESPONSE],
            policy=GlobalRiskPolicy(max_daily_loss_pct=Decimal(3), max_drawdown_pct=Decimal(50)),
        )
        try:
            hold(harness, basket, qty="0.1", price="50000")
            mark(harness, basket, "46000")  # equity 9,600 — 4% below day-start 10,000

            verdict = await harness.watchdog.check(harness.valuation())

            assert verdict.day_halted
            assert not verdict.tripped
            assert not verdict.may_trade
            assert harness.states.load().kill_switch is KillSwitchState.ARMED
        finally:
            harness.close()

    async def test_an_unrealized_gain_raises_the_mark_and_a_giveback_measures_from_it(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """The high-water mark must move on unrealized gains, or drawdown measures from cost."""
        harness = await make_harness(basket, clock, market_data, [DEFAULT_RESPONSE])
        try:
            hold(harness, basket, qty="0.1", price="50000")

            mark(harness, basket, "100000")  # equity 15,000
            await harness.watchdog.check(harness.valuation())
            assert harness.states.load().high_water_mark == Decimal("15000")

            mark(harness, basket, "50000")  # back to 10,000 — a 33% giveback from the peak
            verdict = await harness.watchdog.check(harness.valuation())

            assert verdict.tripped
        finally:
            harness.close()


class TestTheFreezeIsNotAFallback:
    async def test_an_unmarked_position_blocks_the_cycle_rather_than_valuing_it_at_cost(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        harness = await make_harness(basket, clock, market_data, [DEFAULT_RESPONSE])
        try:
            hold(harness, basket, qty="0.1", price="50000")

            valuation = harness.valuation()

            assert valuation.frozen
            assert valuation.equity != Decimal(10_000), "cost basis is not an answer"
        finally:
            harness.close()

    async def test_a_freeze_blocks_new_orders_without_tripping_the_switch(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """The switch is for breaches, not for ignorance (ADR 0027)."""
        harness = await make_harness(basket, clock, market_data, [DEFAULT_RESPONSE])
        try:
            hold(harness, basket, qty="0.1", price="50000")

            verdict = await harness.watchdog.check(harness.valuation())

            assert verdict.frozen
            assert not verdict.may_trade
            assert not verdict.tripped
            assert harness.states.load().kill_switch is KillSwitchState.ARMED
        finally:
            harness.close()

    async def test_a_freeze_writes_no_state_at_all(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """No baseline may move on a number the system has just said it cannot compute."""
        harness = await make_harness(basket, clock, market_data, [DEFAULT_RESPONSE])
        try:
            before = harness.states.load()
            hold(harness, basket, qty="0.1", price="50000")

            await harness.watchdog.check(harness.valuation())
            after = harness.states.load()

            assert after.high_water_mark == before.high_water_mark
            assert after.day_start_equity == before.day_start_equity
            assert after.day_started_on == before.day_started_on
        finally:
            harness.close()

    async def test_the_freeze_clears_on_its_own_when_a_mark_returns(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """An unreachable venue must not need an operator to undo it."""
        harness = await make_harness(basket, clock, market_data, [DEFAULT_RESPONSE])
        try:
            hold(harness, basket, qty="0.1", price="50000")
            assert harness.valuation().frozen

            mark(harness, basket, "50000")

            assert not harness.valuation().frozen
            assert harness.valuation().equity == Decimal(10_000)
        finally:
            harness.close()


class TestCashIsNeverSilentlyWorthNothing:
    async def test_a_stablecoin_balance_counts_toward_equity(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """Finding 3: 1,000 USDT beside 9,000 USDC used to value at 1,000."""
        harness = await make_harness(basket, clock, market_data, [DEFAULT_RESPONSE])
        try:
            harness.ledger.apply_external_change(flow("USDT", "-9000"))  # 10,000 → 1,000 USDT
            harness.ledger.apply_external_change(flow("USDC", "9000"))

            assert harness.valuation().equity == Decimal(10_000)
        finally:
            harness.close()

    async def test_a_stablecoin_deposit_raises_the_baselines_and_equity_together(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """Findings 3+4, the compound case: this used to be a guaranteed spurious trip.

        The deposit raised the high-water mark by 9,000 while contributing 0 to equity, so the very
        next check saw a 9,000 drawdown that never happened — and clearing it needed a human.
        """
        harness = await make_harness(basket, clock, market_data, [DEFAULT_RESPONSE])
        try:
            before = harness.states.load().high_water_mark

            deposit = flow("USDC", "9000")
            harness.ledger.apply_external_change(deposit)
            await harness.watchdog.record_flow(deposit)

            assert harness.states.load().high_water_mark == before + Decimal(9000)
            assert harness.valuation().equity == Decimal(19_000)

            verdict = await harness.watchdog.check(harness.valuation())

            assert not verdict.tripped, "net drawdown is zero; nothing was lost"
        finally:
            harness.close()

    async def test_a_flow_in_a_currency_nothing_can_value_is_refused(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """A baseline adjusted by a number in the wrong unit is worse than no adjustment."""
        harness = await make_harness(basket, clock, market_data, [DEFAULT_RESPONSE])
        try:
            before = harness.states.load()

            with pytest.raises(FailClosedError, match="DOGE"):
                await harness.watchdog.record_flow(flow("DOGE", "9000"))

            assert harness.states.load().high_water_mark == before.high_water_mark
            assert harness.states.load().day_start_equity == before.day_start_equity
        finally:
            harness.close()

    async def test_an_unvaluable_balance_freezes_and_names_the_currency(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        harness = await make_harness(basket, clock, market_data, [DEFAULT_RESPONSE])
        try:
            harness.ledger.apply_external_change(flow("DOGE", "50"))

            valuation = harness.valuation()

            assert valuation.frozen
            assert "DOGE" in valuation.frozen_reason
        finally:
            harness.close()


class TestTheOperatorIsTold:
    async def test_a_freeze_is_recorded_once_per_transition_not_once_per_sweep(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """At the resync cadence a per-sweep event would bury the one that matters (ADR 0027)."""
        harness = await make_harness(basket, clock, market_data, [DEFAULT_RESPONSE])
        try:
            hold(harness, basket, qty="0.1", price="50000")

            for _ in range(3):
                await harness.portfolio.sweep()

            frozen = [e for e in harness.risk_events() if e[0] == VALUATION_RULE]
            assert [action for _, action, _ in frozen] == ["frozen"]
        finally:
            harness.close()

    async def test_the_recovery_is_recorded_too(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """This one clears itself, so an operator must not have to infer it from silence."""
        harness = await make_harness(basket, clock, market_data, [DEFAULT_RESPONSE])
        try:
            hold(harness, basket, qty="0.1", price="50000")
            await harness.portfolio.sweep()

            mark(harness, basket, "50000")
            await harness.portfolio.sweep()

            frozen = [e for e in harness.risk_events() if e[0] == VALUATION_RULE]
            assert [action for _, action, _ in frozen] == ["frozen", "thawed"]
        finally:
            harness.close()

    async def test_the_valuation_basis_change_is_announced_and_changes_nothing(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """D3: no silent re-baseline. The operator is told, and decides (ADR 0027)."""
        harness = await make_harness(basket, clock, market_data, [DEFAULT_RESPONSE] * 2)
        try:
            # Through a real cycle, so the fill is in the log: `recover` replays the ledger from
            # the log, and a position applied directly would not survive it.
            await harness.runner.run_once()
            assert not harness.ledger.position(basket.instruments[0].key).is_flat
            mark(harness, basket, "25000")
            before = harness.states.load()

            await harness.startup.recover()

            after = harness.states.load()
            assert after.high_water_mark == before.high_water_mark, "no silent re-baseline"
            assert after.day_start_equity == before.day_start_equity
            recorded = [e for e in harness.risk_events() if e[0] == "valuation_basis"]
            assert len(recorded) == 1
            assert "cost basis" in recorded[0][2]
        finally:
            harness.close()
