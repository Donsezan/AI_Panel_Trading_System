"""ExecutionService: the durability ordering, and every way a submit can go wrong.

The write-ahead rule is asserted directly, because it is the difference between a recoverable
crash and an orphan order at the venue (PLAN §1.4).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradebot.core.clock import ManualClock
from tradebot.core.enums import OrderState, OrderType, Side
from tradebot.core.errors import FailClosedError, SubmitUnknownError
from tradebot.core.events import EventType
from tradebot.core.instrument import Instrument
from tradebot.core.orders import Fill, OrderIntent
from tradebot.core.portfolio import AccountState
from tradebot.execution.service import ExecutionService
from tradebot.interfaces.broker import (
    BrokerCapabilities,
    CancelAck,
    OrderAck,
    OrderRef,
    OrderStatus,
)
from tradebot.ledger.portfolio import Ledger
from tradebot.persistence.store import EventStore

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
KEY = "sim:BTC/USDT"


class ScriptedBroker:
    """A broker whose every response the test dictates, including the ambiguous ones."""

    venue_id = "scripted"

    def __init__(
        self,
        *,
        ack_state: OrderState = OrderState.SUBMITTED,
        status: OrderStatus | None = None,
        raise_submit_unknown: bool = False,
    ) -> None:
        self._ack_state = ack_state
        self._status = status
        self._raise = raise_submit_unknown
        self.submits = 0

    async def submit(self, intent: OrderIntent) -> OrderAck:
        self.submits += 1
        if self._raise:
            raise SubmitUnknownError("timeout", client_order_id=intent.client_order_id)
        return OrderAck(
            client_order_id=intent.client_order_id,
            venue_order_id="v-1",
            state=self._ack_state,
            accepted_at=NOW,
            reject_reason="insufficient balance"
            if self._ack_state is OrderState.REJECTED
            else None,
        )

    async def cancel(self, order_ref: OrderRef) -> CancelAck:
        return CancelAck(client_order_id=order_ref.client_order_id, cancelled=True)

    async def fetch_order(self, order_ref: OrderRef) -> OrderStatus:
        if self._status is not None:
            return self._status
        return OrderStatus(
            client_order_id=order_ref.client_order_id,
            venue_order_id=None,
            instrument_key=order_ref.instrument_key,
            state=OrderState.REJECTED,
            requested_qty=Decimal(0),
            filled_qty=Decimal(0),
            observed_at=NOW,
            reject_reason="not found at venue",
            found=False,
        )

    async def fetch_open_orders(self) -> tuple[OrderStatus, ...]:
        return ()

    async def fetch_positions_and_balances(self) -> AccountState:
        return AccountState(venue=self.venue_id, observed_at=NOW)

    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(venue_id=self.venue_id, order_types=(OrderType.LIMIT,))


def make_intent(client_order_id: str = "sim-ABCDEF") -> OrderIntent:
    return OrderIntent(
        client_order_id=client_order_id,
        basket_id="b1",
        cycle_id="c1",
        instrument_key=KEY,
        side=Side.BUY,
        qty=Decimal("0.5"),
        order_type=OrderType.LIMIT,
        limit_price=Decimal("50000"),
        created_at=NOW,
    )


def status_with(state: OrderState, fills: tuple[Fill, ...] = (), filled: str = "0") -> OrderStatus:
    return OrderStatus(
        client_order_id="sim-ABCDEF",
        venue_order_id="v-1",
        instrument_key=KEY,
        state=state,
        requested_qty=Decimal("0.5"),
        filled_qty=Decimal(filled),
        fills=fills,
        observed_at=NOW,
    )


def make_fill(qty: str = "0.5") -> Fill:
    return Fill(
        fill_id="f-1",
        client_order_id="sim-ABCDEF",
        instrument_key=KEY,
        side=Side.BUY,
        qty=Decimal(qty),
        price=Decimal("50000"),
        fee=Decimal("25"),
        fee_currency="USDT",
        filled_at=NOW,
    )


def service(
    broker: ScriptedBroker, store: EventStore, ledger: Ledger, clock: ManualClock
) -> ExecutionService:
    return ExecutionService(broker, store, ledger, clock)


class TestDurability:
    async def test_the_intent_is_committed_before_the_network_call(
        self,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """A crash mid-submit must leave a recoverable trace, never an orphan order."""

        class ObservingBroker(ScriptedBroker):
            def __init__(self, store: EventStore) -> None:
                super().__init__(status=status_with(OrderState.OPEN))
                self._store = store
                self.log_at_submit: tuple[EventType, ...] = ()

            async def submit(self, intent: OrderIntent) -> OrderAck:
                self.log_at_submit = tuple(e.type for e in self._store.read_all())
                return await super().submit(intent)

        broker = ObservingBroker(store)
        await service(broker, store, ledger, clock).submit(make_intent(), instrument)
        assert EventType.ORDER_SUBMITTED in broker.log_at_submit


class TestOutcomes:
    async def test_a_filled_order_books_its_fills_into_the_ledger(
        self,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        broker = ScriptedBroker(status=status_with(OrderState.FILLED, (make_fill(),), "0.5"))
        order = await service(broker, store, ledger, clock).submit(make_intent(), instrument)
        assert order.state is OrderState.FILLED
        assert ledger.position(KEY).qty == Decimal("0.5")

    async def test_an_order_resting_open_records_the_state_without_fills(
        self,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        broker = ScriptedBroker(status=status_with(OrderState.OPEN))
        order = await service(broker, store, ledger, clock).submit(make_intent(), instrument)
        assert order.state is OrderState.OPEN
        assert ledger.position(KEY).is_flat

    async def test_a_rejected_order_is_a_result_not_an_exception(
        self,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        broker = ScriptedBroker(ack_state=OrderState.REJECTED)
        order = await service(broker, store, ledger, clock).submit(make_intent(), instrument)
        assert order.state is OrderState.REJECTED
        assert ledger.position(KEY).is_flat

    async def test_a_partial_fill_leaves_the_order_partially_filled(
        self,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        broker = ScriptedBroker(
            status=status_with(OrderState.PARTIALLY_FILLED, (make_fill("0.2"),), "0.2")
        )
        order = await service(broker, store, ledger, clock).submit(make_intent(), instrument)
        assert order.state is OrderState.PARTIALLY_FILLED
        assert order.remaining_qty == Decimal("0.3")
        assert ledger.position(KEY).qty == Decimal("0.2")


class TestSubmitUnknown:
    async def test_the_venue_is_queried_and_the_order_adopted(
        self,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """The only legal resolution: ask the venue by our own id (PLAN §2.3)."""
        broker = ScriptedBroker(
            raise_submit_unknown=True,
            status=status_with(OrderState.FILLED, (make_fill(),), "0.5"),
        )
        order = await service(broker, store, ledger, clock).submit(make_intent(), instrument)
        assert order.state is OrderState.FILLED
        assert broker.submits == 1, "there is no code path that resubmits"

    async def test_an_adopted_resting_order_keeps_its_venue_state(
        self,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        broker = ScriptedBroker(raise_submit_unknown=True, status=status_with(OrderState.OPEN))
        order = await service(broker, store, ledger, clock).submit(make_intent(), instrument)
        assert order.state is OrderState.OPEN

    async def test_a_vanished_order_halts_the_basket_for_human_review(
        self,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """An order that vanished after an ambiguous submit is never routine (REVIEW B4)."""
        broker = ScriptedBroker(raise_submit_unknown=True)  # venue has no record
        with pytest.raises(FailClosedError, match="halted for human review"):
            await service(broker, store, ledger, clock).submit(make_intent(), instrument)

        types = tuple(event.type for event in store.read_all())
        assert EventType.RISK_EVENT in types
        risk_event = next(e for e in store.read_all() if e.type is EventType.RISK_EVENT)
        assert risk_event.payload["rule"] == "order_vanished"
        assert risk_event.payload["action_taken"] == "basket_halted"

    async def test_the_submit_unknown_state_is_recorded_before_resolution(
        self,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        broker = ScriptedBroker(raise_submit_unknown=True, status=status_with(OrderState.OPEN))
        await service(broker, store, ledger, clock).submit(make_intent(), instrument)
        states = [
            event.payload["state"]
            for event in store.read_all()
            if event.type is EventType.ORDER_STATE_CHANGED
        ]
        assert states[0] == OrderState.SUBMIT_UNKNOWN.value


class TestVenueQuirks:
    async def test_a_state_we_have_already_passed_is_logged_not_forced(
        self,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """Venues report `OPEN` for an order we have booked a partial fill on. That is a quirk,
        not an error, and forcing the transition would raise on a perfectly normal response."""
        broker = ScriptedBroker(status=status_with(OrderState.OPEN, (make_fill("0.2"),), "0.2"))

        order = await service(broker, store, ledger, clock).submit(make_intent(), instrument)

        assert order.state is OrderState.PARTIALLY_FILLED
        assert ledger.position(KEY).qty == Decimal("0.2")

    async def test_cancelling_an_already_terminal_order_is_a_no_op(
        self,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        broker = ScriptedBroker(status=status_with(OrderState.FILLED, (make_fill(),), "0.5"))
        execution = service(broker, store, ledger, clock)
        order = await execution.submit(make_intent(), instrument)

        unchanged = await execution.cancel(order, reason="ttl", state=OrderState.EXPIRED)

        assert unchanged.state is OrderState.FILLED
