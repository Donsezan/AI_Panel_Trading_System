"""Submitting an order, and surviving the moment when we don't know whether it worked.

The ordering here is the single most important sequence in the system:

1. Write and **commit** the intent — including its `client_order_id` — to the event log.
2. Only then make the network call.

A crash between the two leaves a recoverable record rather than an orphan order at the venue
(PLAN §1.4). An ambiguous submit becomes `SUBMIT_UNKNOWN`, whose only legal resolutions are
querying the venue by our own id, or failing the order and halting the basket for human review.
**There is no code path in this module that resubmits** — that is the defence against the
duplicate-order-after-retry failure that dominates practitioner incident reports (PLAN §2.3).

Booking is idempotent by fill id, because the `ExecutionMonitor` re-reads the same order every
poll and a fill counted twice is a position that does not exist.

Failure semantics:
* venue ambiguity      → `SUBMIT_UNKNOWN` → adopt what the venue has, else halt the basket
* venue rejects        → recorded as `REJECTED`; a normal outcome, not an exception
* transient venue error → propagates as `RetryableError` for the caller's retry budget
"""

from __future__ import annotations

from tradebot.core.clock import Clock
from tradebot.core.enums import OrderState, RiskTier
from tradebot.core.errors import FailClosedError, SubmitUnknownError
from tradebot.core.events import EventFactory
from tradebot.core.instrument import Instrument
from tradebot.core.logging import get_logger
from tradebot.core.orders import LEGAL_TRANSITIONS, Fill, Order, OrderIntent
from tradebot.interfaces.broker import BrokerAdapter, OrderRef, OrderStatus
from tradebot.ledger.portfolio import Ledger
from tradebot.persistence.store import EventStore

logger = get_logger(__name__)


class ExecutionService:
    """Owns the durable record of an order and the booking of its fills."""

    def __init__(
        self,
        broker: BrokerAdapter,
        store: EventStore,
        ledger: Ledger,
        clock: Clock,
    ) -> None:
        self._broker = broker
        self._store = store
        self._ledger = ledger
        self._clock = clock

    def events_for(self, order: Order) -> EventFactory:
        """Correlate events with the cycle the order came from, even long after it ended."""
        return EventFactory(clock=self._clock, basket_id=order.basket_id, cycle_id=order.cycle_id)

    async def submit(self, intent: OrderIntent, instrument: Instrument) -> Order:
        """Record the intent, submit it, and fold in whatever the venue reports back."""
        order = Order.from_intent(intent)
        events = self.events_for(order)

        # Durable first, network second. Never the other way round.
        await self._store.append(events.order_submitted(order))

        try:
            ack = await self._broker.submit(intent)
        except SubmitUnknownError:
            order = await self._enter_submit_unknown(order, events)
        else:
            order = await self._advance(order, OrderState.SUBMITTED, events)
            order = order.model_copy(update={"venue_order_id": ack.venue_order_id})
            if ack.state is OrderState.REJECTED:
                return await self._advance(order, OrderState.REJECTED, events)

        return await self.sync(order, instrument)

    async def sync(self, order: Order, instrument: Instrument) -> Order:
        """Re-read the order at the venue and fold its truth into ours."""
        status = await self._broker.fetch_order(
            OrderRef(
                client_order_id=order.client_order_id,
                instrument_key=order.instrument_key,
                venue_order_id=order.venue_order_id,
            )
        )
        return await self.apply(order, status, instrument)

    async def apply(self, order: Order, status: OrderStatus, instrument: Instrument) -> Order:
        """Book any fills we have not seen, then adopt the venue's state."""
        events = self.events_for(order)
        if not status.found:
            return await self._resolve_vanished(order, events)

        for fill in order.new_fills(status.fills):
            order = await self._book_fill(order, fill, instrument, events)

        return await self._adopt_state(order, status, events)

    async def _adopt_state(self, order: Order, status: OrderStatus, events: EventFactory) -> Order:
        """Move to the venue's state where the lifecycle allows it.

        Venues legitimately report a state we have already moved past — `OPEN` for an order we
        have booked a partial fill on, for instance. Consulting the transition table rather than
        forcing the move keeps that quirk from raising, while a genuinely impossible report is
        logged instead of silently adopted.
        """
        if status.state is order.state:
            return order
        if status.state in LEGAL_TRANSITIONS[order.state]:
            return await self._advance(order, status.state, events)
        logger.info(
            "venue reports a state the lifecycle has already passed",
            extra={
                "client_order_id": order.client_order_id,
                "ours": order.state.value,
                "theirs": status.state.value,
            },
        )
        return order

    async def cancel(self, order: Order, *, reason: str, state: OrderState) -> Order:
        """Cancel the working remainder and record why. Already-terminal orders are left alone."""
        if not order.state.is_open:
            return order
        events = self.events_for(order)
        ack = await self._broker.cancel(
            OrderRef(
                client_order_id=order.client_order_id,
                instrument_key=order.instrument_key,
                venue_order_id=order.venue_order_id,
            )
        )
        logger.info(
            "cancelled working order",
            extra={
                "client_order_id": order.client_order_id,
                "reason": reason,
                "cancelled": ack.cancelled,
                "fill_ratio": str(order.fill_ratio),
            },
        )
        return await self._advance(order, state, events)

    # ------------------------------------------------------------------ internals

    async def _enter_submit_unknown(self, order: Order, events: EventFactory) -> Order:
        logger.error(
            "submit outcome unknown; querying venue by client_order_id",
            extra={"client_order_id": order.client_order_id},
        )
        return await self._advance(order, OrderState.SUBMIT_UNKNOWN, events)

    async def _advance(self, order: Order, state: OrderState, events: EventFactory) -> Order:
        previous = order.state
        moved = order.transition_to(state, at=self._clock.now())
        await self._store.append(events.order_state_changed(moved, previous))
        return moved

    async def _book_fill(
        self, order: Order, fill: Fill, instrument: Instrument, events: EventFactory
    ) -> Order:
        order = order.with_fill(fill)
        booking = self._ledger.apply_fill(
            fill,
            base_currency=instrument.base_currency,
            quote_currency=instrument.quote_currency,
        )
        records = [events.fill_received(fill, order), events.position_updated(booking.position)]
        if booking.round_trip is not None:
            records.append(events.round_trip_closed(booking.round_trip))
        await self._store.append(*records)
        return order

    async def _resolve_vanished(self, order: Order, events: EventFactory) -> Order:
        """The venue has no record of an order we believe exists.

        Never routine, and deliberately distinguished from a *rejection*: a rejection is a
        definite answer that the order did not execute, while "no record" may mean it was lost,
        or that we are querying the wrong account. Assuming it never landed and carrying on is
        how a duplicate position gets created (PLAN §2.3, REVIEW B4).
        """
        failed = await self._advance(order, OrderState.FAILED, events)
        await self._store.append(
            events.risk_event(
                tier=RiskTier.EXECUTION,
                rule="order_vanished",
                scope=order.client_order_id,
                action="basket_halted",
                detail="the venue has no record of an order we believe we placed",
            )
        )
        raise FailClosedError(
            f"order {order.client_order_id} has vanished from the venue; "
            f"basket {failed.basket_id} halted for human review"
        )
