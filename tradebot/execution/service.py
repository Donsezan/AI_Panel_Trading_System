"""Submitting an order, and surviving the moment when we don't know whether it worked.

The ordering here is the single most important sequence in the system:

1. Write and **commit** the intent — including its `client_order_id` — to the event log.
2. Only then make the network call.

A crash between the two leaves a recoverable record rather than an orphan order at the venue
(PLAN §1.4). An ambiguous submit becomes `SUBMIT_UNKNOWN`, whose only legal resolutions are
querying the venue by our own id, or failing the order and halting the basket for human review.
**There is no code path in this module that resubmits** — that is the defence against the
duplicate-order-after-retry failure that dominates practitioner incident reports (PLAN §2.3).

Failure semantics:
* venue ambiguity      → `SUBMIT_UNKNOWN` → adopt what the venue has, else halt the basket
* venue rejects        → recorded as `REJECTED`; a normal outcome, not an exception
* transient venue error → propagates as `RetryableError` for the caller's retry budget
"""

from __future__ import annotations

from tradebot.core.clock import Clock
from tradebot.core.enums import OrderState
from tradebot.core.errors import FailClosedError, SubmitUnknownError
from tradebot.core.events import EventFactory
from tradebot.core.instrument import Instrument
from tradebot.core.logging import get_logger
from tradebot.core.orders import Fill, Order, OrderIntent
from tradebot.interfaces.broker import BrokerAdapter, OrderRef, OrderStatus
from tradebot.ledger.portfolio import Ledger
from tradebot.persistence.store import EventStore

logger = get_logger(__name__)


class ExecutionService:
    """Owns an order from intent to booked fills."""

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

    async def execute(
        self, intent: OrderIntent, instrument: Instrument, events: EventFactory
    ) -> Order:
        order = Order.from_intent(intent)

        # Durable first, network second. Never the other way round.
        await self._store.append(events.order_submitted(order))

        try:
            ack = await self._broker.submit(intent)
        except SubmitUnknownError:
            order = await self._enter_submit_unknown(order, events)
            status = await self._query(intent, order)
        else:
            order = await self._advance(order, OrderState.SUBMITTED, events)
            if ack.state is OrderState.REJECTED:
                return await self._advance(order, OrderState.REJECTED, events)
            status = await self._query(intent, order)

        return await self._book(order, status, instrument, events)

    async def _enter_submit_unknown(self, order: Order, events: EventFactory) -> Order:
        logger.error(
            "submit outcome unknown; querying venue by client_order_id",
            extra={"client_order_id": order.client_order_id},
        )
        return await self._advance(order, OrderState.SUBMIT_UNKNOWN, events)

    async def _query(self, intent: OrderIntent, order: Order) -> OrderStatus:
        return await self._broker.fetch_order(
            OrderRef(
                client_order_id=intent.client_order_id,
                instrument_key=intent.instrument_key,
                venue_order_id=order.venue_order_id,
            )
        )

    async def _advance(self, order: Order, state: OrderState, events: EventFactory) -> Order:
        previous = order.state
        moved = order.transition_to(state, at=self._clock.now())
        await self._store.append(events.order_state_changed(moved, previous))
        return moved

    async def _book(
        self, order: Order, status: OrderStatus, instrument: Instrument, events: EventFactory
    ) -> Order:
        """Fold the venue's view into ours: fills first, then any remaining state change."""
        if order.state is OrderState.SUBMIT_UNKNOWN and not status.fills:
            return await self._resolve_vanished(order, status, events)

        for fill in status.fills:
            order = await self._book_fill(order, fill, instrument, events)

        if not status.fills and status.state is not order.state:
            order = await self._advance(order, status.state, events)
        return order

    async def _book_fill(
        self, order: Order, fill: Fill, instrument: Instrument, events: EventFactory
    ) -> Order:
        order = order.with_fill(fill)
        position = self._ledger.apply_fill(
            fill,
            base_currency=instrument.base_currency,
            quote_currency=instrument.quote_currency,
        )
        await self._store.append(
            events.fill_received(fill, order), events.position_updated(position)
        )
        return order

    async def _resolve_vanished(
        self, order: Order, status: OrderStatus, events: EventFactory
    ) -> Order:
        """The venue has no record of an order we may have sent.

        A vanished order is never routine. It is marked failed and the basket halts for human
        review — the alternative, assuming it never landed and carrying on, is how a duplicate
        position gets created (PLAN §2.3, REVIEW B4).
        """
        if status.state is not OrderState.REJECTED:
            return await self._advance(order, status.state, events)

        failed = await self._advance(order, OrderState.FAILED, events)
        await self._store.append(
            events.risk_event(
                tier="execution",
                rule="submit_unknown_unresolved",
                scope=order.client_order_id,
                action="basket_halted",
                detail="venue has no record of an order whose submit outcome was unknown",
            )
        )
        raise FailClosedError(
            f"order {order.client_order_id} vanished after an ambiguous submit; "
            f"basket {failed.basket_id} halted for human review"
        )
