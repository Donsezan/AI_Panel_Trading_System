"""The startup / recovery sequence, and its one rule: a partial recovery is not a recovery.

Every step is tested for what it does when it *fails*, because the failure behaviour — process
up, halted, reason in the log — is the whole reason the module exists (DESIGN §8.2 step 5).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tradebot.control.config_store import ConfigStore
from tradebot.control.startup import StartupSequence
from tradebot.control.valuation import PortfolioWatch
from tradebot.core.clock import ManualClock
from tradebot.core.config import Basket, GlobalRiskPolicy
from tradebot.core.enums import KillSwitchState, Mode, OrderState, OrderType, Side
from tradebot.core.errors import VenueError
from tradebot.core.events import EventType
from tradebot.core.instrument import Instrument
from tradebot.core.orders import Order, OrderIntent, ProtectivePlan
from tradebot.core.portfolio import AccountState
from tradebot.execution.brokers.sim import SimBroker, Tick
from tradebot.execution.monitor import ExecutionMonitor
from tradebot.execution.service import ExecutionService
from tradebot.ledger.marks import Marks
from tradebot.ledger.portfolio import Ledger
from tradebot.ledger.reconciler import Reconciler
from tradebot.marketdata.catalogue import sim_catalogue
from tradebot.persistence.store import EventStore
from tradebot.risk.state import RiskStateStore
from tradebot.risk.watchdog import Watchdog


def tick(instrument: Instrument, clock: ManualClock, price: str = "50000") -> Tick:
    value = Decimal(price)
    return Tick(
        instrument_key=instrument.key,
        bid=value,
        ask=value,
        last=value,
        high=value,
        low=value,
        covers_since=clock.now(),
        observed_at=clock.now(),
    )


def entry(instrument: Instrument, clock: ManualClock, *, price: str = "45000") -> OrderIntent:
    return OrderIntent(
        client_order_id="sim-ENTRY",
        basket_id="b1",
        cycle_id="c1",
        instrument_key=instrument.key,
        side=Side.BUY,
        qty=Decimal("0.1"),
        order_type=OrderType.LIMIT,
        limit_price=Decimal(price),
        protective=ProtectivePlan(stop_price=Decimal("40000")),
        created_at=clock.now(),
    )


class Stack:
    """The parts of `build_sim` the startup sequence needs, assembled for one test."""

    def __init__(
        self,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
        broker: object | None = None,
        readiness: object | None = None,
    ) -> None:
        self.broker = broker or SimBroker(clock, balances={"USDT": Decimal(10_000)})
        self.states = RiskStateStore(store.engine, store._writer, clock)
        self.watchdog = Watchdog(GlobalRiskPolicy(), self.states, store, clock)
        self.execution = ExecutionService(self.broker, store, ledger, clock)  # type: ignore[arg-type]
        self.monitor = ExecutionMonitor(self.broker, self.execution, store, clock)  # type: ignore[arg-type]
        self.reconciler = Reconciler(
            self.broker,  # type: ignore[arg-type]
            ledger,
            store,
            clock,
            mode=Mode.SIM,
            instruments=(instrument,),
        )
        # The valuation the sequence reads for its reconciliation tolerance and its first-run
        # baseline. No market data: a flat ledger of quote-currency cash needs no marks at all,
        # which is exactly the property that keeps a fresh database working offline (ADR 0027).
        self.portfolio = PortfolioWatch(
            ledger,
            Marks(),
            ConfigStore(store.engine, store._writer, store, clock),
            self.watchdog,
            clock,
            market_data=None,
            catalogue=sim_catalogue(),
            notional_currency="USDT",
            policy_of=GlobalRiskPolicy,
            resync_seconds=30.0,
        )
        self.sequence = StartupSequence(
            store,
            ledger,
            self.reconciler,
            self.execution,
            self.monitor,
            self.states,
            self.watchdog,
            clock,
            instruments=(instrument,),
            readiness=readiness,  # type: ignore[arg-type]
            portfolio=self.portfolio,
        )


@pytest.fixture
def stack(store: EventStore, ledger: Ledger, clock: ManualClock, instrument: Instrument) -> Stack:
    return Stack(store, ledger, clock, instrument)


class FakeReadiness:
    """Stands in for the live gates, which are absent in every other mode (ADR 0020)."""

    def __init__(self, *failures: str) -> None:
        self._failures = failures
        self.ran = 0

    async def run(self) -> tuple[str, ...]:
        self.ran += 1
        return self._failures


class TestLiveReadiness:
    async def test_a_failed_gate_leaves_the_process_up_and_halted(
        self, store: EventStore, ledger: Ledger, clock: ManualClock, instrument: Instrument
    ) -> None:
        """DESIGN §8.2 step 5, applied to the live-only gates: nothing trades, and the reason is
        in the log rather than in a stack trace."""
        gates = FakeReadiness("no ops alert destination is configured")
        stack = Stack(store, ledger, clock, instrument, readiness=gates)

        recovery = await stack.sequence.recover()

        assert recovery.halted
        assert recovery.failures == ("no ops alert destination is configured",)

    async def test_a_clean_gate_does_not_halt(
        self, store: EventStore, ledger: Ledger, clock: ManualClock, instrument: Instrument
    ) -> None:
        gates = FakeReadiness()
        stack = Stack(store, ledger, clock, instrument, readiness=gates)

        recovery = await stack.sequence.recover()

        assert not recovery.halted
        assert gates.ran == 1

    async def test_the_baselines_are_not_armed_behind_a_failed_gate(
        self, store: EventStore, ledger: Ledger, clock: ManualClock, instrument: Instrument
    ) -> None:
        """A system that never became ready should not have recorded a high-water mark for it."""
        stack = Stack(store, ledger, clock, instrument, readiness=FakeReadiness("panel is down"))

        await stack.sequence.recover()

        assert not stack.states.initialised()


class TestFirstRun:
    async def test_a_fresh_database_is_armed_and_may_trade(self, stack: Stack) -> None:
        recovery = await stack.sequence.recover()

        assert not recovery.halted
        assert recovery.state.kill_switch is KillSwitchState.ARMED
        assert recovery.state.high_water_mark == Decimal(10_000)

    async def test_the_baselines_start_from_actual_equity(
        self, stack: Stack, ledger: Ledger
    ) -> None:
        recovery = await stack.sequence.recover()

        assert recovery.state.day_start_equity == stack.portfolio.valuation().equity


class TestPersistedState:
    async def test_a_tripped_switch_stays_tripped_across_a_restart(self, stack: Stack) -> None:
        """The single most important property of the whole module."""
        await stack.sequence.recover()
        await stack.watchdog.trip("max_drawdown", "equity fell 12%")

        recovery = await stack.sequence.recover()

        assert recovery.halted
        assert recovery.state.kill_switch is KillSwitchState.TRIPPED
        assert "max_drawdown" in recovery.state.reason

    async def test_arming_never_clears_a_switch_tripped_by_a_breach(self, stack: Stack) -> None:
        await stack.sequence.recover()
        await stack.watchdog.trip("max_drawdown", "equity fell 12%")

        recovery = await stack.sequence.recover()

        assert recovery.state.kill_switch is KillSwitchState.TRIPPED

    async def test_a_halted_basket_stays_halted_and_is_excluded(
        self, stack: Stack, basket: Basket
    ) -> None:
        await stack.sequence.recover()
        await stack.watchdog.halt_basket(basket.basket_id, "reconciliation mismatch")

        recovery = await stack.sequence.recover()

        assert not recovery.halted, "one halted basket does not stop the process"
        assert not recovery.may_run(basket)
        assert recovery.halted_baskets == {basket.basket_id: "reconciliation mismatch"}


class TestReplay:
    async def test_the_projections_are_rebuilt_from_the_log(
        self, stack: Stack, store: EventStore
    ) -> None:
        await stack.sequence.recover()
        written = store.count()

        recovery = await stack.sequence.recover()

        assert written > 0
        assert recovery.replayed == written, "the read model is rebuilt from every event"

    async def test_the_ledger_is_rebuilt_from_fills_not_from_memory(
        self,
        store: EventStore,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """The log alone must reconstruct the ledger, or the log is not the source of truth."""
        ledger = Ledger(clock, venue="sim", balances={"USDT": Decimal(10_000)})
        stack = Stack(store, ledger, clock, instrument)
        broker = stack.broker
        assert isinstance(broker, SimBroker)
        broker.observe(tick(instrument, clock, "49000"))
        await stack.execution.submit(entry(instrument, clock, price="50000"), instrument)
        traded = ledger.position(instrument.key).qty

        fresh = Ledger(clock, venue="sim", balances={"USDT": Decimal(10_000)})
        fresh.replay(store.read_all(), {instrument.key: (instrument.base_currency, "USDT")})

        assert fresh.position(instrument.key).qty == traded > 0

    async def test_a_fill_on_an_unknown_instrument_halts_rather_than_being_skipped(
        self,
        store: EventStore,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        ledger = Ledger(clock, venue="sim", balances={"USDT": Decimal(10_000)})
        stack = Stack(store, ledger, clock, instrument)
        broker = stack.broker
        assert isinstance(broker, SimBroker)
        broker.observe(tick(instrument, clock, "49000"))
        await stack.execution.submit(entry(instrument, clock, price="50000"), instrument)

        blind = StartupSequence(
            store,
            Ledger(clock, venue="sim", balances={"USDT": Decimal(10_000)}),
            stack.reconciler,
            stack.execution,
            stack.monitor,
            stack.states,
            stack.watchdog,
            clock,
            instruments=(),
        )
        recovery = await blind.recover()

        assert recovery.halted
        assert any("replay failed" in failure for failure in recovery.failures)


class TestOpenOrderResolution:
    async def test_a_resting_order_is_adopted_and_monitored(
        self,
        store: EventStore,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """DESIGN §8.2 step 3: every non-terminal order becomes terminal or monitored."""
        ledger = Ledger(clock, venue="sim", balances={"USDT": Decimal(10_000)})
        stack = Stack(store, ledger, clock, instrument)
        broker = stack.broker
        assert isinstance(broker, SimBroker)
        broker.observe(tick(instrument, clock, "50000"))
        await stack.execution.submit(entry(instrument, clock), instrument)

        recovery = await stack.sequence.recover()

        assert [order.client_order_id for order in recovery.resolved] == ["sim-ENTRY"]
        assert stack.monitor.working, "an adopted order is polled, not forgotten"

    async def test_an_order_the_venue_no_longer_has_is_resolved_not_left_hanging(
        self,
        store: EventStore,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        ledger = Ledger(clock, venue="sim", balances={"USDT": Decimal(10_000)})
        stack = Stack(store, ledger, clock, instrument)
        broker = stack.broker
        assert isinstance(broker, SimBroker)
        broker.observe(tick(instrument, clock, "50000"))
        await stack.execution.submit(entry(instrument, clock), instrument)
        broker.wipe({"USDT": Decimal(10_000)})

        recovery = await stack.sequence.recover()

        assert recovery.halted, "a vanished order is a venue reset, not a routine cancellation"


class TestFailureHalts:
    async def test_an_unreachable_venue_halts_the_process(
        self,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        class Unreachable:
            venue_id = "sim"

            async def fetch_positions_and_balances(self) -> AccountState:
                raise VenueError("connection reset")

            async def fetch_open_orders(self) -> tuple[object, ...]:
                return ()

        stack = Stack(store, ledger, clock, instrument, broker=Unreachable())

        recovery = await stack.sequence.recover()

        assert recovery.halted
        assert any("reconciliation failed" in failure for failure in recovery.failures)

    async def test_a_failure_is_recorded_as_a_risk_event(
        self,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """A halt nobody can audit is not a control."""

        class Unreachable:
            venue_id = "sim"

            async def fetch_positions_and_balances(self) -> AccountState:
                raise VenueError("connection reset")

            async def fetch_open_orders(self) -> tuple[object, ...]:
                return ()

        stack = Stack(store, ledger, clock, instrument, broker=Unreachable())
        await stack.sequence.recover()

        risk = [e for e in store.read_all() if e.type is EventType.RISK_EVENT]
        assert risk and risk[-1].payload["rule"] == "startup_recovery"

    async def test_a_failed_recovery_never_arms_the_switch(
        self,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        class Unreachable:
            venue_id = "sim"

            async def fetch_positions_and_balances(self) -> AccountState:
                raise VenueError("connection reset")

            async def fetch_open_orders(self) -> tuple[object, ...]:
                return ()

        stack = Stack(store, ledger, clock, instrument, broker=Unreachable())

        recovery = await stack.sequence.recover()

        assert recovery.state.kill_switch is KillSwitchState.TRIPPED


class TestReadiness:
    async def test_a_healthy_system_may_run_its_baskets(self, stack: Stack, basket: Basket) -> None:
        recovery = await stack.sequence.recover()

        assert recovery.may_run(basket)

    async def test_a_halted_process_runs_nothing(self, stack: Stack, basket: Basket) -> None:
        await stack.sequence.recover()
        await stack.watchdog.trip("manual", "operator")

        recovery = await stack.sequence.recover()

        assert not recovery.may_run(basket)


def test_an_order_state_that_is_neither_open_nor_terminal_cannot_exist() -> None:
    """Step 3 enumerates non-terminal states; a gap there would leave an order unresolved."""
    unresolved = [s for s in OrderState if not s.is_terminal and not s.is_open]
    assert {s.value for s in unresolved} == {"pending_submit", "submit_unknown"}


class TestPendingSubmitRecovery:
    """An order committed but never acknowledged is `SUBMIT_UNKNOWN`, not a dead row.

    The gap this closes: syncing a `PENDING_SUBMIT` order directly asks the lifecycle to jump
    straight to whatever the venue reports, which the transition table rightly forbids — so the
    order would stay `PENDING_SUBMIT`, fail `is_open`, and never be tracked. A live order at the
    venue that nothing monitors is the outcome this whole sequence exists to prevent.
    """

    async def _persist_intent_only(
        self, stack: Stack, store: EventStore, clock: ManualClock, instrument: Instrument
    ) -> OrderIntent:
        """Write the durable record, exactly as `submit` does before the network call."""
        intent = entry(instrument, clock)
        order = Order.from_intent(intent)
        await store.append(stack.execution.events_for(order).order_submitted(order))
        return intent

    async def test_an_order_the_venue_has_is_adopted_and_monitored(
        self, stack: Stack, store: EventStore, clock: ManualClock, instrument: Instrument
    ) -> None:
        intent = await self._persist_intent_only(stack, store, clock, instrument)
        await stack.broker.submit(intent)  # type: ignore[attr-defined] — the venue did take it

        recovery = await stack.sequence.recover()

        assert not recovery.halted
        assert [order.state for order in recovery.resolved] == [OrderState.OPEN]
        assert intent.client_order_id in {o.client_order_id for o in stack.monitor.working}

    async def test_the_uncertainty_is_recorded_before_the_answer(
        self, stack: Stack, store: EventStore, clock: ManualClock, instrument: Instrument
    ) -> None:
        """Without this the audit trail claims we always knew where the order stood."""
        intent = await self._persist_intent_only(stack, store, clock, instrument)
        await stack.broker.submit(intent)  # type: ignore[attr-defined]

        await stack.sequence.recover()

        states = [
            event.payload.get("state")
            for event in store.read_all()
            if event.type is EventType.ORDER_STATE_CHANGED
        ]
        assert states[0] == OrderState.SUBMIT_UNKNOWN.value

    async def test_an_order_the_venue_never_saw_halts(
        self, stack: Stack, store: EventStore, clock: ManualClock, instrument: Instrument
    ) -> None:
        """A vanished order is never routine, whatever state our own record was in."""
        await self._persist_intent_only(stack, store, clock, instrument)

        recovery = await stack.sequence.recover()

        assert recovery.halted
        assert any("resolution failed" in failure for failure in recovery.failures)
