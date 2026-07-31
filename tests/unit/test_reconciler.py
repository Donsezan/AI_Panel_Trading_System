"""Reconciliation: the classification table, which *is* the response (DESIGN §6.8).

Getting this wrong is expensive in both directions, so both directions are tested. A monthly
testnet wipe misread as a mismatch trips the kill switch for a routine event (R15); a real
discrepancy misread as drift means trading on a position that does not exist (R5).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tradebot.core.clock import ManualClock
from tradebot.core.enums import Mode, OrderState, ReconcileClass, Side
from tradebot.core.errors import VenueError
from tradebot.core.events import EventType
from tradebot.core.instrument import Instrument
from tradebot.core.orders import Fill
from tradebot.core.portfolio import AccountState, Balance, Position
from tradebot.execution.brokers.sim import SimBroker, Tick
from tradebot.interfaces.broker import OrderStatus
from tradebot.ledger.portfolio import Ledger
from tradebot.ledger.reconciler import CorporateAction, Reconciler
from tradebot.persistence.store import EventStore

KEY = "sim:BTC/USDT"


class StubBroker:
    """A venue whose account state and open orders the test states outright."""

    venue_id = "sim"

    def __init__(self, state: AccountState, open_orders: tuple[object, ...] = ()) -> None:
        self._state = state
        self._open = open_orders

    async def fetch_positions_and_balances(self) -> AccountState:
        return self._state

    async def fetch_open_orders(self) -> tuple[object, ...]:
        return self._open


def venue_state(
    clock: ManualClock,
    *,
    qty: str | None = "0.5",
    usdt: str = "9000",
    locked: str = "0",
) -> AccountState:
    positions = (
        (Position(instrument_key=KEY, qty=Decimal(qty), avg_entry=Decimal("50000")),)
        if qty is not None
        else ()
    )
    return AccountState(
        venue="sim",
        positions=positions,
        balances=(Balance(currency="USDT", free=Decimal(usdt), locked=Decimal(locked)),),
        observed_at=clock.now(),
    )


def open_status(client_order_id: str, clock: ManualClock) -> OrderStatus:
    return OrderStatus(
        client_order_id=client_order_id,
        venue_order_id="v1",
        instrument_key=KEY,
        state=OrderState.OPEN,
        requested_qty=Decimal("0.5"),
        filled_qty=Decimal(0),
        observed_at=clock.now(),
    )


def held_ledger(clock: ManualClock, *, qty: str = "0.5", usdt: str = "9000") -> Ledger:
    ledger = Ledger(clock, venue="sim", balances={"USDT": Decimal(usdt)})
    if Decimal(qty) > 0:
        ledger.apply_fill(
            Fill(
                fill_id="f1",
                client_order_id="sim-ENTRY",
                instrument_key=KEY,
                side=Side.BUY,
                qty=Decimal(qty),
                price=Decimal("50000"),
                filled_at=clock.now(),
            ),
            base_currency="BTC",
            quote_currency="USDT",
        )
        ledger._balances["USDT"] = Decimal(usdt)
    return ledger


def reconciler(
    broker: object,
    ledger: Ledger,
    store: EventStore,
    clock: ManualClock,
    instrument: Instrument,
    **kwargs: object,
) -> Reconciler:
    return Reconciler(
        broker,  # type: ignore[arg-type]
        ledger,
        store,
        clock,
        mode=Mode.SIM,
        instruments=(instrument.model_copy(update={"symbol": "BTC/USDT", "venue": "sim"}),),
        **kwargs,  # type: ignore[arg-type]
    )


class TestClassification:
    async def test_identical_state_is_a_match(
        self, store: EventStore, clock: ManualClock, instrument: Instrument
    ) -> None:
        ledger = held_ledger(clock)
        broker = StubBroker(venue_state(clock))

        report = await reconciler(broker, ledger, store, clock, instrument).reconcile()

        assert report.classification is ReconcileClass.MATCH
        assert report.clean

    async def test_a_small_shortfall_is_drift_and_is_absorbed(
        self, store: EventStore, clock: ManualClock, instrument: Instrument
    ) -> None:
        """Fees, funding and dust: small, and only ever against us."""
        ledger = held_ledger(clock)
        broker = StubBroker(venue_state(clock, qty="0.4999"))

        report = await reconciler(broker, ledger, store, clock, instrument).reconcile()

        assert report.classification is ReconcileClass.DRIFT
        assert report.clean
        assert ledger.position(KEY).qty == Decimal("0.4999")

    async def test_an_unexplained_increase_is_an_external_change(
        self, store: EventStore, clock: ManualClock, instrument: Instrument
    ) -> None:
        """Nothing we do creates funds, so a gain we did not trade for came from outside."""
        ledger = held_ledger(clock)
        broker = StubBroker(venue_state(clock, usdt="14000"))

        report = await reconciler(broker, ledger, store, clock, instrument).reconcile()

        assert report.classification is ReconcileClass.EXTERNAL_CHANGE
        assert report.clean
        changes = [e for e in store.read_all() if e.type is EventType.EXTERNAL_CHANGE]
        assert changes and changes[0].payload["amount"] == "5000"

    async def test_an_unexplained_shortfall_is_a_mismatch_and_halts(
        self, store: EventStore, clock: ManualClock, instrument: Instrument
    ) -> None:
        """It could be a manual sell — or a fill we never booked. Only one of those is safe."""
        ledger = held_ledger(clock)
        broker = StubBroker(venue_state(clock, qty="0.2"))

        report = await reconciler(broker, ledger, store, clock, instrument).reconcile()

        assert report.classification is ReconcileClass.MISMATCH
        assert not report.clean
        assert ledger.position(KEY).qty == Decimal("0.5"), "nothing is adopted from a mismatch"

    async def test_a_matched_corporate_action_is_not_a_mismatch(
        self, store: EventStore, clock: ManualClock, instrument: Instrument
    ) -> None:
        """A 2-for-1 split doubles the position; halting for it would be a false alarm (R14)."""
        ledger = held_ledger(clock)
        broker = StubBroker(venue_state(clock, qty="1.0"))
        engine = reconciler(
            broker,
            ledger,
            store,
            clock,
            instrument,
            corporate_actions=(
                CorporateAction(instrument_key=KEY, ratio=Decimal(2), detail="2-for-1 split"),
            ),
        )

        report = await engine.reconcile()

        assert report.classification is ReconcileClass.CORPORATE_ACTION
        assert [e.type for e in store.read_all()].count(EventType.CORPORATE_ACTION) == 1

    async def test_an_unmatched_action_sized_change_is_still_a_mismatch(
        self, store: EventStore, clock: ManualClock, instrument: Instrument
    ) -> None:
        ledger = held_ledger(clock)
        broker = StubBroker(venue_state(clock, qty="1.5"))
        engine = reconciler(
            broker,
            ledger,
            store,
            clock,
            instrument,
            corporate_actions=(CorporateAction(instrument_key=KEY, ratio=Decimal(2)),),
        )

        report = await engine.reconcile()

        assert report.classification is ReconcileClass.EXTERNAL_CHANGE

    async def test_every_position_vanishing_at_once_is_a_venue_reset(
        self, store: EventStore, clock: ManualClock, instrument: Instrument
    ) -> None:
        """A testnet wipe is routine and must not trip the kill switch (R15)."""
        ledger = held_ledger(clock)
        broker = StubBroker(venue_state(clock, qty=None, usdt="9000"))

        report = await reconciler(broker, ledger, store, clock, instrument).reconcile()

        assert report.classification is ReconcileClass.VENUE_RESET
        assert not report.clean


class TestOrderAdoption:
    async def test_our_orders_are_adopted_and_a_humans_are_left_alone(
        self, store: EventStore, clock: ManualClock, instrument: Instrument
    ) -> None:
        ledger = held_ledger(clock)
        broker = StubBroker(
            venue_state(clock),
            (open_status("sim-OURS", clock), open_status("manual-order-42", clock)),
        )

        report = await reconciler(broker, ledger, store, clock, instrument).reconcile()

        assert report.adopted_orders == ("sim-OURS",)
        assert report.foreign_orders == ("manual-order-42",)


class TestSeverity:
    async def test_a_report_takes_its_worst_line_not_its_last(
        self, store: EventStore, clock: ManualClock, instrument: Instrument
    ) -> None:
        ledger = held_ledger(clock)
        broker = StubBroker(venue_state(clock, qty="0.2", usdt="14000"))

        report = await reconciler(broker, ledger, store, clock, instrument).reconcile()

        assert report.classification is ReconcileClass.MISMATCH

    async def test_a_large_mismatch_exceeds_the_kill_tolerance(
        self, store: EventStore, clock: ManualClock, instrument: Instrument
    ) -> None:
        ledger = held_ledger(clock)
        broker = StubBroker(venue_state(clock, usdt="1000"))
        engine = reconciler(broker, ledger, store, clock, instrument)

        report = await engine.reconcile()

        assert engine.exceeds_kill_tolerance(report, Decimal("10000"))

    async def test_a_small_mismatch_halts_but_does_not_kill(
        self, store: EventStore, clock: ManualClock, instrument: Instrument
    ) -> None:
        ledger = held_ledger(clock)
        broker = StubBroker(venue_state(clock, usdt="8900"))
        engine = reconciler(broker, ledger, store, clock, instrument)

        report = await engine.reconcile()

        assert report.classification is ReconcileClass.MISMATCH
        assert not engine.exceeds_kill_tolerance(report, Decimal("10000"))

    async def test_a_mismatch_that_cannot_be_sized_counts_as_severe(
        self, store: EventStore, clock: ManualClock, instrument: Instrument
    ) -> None:
        ledger = held_ledger(clock)
        broker = StubBroker(venue_state(clock, usdt="1000"))
        engine = reconciler(broker, ledger, store, clock, instrument)

        report = await engine.reconcile()

        assert engine.exceeds_kill_tolerance(report, Decimal(0))

    async def test_a_clean_report_never_exceeds_the_kill_tolerance(
        self, store: EventStore, clock: ManualClock, instrument: Instrument
    ) -> None:
        ledger = held_ledger(clock)
        engine = reconciler(StubBroker(venue_state(clock)), ledger, store, clock, instrument)

        report = await engine.reconcile()

        assert not engine.exceeds_kill_tolerance(report, Decimal("10000"))


class TestSideEffects:
    async def test_every_reconciliation_is_recorded(
        self, store: EventStore, clock: ManualClock, instrument: Instrument
    ) -> None:
        ledger = held_ledger(clock)
        await reconciler(
            StubBroker(venue_state(clock)), ledger, store, clock, instrument
        ).reconcile()

        assert any(e.type is EventType.RECONCILED for e in store.read_all())

    async def test_an_unclean_report_emits_a_risk_event(
        self, store: EventStore, clock: ManualClock, instrument: Instrument
    ) -> None:
        ledger = held_ledger(clock)
        broker = StubBroker(venue_state(clock, qty="0.2"))

        await reconciler(broker, ledger, store, clock, instrument).reconcile()

        risk = [e for e in store.read_all() if e.type is EventType.RISK_EVENT]
        assert risk and risk[0].payload["action_taken"] == "halt"

    async def test_locked_funds_are_adopted_from_the_venue(
        self, store: EventStore, clock: ManualClock, instrument: Instrument
    ) -> None:
        """What a resting order ties up is venue truth, not something to mirror separately."""
        ledger = held_ledger(clock)
        broker = StubBroker(venue_state(clock, locked="1500"))

        await reconciler(broker, ledger, store, clock, instrument).reconcile()

        usdt = ledger.snapshot().balance("USDT")
        assert usdt is not None
        assert usdt.locked == Decimal("1500")

    async def test_cash_flows_move_the_ledger_and_are_reported_for_the_baselines(
        self, store: EventStore, clock: ManualClock, instrument: Instrument
    ) -> None:
        ledger = held_ledger(clock)
        broker = StubBroker(venue_state(clock, usdt="14000"))
        engine = reconciler(broker, ledger, store, clock, instrument)

        flows = engine.apply_external_flows(await engine.reconcile())

        assert [flow.amount for flow in flows] == [Decimal("5000")]
        assert ledger.balance("USDT") == Decimal("14000")

    async def test_a_position_change_is_not_reported_as_a_cash_flow(
        self, store: EventStore, clock: ManualClock, instrument: Instrument
    ) -> None:
        """Only currency movements adjust the baselines; a position is marked, not banked."""
        ledger = held_ledger(clock)
        broker = StubBroker(venue_state(clock, qty="0.9"))
        engine = reconciler(broker, ledger, store, clock, instrument)

        flows = engine.apply_external_flows(await engine.reconcile())

        assert flows == ()


class TestVenueAvailability:
    async def test_a_venue_error_propagates_and_adopts_nothing(
        self, store: EventStore, clock: ManualClock, instrument: Instrument
    ) -> None:
        """A half-applied reconciliation is worse than none."""

        class Unreachable:
            venue_id = "sim"

            async def fetch_positions_and_balances(self) -> AccountState:
                raise VenueError("connection reset")

            async def fetch_open_orders(self) -> tuple[object, ...]:
                return ()

        ledger = held_ledger(clock)
        with pytest.raises(VenueError):
            await reconciler(Unreachable(), ledger, store, clock, instrument).reconcile()

        assert ledger.position(KEY).qty == Decimal("0.5")


def test_only_explainable_classifications_permit_trading() -> None:
    """The default — reached when nothing matched — must be the one that stops trading."""
    clean = {c for c in ReconcileClass if c.is_clean}
    assert clean == {
        ReconcileClass.MATCH,
        ReconcileClass.DRIFT,
        ReconcileClass.EXTERNAL_CHANGE,
    }


def test_the_sim_broker_can_produce_a_venue_reset(clock: ManualClock) -> None:
    """The scenario the classifier is written for is reproducible, not hypothetical."""
    broker = SimBroker(clock, balances={"USDT": Decimal(10_000)})
    broker.observe(
        Tick(
            instrument_key=KEY,
            bid=Decimal("50000"),
            ask=Decimal("50000"),
            last=Decimal("50000"),
            high=Decimal("50000"),
            low=Decimal("50000"),
            covers_since=clock.now(),
            observed_at=clock.now(),
        )
    )
    broker.wipe({"USDT": Decimal(10_000)})
    assert broker._qty == {}
