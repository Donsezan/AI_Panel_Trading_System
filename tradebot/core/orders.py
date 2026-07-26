"""Order intents, orders, fills, and the risk provenance attached to every one of them.

Positions and balances update from **fills only**, never from an order reaching a terminal
state. Partial fills are the normal case, not an edge case (PLAN §2.5).
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import model_validator

from tradebot.core.enums import OrderState, OrderType, RiskDecision, Side
from tradebot.core.errors import IllegalTransitionError
from tradebot.core.money import ZERO, divide, multiply
from tradebot.core.schema import DomainModel, Money, UtcDatetime

#: The order lifecycle, as an explicit transition table (DESIGN §6.7). Anything absent is
#: illegal and raises — an order in an impossible state must never reach a venue.
LEGAL_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.PENDING_SUBMIT: frozenset(
        {OrderState.SUBMITTED, OrderState.SUBMIT_UNKNOWN, OrderState.REJECTED, OrderState.FAILED}
    ),
    OrderState.SUBMITTED: frozenset(
        {
            OrderState.OPEN,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
            OrderState.REJECTED,
        }
    ),
    # The only exits are "adopt what the venue actually has" and "give up and halt the basket".
    OrderState.SUBMIT_UNKNOWN: frozenset(
        {
            OrderState.OPEN,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
            OrderState.REJECTED,
            OrderState.FAILED,
        }
    ),
    OrderState.OPEN: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
        }
    ),
    OrderState.PARTIALLY_FILLED: frozenset(
        {OrderState.FILLED, OrderState.CANCELLED, OrderState.EXPIRED}
    ),
    OrderState.FILLED: frozenset(),
    OrderState.CANCELLED: frozenset(),
    OrderState.EXPIRED: frozenset(),
    OrderState.REJECTED: frozenset(),
    OrderState.FAILED: frozenset(),
}


def assert_legal_transition(current: OrderState, target: OrderState) -> None:
    """Raise unless `current → target` is a transition the lifecycle permits."""
    if target not in LEGAL_TRANSITIONS[current]:
        raise IllegalTransitionError(f"illegal order transition {current} → {target}")


class RiskCheckResult(DomainModel):
    """One rule's verdict, with the numbers that produced it.

    Every intent carries its full set, so "why was this order this size" is answerable from the
    event log alone rather than by re-running the risk engine against changed state.
    """

    rule: str
    decision: RiskDecision
    detail: str = ""
    limit: Money | None = None
    observed: Money | None = None
    #: The largest quantity this rule permits, if it caps at all. Rules express *caps* rather
    #: than mutating a size, so the engine composes them with `min()` and no rule ordering
    #: can accidentally widen a limit an earlier rule imposed.
    max_qty: Money | None = None

    @property
    def blocked(self) -> bool:
        return self.decision is RiskDecision.VETO


class OrderIntent(DomainModel):
    """A risk-approved, sized, ready-to-submit instruction.

    Written and committed to the DB *before* the network call, so a crash mid-submit leaves a
    recoverable trace rather than an orphan order at the venue (PLAN §1.4).
    """

    client_order_id: str
    basket_id: str
    cycle_id: str
    instrument_key: str
    side: Side
    qty: Money
    order_type: OrderType = OrderType.LIMIT
    limit_price: Money | None = None
    ttl_seconds: int | None = None
    risk_checks: tuple[RiskCheckResult, ...] = ()
    created_at: UtcDatetime

    @model_validator(mode="after")
    def _check_submittable(self) -> OrderIntent:
        if self.qty <= ZERO:
            raise ValueError(f"intent quantity must be positive, got {self.qty}")
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit order requires a limit_price")
        if any(check.blocked for check in self.risk_checks):
            raise ValueError("a vetoed proposal must never become an OrderIntent")
        return self

    @property
    def notional(self) -> Money:
        return multiply(self.qty, self.limit_price) if self.limit_price else ZERO


class Fill(DomainModel):
    """An execution. The only thing that may move a position or a balance."""

    fill_id: str
    client_order_id: str
    instrument_key: str
    side: Side
    qty: Money
    price: Money
    fee: Money = Decimal(0)
    fee_currency: str = ""
    filled_at: UtcDatetime

    @model_validator(mode="after")
    def _check_positive(self) -> Fill:
        if self.qty <= ZERO or self.price <= ZERO:
            raise ValueError("a fill must have positive quantity and price")
        return self

    @property
    def notional(self) -> Money:
        return multiply(self.qty, self.price)


class Order(DomainModel):
    """An order and its lifecycle, as the system currently understands it.

    `state` is our belief; the venue is the truth. The reconciler exists precisely because the
    two can differ (DESIGN §6.8).
    """

    client_order_id: str
    basket_id: str
    cycle_id: str
    instrument_key: str
    side: Side
    qty: Money
    order_type: OrderType
    limit_price: Money | None = None
    state: OrderState = OrderState.PENDING_SUBMIT
    venue_order_id: str | None = None
    fills: tuple[Fill, ...] = ()
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @classmethod
    def from_intent(cls, intent: OrderIntent) -> Order:
        return cls(
            client_order_id=intent.client_order_id,
            basket_id=intent.basket_id,
            cycle_id=intent.cycle_id,
            instrument_key=intent.instrument_key,
            side=intent.side,
            qty=intent.qty,
            order_type=intent.order_type,
            limit_price=intent.limit_price,
            created_at=intent.created_at,
            updated_at=intent.created_at,
        )

    @property
    def filled_qty(self) -> Money:
        return sum((fill.qty for fill in self.fills), start=ZERO)

    @property
    def remaining_qty(self) -> Money:
        return self.qty - self.filled_qty

    @property
    def avg_fill_price(self) -> Money:
        filled = self.filled_qty
        if filled <= ZERO:
            return ZERO
        return divide(sum((fill.notional for fill in self.fills), start=ZERO), filled)

    @property
    def fill_ratio(self) -> Money:
        return divide(self.filled_qty, self.qty)

    def transition_to(self, state: OrderState, *, at: UtcDatetime) -> Order:
        """Return this order in `state`, raising if the lifecycle forbids the move."""
        assert_legal_transition(self.state, state)
        return self.model_copy(update={"state": state, "updated_at": at})

    def with_fill(self, fill: Fill) -> Order:
        """Book a fill and advance the state to match what is now executed.

        Over-filling is a venue-truth contradiction rather than an arithmetic detail, so it
        raises instead of silently clamping.
        """
        if fill.client_order_id != self.client_order_id:
            raise IllegalTransitionError(
                f"fill {fill.fill_id} belongs to {fill.client_order_id}, not {self.client_order_id}"
            )
        fills = (*self.fills, fill)
        total = sum((f.qty for f in fills), start=ZERO)
        if total > self.qty:
            raise IllegalTransitionError(
                f"fills {total} exceed order quantity {self.qty} on {self.client_order_id}"
            )
        target = OrderState.FILLED if total == self.qty else OrderState.PARTIALLY_FILLED
        assert_legal_transition(self.state, target)
        return self.model_copy(
            update={"fills": fills, "state": target, "updated_at": fill.filled_at}
        )
