"""Turning an entry fill into venue-held protective legs (DESIGN §6.7).

A cycle-based system cannot babysit a stop: between cycles the *venue* has to hold it. So every
entry fill is immediately followed by linked exit legs implementing the Tier-1 SL/TP policy that
sized the trade in the first place — without them, `risk_amount` in the sizing formula is a
number nobody is honouring.

Two rules here exist to stop the protection from becoming the hazard:

* **Legs are sized to what actually filled**, never to what was ordered. A leg for the full
  order quantity after a half fill would try to sell more than is held.
* **Without venue-side OCO, only the stop is placed.** Two unlinked exit orders on one holding
  can both fill, and the second sells a position that is no longer there — a short in a
  long-only system. A take-profit is an optimisation; a double sell is an incident.

Failure semantics: a leg that cannot be expressed at venue precision (below `min_qty` or
`min_notional`) is *not* silently skipped — `plan_legs` returns the reason, the caller records
an `unprotected_position` risk event, and the operator can see that the guard is missing.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tradebot.core.enums import OrderRole, Side
from tradebot.core.ids import protective_order_id
from tradebot.core.instrument import Instrument
from tradebot.core.money import ZERO, multiply, percent_of, quantize_order, quantize_price
from tradebot.core.orders import Order, OrderIntent, ProtectivePlan
from tradebot.core.schema import UtcDatetime
from tradebot.interfaces.broker import BrokerCapabilities

#: The exit side for a position opened on each side. v1 is long-only, so in practice only BUY
#: entries occur; the table keeps the module honest rather than assuming.
_EXIT_SIDE: dict[Side, Side] = {Side.BUY: Side.SELL, Side.SELL: Side.BUY}

#: Which way a leg's limit sits relative to its trigger, keyed on the *exit* side.
#:
#: Through the trigger, for both legs. A stop must cross to escape a falling market; a
#: take-profit must cross to actually realise the gain — a target that triggers and then rests
#: unfilled is not a conservative exit, it is a missing one, and it leaves the OCO group open.
_OFFSET_SIGN: dict[Side, Decimal] = {Side.SELL: Decimal(-1), Side.BUY: Decimal(1)}


@dataclass(frozen=True, slots=True)
class LegPlan:
    """The legs to place for one entry, or the reason there are none."""

    intents: tuple[OrderIntent, ...] = ()
    unprotected_reason: str = ""

    @property
    def protected(self) -> bool:
        return bool(self.intents)


def plan_legs(
    entry: Order,
    instrument: Instrument,
    capabilities: BrokerCapabilities,
    *,
    at: UtcDatetime,
    revision: int = 0,
) -> LegPlan:
    """Build the protective legs guarding `entry`'s filled quantity."""
    plan = entry.protective
    if plan is None:
        return LegPlan(unprotected_reason="no protective plan on the entry")
    if not capabilities.protective_orders:
        return LegPlan(unprotected_reason=f"{capabilities.venue_id} holds no protective orders")

    qty = entry.filled_qty
    if qty <= ZERO:
        return LegPlan(unprotected_reason="entry has no fills to protect")

    side = _EXIT_SIDE[entry.side]
    roles: list[tuple[OrderRole, Decimal]] = [(OrderRole.STOP_LOSS, plan.stop_price)]
    if capabilities.oco_groups and plan.take_profit_price is not None:
        roles.append((OrderRole.TAKE_PROFIT, plan.take_profit_price))

    intents: list[OrderIntent] = []
    for role, trigger in roles:
        leg = _leg(entry, instrument, plan, role, trigger, side, qty, at=at, revision=revision)
        if leg is None:
            return LegPlan(unprotected_reason=f"{role.value} leg of {qty} is below venue minimums")
        intents.append(leg)
    return LegPlan(intents=tuple(intents))


def _leg(
    entry: Order,
    instrument: Instrument,
    plan: ProtectivePlan,
    role: OrderRole,
    trigger: Decimal,
    side: Side,
    qty: Decimal,
    *,
    at: UtcDatetime,
    revision: int,
) -> OrderIntent | None:
    rules = instrument.trading_rules
    # The trigger is a venue price like any other and must sit on a tick, or the venue rejects
    # the whole leg — leaving the position unguarded for the reason least likely to be noticed.
    stop = quantize_price(trigger, rules.tick_size, side)
    offset = multiply(percent_of(stop, plan.limit_offset_pct), _OFFSET_SIGN[side])
    quantized = quantize_order(qty, stop + offset, side, rules)
    if not quantized.approved:
        return None
    return OrderIntent(
        client_order_id=protective_order_id(entry.client_order_id, role, revision),
        basket_id=entry.basket_id,
        cycle_id=entry.cycle_id,
        instrument_key=entry.instrument_key,
        side=side,
        qty=quantized.qty,
        order_type=role.order_type,
        limit_price=quantized.price,
        stop_price=stop,
        role=role,
        group_id=entry.group_id,
        created_at=at,
    )
