"""Order intents, orders, fills, and the risk provenance attached to every one of them.

Positions and balances update from **fills only**, never from an order reaching a terminal
state. Partial fills are the normal case, not an edge case (PLAN §2.5).

An entry and its protective legs form a **group** keyed by the entry's `client_order_id`: one
leg filling cancels the sibling, and a partial entry fill resizes the legs down to what is
actually held (DESIGN §6.7). The group id is derived rather than generated, so it survives a
crash without needing to have been stored separately.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from pydantic import model_validator

from tradebot.core.enums import OrderRole, OrderState, OrderType, RiskDecision, Side
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


class ProtectivePlan(DomainModel):
    """Where the exits sit for an entry, decided by Tier-1 risk and placed by execution.

    Risk owns these prices because `risk_amount` in the sizing formula is only a truthful
    "amount at risk" if a stop actually sits at `stop_multiple × ATR` (DESIGN §6.6). Execution
    owns *placing* them. Carrying the plan on the order is what lets recovery rebuild a leg
    after a crash without recomputing ATR from data that has since moved.
    """

    stop_price: Money
    take_profit_price: Money | None = None
    #: How far through the trigger the leg's limit sits, so a triggered stop can actually fill
    #: instead of resting untouched while the market runs away from it.
    limit_offset_pct: Money = Decimal("0.5")

    @model_validator(mode="after")
    def _check_prices(self) -> ProtectivePlan:
        if self.stop_price <= ZERO:
            raise ValueError(f"stop price must be positive, got {self.stop_price}")
        if self.limit_offset_pct < ZERO:
            raise ValueError("limit_offset_pct must not be negative")
        return self


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
    #: Trigger price for a protective leg. The leg becomes a limit order once the venue's last
    #: trade crosses it.
    stop_price: Money | None = None
    role: OrderRole = OrderRole.ENTRY
    #: The entry's `client_order_id`. An entry is its own group, so this is never empty.
    group_id: str = ""
    #: Where this entry's protective legs will sit. `None` means the position is unguarded
    #: between cycles, which Tier-1 has already priced in as a sizing haircut.
    protective: ProtectivePlan | None = None
    ttl_seconds: int | None = None
    risk_checks: tuple[RiskCheckResult, ...] = ()
    created_at: UtcDatetime

    @model_validator(mode="after")
    def _check_submittable(self) -> OrderIntent:
        if self.qty <= ZERO:
            raise ValueError(f"intent quantity must be positive, got {self.qty}")
        if self.order_type is not OrderType.MARKET and self.limit_price is None:
            raise ValueError(f"{self.order_type} requires a limit_price")
        if self.order_type.needs_stop_price and self.stop_price is None:
            raise ValueError(f"{self.order_type} requires a stop_price")
        if any(check.blocked for check in self.risk_checks):
            raise ValueError("a vetoed proposal must never become an OrderIntent")
        return self

    @model_validator(mode="before")
    @classmethod
    def _default_group(cls, data: Any) -> Any:
        """An entry is its own group. Applied *before* validation: an `after` validator that
        returns a copy is silently discarded when the model is built by `__init__`."""
        if isinstance(data, dict) and not data.get("group_id"):
            return {**data, "group_id": data.get("client_order_id", "")}
        return data

    @property
    def notional(self) -> Money:
        return multiply(self.qty, self.limit_price) if self.limit_price else ZERO

    def expires_at(self) -> UtcDatetime | None:
        """When the bot cancels the remainder. TTL is bot-enforced — Binance spot has no GTT."""
        if self.ttl_seconds is None:
            return None
        return self.created_at + timedelta(seconds=self.ttl_seconds)


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
    stop_price: Money | None = None
    role: OrderRole = OrderRole.ENTRY
    group_id: str = ""
    protective: ProtectivePlan | None = None
    expires_at: UtcDatetime | None = None
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
            stop_price=intent.stop_price,
            role=intent.role,
            group_id=intent.group_id,
            protective=intent.protective,
            expires_at=intent.expires_at(),
            created_at=intent.created_at,
            updated_at=intent.created_at,
        )

    def is_expired(self, now: UtcDatetime) -> bool:
        """True once the bot-enforced TTL has passed and the order is still working."""
        return self.expires_at is not None and now >= self.expires_at and self.state.is_open

    def new_fills(self, observed: tuple[Fill, ...]) -> tuple[Fill, ...]:
        """Fills the venue reports that we have not booked yet.

        The monitor re-reads the same order every poll, so booking is idempotent by fill id.
        Without this a position would grow by the whole order on every poll.
        """
        known = {fill.fill_id for fill in self.fills}
        return tuple(fill for fill in observed if fill.fill_id not in known)

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
        if target is not self.state:
            assert_legal_transition(self.state, target)
        return self.model_copy(
            update={"fills": fills, "state": target, "updated_at": fill.filled_at}
        )
